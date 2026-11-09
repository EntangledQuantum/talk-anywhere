"""Download one Bark model. Prints JSON lines for the settings window."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from talk_anywhere.models import directory, ensure_processor, get_model, model_ready


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _directory_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for file in path.rglob("*"):
        if file.is_file():
            try:
                total += file.stat().st_size
            except OSError:
                pass
    return total


def download(model_key: str = "pro") -> None:
    spec = get_model(model_key)
    folder = directory(spec)
    folder.mkdir(parents=True, exist_ok=True)
    if model_ready(spec):
        emit({"event": "done", "path": str(folder), "bytes": spec.weights_bytes, "total": spec.weights_bytes})
        return

    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(0.4):
            emit(
                {
                    "event": "progress",
                    "bytes": _directory_bytes(folder),
                    "total": spec.weights_bytes,
                }
            )

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        from huggingface_hub import snapshot_download

        if spec.kind == "safetensors":
            patterns = [
                "model.safetensors",
                "config.json",
                *tuple(name for name in (
                    "processor_config.json",
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "generation_config.json",
                )),
                "NOTICE",
                "LICENSE",
            ]
        else:
            patterns = ["model.bark", "bark_loader.py", "NOTICE", "LICENSE"]
        snapshot_download(
            repo_id=spec.repo,
            local_dir=str(folder),
            allow_patterns=patterns,
        )
        if spec.kind == "bark":
            ensure_processor()
    except Exception as exc:
        emit({"event": "error", "message": f"Could not download {spec.title}. {exc}"})
        return
    finally:
        stop.set()
        watcher.join(timeout=1)

    if not model_ready(spec):
        emit(
            {
                "event": "error",
                "message": f"The download finished, but {spec.title} is incomplete.",
            }
        )
        return
    emit({"event": "done", "path": str(folder), "bytes": spec.weights_bytes, "total": spec.weights_bytes})


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "pro"
    download(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
