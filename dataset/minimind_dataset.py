import math
from typing import List, Optional, Tuple, Union
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch import nn
import os
import pandas as pd

from torch.utils.data import IterableDataset, Dataset
import json
import numpy as np
from transformers import  PreTrainedModel
from transformers.modeling_outputs import CausalLMOutputWithPast
from transformers import PretrainedConfig
from transformers import Trainer, TrainingArguments, AutoModelForCausalLM, AutoTokenizer, DefaultDataCollator, DataCollatorForTokenClassification, AutoConfig


class LLMDataset(Dataset):
    def __init__(self, data_path, tokenizer, max_seq_len):
        super().__init__()
        self.data_path = data_path
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
        with open(self.data_path, 'r', encoding='utf-8') as f:
            self.data = f.readlines()
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, index: int):
        
        line = self.data[index]
        line = json.loads(line)
        text = line['text'] + "<|endoftext|>"
        input_ids = self.tokenizer.encode(text)
        text_len = len(input_ids)
        if text_len > self.max_seq_len:
            input_ids = input_ids[:self.max_seq_len]
        else:
            input_ids = input_ids + [0] * (self.max_seq_len - text_len)
        input_ids = np.array(input_ids)
        X = np.array(input_ids[:-1]).astype(np.int64)
        Y = np.array(input_ids[1:]).astype(np.int64)
        return {
            'input_ids': torch.from_numpy(X),
            'labels': torch.from_numpy(Y),
        }
    

class SFTDataset(Dataset):
    def __init__(self, jsonl_path, tokenizer, max_seq_len=1024):
        super().__init__()
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
        self.samples = self.load_data(jsonl_path)
        self.bos_id = tokenizer('<|im_start|>assistant', add_special_tokens=False).input_ids
        self.eos_id = tokenizer('<|im_end|>', add_special_tokens=False).input_ids

    def __len__(self):
        return len(self.samples)

    def load_data(self, path):
        samples = []
        with open(path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                data = json.loads(line.strip())
                samples.append(data)
        return samples

    def _create_chat_prompt(self, conversations):
        """构建符合ChatML格式的对话"""
        messages = []
        for i, turn in enumerate(conversations):
            role = 'user' if i % 2 == 0 else 'assistant'
            messages.append({"role": role, "content": turn['content']})
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False
        )

    def __getitem__(self, index):
        sample = self.samples[index]
        # 构建对话提示
        prompt = self._create_chat_prompt(sample['conversations'])
        input_ids = self.tokenizer(prompt).input_ids[:self.max_seq_len]
        input_ids += [self.tokenizer.pad_token_id] * (self.max_seq_len - len(input_ids))

        # 构建训练数据
        X = torch.tensor(input_ids[:-1], dtype=torch.long)
        Y = torch.tensor(input_ids[1:], dtype=torch.long)

        return {'input_ids': X, 'labels': Y}
    
    
# 内存不够，可使用如下方法加载数据
# class LLMDataset(IterableDataset):
#     def __init__(self, data_path, tokenizer, max_seq_len):
#         super().__init__()
#         self.data_path = data_path
#         self.tokenizer = tokenizer
#         self.max_seq_len = max_seq_len
    
#     def __iter__(self):
#         return self.data_process()
    
#     def data_process(self):
#         with open(self.data_path, 'r', encoding='utf-8') as f:
#             for line in f:
#                 line = json.loads(line)
#                 text = '<s>' + line['text'] + '</s>'
#                 input_ids = self.tokenizer.encode(text)
#                 text_len = len(input_ids)
#                 if text_len > self.max_seq_len:
#                     input_ids = input_ids[:self.max_seq_len]
#                 else:
#                     input_ids = input_ids + [0] * (self.max_seq_len - text_len)
#                 input_ids = np.array(input_ids)
#                 X = np.array(input_ids[:-1]).astype(np.int64)
#                 Y = np.array(input_ids[1:]).astype(np.int64)
#                 yield {
#                     'input_ids': torch.from_numpy(X),
#                     'labels': torch.from_numpy(Y),
#                 }

class DPODataset(Dataset):
    def __init__(self, data_path, tokenizer):
        super().__init__()
        self.data_path = data_path
        self.tokenizer = tokenizer
        
        with open(self.data_path, 'r', encoding='utf-8') as f:
            self.datas = json.load(f)
        
    def __getitem__(self, index):
        sample = self.datas[index]
        prompt = sample['prompt']
        chosen = sample['chosen']
        rejected = sample['rejected']
        messages = [
            {"role": "user", "content": prompt}
        ]
        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
        prompt_inputs = self.tokenizer(text=text)['input_ids']
        rejected_inputs = self.tokenizer(text=rejected)['input_ids'] + [self.tokenizer.eos_token_id]
        chosen_inputs = self.tokenizer(text=chosen)['input_ids'] + [self.tokenizer.eos_token_id]
        return [prompt_inputs, chosen_inputs, rejected_inputs]
    
    def __len__(self):
        return len(self.datas)
    
    
class DPODataCollator:
    def __init__(self, tokenizer, max_seq_len):
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
    def __call__(self, features):
        inputs_ids = []
        labels = []
        
        for feature in features:
            inputs_ids.append(feature[0] + feature[1])
            labels.append([0]*len(feature[0]) + feature[1])
        for feature in features:
            inputs_ids.append(feature[0] + feature[2])
            labels.append([0]*len(feature[0]) + feature[2])
            
        def process(inputs_ids, labels):
            inputs_ids = [input_ids[:self.max_seq_len] for input_ids in inputs_ids]
            labels = [label[:self.max_seq_len] for label in labels]
            max_len = max([len(input_ids) for input_ids in inputs_ids])
            batch_input_ids = []
            batch_labels = []
            
            for input_ids, label in zip(inputs_ids, labels):
                if len(input_ids) <= max_len:
                    input_ids = input_ids+[0]*(max_len-len(input_ids))
                    label = label+[0]*(max_len-len(label))
                    batch_input_ids.append(input_ids[:-1])
                    batch_labels.append(label[1:])
            return batch_input_ids, batch_labels
        
        inputs_ids, labels = process(inputs_ids, labels)
        
        return {
            "input_ids": torch.tensor(inputs_ids),
            "labels": torch.tensor(labels)
            }
        
        
            
            