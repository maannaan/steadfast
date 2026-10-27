"""Playwright captures for the demo video: terminal replays, stills, captions, the console.

    .venv/bin/python demo/capture.py term smoke                   # preview one replay
    .venv/bin/python demo/capture.py console load-dashboard --seconds 70

build_video.py imports this module for everything except the console, which has to be
recorded live while the stack exists (scripts/teardown.sh destroys the dashboard).

Privacy in console captures: the account menu (account ID, user name) is hidden with
injected CSS, and build_video.py also crops the console's top bar off every frame
(CONSOLE_TOP_CROP_PX), so either safeguard alone keeps the ID out of the video.
"""

import argparse
import base64
import html
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "demo"
OUT = DEMO / "out"
CLIPS = OUT / "clips"
SIZE = {"width": 1920, "height": 1080}
CONSOLE_TOP_CROP_PX = 56  # the console's top bar (account menu) if the CSS ever misses it

HIDE_ACCOUNT_CSS = """
#awsc-nav-header, #awsc-top-level-nav, header#awsc-nav-header,
[data-testid="awsc-nav-account-menu-button"],
[data-testid="more-menu__awsc-nav-account-menu-button"],
#nav-usernameMenu, #consoleNavHeader, [data-testid="awsc-nav-header"],
#awsccc-cb-content, #awsccc-sb-ux-c, [data-testid="awsc-footer"], #console-nav-footer
{ display: none !important; visibility: hidden !important; }
"""
# Injected before the console's own scripts run, so the menu is never painted at all.
HIDE_ACCOUNT_INIT = (
    "document.addEventListener('DOMContentLoaded', () => {"
    "const s = document.createElement('style'); s.textContent = %s;"
    "document.documentElement.appendChild(s); });"
)


def _record(
    browser: Browser, name: str, drive: Callable[[Page], None], *, bypass_csp: bool = False
) -> Path:
    """Run `drive(page)` in a fresh recording context; save the video as clips/<name>.webm.

    bypass_csp: the console's Content Security Policy forbids inline styles, which would
    block the CSS that hides the account menu. It only affects this recording browser.
    """
    tmp = OUT / "tmp-video"
    context = browser.new_context(
        viewport=SIZE,
        record_video_dir=str(tmp),
        record_video_size=SIZE,
        device_scale_factor=1,
        bypass_csp=bypass_csp,
    )
    page = context.new_page()
    try:
        drive(page)
    finally:
        video = page.video
        page.close()
        context.close()
    CLIPS.mkdir(parents=True, exist_ok=True)
    dest = CLIPS / f"{name}.webm"
    assert video is not None
    shutil.move(video.path(), dest)
    return dest


def render_term(
    browser: Browser, capture: str, *, title: str, start_at: str | None, max_gap: float = 1.0
) -> Path:
    rows = [
        json.loads(x) for x in (DEMO / "captures" / f"{capture}.jsonl").read_text().splitlines()
    ]
    data = {
        "cmd": rows[0]["cmd"],
        "lines": [r for r in rows[1:] if "line" in r],
        "exit": next((r["exit"] for r in rows if "exit" in r), None),
    }
    opts = {"title": title, "maxGap": max_gap, "startAt": start_at}

    def drive(page: Page) -> None:
        page.add_init_script(
            f"window.__CAPTURE__ = {json.dumps(data)}; window.__OPTS__ = {json.dumps(opts)};"
        )
        page.goto((DEMO / "term.html").as_uri())
        page.wait_for_function("window.__done === true", timeout=30 * 60 * 1000, polling=500)

    return _record(browser, capture, drive)


def _html_page(body: str, *, transparent: bool = False) -> str:
    css = (DEMO / "overlay.css").read_text()
    theme = "clear" if transparent else "dark"
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>'
        f'<body class="{theme}">{body}</body></html>'
    )


def render_card(page: Page, out: Path, *, title: str, subtitle: str, image: str | None) -> Path:
    # Inlined as a data URI: a page made with set_content may not load file:// images.
    img = ""
    if image:
        data = base64.b64encode((ROOT / image).read_bytes()).decode()
        img = f'<img src="data:image/png;base64,{data}">'
    body = (
        f'<div class="card"><h1>{html.escape(title)}</h1>{img}<p>{html.escape(subtitle)}</p></div>'
    )
    page.set_content(_html_page(body))
    page.wait_for_load_state("load")
    page.screenshot(path=str(out))
    return out


def render_overlay(
    page: Page, out: Path, *, caption: str | None = None, badge: str | None = None
) -> Path:
    """Full-frame transparent PNG with a caption at the bottom and/or a badge top-right."""
    body = ""
    if caption:
        body += f'<div class="cap"><span>{html.escape(caption)}</span></div>'
    if badge:
        body += f'<div class="badge">{html.escape(badge)}</div>'
    page.set_content(_html_page(body, transparent=True))
    page.screenshot(path=str(out), omit_background=True)
    return out


def record_console(
    browser: Browser, name: str, *, seconds: int, region: str, dashboard: str, minutes: int = 30
) -> Path:
    import console_url  # local module; imported here so other captures need no AWS access

    url = console_url.signin_url(console_url.dashboard_url(region, dashboard, minutes))

    started = time.monotonic()
    ready_at: list[float] = []

    def drive(page: Page) -> None:
        page.add_init_script(HIDE_ACCOUNT_INIT % json.dumps(HIDE_ACCOUNT_CSS))
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_url("**/cloudwatch/**", timeout=120_000)
        page.add_style_tag(content=HIDE_ACCOUNT_CSS)
        # Sign-in redirects and the console's first load are blank frames: note when the
        # dashboard's toolbar is up and widgets have had time to draw, and trim before it.
        page.get_by_text("Autosave").first.wait_for(timeout=120_000)
        page.wait_for_timeout(12_000)
        ready_at.append(time.monotonic() - started)
        end = time.monotonic() + seconds
        step = 0
        while time.monotonic() < end:
            # Scroll slowly down the dashboard and back, so every widget is on screen.
            page.mouse.wheel(0, 140 if (step // 20) % 2 == 0 else -140)
            page.add_style_tag(content=HIDE_ACCOUNT_CSS)
            page.wait_for_timeout(1000)
            step += 1

    clip = _record(browser, name, drive, bypass_csp=True)
    clip.with_suffix(".json").write_text(json.dumps({"trim_start_s": round(ready_at[0], 1)}))
    return clip


def main() -> None:
    import sys

    sys.path.insert(0, str(DEMO))
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="kind", required=True)
    t = sub.add_parser("term")
    t.add_argument("capture")
    t.add_argument("--start-at")
    t.add_argument("--max-gap", type=float, default=1.0)
    c = sub.add_parser("console")
    c.add_argument("name")
    c.add_argument("--seconds", type=int, default=70)
    c.add_argument("--region", default="ap-south-1")
    c.add_argument("--dashboard", default="steadfast")
    c.add_argument("--minutes", type=int, default=30, help="dashboard time range")
    a = p.parse_args()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        if a.kind == "term":
            out = render_term(
                browser, a.capture, title=a.capture, start_at=a.start_at, max_gap=a.max_gap
            )
        else:
            out = record_console(
                browser,
                a.name,
                seconds=a.seconds,
                region=a.region,
                dashboard=a.dashboard,
                minutes=a.minutes,
            )
        browser.close()
    print(f"wrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
