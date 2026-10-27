"""Checks the built video before it is uploaded.

    .venv/bin/python demo/check_video.py [--video demo/out/steadfast-demo.mp4]

1. Length: at most 5:00 (ffprobe).
2. Privacy, automated: every text that can reach the screen (terminal captures, captions,
   card subtitles) is scanned for an account-shaped number, an access key ID or an e-mail
   address. Console clips are pixels, not text: for those see 3.
3. Privacy, by eye: one frame every 5 s is written to demo/out/frames/ to look through
   (console scenes especially).
4. Narration: the audio is transcribed with faster-whisper small (the model tier's own STT,
   run locally in a container) and compared word by word with script.yaml. A low score
   means a mispronounced or cut-off line.

Exit 1 if any automated check fails.
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo" / "out"
LEAKS = [
    re.compile(r"(?<!\d)\d{12}(?!\d)"),
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
]
TRANSCRIBE = """
import json, sys
from faster_whisper import WhisperModel
model = WhisperModel("/whisper", device="cpu", compute_type="int8")
segments, _ = model.transcribe("/audio/narration.wav", language="en", beam_size=5)
print(json.dumps(" ".join(s.text.strip() for s in segments)))
"""


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def check_length(video: Path) -> bool:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    seconds = float(out.stdout)
    ok = seconds <= 300.5
    print(f"[{'ok' if ok else 'FAIL'}] length {int(seconds // 60)}:{seconds % 60:04.1f} (max 5:00)")
    return ok


def check_text(script: dict) -> bool:
    texts: list[tuple[str, str]] = []
    for path in sorted((ROOT / "demo" / "captures").glob("*.jsonl")):
        for row in map(json.loads, path.read_text().splitlines()):
            texts.append((path.name, row.get("line") or row.get("cmd") or ""))
    for s in script["scenes"]:
        texts.append((f"scene {s['id']}", s["narration"] + " " + s["visual"].get("subtitle", "")))
    hits = [(src, t) for src, t in texts if any(p.search(t) for p in LEAKS)]
    for src, t in hits[:20]:
        print(f"    {src}: {t[:120]}")
    print(f"[{'ok' if not hits else 'FAIL'}] on-screen text: {len(hits)} possible leaks")
    return not hits


def extract_frames(video: Path) -> None:
    frames = OUT / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob("*.jpg"):
        old.unlink()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            "fps=1/5,scale=1280:-1",
            str(frames / "t%03d.jpg"),
        ],
        check=True,
    )
    count = len(list(frames.glob("*.jpg")))
    print(f"[look] {count} frames in {frames.relative_to(ROOT)} (t001 = 0 s, one every 5 s)")


def check_narration(video: Path, script: dict, threshold: float) -> bool:
    audio_dir = OUT / "audio-check"
    audio_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_dir / "narration.wav"),
        ],
        check=True,
    )
    whisper = ROOT / "model-weights" / "cpu" / "faster-whisper-small"
    done = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "-v",
            f"{whisper}:/whisper:ro",
            "-v",
            f"{audio_dir}:/audio:ro",
            "python:3.12-slim",
            "sh",
            "-c",
            "pip install -q faster-whisper==1.1.1 requests==2.34.2 av==15.1.0 >/dev/null 2>&1 "
            f"&& python -c '{TRANSCRIBE}'",
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        print(f"[FAIL] transcription failed:\n{done.stderr[-1500:]}")
        return False
    heard = json.loads(done.stdout.strip().splitlines()[-1])
    (OUT / "transcript.txt").write_text(heard + "\n")
    expected = " ".join(s["narration"] for s in script["scenes"])
    ratio = difflib.SequenceMatcher(None, words(expected), words(heard)).ratio()
    ok = ratio >= threshold
    print(
        f"[{'ok' if ok else 'FAIL'}] narration word match {ratio:.0%} (min {threshold:.0%}); "
        f"transcript in {(OUT / 'transcript.txt').relative_to(ROOT)}"
    )
    return ok


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--video", type=Path, default=OUT / "steadfast-demo.mp4")
    p.add_argument("--var", action="append", default=[], help="same --var values as the build")
    p.add_argument("--min-match", type=float, default=0.85)
    p.add_argument("--only", help="scene ids in the video, if it is a --only preview")
    a = p.parse_args()

    script = yaml.safe_load((ROOT / "demo" / "script.yaml").read_text())
    if a.only:
        script["scenes"] = [s for s in script["scenes"] if s["id"] in a.only.split(",")]
    variables = dict(v.split("=", 1) for v in a.var)
    for s in script["scenes"]:
        s["narration"] = " ".join(s["narration"].split()).format_map(variables)
        if "subtitle" in s["visual"]:
            s["visual"]["subtitle"] = s["visual"]["subtitle"].format_map(variables)

    results = [check_length(a.video), check_text(script)]
    extract_frames(a.video)
    results.append(check_narration(a.video, script, a.min_match))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
