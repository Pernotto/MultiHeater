"""Pre-flight checks for the saved configuration: reachability, entities, fresh and plausible values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .ha import HAError

OK, WARN, FAIL = "ok", "warn", "fail"

# Home Assistant only moves an entity's timestamps when its value changes, so a steady
# temperature looks "old". Freshness is therefore judged across all the fast probes together:
# at least one of them must have changed within this many seconds.
ACTIVITY_WARN_AGE = 120.0

WATER_RANGE = (0.0, 100.0)
AMBIENT_RANGE = (-30.0, 50.0)
ELEMENT_MIN = -10.0


@dataclass
class Check:
    level: str
    name: str
    detail: str


def _age(state: dict, now: datetime) -> float | None:
    """Seconds since the entity's value last changed."""
    stamp = state.get("last_reported") or state.get("last_updated")
    if not stamp:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(stamp)).total_seconds())
    except (ValueError, TypeError):
        return None


def _value(state: dict) -> float | None:
    try:
        v = float(state["state"])
    except (ValueError, TypeError, KeyError):
        return None
    return None if v != v else v


def _fmt_age(age: float | None) -> str:
    if age is None:
        return "age unknown"
    return f"{age:.0f} s" if age < 120 else f"{age / 60:.0f} min"


def _sensor(ha, now, name, entity_id, lo, hi) -> tuple[Check, float | None, float | None]:
    """Returns (check, value, seconds since the value last changed)."""
    try:
        state = ha.get_state(entity_id)
    except HAError as e:
        return Check(FAIL, name, f"{entity_id}: {e}"), None, None
    v = _value(state)
    if v is None:
        return Check(FAIL, name, f"{entity_id} is '{state.get('state')}' (no numeric value)"), None, None
    age = _age(state, now)
    detail = f"{entity_id} = {v:.2f} °C, unchanged for {_fmt_age(age)}"
    if not lo <= v <= hi:
        return Check(FAIL, name, f"{detail}: outside the plausible range {lo:g}..{hi:g} °C"), v, age
    return Check(OK, name, detail), v, age


def _duty(ha, name, entity_id) -> Check:
    try:
        state = ha.get_state(entity_id)
    except HAError as e:
        return Check(FAIL, name, f"{entity_id}: {e}")
    v = _value(state)
    if v is None:
        return Check(FAIL, name, f"{entity_id} is '{state.get('state')}' (no numeric value)")
    attrs = state.get("attributes", {})
    lo, hi = attrs.get("min"), attrs.get("max")
    if lo is not None and hi is not None and (lo != 0 or hi != 100):
        return Check(FAIL, name, f"{entity_id} range is {lo:g}..{hi:g}, expected 0..100 %")
    if v != 0:
        return Check(WARN, name, f"{entity_id} = {v:g} %: the element is being driven right now")
    # Writing the value it already has proves the token may control it, without heating anything.
    try:
        ha.set_number(entity_id, 0)
    except HAError as e:
        return Check(FAIL, name, f"{entity_id} = 0 % but cannot be written: {e}")
    return Check(OK, name, f"{entity_id} = 0 %, writable")


def check_config(cfg: Config, ha, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    out: list[Check] = []

    if not cfg.complete:
        return [Check(FAIL, "Configuration", "incomplete: run Configure first")]
    try:
        out.append(Check(OK, "Home Assistant", f"{cfg.ha_url}: {ha.ping()}"))
    except HAError as e:
        return [Check(FAIL, "Home Assistant", f"{cfg.ha_url}: {e}")]

    water_check, water, water_age = _sensor(ha, now, "Water sensor", cfg.control_sensor, *WATER_RANGE)
    out.append(water_check)
    probe_ages = [water_age]

    if cfg.ambient_sensor:
        out.append(_sensor(ha, now, "Ambient sensor", cfg.ambient_sensor, *AMBIENT_RANGE)[0])
    else:
        out.append(Check(OK, "Ambient sensor", "none: Launch test will ask for a fixed value"))

    if cfg.water_mass_g is None:
        out.append(Check(WARN, "Water mass", "not set: Launch test will ask"))
    elif cfg.water_mass_g < 1000:
        out.append(Check(FAIL, "Water mass", f"{cfg.water_mass_g:g} g: implausible, the value must be in grams"))
    else:
        out.append(Check(OK, "Water mass", f"{cfg.water_mass_g:g} g"))

    if not cfg.elements:
        out.append(Check(WARN, "Elements", "no presets saved: Launch test will ask for the entities"))
    for name, el in cfg.elements.items():
        t_max = el.get("t_max")
        if el.get("duty_entity"):
            out.append(_duty(ha, f"{name}: duty", el["duty_entity"]))
        else:
            out.append(Check(FAIL, f"{name}: duty", "no duty_entity in the preset"))
        if not el.get("temperature_sensor"):
            out.append(Check(FAIL, f"{name}: temperature", "no temperature_sensor in the preset"))
            continue
        max_delta = el.get("max_delta")
        hi = t_max if t_max is not None else 300.0
        check, tel, age = _sensor(ha, now, f"{name}: temperature", el["temperature_sensor"], ELEMENT_MIN, hi)
        probe_ages.append(age)
        if check.level == FAIL and tel is not None and tel > hi:
            check.detail = f"{el['temperature_sensor']} = {tel:.2f} °C is above its ceiling {hi:g} °C: it will not heat"
        out.append(check)
        if max_delta is None:
            ceiling = f"only the ceiling of {t_max:g} °C" if t_max is not None else "none: the test runs at full duty"
            out.append(Check(OK, f"{name}: limit", ceiling))
        elif max_delta <= 0:
            out.append(Check(FAIL, f"{name}: limit", f"max_delta {max_delta:g} °C must be positive"))
        elif tel is not None and water is not None and tel - water >= max_delta:
            out.append(Check(FAIL, f"{name}: limit", f"element is {tel - water:.1f} °C above the water, "
                                                    f"max_delta is {max_delta:g} °C: it will not heat"))
        else:
            ceiling = f", ceiling {t_max:g} °C" if t_max is not None else ""
            now_limit = f" (limit now {water + max_delta:.0f} °C)" if water is not None else ""
            out.append(Check(OK, f"{name}: limit", f"water + {max_delta:g} °C{ceiling}{now_limit}"))

    ages = [a for a in probe_ages if a is not None]
    if ages:
        freshest = min(ages)
        if freshest > ACTIVITY_WARN_AGE:
            out.append(Check(WARN, "Sensor activity",
                             f"no probe value has changed for {_fmt_age(freshest)}: is the device online and reporting?"))
        else:
            out.append(Check(OK, "Sensor activity", f"latest probe change {_fmt_age(freshest)} ago"))
    return out


def worst(checks: list[Check]) -> str:
    levels = {c.level for c in checks}
    return FAIL if FAIL in levels else WARN if WARN in levels else OK
