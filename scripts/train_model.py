# scripts/train_model.py

import sys
import os
import argparse

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.training import DostoevskyTrainer
from src.config import Config

def main(csv_file, push, hub_id):
    csv_path = os.path.join(Config.PROCESSED_DATA_DIR, csv_file)

    trainer = DostoevskyTrainer()

    # load data
    dataset = trainer.prepare_data(csv_path)

    # train
    trainer.train(dataset)

    # export
    save_path = os.path.join(Config.DATA_DIR, "dostoevsky_model_merged")
    trainer.export_model(save_path, push_to_hub = push, hub_id = hub_id)

    print("Training pipeline complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default = "training_data.csv", help = "Name of CSV in data/processed/")
    parser.add_argument("--push", action = "store_true", help = "Push to HuggingFace Hub")
    parser.add_argument("--hub_id", type = str, default = "tri282/dostoevskyGPT_merged", help="HF Repo ID")

    args = parser.parse_args()
    main(args.csv, args.push, args.hub_id)
