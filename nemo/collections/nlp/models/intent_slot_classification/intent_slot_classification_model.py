# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import Dict, List, Optional

import onnx
import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from torch.utils.data import DataLoader

from nemo.collections.common.losses import AggregatorLoss, CrossEntropyLoss
from nemo.collections.nlp.data.intent_slot_classification import (
    IntentSlotClassificationDataset,
    IntentSlotDataDesc,
    IntentSlotInferenceDataset,
)
from nemo.collections.nlp.metrics.classification_report import ClassificationReport
from nemo.collections.nlp.models.nlp_model import NLPModel
from nemo.collections.nlp.modules.common import SequenceTokenClassifier
from nemo.collections.nlp.modules.common.lm_utils import get_lm_model
from nemo.collections.nlp.modules.common.tokenizer_utils import get_tokenizer
from nemo.collections.nlp.parts.utils_funcs import tensor2list
from nemo.core.classes import typecheck
from nemo.core.classes.common import PretrainedModelInfo
from nemo.core.classes.exportable import Exportable
from nemo.core.neural_types import NeuralType
from nemo.utils import logging
from nemo.utils.export_utils import attach_onnx_to_onnx


class IntentSlotClassificationModel(NLPModel, Exportable):
    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.bert_model.input_types

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.classifier.output_types

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        """ Initializes BERT Joint Intent and Slot model.
        """

        self.data_dir = cfg.data_dir
        self.max_seq_length = cfg.language_model.max_seq_length

        self.data_desc = IntentSlotDataDesc(
            data_dir=cfg.data_dir, modes=[cfg.train_ds.prefix, cfg.validation_ds.prefix]
        )

        self._setup_tokenizer(cfg.tokenizer)
        # init superclass
        super().__init__(cfg=cfg, trainer=trainer)

        # initialize Bert model
        self.bert_model = get_lm_model(
            pretrained_model_name=cfg.language_model.pretrained_model_name,
            config_file=cfg.language_model.config_file,
            config_dict=OmegaConf.to_container(cfg.language_model.config) if cfg.language_model.config else None,
            checkpoint_file=cfg.language_model.lm_checkpoint,
        )

        self.classifier = SequenceTokenClassifier(
            hidden_size=self.bert_model.config.hidden_size,
            num_intents=self.data_desc.num_intents,
            num_slots=self.data_desc.num_slots,
            dropout=cfg.head.fc_dropout,
            num_layers=cfg.head.num_output_layers,
            log_softmax=False,
        )

        # define losses
        if cfg.class_balancing == 'weighted_loss':
            # You may need to increase the number of epochs for convergence when using weighted_loss
            self.intent_loss = CrossEntropyLoss(logits_ndim=2, weight=self.data_desc.intent_weights)
            self.slot_loss = CrossEntropyLoss(logits_ndim=3, weight=self.data_desc.slot_weights)
        else:
            self.intent_loss = CrossEntropyLoss(logits_ndim=2)
            self.slot_loss = CrossEntropyLoss(logits_ndim=3)

        self.total_loss = AggregatorLoss(num_inputs=2, weights=[cfg.intent_loss_weight, 1.0 - cfg.intent_loss_weight])

        # setup to track metrics
        self.intent_classification_report = ClassificationReport(
            num_classes=self.data_desc.num_intents,
            label_ids=self.data_desc.intents_label_ids,
            dist_sync_on_step=True,
            mode='micro',
        )
        self.slot_classification_report = ClassificationReport(
            num_classes=self.data_desc.num_slots,
            label_ids=self.data_desc.slots_label_ids,
            dist_sync_on_step=True,
            mode='micro',
        )

    def update_data_dir(self, data_dir: str) -> None:
        """
        Update data directory and get data stats with Data Descriptor
        Weights are later used to setup loss

        Args:
            data_dir: path to data directory
        """
        self.data_dir = data_dir
        logging.info(f'Setting model.data_dir to {data_dir}.')

    @typecheck()
    def forward(self, input_ids, token_type_ids, attention_mask):
        """
        No special modification required for Lightning, define it as you normally would
        in the `nn.Module` in vanilla PyTorch.
        """
        hidden_states = self.bert_model(
            input_ids=input_ids, token_type_ids=token_type_ids, attention_mask=attention_mask
        )
        intent_logits, slot_logits = self.classifier(hidden_states=hidden_states)
        return intent_logits, slot_logits

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        # forward pass
        input_ids, input_type_ids, input_mask, loss_mask, subtokens_mask, intent_labels, slot_labels = batch
        intent_logits, slot_logits = self(
            input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask
        )

        # calculate combined loss for intents and slots
        intent_loss = self.intent_loss(logits=intent_logits, labels=intent_labels)
        slot_loss = self.slot_loss(logits=slot_logits, labels=slot_labels, loss_mask=loss_mask)
        train_loss = self.total_loss(loss_1=intent_loss, loss_2=slot_loss)
        lr = self._optimizer.param_groups[0]['lr']

        self.log('train_loss', train_loss)
        self.log('lr', lr, prog_bar=True)

        return {
            'loss': train_loss,
            'lr': lr,
        }

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        input_ids, input_type_ids, input_mask, loss_mask, subtokens_mask, intent_labels, slot_labels = batch
        intent_logits, slot_logits = self(
            input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask
        )

        # calculate combined loss for intents and slots
        intent_loss = self.intent_loss(logits=intent_logits, labels=intent_labels)
        slot_loss = self.slot_loss(logits=slot_logits, labels=slot_labels, loss_mask=loss_mask)
        val_loss = self.total_loss(loss_1=intent_loss, loss_2=slot_loss)

        # calculate accuracy metrics for intents and slot reporting
        # intents
        preds = torch.argmax(intent_logits, axis=-1)
        self.intent_classification_report.update(preds, intent_labels)
        # slots
        subtokens_mask = subtokens_mask > 0.5
        preds = torch.argmax(slot_logits, axis=-1)[subtokens_mask]
        slot_labels = slot_labels[subtokens_mask]
        self.slot_classification_report.update(preds, slot_labels)

        return {
            'val_loss': val_loss,
            'intent_tp': self.intent_classification_report.tp,
            'intent_fn': self.intent_classification_report.fn,
            'intent_fp': self.intent_classification_report.fp,
            'slot_tp': self.slot_classification_report.tp,
            'slot_fn': self.slot_classification_report.fn,
            'slot_fp': self.slot_classification_report.fp,
        }

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        :param outputs: list of individual outputs of each validation step.
        """
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        # calculate metrics and log classification report (separately for intents and slots)
        intent_precision, intent_recall, intent_f1, intent_report = self.intent_classification_report.compute()
        logging.info(f'Intent report: {intent_report}')

        slot_precision, slot_recall, slot_f1, slot_report = self.slot_classification_report.compute()
        logging.info(f'Slot report: {slot_report}')

        self.log('val_loss', avg_loss)
        self.log('intent_precision', intent_precision)
        self.log('intent_recall', intent_recall)
        self.log('intent_f1', intent_f1)
        self.log('slot_precision', slot_precision)
        self.log('slot_recall', slot_recall)
        self.log('slot_f1', slot_f1)

        return {
            'val_loss': avg_loss,
            'intent_precision': intent_precision,
            'intent_recall': intent_recall,
            'intent_f1': intent_f1,
            'slot_precision': slot_precision,
            'slot_recall': slot_recall,
            'slot_f1': slot_f1,
        }

    def test_step(self, batch, batch_idx):
        """
        Lightning calls this inside the test loop with the data from the test dataloader
        passed in as `batch`.
        """
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        """
        Called at the end of test to aggregate outputs.
        :param outputs: list of individual outputs of each test step.
        """
        return self.validation_epoch_end(outputs)

    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config)

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        self._test_dl = self._setup_dataloader_from_config(cfg=test_data_config)

    def _setup_dataloader_from_config(self, cfg: DictConfig):
        input_file = f'{self.data_dir}/{cfg.prefix}.tsv'
        slot_file = f'{self.data_dir}/{cfg.prefix}_slots.tsv'

        if not (os.path.exists(input_file) and os.path.exists(slot_file)):
            raise FileNotFoundError(
                f'{input_file} or {slot_file} not found. Please refer to the documentation for the right format \
                 of Intents and Slots files.'
            )

        dataset = IntentSlotClassificationDataset(
            input_file=input_file,
            slot_file=slot_file,
            tokenizer=self.tokenizer,
            max_seq_length=self.max_seq_length,
            num_samples=cfg.num_samples,
            pad_label=self.data_desc.pad_label,
            ignore_extra_tokens=self._cfg.ignore_extra_tokens,
            ignore_start_end=self._cfg.ignore_start_end,
        )

        return DataLoader(
            dataset=dataset,
            batch_size=cfg.batch_size,
            shuffle=cfg.shuffle,
            num_workers=cfg.num_workers,
            pin_memory=cfg.pin_memory,
            drop_last=cfg.drop_last,
            collate_fn=dataset.collate_fn,
        )

    def _setup_infer_dataloader(self, queries: List[str], batch_size: int) -> 'torch.utils.data.DataLoader':
        """
        Setup function for a infer data loader.
        Args:
            queries: text
            batch_size: batch size to use during inference
        Returns:
            A pytorch DataLoader.
        """
        dataset = IntentSlotInferenceDataset(
            tokenizer=self.tokenizer, queries=queries, max_seq_length=-1, do_lower_case=False
        )

        return torch.utils.data.DataLoader(
            dataset=dataset,
            collate_fn=dataset.collate_fn,
            batch_size=batch_size,
            shuffle=False,
            num_workers=self._cfg.test_ds.num_workers,
            pin_memory=self._cfg.test_ds.pin_memory,
            drop_last=False,
        )

    def predict_from_examples(self, queries: List[str], batch_size: int = 32) -> List[List[str]]:
        """
        Get prediction for the queries (intent and slots)
        Args:
            queries: text sequences
            batch_size: batch size to use during inference
        Returns:
            predicted_intents, predicted_slots: model intent and slot predictions
        """
        predicted_intents = []
        predicted_slots = []
        mode = self.training
        try:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            # Switch model to evaluation mode
            self.eval()
            self.to(device)
            infer_datalayer = self._setup_infer_dataloader(queries, batch_size)

            # load intent and slot labels from the dictionary files (user should have them in a data directory)
            intent_labels, slot_labels = IntentSlotDataDesc.intent_slot_dicts(self.data_dir)

            for batch in infer_datalayer:
                input_ids, input_type_ids, input_mask, loss_mask, subtokens_mask = batch

                intent_logits, slot_logits = self.forward(
                    input_ids=input_ids.to(device),
                    token_type_ids=input_type_ids.to(device),
                    attention_mask=input_mask.to(device),
                )

                # predict intents and slots for these examples
                # intents
                intent_preds = tensor2list(torch.argmax(intent_logits, axis=-1))

                # convert numerical outputs to Intent and Slot labels from the dictionaries
                for intent_num in intent_preds:
                    if intent_num < len(intent_labels):
                        predicted_intents.append(intent_labels[intent_num])
                    else:
                        # should not happen
                        predicted_intents.append("Unknown Intent")

                # slots
                slot_preds = torch.argmax(slot_logits, axis=-1)

                for slot_preds_query, mask_query in zip(slot_preds, subtokens_mask):
                    query_slots = ''
                    for slot, mask in zip(slot_preds_query, mask_query):
                        if mask == 1:
                            if slot < len(slot_labels):
                                query_slots += slot_labels[slot] + ' '
                            else:
                                query_slots += 'Unknown_slot '
                    predicted_slots.append(query_slots.strip())

        finally:
            # set mode back to its original value
            self.train(mode=mode)

        return predicted_intents, predicted_slots

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        model = PretrainedModelInfo(
            pretrained_model_name="Joint_Intent_Slot_Assistant",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemonlpmodels/versions/1.0.0a5/files/Joint_Intent_Slot_Assistant.nemo",
            description="This models is trained on this https://github.com/xliuhw/NLU-Evaluation-Data dataset which includes 64 various intents and 55 slots. Final Intent accuracy is about 87%, Slot accuracy is about 89%.",
        )
        result.append(model)
        return result

    def export(
        self,
        output: str,
        input_example=None,
        output_example=None,
        verbose=False,
        export_params=True,
        do_constant_folding=True,
        keep_initializers_as_inputs=False,
        onnx_opset_version: int = 12,
        try_script: bool = False,
        set_eval: bool = True,
        check_trace: bool = True,
        use_dynamic_axes: bool = True,
    ):
        if input_example is not None or output_example is not None:
            logging.warning(
                "Passed input and output examples will be ignored and recomputed since"
                " IntentSlotClassificationModel consists of two separate models with different"
                " inputs and outputs."
            )

        qual_name = self.__module__ + '.' + self.__class__.__qualname__
        output1 = os.path.join(os.path.dirname(output), 'bert_' + os.path.basename(output))
        output1_descr = qual_name + ' BERT exported to ONNX'
        bert_model_onnx = self.bert_model.export(
            output1,
            None,  # computed by input_example()
            None,
            verbose,
            export_params,
            do_constant_folding,
            keep_initializers_as_inputs,
            onnx_opset_version,
            try_script,
            set_eval,
            check_trace,
            use_dynamic_axes,
        )

        output2 = os.path.join(os.path.dirname(output), 'classifier_' + os.path.basename(output))
        output2_descr = qual_name + ' Classifier exported to ONNX'
        classifier_onnx = self.classifier.export(
            output2,
            None,  # computed by input_example()
            None,
            verbose,
            export_params,
            do_constant_folding,
            keep_initializers_as_inputs,
            onnx_opset_version,
            try_script,
            set_eval,
            check_trace,
            use_dynamic_axes,
        )

        output_model = attach_onnx_to_onnx(bert_model_onnx, classifier_onnx, "ISC")
        output_descr = qual_name + ' BERT+Classifier exported to ONNX'
        onnx.save(output_model, output)
        return ([output, output1, output2], [output_descr, output1_descr, output2_descr])
