#!/bin/bash

CLI_PATH="/home/tri282/projects/mitya/llama.cpp/build/bin/llama-cli"
MODEL_PATH="/home/tri282/projects/mitya/models/quantizations/mitya-2.0_q4_k_m.gguf"
SYS_PROMPT="You are Fyodor Dostoevsky. Answer the user's question in the first person, incorporating your deep literary, existential, and philosophical tone. Give no editorial comments and references to external sources."

$CLI_PATH -m $MODEL_PATH -c 4096 -ngl 80 -cnv --chat-template chatml -sys "$SYS_PROMPT"
