"""Register one GNOME shortcut that toggles dictation.

Super+Space and Shift+Super+Space are already used to switch keyboard
layout, so the default is Ctrl+Super+Space.
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
BINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/talk-anywhere/"

_MODIFIER_KEYS = {
    Gdk.KEY_Shift_L,
    Gdk.KEY_Shift_R,
    Gdk.KEY_Control_L,
    Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L,
    Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L,
    Gdk.KEY_Super_R,
    Gdk.KEY_Meta_L,
    Gdk.KEY_Meta_R,
    Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_ISO_Level5_Shift,
}

_SPECIAL = {
    "return": "Return",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
}


class ShortcutError(RuntimeError):
    pass


def validate_shortcut(binding: str) -> str | None:
    if "<Super>" not in binding and "<Mod4>" not in binding:
        return "Include the Windows key in the shortcut."
    key = re.sub(r"<[^>]+>", "", binding)
    if not key:
        return "Press the Windows key plus another key."
    return None


def pretty_shortcut(binding: str) -> str:
    if not binding:
        return "Not set"
    names = {
        "Control": "Ctrl",
        "Primary": "Ctrl",
        "Super": "Super",
        "Mod4": "Super",
        "Shift": "Shift",
        "Alt": "Alt",
    }
    parts: list[str] = []
    for modifier, key in re.findall(r"<([^>]+)>|([^<]+)", binding):
        if modifier:
            parts.append(names.get(modifier, modifier))
        elif key:
            shown = _SPECIAL.get(key.lower(), key)
            if shown.startswith("F") and shown[1:].isdigit():
                parts.append(shown.upper())
            elif len(shown) == 1:
                parts.append(shown.upper())
            elif shown == "space":
                parts.append("Space")
            else:
                parts.append(shown)
    return " + ".join(parts)


def binding_from_event(event: Gdk.EventKey) -> str | None:
    """Return a GNOME accelerator, 'cancel', or None while a modifier is held."""
    if event.keyval == Gdk.KEY_Escape:
        return "cancel"
    if event.keyval in _MODIFIER_KEYS:
        return None
    state = event.state
    parts: list[str] = []
    if state & Gdk.ModifierType.CONTROL_MASK:
        parts.append("<Control>")
    if state & Gdk.ModifierType.MOD1_MASK:
        parts.append("<Alt>")
    if state & Gdk.ModifierType.SHIFT_MASK:
        parts.append("<Shift>")
    super_mask = getattr(Gdk.ModifierType, "SUPER_MASK", 0)
    mod4_mask = getattr(Gdk.ModifierType, "MOD4_MASK", 0)
    if (super_mask and state & super_mask) or (mod4_mask and state & mod4_mask):
        parts.append("<Super>")
    name = Gdk.keyval_name(Gdk.keyval_to_lower(event.keyval)) or ""
    if not name:
        return None
    if len(name) == 1:
        key = name.lower()
    elif name.lower().startswith("f") and name[1:].isdigit():
        key = "F" + name[1:]
    else:
        key = _SPECIAL.get(name.lower(), name)
    parts.append(key)
    return "".join(parts)


def register_shortcut(binding: str, command: str) -> None:
    problem = validate_shortcut(binding)
    if problem:
        raise ShortcutError(problem)
    try:
        custom = Gio.Settings.new_with_path(CUSTOM, BINDING_PATH)
        media = Gio.Settings.new(SCHEMA)
    except GLib.Error as exc:
        raise ShortcutError(f"GNOME keyboard settings are not available. {exc}") from exc
    custom.set_string("name", "Talk Anywhere")
    custom.set_string("command", command)
    custom.set_string("binding", binding)
    paths = list(media.get_strv("custom-keybindings"))
    if BINDING_PATH not in paths:
        paths.append(BINDING_PATH)
        media.set_strv("custom-keybindings", paths)
    Gio.Settings.sync()


def current_binding() -> str:
    try:
        custom = Gio.Settings.new_with_path(CUSTOM, BINDING_PATH)
    except GLib.Error:
        return ""
    return custom.get_string("binding") or ""


def accelerator_label(binding: str) -> str:
    ok, key, mods = Gtk.accelerator_parse(binding)
    if not ok:
        return pretty_shortcut(binding)
    return Gtk.accelerator_get_label(key, mods) or pretty_shortcut(binding)
