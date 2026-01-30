# src/training.py

import torch
import pandas as pd
import re
from datasets import Dataset
from unsloth import FastLanguageModel, is_bfloat16_supported
from trl import SFTTrainer
from transformers import TrainingArguments
from src.config import Config

class MityaTrainer:
    def __init__(self, model_name="unsloth/mistral-7b-v0.3", max_seq_length = 2048):
        self.model_name = model_name
        self.max_seq_length = max_seq_length
        self.dtype = None # auto detect
        self.load_in_4bit = True
        self.model = None
        self.tokenizer = None

    def _clean_text(self, text):
        if not isinstance(text, str): return ""

        # remote html
        text = re.sub(r"<.*?>|&([a-z0-9]+|#[0-9]{1,6}|#x[0-9a-f]{1,6});", "", text)

        # remove non-ascii
        text = re.sub(r'[^\x00-\x7f]', r'', text)

        return text.strip()

    def prepare_data(self, csv_path):
        '''
        load and clean
        '''

        print(f"Loading data from {csv_path}...")
        df = pd.read_csv(csv_path)

        # clean
        df['instruction'] = df['instruction'].apply(self._clean_text)
        df['output'] = df['output'].apply(self._clean_text)

        # format for unsloth
        prompt_style = """Below is an instruction that describes a task, paired with an input that provides further context (if any). Write a response that appropriately completes the request.

### Instruction:
{}

### Input:
{}

### Response:
{}"""

        if not self.tokenizer:
            self.load_model()

        EOS_TOKEN = self.tokenizer.eos_token

        def formatting_prompts_func(examples):
            instructions = examples["instruction"]
            inputs       = examples["input"]
            outputs      = examples["output"]
            texts = []

            for instruction, inp, output in zip(instructions, inputs, outputs):
                text = prompt_style.format(instruction, inp, output) + EOS_TOKEN
                texts.append(text)
            return { "text" : texts }

        dataset = Dataset.from_pandas(df)
        dataset = dataset.map(formatting_prompts_func, batched = True)

        return dataset

    def load_model(self):
        print("Loading Model")
        self.model, self.tokenizer = FastLanguageModel.from_pretrained(
            model_name = self.model_name,
            max_seq_length = self.max_seq_length,
            dtype = self.dtype,
            load_in_4bit = self.load_in_4bit,
        )

        # lora adapter
        self.model = FastLanguageModel.get_peft_model(
            self.model,
            # lower rank reduce the number of trainable params (e.g. 8, 16, 32, 64, 128)
            r = 64,
            # use all modules
            target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                              "gate_proj", "up_proj", "down_proj",],
            # higher ranks, more influence upon the updated weights, since we want personalized LLM, it's better to increase alpha here
            lora_alpha = 96,
            # since most training data or relatively similar in style, it's good to apply some regularization
            lora_dropout = 0.1,
            # less prone to overfitting than "all", but since we want to capture more with regards to new data, then yes
            bias = "lora_only",
            # recomputing intermediate outputs during the backprob, rather than storing them -> longer time, less memory
            use_gradient_checkpointing = "unsloth",
            random_state = 2802,
            # using rank-stabilized matrix, reduce training loss
            use_rslora = False,
            loftq_config = None,
        )

    def train(self, dataset, output_dir = "outputs"):
        print("Starting Training...")
        training_args = TrainingArguments(
            # default 8, batch size spread across each gpu, smaller -> generalize better, larger -> train faster, more memory
            per_device_train_batch_size = 4,
            # #instead of updating weights after each batch,
            # gradients are accumulated over smaller batches over k steps, then used to updates the weights
            # similar to setting batch size 16 and 0 steps
            gradient_accumulation_steps = 4,
            # steps used for a linear warmup from 0 to learning_rate, prevent early overfitting
            warmup_steps = 5,
            # number of steps before stopping
            max_steps = -1,
            num_train_epochs = 3,
            learning_rate = 2e-4,
            fp16 = not is_bfloat16_supported(),
            bf16 = is_bfloat16_supported(),
            # number of steps before training info update
            logging_steps = 100,
            optim = "adamw_8bit",
            # apply a penalty to the weights, prevent overfitting
            # might consider batch normalization rather than weight decay
            weight_decay = 0.01,
            lr_scheduler_type = "linear",
            seed = 2802,
            output_dir = output_dir,
        )

        trainer = SFTTrainer(
            model = self.model,
            tokenizer = self.tokenizer,
            train_dataset = dataset,
            dataset_text_field = "text",
            max_seq_length = self.max_seq_length,
            dataset_num_proc = 2,
            # only true when using constant length dataset, trains faster for shorter sequences
            packing = False,
            args = training_args,
        )

        trainer.train()

        return trainer

    def export_model(self, save_path, push_to_hub = False, hub_id = None):
        '''
        Saves the merged 16bit model.
        '''
        print(f"Saving merged model to {save_path}...")
        self.model.save_pretrained_merged(save_path, self.tokenizer, save_method="merged_16bit")

        if push_to_hub and hub_id:
            print(f"Pushing to HuggingFace Hub: {hub_id}...")
            self.model.push_to_hub_merged(hub_id, self.tokenizer, save_method="merged_16bit")
