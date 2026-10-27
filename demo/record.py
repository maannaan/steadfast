"""Run a demo command and save its output, line by line with timestamps, for replay.

    .venv/bin/python demo/record.py smoke              # the scene's `record` command
    .venv/bin/python demo/record.py smoke -- make aws-smoke   # or an explicit one

Why record text instead of screen-recording a terminal: the output can be redacted before
anything is rendered, replayed at any speed by demo/term.html, and re-rendered without
re-running AWS. Output also streams to this terminal as usual.

Redacted: the AWS account ID (looked up, never written down), access key IDs, e-mail
addresses, the service access key, and the load balancer's host name.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "demo" / "captures"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def account_id() -> str | None:
    try:
        out = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def make_redactor(account: str | None) -> Callable[[str], str]:
    exact: list[tuple[str, str]] = []
    if account:
        exact.append((account, "<account>"))
    if key := os.environ.get("STEADFAST_API_KEY"):
        exact.append((key, "<access-key>"))
    if url := os.environ.get("STEADFAST_URL"):
        host = re.sub(r"^https?://", "", url).split("/")[0]
        if host and "localhost" not in host:
            exact.append((host, "<load-balancer>"))
    patterns = [
        (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "<aws-key-id>"),
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
        (re.compile(r"(?<!\d)\d{12}(?!\d)"), "<account>"),  # any other account-shaped number
    ]

    def redact(line: str) -> str:
        line = ANSI.sub("", line)
        for value, label in exact:
            line = line.replace(value, label)
        for pattern, label in patterns:
            line = pattern.sub(label, line)
        return line

    return redact


def scene_command(scene_id: str) -> tuple[str, str]:
    """(capture name, command) for a scene in demo/script.yaml."""
    script = yaml.safe_load((ROOT / "demo" / "script.yaml").read_text())
    for scene in script["scenes"]:
        if scene["id"] == scene_id:
            visual = scene["visual"]
            if "record" not in scene:
                sys.exit(f"scene {scene_id} has no record command")
            return visual.get("capture", scene_id), scene["record"]
    sys.exit(f"no scene {scene_id} in demo/script.yaml")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("scene")
    p.add_argument("command", nargs=argparse.REMAINDER, help="-- command to run instead")
    a = p.parse_args()

    capture, command = scene_command(a.scene)
    if a.command:
        command = " ".join(a.command[1:] if a.command[0] == "--" else a.command)
    redact = make_redactor(account_id())
    CAPTURES.mkdir(parents=True, exist_ok=True)
    out_path = CAPTURES / f"{capture}.jsonl"

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
    start = time.monotonic()
    with out_path.open("w") as out:
        out.write(json.dumps({"cmd": redact(command), "started": datetime.now(UTC).isoformat()}))
        out.write("\n")
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = redact(raw.rstrip("\n"))
            print(line, flush=True)
            t = round(time.monotonic() - start, 2)
            out.write(json.dumps({"t": t, "line": line}) + "\n")
            out.flush()
        code = proc.wait()
        out.write(json.dumps({"t": round(time.monotonic() - start, 2), "exit": code}) + "\n")
    print(f"[record] {out_path.relative_to(ROOT)} (exit {code})", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
