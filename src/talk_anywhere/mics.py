"""Read the microphone list. This module never changes volumes, mute, or defaults."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


class MicError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mic:
    name: str
    description: str
    spec: str

    @property
    def label(self) -> str:
        spec = self.spec or "input"
        return f"{self.description}   ·   {spec}"


def list_mics() -> list[Mic]:
    """Return real input devices. Speaker monitors are left out."""
    try:
        result = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MicError("pactl is not installed, so the microphone list cannot be read.") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise MicError(detail or "Could not list microphones.")

    mics: list[Mic] = []
    name = ""
    description = ""
    spec = ""

    def flush() -> None:
        nonlocal name, description, spec
        if name and ".monitor" not in name:
            mics.append(Mic(name=name, description=description or name, spec=spec))
        name = ""
        description = ""
        spec = ""

    for line in result.stdout.splitlines():
        if line.startswith("Source #"):
            flush()
            continue
        stripped = line.strip()
        if stripped.startswith("Name: "):
            name = stripped[len("Name: ") :]
        elif stripped.startswith("Description: "):
            description = stripped[len("Description: ") :]
        elif stripped.startswith("Sample Specification: "):
            spec = stripped[len("Sample Specification: ") :]
    flush()
    return mics


def resolve_mic(mics: list[Mic], saved_name: str, saved_description: str) -> Mic | None:
    """Find the saved device. Never substitute a different microphone."""
    for mic in mics:
        if saved_name and mic.name == saved_name:
            return mic
    if saved_description:
        hits = [mic for mic in mics if mic.description == saved_description]
        if len(hits) == 1:
            return hits[0]
    return None


def prefer_camera(mics: list[Mic]) -> Mic | None:
    """Pick Camera Mono when it is present. Do not guess another device."""
    for mic in mics:
        text = f"{mic.description} {mic.name}".lower()
        if "camera" in text and "mono" in text:
            return mic
    return None
