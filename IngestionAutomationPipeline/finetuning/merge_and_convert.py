import os, subprocess, sys
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import AutoPeftModelForSequenceClassification
import torch

ADAPTER_DIR = "."
MERGED_DIR = "./lead-model-merged"
CAUSAL_DIR = "./lead-model-causal"
GGUF_PATH = "./lead-classifier.gguf"

# 1. Merge LoRA into base
print("Merging...")
model = AutoPeftModelForSequenceClassification.from_pretrained(
    ADAPTER_DIR, torch_dtype=torch.float16
)
model = model.merge_and_unload()
model.save_pretrained(MERGED_DIR)

tokenizer = AutoTokenizer.from_pretrained(ADAPTER_DIR)
tokenizer.save_pretrained(MERGED_DIR)

# 2. Reload as CausalLM (drops score head for llama.cpp compat)
print("Re-saving as CausalLM...")
causal = AutoModelForCausalLM.from_pretrained(
    MERGED_DIR, torch_dtype=torch.float16, ignore_mismatched_sizes=True
)
causal.save_pretrained(CAUSAL_DIR)
tokenizer.save_pretrained(CAUSAL_DIR)

# 3. Convert to GGUF
print("Converting to GGUF...")
subprocess.run(
    [
        sys.executable,
        "./llama.cpp/convert_hf_to_gguf.py",
        CAUSAL_DIR,
        "--outfile",
        GGUF_PATH,
        "--outtype",
        "f16",
    ],
    check=True,
)

print(f"\nDone → {GGUF_PATH}")

# 4. Write Modelfile
with open("./Modelfile", "w") as f:
    f.write(
        "FROM ./lead-classifier.gguf\n\n"
        'SYSTEM """You are a lead classifier. A lead is a business owner '
        "describing an operational pain-point that could be solved with custom "
        "software. Non-leads include job postings, developer questions, product "
        "showcases, and general opinions. "
        'Reply only with JSON: {\\"lead\\": 0 or 1, \\"reason\\": \\"one sentence\\"}"""\n'
    )

print("Modelfile written.")
print("\nNow run:")
print("  ollama create lead-classifier -f ./Modelfile")
print("  ollama run lead-classifier")
