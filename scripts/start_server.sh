#!/bin/bash

CLI_PATH="/home/tri282/projects/mitya/llama.cpp/build/bin/llama-cli"
MODEL_PATH="/home/tri282/projects/mitya/models/quantizations/mitya-2.0_q4_k_m.gguf"

$CLI_PATH -m $MODEL_PATH -c 4096 -ngl 80 -cnv --chat-template chatml --port 8080
