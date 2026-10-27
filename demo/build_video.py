"""Assemble demo/out/steadfast-demo.mp4 (1080p H.264 + AAC, at most 5:00) from script.yaml.

    .venv/bin/python demo/build_video.py --var session_cost='$2.84' \
        --var session_cost_spoken='two dollars eighty four'

Per scene: the narration is synthesised with Piper (the model tier's own open-weight
TTS, MIT, ljspeech voice), the visual (still, terminal replay or console clip) is fitted to
the scene's slot, sped up if longer (with an "N× speed" badge), captions are overlaid
sentence by sentence, and the scene is encoded. Scenes are then joined without
re-encoding. Inputs that are missing fail the build with the command that makes them.

Piper runs from its Linux release inside a python:3.12-slim container: the macOS release
of the same version is missing its shared libraries.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture  # noqa: E402

ROOT = capture.ROOT
OUT = capture.OUT
FPS = 30
MAX_TOTAL_S = 300
AUDIO_LEAD_S = 0.3
PIPER_URL = (
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz"
)
PIPER_DIR = ROOT / "demo" / ".tools" / "linux"


def run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd[:6])} ...\n{done.stderr[-3000:]}")
    return done.stdout


def duration(path: Path) -> float:
    out = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    )
    return float(out.strip())


# ---------- narration ----------


def ensure_piper() -> Path:
    binary = PIPER_DIR / "piper" / "piper"
    if not binary.exists():
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        archive = PIPER_DIR / "piper_linux_x86_64.tar.gz"
        urllib.request.urlretrieve(PIPER_URL, archive)  # noqa: S310
        with tarfile.open(archive) as tar:
            tar.extractall(PIPER_DIR, filter="data")
    return binary.parent


def synthesise(scenes: list[dict], voice: dict[str, Any]) -> dict[str, Path]:
    """One WAV per scene, re-made only when its text or the voice settings change."""
    audio_dir = OUT / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    wavs: dict[str, Path] = {}
    jobs: list[dict[str, str]] = []
    for s in scenes:
        key = hashlib.sha256(json.dumps([s["narration"], voice]).encode()).hexdigest()[:12]
        wav = audio_dir / f"{s['id']}-{key}.wav"
        wavs[s["id"]] = wav
        if not wav.exists():
            jobs.append({"text": s["narration"], "output_file": f"/out/{wav.name}"})
    if jobs:
        piper = ensure_piper()
        model = ROOT / voice["model"]
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--platform",
            "linux/amd64",
            "-v",
            f"{piper}:/opt/piper:ro",
            "-v",
            f"{model.parent}:/voice:ro",
            "-v",
            f"{audio_dir}:/out",
            "python:3.12-slim",
            "/opt/piper/piper",
            "--model",
            f"/voice/{model.name}",
            "--json-input",
            "--length_scale",
            str(voice.get("length_scale", 1.0)),
            "--sentence_silence",
            str(voice.get("sentence_silence", 0.25)),
        ]
        done = subprocess.run(
            cmd, input="\n".join(json.dumps(j) for j in jobs) + "\n", capture_output=True, text=True
        )
        if done.returncode != 0:
            sys.exit(f"piper failed:\n{done.stderr[-2000:]}")
    return wavs


# ---------- captions ----------


def caption_chunks(text: str, limit: int = 80) -> list[str]:
    """Sentences, with long ones split at commas/colons so each fits on one or two lines."""
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        parts = re.split(r"(?<=[,:])\s+", sentence)
        current = ""
        for part in parts:
            if current and len(current) + len(part) + 1 > limit:
                chunks.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
        if current:
            chunks.append(current)
    return chunks


def caption_times(chunks: list[str], speech_s: float) -> list[tuple[float, float]]:
    total = sum(len(c) for c in chunks)
    t, spans = AUDIO_LEAD_S, []
    for c in chunks:
        d = speech_s * len(c) / total
        spans.append((t, t + d))
        t += d
    return spans


# ---------- scenes ----------


def normalise_clip(src: Path) -> Path:
    """Playwright's WebM has no reliable duration header: re-encode once to a constant-rate MP4."""
    dest = src.with_suffix(".norm.mp4")
    sidecar = src.with_suffix(".json")  # console clips: seconds of sign-in to skip
    trim = json.loads(sidecar.read_text())["trim_start_s"] if sidecar.exists() else 0
    if not dest.exists() or dest.stat().st_mtime < src.stat().st_mtime:
        run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-ss",
                str(trim),
                "-i",
                str(src),
                "-r",
                str(FPS),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(dest),
            ]
        )
    return dest


def visual_for(scene: dict, browser: Any, page: Any) -> tuple[Path, bool]:
    """(path, is_still) for the scene's visual, rendering stills and terminal replays."""
    v = scene["visual"]
    if v["type"] == "card":
        out = OUT / "cards" / f"{scene['id']}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        capture.render_card(
            page, out, title=v["title"], subtitle=v["subtitle"], image=v.get("image")
        )
        return out, True
    name = v.get("capture", scene["id"])
    clip = capture.CLIPS / f"{name}.webm"
    if v["type"] == "term":
        src = ROOT / "demo" / "captures" / f"{name}.jsonl"
        if not src.exists():
            sys.exit(f"missing capture {src.relative_to(ROOT)}: run demo/record.py {scene['id']}")
        if not clip.exists() or clip.stat().st_mtime < src.stat().st_mtime:
            print(f"  rendering terminal replay {name}")
            capture.render_term(
                browser,
                name,
                title=scene.get("record", name),
                start_at=v.get("start_at"),
                max_gap=v.get("max_gap", 1.0),
            )
    elif not clip.exists():
        sys.exit(
            f"missing console clip {clip.relative_to(ROOT)}: run demo/capture.py console {name}"
        )
    return normalise_clip(clip), False


def build_scene(scene: dict, visual: Path, still: bool, wav: Path, page: Any) -> Path:
    slot = float(scene["slot_s"])
    speech = duration(wav)
    if AUDIO_LEAD_S + speech > slot - 0.3:
        sys.exit(f"scene {scene['id']}: narration {speech:.1f}s does not fit its {slot:.0f}s slot")

    overlays: list[tuple[Path, float, float]] = []
    ov_dir = OUT / "overlays" / scene["id"]
    ov_dir.mkdir(parents=True, exist_ok=True)
    chunks = caption_chunks(scene["narration"])
    for i, (chunk, (a, b)) in enumerate(zip(chunks, caption_times(chunks, speech), strict=True)):
        png = capture.render_overlay(page, ov_dir / f"cap{i}.png", caption=chunk)
        overlays.append((png, a, b))

    speed = 1.0
    if not still:
        clip_s = duration(visual)
        speed = max(clip_s / slot, 1.0)
        if speed >= 1.5:
            badge = capture.render_overlay(page, ov_dir / "badge.png", badge=f"{speed:.0f}× speed")
            overlays.append((badge, 0.0, slot))

    inputs = (
        ["-loop", "1", "-framerate", str(FPS), "-t", str(slot), "-i", str(visual)]
        if still
        else ["-i", str(visual)]
    )
    for png, _, _ in overlays:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", str(slot), "-i", str(png)]
    inputs += ["-i", str(wav)]

    crop = ""
    if scene["visual"]["type"] == "console":
        crop = f"crop=iw:ih-{capture.CONSOLE_TOP_CROP_PX}:0:{capture.CONSOLE_TOP_CROP_PX},"
    base = (
        f"[0:v]{crop}setpts=(PTS-STARTPTS)/{speed:.4f},"
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0f14,"
        f"fps={FPS},tpad=stop_mode=clone:stop_duration={slot},trim=duration={slot},setsar=1[v0]"
    )
    chain = [base]
    for i, (_, a, b) in enumerate(overlays, start=1):
        chain.append(f"[v{i - 1}][{i}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[v{i}]")
    audio_in = len(overlays) + 1
    lead_ms = int(AUDIO_LEAD_S * 1000)
    chain.append(
        f"[{audio_in}:a]aresample=48000,aformat=channel_layouts=stereo,"
        f"adelay={lead_ms}|{lead_ms},apad,atrim=duration={slot}[a]"
    )
    out = OUT / "scenes" / f"{scene['id']}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(chain),
            "-map",
            f"[v{len(overlays)}]",
            "-map",
            "[a]",
            "-t",
            str(slot),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(out),
        ]
    )
    print(f"  {scene['id']:<15} slot {slot:>4.0f}s  speech {speech:4.1f}s  speed {speed:5.1f}x")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument(
        "--var", action="append", default=[], help="name=value for {name} in script.yaml"
    )
    p.add_argument("--only", help="build just these scene ids (comma-separated), for previews")
    a = p.parse_args()
    variables = dict(v.split("=", 1) for v in a.var)

    script = yaml.safe_load((ROOT / "demo" / "script.yaml").read_text())
    scenes = script["scenes"]
    total = sum(s["slot_s"] for s in scenes)
    if total > MAX_TOTAL_S:
        sys.exit(f"slots add up to {total}s, over {MAX_TOTAL_S}s")
    try:
        for s in scenes:
            s["narration"] = " ".join(s["narration"].split()).format_map(variables)
            if s["visual"]["type"] == "card":
                s["visual"]["subtitle"] = s["visual"]["subtitle"].format_map(variables)
    except KeyError as exc:
        sys.exit(f"missing --var {exc.args[0]}=...")
    if a.only:
        wanted = set(a.only.split(","))
        scenes = [s for s in scenes if s["id"] in wanted]

    wavs = synthesise(scenes, script["voice"])
    parts: list[Path] = []
    with capture.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=capture.SIZE)
        for s in scenes:
            visual, still = visual_for(s, browser, page)
            parts.append(build_scene(s, visual, still, wavs[s["id"]], page))
        browser.close()

    listing = OUT / "scenes.txt"
    listing.write_text("".join(f"file '{p_.resolve()}'\n" for p_ in parts))
    final = OUT / ("preview.mp4" if a.only else "steadfast-demo.mp4")
    run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(final),
        ]
    )
    length = duration(final)
    print(f"wrote {final.relative_to(ROOT)} ({length:.1f}s)")
    if length > MAX_TOTAL_S + 0.5:
        sys.exit(f"video is {length:.1f}s, over the {MAX_TOTAL_S}s limit")


if __name__ == "__main__":
    main()
