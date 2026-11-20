"""
Proves transcription is really running on the graphics card.

Windows GPU installs often fail quietly and fall back to the processor.
This checks both pieces WhisperX uses, then does a tiny real transcription.

Run it with:  .venv\\Scripts\\python check_gpu.py
"""

import sys
import time

import torch

print(f"Python {sys.version.split()[0]}")
print(f"torch {torch.__version__}")

# 1. PyTorch (used for word-level timing alignment)
if not torch.cuda.is_available():
    print("FAIL: PyTorch can't see a CUDA GPU. Everything would run on the processor.")
    sys.exit(1)
print(f"PyTorch is using CUDA on: {torch.cuda.get_device_name(0)}")

# 2. CTranslate2 (the engine that does the actual transcribing)
import ctranslate2

count = ctranslate2.get_cuda_device_count()
if count == 0:
    print("FAIL: CTranslate2 can't see the GPU. Transcription would run on the processor.")
    sys.exit(1)
print(f"CTranslate2 sees {count} CUDA device(s)")

# 3. Load a small Whisper model onto the GPU and transcribe 3 seconds of silence.
#    This fails if the GPU math libraries (cuBLAS / cuDNN) are missing.
import numpy as np
import whisperx

start = time.time()
model = whisperx.load_model("tiny", device="cuda", compute_type="float16")
model.transcribe(np.zeros(16000 * 3, dtype=np.float32), batch_size=1)
print(f"OK: Whisper model loaded and ran on CUDA in {time.time() - start:.1f}s")
