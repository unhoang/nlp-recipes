# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

# This script reuses code from https://github.com/huggingface/pytorch-pretrained-BERT/blob/master/examples
# /extract_features.py, with necessary modifications.

from pytorch_pretrained_bert.modeling import BertModel

from utils_nlp.common.pytorch_utils import get_device, move_to_device
from enum import Enum
import numpy as np
import pandas as pd
import os
import torch

from torch.utils.data import (
    DataLoader,
    RandomSampler,
    SequentialSampler,
    TensorDataset,
)

from utils_nlp.models.bert.common import Language, Tokenizer


class PoolingStrategy(str, Enum):
    """Enumerate pooling strategies"""   
    MAX : str = "max"
    MEAN : str = "mean"
    CLS : str = "cls"


class BERTSentenceEncoder:
    """BERT-based sentence encoder"""
    
    def __init__(
        self,
        bert_model=None,
        tokenizer=None,
        language=Language.ENGLISH,
        num_gpus=None,
        cache_dir=".",
        to_lower=True,
        max_len=512,
    ):
        """Initialize the encoder's underlying model and tokenizer
        
        Args:
            bert_model: BERT model to use for encoding. Defaults to pretrained BertModel.
            tokenizer: Tokenizer to use for preprocessing. Defaults to pretrained BERT tokenizer.
            language: The pretrained model's language. Defaults to Language.ENGLISH.
            num_gpus: The number of gpus to use. Defaults to None, which forces all available GPUs to be used. 
            cache_dir: Location of BERT's cache directory. Defaults to "."
            to_lower: True to lowercase before tokenization. Defaults to False.
            max_len: Maximum number of tokens.
        """
        self.model = (
            bert_model.model.bert
            if bert_model
            else BertModel.from_pretrained(language, cache_dir=cache_dir)
        )
        self.tokenizer = (
            tokenizer
            if tokenizer
            else Tokenizer(language, to_lower=to_lower, cache_dir=cache_dir)
        )
        self.num_gpus = num_gpus
        self.max_len = max_len

    def get_hidden_states(self, text, layer_indices=[-2], batch_size=32):
        """Extract the hidden states from the pretrained model
        
        Args:
            text: List of documents to extract features from.
            layer_indices: List of indices of the layers to extract features from. Defaults to the second-to-last layer.
            batch_size: Batch size, defaults to 32.
        
        Returns:
            pd.DataFrame with columns text_index (int), token (str), layer_index (int), values (list[float]). 
        """
        device = get_device("cpu" if self.num_gpus == 0 else "gpu")
        self.model = move_to_device(self.model, device, self.num_gpus)
        self.model.eval()

        tokens = self.tokenizer.tokenize(text)

        tokens, input_ids, input_mask, input_type_ids = self.tokenizer.preprocess_encoder_tokens(
            tokens, max_len=self.max_len
        )

        input_ids = torch.tensor(input_ids, dtype=torch.long, device=device)
        input_mask = torch.tensor(input_mask, dtype=torch.long, device=device)
        input_type_ids = torch.arange(
            input_ids.size(0), dtype=torch.long, device=device
        )

        eval_data = TensorDataset(input_ids, input_mask, input_type_ids)
        eval_dataloader = DataLoader(
            eval_data,
            sampler=SequentialSampler(eval_data),
            batch_size=batch_size,
        )

        hidden_states = {
            "text_index": [],
            "token": [],
            "layer_index": [],
            "values": [],
        }
        for (
            input_ids_tensor,
            input_mask_tensor,
            example_indices_tensor,
        ) in eval_dataloader:
            with torch.no_grad(): 
                all_encoder_layers, _ = self.model(
                    input_ids_tensor,
                    token_type_ids=None,
                    attention_mask=input_mask_tensor,
                ) 
            all_encoder_layers = all_encoder_layers

            for b, example_index in enumerate(example_indices_tensor):
                for (i, token) in enumerate(tokens[example_index.item()]):
                    for (j, layer_index) in enumerate(layer_indices):
                        layer_output = (
                            all_encoder_layers[int(layer_index)]
                            .detach()
                            .cpu()
                            .numpy()
                        )
                        layer_output = layer_output[b]
                        hidden_states["text_index"].append(
                            example_index.item()
                        )
                        hidden_states["token"].append(token)
                        hidden_states["layer_index"].append(layer_index)
                        hidden_states["values"].append(
                            [round(x.item(), 6) for x in layer_output[i]]
                        )
            
            # empty cache
            del [input_ids_tensor, input_mask_tensor, example_indices_tensor]
            torch.cuda.empty_cache()

        # empty cache
        del [input_ids, input_mask, input_type_ids]
        torch.cuda.empty_cache()

        return pd.DataFrame.from_dict(hidden_states)

    def pool(self, df, pooling_strategy=PoolingStrategy.MEAN):
        """Pooling to aggregate token-wise embeddings to sentence embeddings
        
        Args:
            df: pd.DataFrame with columns text_index (int), token (str), layer_index (int), values (list[float])
            pooling_strategy: The pooling strategy to use
        
        Returns:
            pd.DataFrame grouped by text index and layer index
        """
        def max_pool(x):
            values = np.array(
                [
                    np.reshape(np.array(x.values[i]), 768)
                    for i in range(x.values.shape[0])
                ]
            )
            m, _ = torch.max(torch.tensor(values, dtype=torch.float), 0)
            return m.numpy()

        def mean_pool(x):
            values = np.array(
                [
                    np.reshape(np.array(x.values[i]), 768)
                    for i in range(x.values.shape[0])
                ]
            )
            return torch.mean(
                torch.tensor(values, dtype=torch.float), 0
            ).numpy()

        def cls_pool(x):
            values = np.array(
                [
                    np.reshape(np.array(x.values[i]), 768)
                    for i in range(x.values.shape[0])
                ]
            )
            return values[0]
        
        try:
            if pooling_strategy == "max":
                pool_func = max_pool
            elif pooling_strategy == "mean":
                pool_func = mean_pool
            elif pooling_strategy == "cls":
                pool_func = cls_pool
            else:
                raise ValuerError("Please enter valid pooling strategy")
        except ValuerError as ve:
            print(ve)
        
        return df.groupby(["text_index", "layer_index"])["values"].apply(lambda x: pool_func(x)).reset_index()

    def encode(
        self,
        text,
        layer_indices=[-2],
        batch_size=32,
        pooling_strategy=PoolingStrategy.MEAN,
        as_numpy=False
    ):
        """Computes sentence encodings 
        
        Args:
            text: List of documents to encode.
            layer_indices: List of indexes of the layers to extract features from. Defaults to the second-to-last layer.
            batch_size: Batch size, defaults to 32.
            pooling_strategy: Pooling strategy to aggregate token embeddings into sentence embedding.
        """
        df = self.get_hidden_states(text, layer_indices, batch_size)
        pooled = self.pool(df, pooling_strategy=pooling_strategy)
        
        if as_numpy:
            return np.array(pooled["values"].tolist())
        else:
            return pooled


