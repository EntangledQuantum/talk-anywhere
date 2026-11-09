"""Filesystem locations. Nothing here opens or reconfigures a microphone."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("TALK_ANYWHERE_ROOT", Path(__file__).resolve().parents[2])).resolve()
ENGINE_PYTHON = Path(
    os.environ.get("TALK_ANYWHERE_ENGINE", str(ROOT / ".venv" / "bin" / "python"))
)

PROCESSOR_FILES = (
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "generation_config.json",
)

DEFAULT_SHORTCUT = "<Control><Super>space"


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_root() -> Path:
    return _mkdir(ROOT / "models")


def processor_dir() -> Path:
    return models_root() / "processor"


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return _mkdir(base / "talk-anywhere") / "config.json"


def log_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return _mkdir(base / "talk-anywhere") / "app.log"


def socket_path() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "talk-anywhere.sock"


def icon_dir() -> Path:
    return _mkdir(Path.home() / ".local" / "share" / "icons" / "talk-anywhere")


def bin_dir() -> Path:
    return _mkdir(Path.home() / ".local" / "bin")


def applications_dir() -> Path:
    return _mkdir(Path.home() / ".local" / "share" / "applications")


def autostart_dir() -> Path:
    return _mkdir(Path.home() / ".config" / "autostart")
