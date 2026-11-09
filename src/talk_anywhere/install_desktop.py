"""Install the GNOME launcher, autostart entry, icon, and shortcut."""

from __future__ import annotations

import os
import stat
import subprocess

from talk_anywhere.icons import write_icons
from talk_anywhere.paths import (
    DEFAULT_SHORTCUT,
    ENGINE_PYTHON,
    ROOT,
    applications_dir,
    autostart_dir,
    bin_dir,
    icon_dir,
)
from talk_anywhere.shortcut import pretty_shortcut, register_shortcut


def wrapper_text() -> str:
    return f"""#!/bin/bash
export TALK_ANYWHERE_ROOT={str(ROOT)!r}
export TALK_ANYWHERE_ENGINE={str(ENGINE_PYTHON)!r}
export PYTHONPATH={str(ROOT / "src")!r}${{PYTHONPATH:+:$PYTHONPATH}}
export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
export DBUS_SESSION_BUS_ADDRESS="${{DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}}"
exec /usr/bin/python3 -m talk_anywhere "$@"
"""


def desktop_text(icon: str) -> str:
    command = str(bin_dir() / "talk-anywhere")
    return f"""[Desktop Entry]
Type=Application
Name=Talk Anywhere
GenericName=Dictation
Comment=Toggle English speech to text in the focused text field
Exec={command}
Icon={icon}
Terminal=false
Categories=Utility;AudioVideo;
StartupNotify=false
StartupWMClass=talk-anywhere
Keywords=dictation;speech;voice;whisper;
"""


def autostart_text(icon: str, enabled: bool) -> str:
    flag = "true" if enabled else "false"
    body = desktop_text(icon)
    return body + f"X-GNOME-Autostart-enabled={flag}\n"


def install_wrapper() -> None:
    path = bin_dir() / "talk-anywhere"
    path.write_text(wrapper_text(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def install_autostart(enabled: bool, icon: str) -> None:
    path = autostart_dir() / "talk-anywhere.desktop"
    path.write_text(autostart_text(icon, enabled), encoding="utf-8")


def install() -> None:
    icons = write_icons(icon_dir())
    icon = str(icons["app.png"])
    install_wrapper()
    desktop = applications_dir() / "talk-anywhere.desktop"
    desktop.write_text(desktop_text(icon), encoding="utf-8")
    install_autostart(True, icon)
    command = f"{bin_dir() / 'talk-anywhere'} toggle"
    register_shortcut(DEFAULT_SHORTCUT, command)
    subprocess.run(
        ["update-desktop-database", str(applications_dir())],
        check=False,
        capture_output=True,
    )
    print(f"Installed launcher and shortcut {pretty_shortcut(DEFAULT_SHORTCUT)}")
    print(f"Command: {command}")
    if not ENGINE_PYTHON.is_file():
        print(f"Speech engine Python is not ready yet: {ENGINE_PYTHON}")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        print("Warning: no session bus, so the shortcut may not have been saved.")
