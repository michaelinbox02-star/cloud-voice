"""Download the base model assets RVC needs, straight onto the GPU worker."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from huggingface_hub import hf_hub_download

REPO = "lj1995/VoiceConversionWebUI"
ROOT = Path("/opt/rvc")
ASSETS = ROOT / "assets"

INFERENCE_FILES = [
    ("hubert_base/config.json", "assets/hubert_base/config.json"),
    ("hubert_base/preprocessor_config.json", "assets/hubert_base/preprocessor_config.json"),
    ("hubert_base/pytorch_model.bin", "assets/hubert_base/pytorch_model.bin"),
    ("rmvpe.pt", "assets/rmvpe/rmvpe.pt"),
]

TRAINING_FILES = [
    ("pretrained_v2/f0G40k.pth", "assets/pretrained_v2/f0G40k.pth"),
    ("pretrained_v2/f0D40k.pth", "assets/pretrained_v2/f0D40k.pth"),
    ("pretrained_v2/G40k.pth", "assets/pretrained_v2/G40k.pth"),
    ("pretrained_v2/D40k.pth", "assets/pretrained_v2/D40k.pth"),
]


def _fetch(remote: str, relative: str) -> None:
    destination = ROOT / relative
    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    cached = hf_hub_download(repo_id=REPO, filename=remote, cache_dir="/opt/rvc/.hf")
    shutil.copyfile(cached, destination)


def prepare(training: bool = False) -> dict:
    """Fetch what inference needs, and the fine-tuning checkpoints on request."""
    wanted = list(INFERENCE_FILES)
    if training:
        wanted += TRAINING_FILES
    for remote, relative in wanted:
        _fetch(remote, relative)

    if training:
        mute_dir = ROOT / "logs" / "mute"
        if not mute_dir.is_dir() or not any(mute_dir.iterdir()):
            cache = hf_hub_download(repo_id=REPO, filename="mute.zip", cache_dir="/opt/rvc/.hf")
            with zipfile.ZipFile(cache) as archive:
                archive.extractall(ROOT / "logs")

    for sub in ("weights", "indices"):
        (ASSETS / sub).mkdir(parents=True, exist_ok=True)

    return {
        "assets_root": str(ASSETS),
        "inference_ready": all((ROOT / path).is_file() for _, path in INFERENCE_FILES),
        "training_ready": all((ROOT / path).is_file() for _, path in TRAINING_FILES),
        "weights": sorted(p.name for p in (ASSETS / "weights").glob("*.pth")),
        "indices": sorted(p.name for p in (ASSETS / "indices").glob("*.index")),
    }
