"""Calibration run: heat one element, then coast/cool, logging everything."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

from .ha import HAError


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass
class TestParams:
    __test__ = False  # not a pytest class

    element: str
    duty_entity: str  # number.* 0..100
    element_sensor: str  # sensor.* element temperature
    water_sensor: str
    mass: float  # kg
    max_water_temp: float
    cool_minutes: float
    # Optional element limits. With neither set, the test runs at the full requested duty.
    max_delta: float | None = None  # element may be at most this much hotter than the water (degC)
    t_max: float | None = None  # absolute element ceiling (degC)
    ambient_sensor: str | None = None
    ambient: float | None = None  # used when there is no ambient sensor
    nominal_power: float | None = None
    duty_steps: list[tuple[float, float]] = field(default_factory=list)  # (water temp, duty %)
    # Duty control: start from a rough value and move it a few points at a time.
    start_duty: float = 70.0  # %, rough guess of what the element can take
    trim_step: float = 2.0  # points per adjustment
    trim_interval: float = 10.0  # s between adjustments
    avg_window: float = 30.0  # s of element temperature averaged for each decision
    limit_band: float = 5.0  # degC under the limit where the duty is left alone
    hard_margin: float = 15.0  # degC over the limit: off at once, come back lower
    settle_band: float = 1.0  # degC change across the averaging window that still counts as steady
    poll_interval: float = 5.0
    timeout_hours: float = 8.0
    max_failures: int = 30  # consecutive Home Assistant errors before giving up


def parse_duty_steps(text: str) -> list[tuple[float, float]]:
    """'60:50, 70:25' -> [(60, 50), (70, 25)]: at water >= 60 degC run 50 %, ..."""
    steps = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        temp, duty = part.split(":")
        steps.append((float(temp), float(duty)))
    for _, duty in steps:
        if not 0 <= duty <= 100:
            raise ValueError("duty must be 0..100")
    return sorted(steps)


def step_duty(steps: list[tuple[float, float]], tw: float) -> float:
    duty = 100.0
    for temp, d in steps:
        if tw >= temp:
            duty = d
    return duty


def element_limit(tw: float, max_delta: float | None, t_max: float | None = None) -> float:
    """Highest element temperature allowed right now (infinite when no limit is set)."""
    limit = float("inf") if max_delta is None else tw + max_delta
    return limit if t_max is None else min(limit, t_max)


def limited_duty(requested_pct: float, tel: float, limit: float, band: float) -> float:
    """Same limiter the ESPHome component applies: linear fade over the last `band` degC below the limit."""
    if tel >= limit:
        return 0.0
    return min(requested_pct, 100.0 * min(1.0, (limit - tel) / band))


class DutyTrim:
    """Slowly adjusted duty for the heat phase.

    The element answers a power change several seconds late, so a duty derived from the
    instantaneous distance to the limit swings between 0 and 100 %. This starts from a rough
    value and nudges it by `trim_step` every `trim_interval`, judging by the element's excess
    over its limit averaged over `avg_window`.
    """

    def __init__(self, p: TestParams):
        self.p = p
        self.duty = max(0.0, min(100.0, p.start_duty))
        self._last_adjust: float | None = None
        self._history: deque[tuple[float, float]] = deque()  # (time, element - limit)
        self._tripped = False

    def trip(self) -> None:
        """Back off hard: the element went far past its limit, or the firmware cut the heater."""
        if not self._tripped:
            self._tripped = True
            self.duty = max(0.0, self.duty - 5 * self.p.trim_step)

    def update(self, t: float, tw: float, tel: float, cap: float) -> float:
        """Returns the duty (%) to apply now. `cap` is the most the test wants (duty steps)."""
        p = self.p
        excess = tel - element_limit(tw, p.max_delta, p.t_max)
        self._history.append((t, excess))
        while self._history[0][0] < t - p.avg_window:
            self._history.popleft()
        if self._last_adjust is None:
            self._last_adjust = t

        # Safety net: far past the limit means the rough value was much too high.
        if excess >= p.hard_margin:
            self.trip()
            return 0.0
        if self._tripped:
            if excess > 0:
                return 0.0
            self._tripped = False
            self._last_adjust = t

        enough_history = self._history[0][0] <= t - p.avg_window / 2
        if enough_history and t - self._last_adjust >= p.trim_interval:
            self._last_adjust = t
            values = [e for _, e in self._history]
            avg = sum(values) / len(values)
            half = len(values) // 2
            # Trend across the window: positive while the element is still warming up.
            trend = sum(values[half:]) / (len(values) - half) - sum(values[:half]) / max(half, 1)
            settled = abs(trend) <= p.settle_band
            if avg > 0 and trend > -p.settle_band:
                # Over the limit and not already coming down.
                self.duty -= p.trim_step * (2 if avg > p.limit_band else 1)
            elif avg < -p.limit_band and settled and self.duty < cap:
                # Comfortably under the limit and no longer climbing: there is room for more.
                self.duty += p.trim_step
            self.duty = max(0.0, min(100.0, self.duty))
        return min(self.duty, cap)


def run_test(
    ha,
    clock: Clock,
    p: TestParams,
    on_sample: Callable[[dict], None] | None = None,
) -> dict:
    """Runs heat then cool-down. Always returns what was recorded, whatever interrupts it."""
    samples: list[dict] = []
    notes: list[str] = []
    t0 = clock.now()
    last_written: float | None = None
    trim = DutyTrim(p)
    limited = p.max_delta is not None or p.t_max is not None

    def ambient() -> float | None:
        if p.ambient_sensor:
            v = ha.get_float(p.ambient_sensor)
            if v is not None:
                return v
        return p.ambient

    def poll(phase: str) -> dict:
        nonlocal last_written
        tw = ha.get_float(p.water_sensor)
        tel = ha.get_float(p.element_sensor)
        readback = ha.get_float(p.duty_entity)
        cut = last_written is not None and readback is not None and last_written > 0 and readback < 0.5
        if phase != "heat" or tw is None or tel is None:
            duty = 0.0  # cooling, or fail safe on a missing sensor
        else:
            cap = step_duty(p.duty_steps, tw)
            if not limited:
                duty = round(cap)  # no element limit: run at the requested duty
            else:
                if cut:
                    trim.trip()  # the firmware cut the heater: stay off until under the limit
                duty = round(trim.update(clock.now() - t0, tw, tel, cap))
        if readback is None or abs(duty - readback) >= 0.5:
            ha.set_number(p.duty_entity, duty)
        last_written = duty
        s = {
            "t": clock.now() - t0,
            "phase": phase,
            "tw": tw,
            "tel": tel,
            "tamb": ambient(),
            "duty": duty / 100.0,
            "cut": bool(cut),
        }
        samples.append(s)
        if on_sample:
            try:
                on_sample(s)
            except Exception:  # noqa: BLE001  a display problem must not stop the test
                pass
        return s

    phase = "heat"
    cool_end = None
    failures = 0
    try:
        while True:
            try:
                if phase == "cool" and clock.now() >= cool_end:
                    break
                s = poll(phase)
                failures = 0
                if phase == "heat":
                    done = None
                    if s["tw"] is not None and s["tw"] >= p.max_water_temp:
                        done = "max water temperature reached"
                    elif s["t"] > p.timeout_hours * 3600:
                        done = "timeout"
                    if done:
                        notes.append(f"heat phase ended: {done}")
                        phase = "cool"
                        cool_end = clock.now() + p.cool_minutes * 60
                        continue  # poll again right away: that sets the duty to 0
                clock.sleep(p.poll_interval)
            except KeyboardInterrupt:
                if phase == "cool":
                    notes.append("cool phase interrupted by user")
                    break
                notes.append("heat phase interrupted by user")
                phase = "cool"
                cool_end = clock.now() + p.cool_minutes * 60
            except HAError as e:
                failures += 1
                if failures == 1:
                    notes.append(f"Home Assistant error at t={clock.now() - t0:.0f}s: {e}")
                if failures > p.max_failures:
                    notes.append("aborted: Home Assistant unreachable")
                    break
                clock.sleep(p.poll_interval)
    # Nothing may escape without returning the samples: the caller saves them.
    except KeyboardInterrupt:
        notes.append("interrupted by user")
    except Exception as e:  # noqa: BLE001
        notes.append(f"aborted by an unexpected error: {e!r}")
    finally:
        try:
            ha.set_number(p.duty_entity, 0)
        except (HAError, KeyboardInterrupt):
            notes.append("WARNING: could not set the duty to 0 at the end; check the element")

    config = asdict(p)
    config["duty_steps"] = [list(s) for s in p.duty_steps]
    return {"config": config, "samples": samples, "notes": notes}
