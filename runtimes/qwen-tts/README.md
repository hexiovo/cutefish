# Qwen3-TTS runtime

This isolated Windows runtime keeps CUDA/PyTorch and Qwen dependencies out of the accepted ASR environment. It targets CPython 3.12 and pins the CUDA 12.8 PyTorch wheels to the official PyTorch index; all other packages resolve from PyPI. `uv.lock` is the reproducible dependency lock.

On the development host, set `UV_PROJECT_ENVIRONMENT` to the external runtime directory so the virtual environment and its packages stay outside the repository:

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'E:\笨蛋小鱼\runtime\qwen-tts\.venv'
$env:UV_CACHE_DIR = 'E:\笨蛋小鱼\models\.cache\uv'
uv sync --project runtimes/qwen-tts --locked --python 3.12
```

The application host configuration points `qwen_tts_python` to that environment's `Scripts\python.exe`. Model weights remain outside Git under `model_root/qwen3-tts/{voice-design,base}`; their fixed revisions, exact allowlists, expected sizes, and SHA-256 values are recorded in the corresponding `model.manifest.json` files.

The runtime is local-only, uses `safetensors`, keeps `trust_remote_code` disabled, and must run with offline model loading. Load only one checkpoint in a worker at a time. A Base voice-clone prompt is rebuilt from the generated WAV and its text after the VoiceDesign process exits; do not persist Python/pickle prompt objects.
