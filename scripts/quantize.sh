echo "clone and compile llama cpp"
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp
cmake -B build
cmake --build build --config Release -j
pip install -r requirements.txt -q

echo "convert merge hf to gguf"
python convert_hf_to_gguf.py path/to/mitya_16bit --outfile /path/to/mitya-2.0-F16.gguf --outtype f16

echo "quantize"

./build/bin/llama-quantize path/to/mitya-2.0-F16.gguf "path/to/mitya-2.0_q4_k_m.gguf" q4_k_m

./build/bin/llama-quantize path/to/mitya-2.0-F16.gguf "path/to/mitya-2.0_q5_k_m.gguf" q5_k_m

./build/bin/llama-quantize path/to/mitya-2.0-F16.gguf "path/to/mitya-2.0_q8_0.gguf" q8_0

echo "done"
