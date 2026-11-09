"""User settings. The microphone here is the app's choice, not a system default."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from talk_anywhere.models import get_model
from talk_anywhere.paths import DEFAULT_SHORTCUT, config_path
from talk_anywhere.sounds import DEFAULT_PAIR, PAIRS


@dataclass
class Config:
    shortcut: str = DEFAULT_SHORTCUT
    mic_name: str = ""
    mic_description: str = ""
    model_key: str = "pro"
    sound_pair: str = DEFAULT_PAIR
    start_on_login: bool = True
    intro_shown: bool = False

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = cls()
            cfg.save()
            return cfg
        sound = str(data.get("sound_pair") or DEFAULT_PAIR)
        if sound not in PAIRS:
            sound = DEFAULT_PAIR
        return cls(
            shortcut=str(data.get("shortcut") or ""),
            mic_name=str(data.get("mic_name") or ""),
            mic_description=str(data.get("mic_description") or ""),
            model_key=get_model(str(data.get("model_key") or "pro")).key,
            sound_pair=sound,
            start_on_login=bool(data.get("start_on_login", True)),
            intro_shown=bool(data.get("intro_shown", False)),
        )

    def save(self) -> None:
        payload = asdict(self)
        payload["model_repo"] = get_model(self.model_key).repo
        payload["language"] = "en"
        config_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
