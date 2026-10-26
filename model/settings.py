"""Model server configuration. Defaults are the CPU tier; Terraform sets the GPU values."""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Weights: fetched once from s3://{weights_bucket}/{weights_prefix} into models_dir (a
    # host volume, so a restarted task reuses them). Empty bucket = already on disk.
    weights_bucket: str = ""
    weights_prefix: str = "models/"
    models_dir: str = "/models"

    # STT: a CTranslate2 Whisper directory under models_dir.
    whisper_dir: str = "faster-whisper-small"
    whisper_device: Literal["cpu", "cuda"] = "cpu"
    whisper_compute_type: str = "int8"

    # LLM: a GGUF file under models_dir, run by llama.cpp.
    llm_file: str = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    llm_gpu_layers: int = 0  # -1 = all layers on the GPU
    llm_threads: int | None = None  # None = all cores
    llm_context: int = 1024
    max_tokens: int = Field(default=80, ge=1)

    # TTS: the Piper binary (separate process) and a voice under models_dir.
    piper_bin: str = "/opt/piper/piper"
    voice_file: str = "en_US-ljspeech-medium.onnx"
    samples_dir: str = "/tmp/samples"
    tts_dir: str = "/tmp/tts"

    # Fault injection, as the challenge asks: this fraction of requests fail with a 503
    # before any work is done.
    fault_rate: float = Field(default=0.2, ge=0, le=1)
    seed: int | None = None

    # Admission control. One turn at a time per model (they use every core / the GPU);
    # a short queue absorbs bursts; beyond it requests get 429 at once, so overload is a
    # fast retryable error for the orchestrator instead of a growing wait.
    max_concurrency: int = Field(default=1, ge=1)
    max_queue: int = Field(default=4, ge=0)
    queue_timeout_s: float = Field(default=15.0, gt=0)
