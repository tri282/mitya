#!/usr/bin/env bash
# Launch llama-server (OpenAI-compatible HTTP API) for the RAG serving path.
# llama-cli (previous version of this script) has no HTTP API -- see
# docs/RAG.md V "Hardware Budget" / ruling R2.
set -euo pipefail

BIN="/home/tri282/projects/mitya/llama.cpp/build/bin/llama-server"
MODEL="/home/tri282/projects/mitya/models/quantizations/mitya-2.0_q4_k_m.gguf"

# -fa on is required for V-cache quantization (--cache-type-v) to take effect;
# silently ignored otherwise. Budget: ~5.2-5.5GB VRAM at -c 4096 (docs/RAG.md V).
# Default is -c 3072: matches the R25 generation measurement and leaves ~100MiB
# for other work on the card. Override with CTX_SIZE=4096.
"$BIN" -m "$MODEL" \
  -ngl 99 -c "${CTX_SIZE:-3072}" -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --parallel 1 --host 127.0.0.1 --port 8080
