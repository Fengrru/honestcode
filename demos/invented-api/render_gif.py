"""Generate a terminal-style demo GIF without external recorders.

This avoids depending on vhs/ttyd/ffmpeg on Windows. It uses Pillow to render
the exact text produced by ``run.py`` as an animated terminal GIF.

Usage:
    cd demos/invented-api
    python render_gif.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 920
HEIGHT = 600
PADDING = 24
LINE_HEIGHT = 22
FONT_SIZE = 15
BG = "#0d1117"
FG = "#c9d1d9"
PROMPT_FG = "#58a6ff"
OK_FG = "#3fb950"
FAIL_FG = "#f85149"
HEADER_BG = "#161b22"
DOT_RED = "#ff5f56"
DOT_YELLOW = "#ffbd2e"
DOT_GREEN = "#27c93f"
PROMPT = "~/demos/invented-api $ "


def load_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/Consola.ttf",
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/Courier New.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, FONT_SIZE)
            except OSError:
                continue
    return ImageFont.load_default()


def run_demo() -> str:
    """Execute ``run.py`` and return its UTF-8 stdout."""
    here = Path(__file__).parent.resolve()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(here / "run.py")],
        cwd=str(here),
        capture_output=True,
        env=env,
        encoding="utf-8",
    )
    return result.stdout


def shorten_output(text: str) -> list[str]:
    """Trim absolute paths and split into display lines."""
    here = str(Path(__file__).parent.resolve())
    text = text.replace(here, "~/demos/invented-api")
    text = text.replace("\\", "/")
    return [line.rstrip() for line in text.splitlines()]


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def make_frame(
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    prompt_width: int,
    output_lines: list[str],
    visible_output_count: int,
    command_text: str = "",
    cursor_pos: int | None = None,
    show_cursor: bool = True,
) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    # Header bar
    draw.rectangle([0, 0, WIDTH, 34], fill=HEADER_BG)
    draw.ellipse([PADDING, 12, PADDING + 10, 22], fill=DOT_RED)
    draw.ellipse([PADDING + 16, 12, PADDING + 26, 22], fill=DOT_YELLOW)
    draw.ellipse([PADDING + 32, 12, PADDING + 42, 22], fill=DOT_GREEN)

    # Prompt + command
    y = 44
    draw.text((PADDING, y), PROMPT, fill=PROMPT_FG, font=font)
    if command_text:
        draw.text((PADDING + prompt_width, y), command_text, fill=FG, font=font)

    # Blinking cursor inside command line
    if show_cursor and cursor_pos is not None:
        prefix = command_text[:cursor_pos]
        cw, _ = text_size(draw, prefix, font)
        cursor_x = PADDING + prompt_width + cw
        draw.rectangle([cursor_x, y, cursor_x + 8, y + 16], fill=FG)

    # Output lines
    y += LINE_HEIGHT
    for line in output_lines[:visible_output_count]:
        color = FG
        if line.startswith("✓"):
            color = OK_FG
        elif line.startswith("✗"):
            color = FAIL_FG
        draw.text((PADDING, y), line, fill=color, font=font)
        y += LINE_HEIGHT

    return img


def add_frames(frames: list[Image.Image], frame: Image.Image, count: int = 2) -> None:
    frames.extend([frame] * count)


def main() -> int:
    font = load_font()
    output_lines = shorten_output(run_demo())
    command = "python run.py"

    # Measure prompt width once so the command aligns after it.
    tmp = Image.new("RGB", (WIDTH, HEIGHT), BG)
    prompt_width, _ = text_size(ImageDraw.Draw(tmp), PROMPT, font)

    frames: list[Image.Image] = []

    # Cursor blinks on empty prompt.
    for show in (True, False, True, False):
        add_frames(frames, make_frame(font, prompt_width, output_lines, 0, "", 0, show))

    # Type command.
    chars_per_frame = 2
    for i in range(0, len(command) + 1, chars_per_frame):
        add_frames(
            frames,
            make_frame(font, prompt_width, output_lines, 0, command[:i], i, show_cursor=True),
        )

    # Command committed, no cursor.
    add_frames(frames, make_frame(font, prompt_width, output_lines, 0, command, None, False), count=4)

    # Output scrolls in.
    for visible in range(1, len(output_lines) + 1):
        add_frames(
            frames,
            make_frame(font, prompt_width, output_lines, visible, command, None, False),
        )

    # Hold final frame.
    add_frames(
        frames,
        make_frame(font, prompt_width, output_lines, len(output_lines), command, None, False),
        count=30,
    )

    out = Path(__file__).parent / "demo.gif"
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
        optimize=True,
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
