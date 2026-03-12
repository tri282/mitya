# src/training.py

import torch
import pandas as pd
import json
import re

from datasets import load_dataset
from unsloth.chat_templates import get_chat_template
from unsloth import FastLanguageModel, is_bfloat16_supported
from trl import SFTTrainer, SFTConfig
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

from src.config import Config

SYSTEM_PROMPT = (
    "You are Fyodor Dostoevsky. Answer the user's question in the first person, "
    "incorporating your deep literary, existential, and philosophical tone. "
    "Give no editorial comments and references to external sources."
)

class MityaTrainer:
    def __init__(self, model_name = "unsloth/mistral-7b-instruct-v0.3-bnb-4bit", max_seq_length = 1024):
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

    def _convert_to_chatml(self, row):
        '''
        change alpaca to chatml format
        '''
        user_message = str(row['instruction']).strip()
        assistant_message = str(row['output']).strip()

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message}
        ]

    def prepare_data(self, csv_path):
        '''
        clean data, convert to chatml format, and map to Hugging Face Dataset
        '''

        print(f"Loading data from {csv_path}...")
        df = pd.read_csv(csv_path)

        # clean
        df['instruction'] = df['instruction'].apply(self._clean_text)
        df['output'] = df['output'].apply(self._clean_text)

        # convert to chatml
        df['messages'] = df.apply(self._convert_to_chatml, axis=1)
        jsonl_path = "chatml_cleaned_data.jsonl"

        with open(jsonl_path, 'w', encoding = 'utf-8') as f:
            for messages in df['messages']:
                f.write(json.dumps({"messages": messages}) + '\n')

        # load model + tokenizer
        if not self.tokenizer:
            self.load_model()

        # load chat template
        self.tokenizer = get_chat_template(
            self.tokenizer,
            chat_template = "mistral",
        )

        # load json to dataset
        dataset = load_dataset("json", data_files = jsonl_path, split = "train")

        def formatting_prompts_func(examples):
            texts = [
                self.tokenizer.apply_chat_template(messages, tokenize = False, add_generation_prompt = False)
                for messages in examples["messages"]
            ]
            return { "text" : texts }

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
            r = 32,
            # use all modules
            target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                              "gate_proj", "up_proj", "down_proj",],

            # higher ranks, more influence upon the updated weights, since we want personalized LLM, it's better to increase alpha here
            lora_alpha = 32,
            # since most training data or relatively similar in style, it's good to apply some regularization
            lora_dropout = 0.1,
            # less prone to overfitting than "all", but since we want to capture more with regards to new data, then yes
            bias = "lora_only",
            # recomputing intermediate outputs during the backprob, rather than storing them -> longer time, less memory
            use_gradient_checkpointing = "unsloth",
            random_state = 2802,

            # using rank-stabilized matrix, reduce training loss
            use_rslora = True,
            use_dora = False,
            loftq_config = None,
        )

    def train(self, dataset, output_dir = "outputs"):
        print("Starting Training...")
        training_args = SFTConfig(
            # default 8, batch size spread across each gpu, smaller -> generalize better, larger -> train faster, more memory
            per_device_train_batch_size = 4,
            # instead of updating weights after each batch,
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
            dataset_text_field = "text",
            max_seq_length = self.max_seq_length,
            dataset_num_proc = 2,
            packing = True,
            seed = 2802,
            output_dir = output_dir,
        )

        trainer = SFTTrainer(
            model = self.model,
            tokenizer = self.tokenizer,
            train_dataset = dataset,
            args = training_args,
        )

        trainer.train()

        return trainer

    def export_model(self, save_path, push_to_hub = False, hub_id = None):
        '''
        save lora adapters, merged 16 bit model, and the quantizations
        '''
        # save lora
        self.model.save_pretrained(f"{save_path}_lora")
        self.tokenizer.save_pretrained(f"{save_path}_lora")

        # merge 16 bit
        base_model_id = "unsloth/mistral-7b-instruct-v0.3"
        base_model = AutoModelForCausalLM.from_pretrained(
            base_model_id,
            dtype = torch.float16,
            device_map = "auto"
        )
        tokenizer = AutoTokenizer.from_pretrained(base_model_id)
        model = PeftModel.from_pretrained(base_model, lora_dir)
        model = model.merge_and_unload()
        model.save_pretrained(MERGE_DIR)
        tokenizer.save_pretrained(MERGE_DIR)
