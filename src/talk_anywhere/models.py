"""Bark models a new install can download. English only."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from talk_anywhere.paths import PROCESSOR_FILES, models_root, processor_dir


@dataclass(frozen=True)
class BarkModel:
    key: str
    title: str
    repo: str
    kind: str
    summary: str
    weights_name: str
    weights_bytes: int
    dirname: str

    @property
    def label(self) -> str:
        return f"{self.title}   ·   {self.summary}"


MODELS: tuple[BarkModel, ...] = (
    BarkModel(
        key="pro",
        title="Bark 3.0 Pro",
        repo="ConwayResearch/Underdog-Bark-3.0-Pro",
        kind="safetensors",
        summary="Most accurate, 1.25 GB",
        weights_name="model.safetensors",
        weights_bytes=1_254_196_636,
        dirname="Underdog-Bark-3.0-Pro",
    ),
    BarkModel(
        key="plus",
        title="Bark 3.0 Plus",
        repo="ConwayResearch/Underdog-Bark-3.0-Plus",
        kind="bark",
        summary="Every word, 180 MB",
        weights_name="model.bark",
        weights_bytes=179_524_845,
        dirname="Underdog-Bark-3.0-Plus",
    ),
    BarkModel(
        key="base",
        title="Bark 3.0",
        repo="ConwayResearch/Underdog-Bark-3.0",
        kind="bark",
        summary="Every word, 163 MB",
        weights_name="model.bark",
        weights_bytes=163_398_645,
        dirname="Underdog-Bark-3.0",
    ),
    BarkModel(
        key="clean",
        title="Bark 3.0 Clean",
        repo="ConwayResearch/Underdog-Bark-3.0-Clean",
        kind="bark",
        summary="Drops um and false starts, 180 MB",
        weights_name="model.bark",
        weights_bytes=179_458_613,
        dirname="Underdog-Bark-3.0-Clean",
    ),
)


def get_model(key: str) -> BarkModel:
    for spec in MODELS:
        if spec.key == key:
            return spec
    return MODELS[0]


def directory(spec: BarkModel) -> Path:
    return models_root() / spec.dirname


def _files_ready(folder: Path, names: tuple[str, ...]) -> bool:
    for name in names:
        file = folder / name
        if not file.is_file() or file.stat().st_size <= 0:
            return False
    return True


def processor_source(spec: BarkModel | None = None) -> Path:
    """Folder that contains the tokenizer. Packed models share Pro's processor files."""
    if spec is not None and spec.kind == "safetensors":
        return directory(spec)
    pro = directory(get_model("pro"))
    if _files_ready(pro, PROCESSOR_FILES):
        return pro
    return processor_dir()


def model_ready(spec: BarkModel | None = None) -> bool:
    spec = spec or get_model("pro")
    folder = directory(spec)
    weights = folder / spec.weights_name
    if not weights.is_file() or weights.stat().st_size != spec.weights_bytes:
        return False
    if spec.kind == "bark" and not (folder / "bark_loader.py").is_file():
        return False
    if spec.kind == "safetensors":
        return _files_ready(folder, ("config.json", *PROCESSOR_FILES))
    return _files_ready(processor_source(spec), PROCESSOR_FILES)


def ensure_processor() -> Path:
    """Copy or download the small tokenizer files. Never downloads Pro's weights."""
    dest = processor_dir()
    if _files_ready(dest, PROCESSOR_FILES):
        return dest
    pro = directory(get_model("pro"))
    if _files_ready(pro, PROCESSOR_FILES):
        dest.mkdir(parents=True, exist_ok=True)
        for name in PROCESSOR_FILES:
            target = dest / name
            if not target.is_file():
                shutil.copy2(pro / name, target)
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=get_model("pro").repo,
        local_dir=str(dest),
        allow_patterns=list(PROCESSOR_FILES),
    )
    return dest
