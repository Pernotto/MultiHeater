"""Result files: one JSON per calibration run (config + samples + analysis)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


def new_path(results_dir: str | Path, element: str) -> Path:
    d = Path(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", element).strip("_") or "element"
    return d / f"{slug}_{datetime.now():%Y%m%d-%H%M}.json"


def save(path: Path, run: dict) -> None:
    path.write_text(json.dumps(run, indent=1))


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def list_results(results_dir: str | Path) -> list[Path]:
    d = Path(results_dir)
    return sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
