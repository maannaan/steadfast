"""The three inference stages: STT (faster-whisper), LLM (llama.cpp), TTS (Piper).

Each stage is a plain blocking object; the app runs a whole turn in a worker thread. The
heavy libraries are imported inside the constructors, so the HTTP layer (and its tests)
never needs them.

Licences (all compatible with publishing this repo under Apache-2.0):
- faster-whisper / CTranslate2: MIT; Whisper weights: MIT.
- llama-cpp-python: MIT; Qwen2.5-1.5B-Instruct: Apache-2.0; Mistral-7B-Instruct-v0.3:
  Apache-2.0.
- Piper 2023.11.14-2 (rhasspy/piper, the MIT release, not the later GPL piper1-gpl): run
  as a separate program over stdin/stdout. It ships espeak-ng (GPL-3.0) for phonemes;
  keeping Piper a separate process means this code never links or imports GPL code.
- Voice en_US-ljspeech-medium: trained on LJ Speech (public domain). The Hindi Piper
  voices are CC BY-NC-SA 4.0 (non-commercial), so they are deliberately not used.
"""

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

SYSTEM_PROMPT = (
    "You are a polite collections agent calling on behalf of an Indian digital lender, "
    "following the RBI Fair Practices Code. Reply in at most two short sentences. Be "
    "respectful; never threaten, shame or pressure the borrower, and never discuss the "
    "loan with anyone except the borrower. Help them find a way to pay, such as a "
    "payment date or a partial payment. If they ask to be called later, agree and offer "
    "a time between 8 am and 7 pm."
)

# Borrower lines synthesised into the STT input clips at startup (no real audio exists).
SAMPLE_LINES = (
    "Hello, yes, this is Ravi speaking. Who is calling?",
    "I know about the payment. I can pay the full amount this Friday.",
    "I already paid last week through the app. Please check your records.",
    "I lost my job last month. Can I pay half now and the rest later?",
    "I am driving right now. Please call me back in the evening.",
    "Why do you keep calling me? I told you I will pay when I get my salary.",
)


class Stages(Protocol):
    def transcribe(self, wav_path: Path) -> str: ...
    def reply(self, transcript: str) -> str: ...
    def synthesize(self, text: str) -> Path: ...


class WhisperStt:
    def __init__(self, model_dir: Path, device: str, compute_type: str) -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)

    def transcribe(self, wav_path: Path) -> str:
        # Greedy decoding and a fixed language: the clips are short English utterances,
        # and beam search would multiply STT time for no gain here.
        segments, _ = self._model.transcribe(str(wav_path), beam_size=1, language="en")
        return " ".join(s.text.strip() for s in segments).strip()


class LlamaLlm:
    def __init__(
        self, model_file: Path, gpu_layers: int, threads: int | None, context: int, max_tokens: int
    ) -> None:
        from llama_cpp import Llama

        self._llm = Llama(
            model_path=str(model_file),
            n_ctx=context,
            n_gpu_layers=gpu_layers,
            n_threads=threads,
            verbose=False,
        )
        self._max_tokens = max_tokens

    def reply(self, transcript: str) -> str:
        out = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=self._max_tokens,
            temperature=0.3,
        )
        return str(out["choices"][0]["message"]["content"]).strip()


class PiperTts:
    """One long-lived Piper process: loading the voice takes longer than a sentence, so it
    is loaded once. In --output_dir mode Piper reads one line of text per utterance from
    stdin and prints the path of the WAV it wrote."""

    def __init__(self, binary: str, voice: Path, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            [binary, "--model", str(voice), "--output_dir", str(output_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()

    def synthesize(self, text: str) -> Path:
        line = " ".join(text.split())  # one utterance per line
        with self._lock:
            if self._proc.poll() is not None or not self._proc.stdin or not self._proc.stdout:
                raise RuntimeError(f"piper exited with {self._proc.returncode}")
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
            path = self._proc.stdout.readline().strip()
        if not path:
            raise RuntimeError("piper produced no output")
        return Path(path)

    def close(self) -> None:
        self._proc.terminate()


class RealStages:
    def __init__(self, stt: WhisperStt, llm: LlamaLlm, tts: PiperTts) -> None:
        self.transcribe = stt.transcribe
        self.reply = llm.reply
        self.synthesize = tts.synthesize
        self.tts = tts


def make_samples(tts: PiperTts, samples_dir: Path) -> list[Path]:
    """Synthesise the borrower clips that stand in for call audio."""
    samples_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, line in enumerate(SAMPLE_LINES):
        target = samples_dir / f"sample-{i}.wav"
        tts.synthesize(line).replace(target)
        paths.append(target)
    return paths


def timed[T](fn: Callable[..., T], *args: Any) -> tuple[T, int]:
    start = time.perf_counter()
    value = fn(*args)
    return value, round((time.perf_counter() - start) * 1000)
