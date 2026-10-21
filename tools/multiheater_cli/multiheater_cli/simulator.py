"""Fake Home Assistant backed by the two-node thermal model, for running without hardware."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from dataclasses import dataclass, field

from .analysis import C_WATER
from .ha import HAError


@dataclass
class SimElement:
    name: str
    power: float  # W at 100 %
    conductance: float  # W/K
    heat_capacity: float  # J/K
    firmware_max_delta: float = 70.0  # calibration.yaml hard cut: element - water
    firmware_t_max: float = 160.0  # calibration.yaml absolute hard cut
    tel: float = 20.0
    duty: float = 0.0  # 0..1


@dataclass
class Simulator:
    mass: float = 20.0
    loss: float = 3.0  # k W/K
    loss_quadratic: float = 0.0  # W/K^2
    element_sensor_offset: float = 0.0  # element probes read this much above the water probe at rest
    ambient: float = 20.0
    tw: float = 20.0
    noise: float = 0.02
    quantum: float = 0.0625  # DS18B20 12-bit
    seed: int = 1
    elements: list[SimElement] = field(default_factory=list)
    t: float = 0.0

    def __post_init__(self):
        if not self.elements:
            self.elements = [
                SimElement("element_1", power=2000, conductance=40, heat_capacity=1500),
                SimElement("element_2", power=1000, conductance=25, heat_capacity=800),
            ]
        for e in self.elements:
            e.tel = self.tw
        self._rng = random.Random(self.seed)

    def step(self, dt: float) -> None:
        mc = self.mass * C_WATER
        remaining = dt
        while remaining > 0:
            h = min(0.5, remaining)
            d_t = self.tw - self.ambient
            q_water = -(self.loss * d_t + self.loss_quadratic * d_t * d_t)
            for e in self.elements:
                if e.tel >= e.firmware_t_max or e.tel - self.tw >= e.firmware_max_delta:
                    e.duty = 0.0  # on_value_range hard cut
                q = e.conductance * (e.tel - self.tw)
                e.tel += h * (e.duty * e.power - q) / e.heat_capacity
                q_water += q
            self.tw += h * q_water / mc
            self.t += h
            remaining -= h

    def _measure(self, v: float) -> float:
        v += self._rng.gauss(0, self.noise)
        return round(v / self.quantum) * self.quantum

    def _element(self, entity_id: str) -> SimElement | None:
        for e in self.elements:
            if entity_id in (f"number.{e.name}_duty", f"sensor.{e.name}_temperature"):
                return e
        return None

    # --- HAClient interface -------------------------------------------------
    def ping(self) -> str:
        return "API running. (simulator)"

    def get_float(self, entity_id: str) -> float | None:
        if entity_id == "sensor.water_temperature":
            return self._measure(self.tw)
        if entity_id == "sensor.ambient_temperature":
            return self._measure(self.ambient)
        e = self._element(entity_id)
        if e is None:
            raise HAError(f"not found: {entity_id}")
        if entity_id.startswith("number."):
            return round(e.duty * 100.0)
        return self._measure(e.tel + self.element_sensor_offset)

    def get_state(self, entity_id: str) -> dict:
        attrs = {"min": 0.0, "max": 100.0, "step": 1.0} if entity_id.startswith("number.") else {}
        now = datetime.now(timezone.utc).isoformat()
        return {"entity_id": entity_id, "state": str(self.get_float(entity_id)), "attributes": attrs,
                "last_reported": now, "last_updated": now}

    def set_number(self, entity_id: str, value: float) -> None:
        e = self._element(entity_id)
        if e is None or not entity_id.startswith("number."):
            raise HAError(f"not found: {entity_id}")
        e.duty = max(0.0, min(100.0, float(value))) / 100.0

    def list_entities(self, domain: str) -> list[tuple[str, str]]:
        ids = ["sensor.water_temperature", "sensor.ambient_temperature"]
        for e in self.elements:
            ids += [f"number.{e.name}_duty", f"sensor.{e.name}_temperature"]
        return [(i, i) for i in sorted(ids) if i.startswith(f"{domain}.")]


class SimClock:
    def __init__(self, sim: Simulator):
        self.sim = sim

    def now(self) -> float:
        return self.sim.t

    def sleep(self, seconds: float) -> None:
        self.sim.step(seconds)
