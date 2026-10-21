"""Local CLI configuration stored in ~/.multiheater.json."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("MULTIHEATER_CONFIG", Path.home() / ".multiheater.json"))


@dataclass
class Config:
    ha_url: str = ""
    token: str = ""
    control_sensor: str = ""  # water temperature entity
    ambient_sensor: str | None = None
    poll_interval: float = 5.0
    results_dir: str = "results"
    water_mass_g: float | None = None  # grams, default for the Launch test prompt
    # Presets: name -> {"duty_entity", "temperature_sensor", "max_delta", optional "t_max", "nominal_power"}
    #   max_delta: degC the element may be above the water; t_max: optional absolute ceiling
    elements: dict[str, dict] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.ha_url and self.token and self.control_sensor)


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        return Config()
    data = json.loads(path.read_text())
    if "water_mass" in data and "water_mass_g" not in data:  # older files stored kg
        kg = data.pop("water_mass")
        data["water_mass_g"] = None if kg is None else kg * 1000.0
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in data.items() if k in known})


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(asdict(cfg), indent=2))
    path.chmod(0o600)
