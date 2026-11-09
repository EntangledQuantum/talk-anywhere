"""Type into the focused X11 window. The clipboard is left alone."""

from __future__ import annotations

import os
import subprocess

_STRAIGHT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)


class InjectError(RuntimeError):
    pass


def sanitize(text: str) -> str:
    return text.translate(_STRAIGHT)


def focused_window() -> tuple[str, str]:
    """Return (window id, owning pid). Pid is empty when xdotool cannot tell."""
    try:
        result = subprocess.run(
            ["xdotool", "getwindowfocus"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise InjectError("xdotool is not installed, so text cannot be typed.") from exc
    window_id = result.stdout.strip()
    if result.returncode != 0 or not window_id or window_id == "0":
        raise InjectError("No window is focused. Click a text field, then turn dictation on.")
    pid = subprocess.run(
        ["xdotool", "getwindowpid", window_id],
        capture_output=True,
        text=True,
        check=False,
    )
    return window_id, pid.stdout.strip()


def assert_foreign_focus() -> None:
    window_id, pid = focused_window()
    if pid and pid == str(os.getpid()):
        raise InjectError(
            "Talk Anywhere is the focused window. Click the text field you want, then dictate."
        )
    name = subprocess.run(
        ["xdotool", "getwindowname", window_id],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if name == "Talk Anywhere":
        raise InjectError(
            "Talk Anywhere is the focused window. Click the text field you want, then dictate."
        )


def backspace(count: int) -> None:
    remaining = count
    while remaining > 0:
        step = min(remaining, 30)
        _run(
            [
                "xdotool",
                "key",
                "--clearmodifiers",
                "--delay",
                "0",
                "--repeat",
                str(step),
                "BackSpace",
            ]
        )
        remaining -= step


def type_text(text: str) -> None:
    text = sanitize(text)
    if not text:
        return
    _run(["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", text])


def _run(argv: list[str]) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise InjectError("xdotool is not installed, so text cannot be typed.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InjectError(detail or "Could not type into the focused window.")
