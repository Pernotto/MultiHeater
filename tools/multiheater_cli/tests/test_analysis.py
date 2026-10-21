import pytest

from multiheater_cli.analysis import AnalysisOptions, analyze
from multiheater_cli.runner import TestParams, element_limit, limited_duty, parse_duty_steps, run_test, step_duty
from multiheater_cli.simulator import SimClock, Simulator


def _run(duty_steps, element="element_1", max_delta=None, mass=20.0, cool_minutes=60, t_max=None):
    sim = Simulator(mass=mass)
    p = TestParams(
        element=element,
        duty_entity=f"number.{element}_duty",
        element_sensor=f"sensor.{element}_temperature",
        water_sensor="sensor.water_temperature",
        ambient_sensor="sensor.ambient_temperature",
        mass=mass,
        max_delta=max_delta,
        t_max=t_max,
        max_water_temp=80,
        cool_minutes=cool_minutes,
        duty_steps=duty_steps,
    )
    run = run_test(sim, SimClock(sim), p)
    truth = next(e for e in sim.elements if e.name == element)
    return run, truth, sim


def _assert_close(actual, expected, rel=0.05):
    assert actual == pytest.approx(expected, rel=rel)


@pytest.mark.parametrize(
    "duty_steps, max_delta, expect_limited",
    [
        ([(50, 60), (65, 30)], None, False),  # no element limit: the duty follows the steps exactly
        ([], 40, True),  # limit below the natural gap: the limiter throttles the whole run
    ],
)
def test_fit_recovers_parameters(duty_steps, max_delta, expect_limited):
    run, truth, sim = _run(duty_steps, max_delta=max_delta)
    r = analyze(run, AnalysisOptions(mass=sim.mass))
    _assert_close(r["power"], truth.power)
    _assert_close(r["conductance"], truth.conductance)
    # A run throttled from start to finish has few element transients, so C_el is less well determined.
    _assert_close(r["heat_capacity"], truth.heat_capacity, rel=0.1)
    _assert_close(r["loss_coefficient"], sim.loss, rel=0.1)
    assert r["limited_regime"] is expect_limited
    assert abs(r["energy_balance"]["residual_pct"]) < 5


def test_duty_settles_without_swinging():
    """element_1 sits 50 degC above the water at 100 %, so a 40 degC limit needs about 80 % duty."""
    run, truth, _ = _run([], max_delta=40)
    heat = [s for s in run["samples"] if s["phase"] == "heat"]
    assert max(s["tel"] - s["tw"] for s in heat) <= 43
    assert not any(s["cut"] for s in run["samples"])
    duties = [s["duty"] * 100 for s in heat]
    assert max(abs(b - a) for a, b in zip(duties, duties[1:])) <= 4  # a few points at a time, never 0 <-> 100
    settled = [d for s, d in zip(heat, duties) if s["t"] > 600]
    assert 70 <= min(settled) and max(settled) <= 90


def test_duty_climbs_to_full_power_when_the_limit_allows():
    run, _, _ = _run([], max_delta=60)
    heat = [s for s in run["samples"] if s["phase"] == "heat"]
    assert heat[0]["duty"] == pytest.approx(0.70)
    assert heat[-1]["duty"] == 1.0


def test_no_limits_runs_at_full_duty():
    run, _, _ = _run([])
    heat = [s for s in run["samples"] if s["phase"] == "heat"]
    assert all(s["duty"] == 1.0 for s in heat)
    from multiheater_cli.analysis import AnalysisOptions, analyze
    assert not analyze(run, AnalysisOptions(mass=20)).get("warnings")


def test_absolute_ceiling_still_applies():
    run, _, _ = _run([], max_delta=60, t_max=100)
    assert max(s["tel"] for s in run["samples"]) <= 103


def test_trim_trips_when_far_over_the_limit():
    from multiheater_cli.runner import DutyTrim

    p = TestParams(element="e", duty_entity="n", element_sensor="s", water_sensor="w", mass=1,
                   max_delta=50, max_water_temp=80, cool_minutes=1)
    trim = DutyTrim(p)
    assert trim.update(0, 20, 30, 100) == 70
    assert trim.update(5, 20, 86, 100) == 0  # 16 degC over the limit of 70
    assert trim.update(10, 20, 72, 100) == 0  # still over
    assert trim.update(15, 20, 69, 100) == 60  # back under: resumes 10 points lower


def test_second_element():
    run, truth, sim = _run([(60, 50)], element="element_2")
    r = analyze(run, AnalysisOptions(mass=sim.mass))
    _assert_close(r["power"], truth.power)
    _assert_close(r["conductance"], truth.conductance)
    _assert_close(r["heat_capacity"], truth.heat_capacity)


def test_short_cooldown_warns():
    run, _, sim = _run([], cool_minutes=5)
    r = analyze(run, AnalysisOptions(mass=sim.mass))
    assert any("cooling tail" in w for w in r["warnings"])


def test_helpers():
    assert parse_duty_steps("70:25, 60:50") == [(60, 50), (70, 25)]
    with pytest.raises(ValueError):
        parse_duty_steps("60:150")
    assert step_duty([(60, 50), (70, 25)], 65) == 50
    assert step_duty([(60, 50)], 20) == 100
    assert element_limit(70, 50) == 120
    assert element_limit(90, 50, t_max=130) == 130
    assert limited_duty(100, 117.5, 120, 5) == pytest.approx(50)
    assert limited_duty(100, 121, 120, 5) == 0


def test_check_config():
    from datetime import datetime, timedelta, timezone

    from multiheater_cli import checks
    from multiheater_cli.config import Config

    sim = Simulator()
    cfg = Config(
        ha_url="sim", token="sim", control_sensor="sensor.water_temperature",
        ambient_sensor="sensor.ambient_temperature", water_mass_g=20000,
        elements={"element_1": {"duty_entity": "number.element_1_duty",
                                "temperature_sensor": "sensor.element_1_temperature", "max_delta": 50, "t_max": 120}},
    )
    assert checks.worst(checks.check_config(cfg, sim)) == checks.OK

    # Nothing has changed for 10 minutes: steady values are fine, a silent device is a warning.
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    stale = {c.name: c.level for c in checks.check_config(cfg, sim, now=later)}
    assert stale["Water sensor"] == checks.OK
    assert stale["Sensor activity"] == checks.WARN

    # Element already driven, element above its limit, missing entity.
    sim.elements[0].duty = 0.5
    sim.elements[0].tel = 130
    cfg.elements["ghost"] = {"duty_entity": "number.ghost_duty", "temperature_sensor": "sensor.ghost", "max_delta": 50}
    res = {c.name: c for c in checks.check_config(cfg, sim)}
    assert res["element_1: duty"].level == checks.WARN
    assert res["element_1: temperature"].level == checks.FAIL and "above its ceiling" in res["element_1: temperature"].detail
    assert res["ghost: duty"].level == checks.FAIL
    assert sim.elements[0].duty == 0.5  # the check must not touch a running element

    assert checks.check_config(Config(), sim)[0].level == checks.FAIL


def test_run_survives_interrupts_and_ha_errors():
    """Ctrl-C and Home Assistant hiccups must never lose the recorded samples."""
    from multiheater_cli.ha import HAError

    sim = Simulator()
    clock = SimClock(sim)
    events = {600: KeyboardInterrupt, 900: HAError("boom"), 1500: KeyboardInterrupt}
    real_sleep = clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        for at in sorted(events):
            if sim.t >= at:
                exc = events.pop(at)
                raise exc if not isinstance(exc, type) else exc()

    clock.sleep = sleep
    p = TestParams(element="element_1", duty_entity="number.element_1_duty",
                   element_sensor="sensor.element_1_temperature", water_sensor="sensor.water_temperature",
                   mass=20, max_delta=60, max_water_temp=80, cool_minutes=60)
    run = run_test(sim, clock, p)
    phases = [s["phase"] for s in run["samples"]]
    assert "heat" in phases and "cool" in phases
    assert run["samples"][-1]["t"] >= 1400
    assert any("heat phase interrupted" in n for n in run["notes"])
    assert any("cool phase interrupted" in n for n in run["notes"])
    assert any("Home Assistant error" in n for n in run["notes"])
    assert sim.elements[0].duty == 0


def test_firmware_cut_is_not_fought():
    """When the firmware zeroes the duty, the CLI stays off until the element is under its limit."""
    sim = Simulator()
    sim.elements[0].firmware_max_delta = 45  # firmware cuts 5 degC above the CLI limit
    p = TestParams(element="element_1", duty_entity="number.element_1_duty",
                   element_sensor="sensor.element_1_temperature", water_sensor="sensor.water_temperature",
                   mass=20, max_delta=40, max_water_temp=40, cool_minutes=1, start_duty=100)
    run = run_test(sim, SimClock(sim), p)
    heat = [s for s in run["samples"] if s["phase"] == "heat"]
    cuts = [i for i, s in enumerate(heat) if s["cut"]]
    assert cuts, "the scenario should trigger the firmware cut"
    for i in cuts:
        assert heat[i]["duty"] == 0  # not rewritten straight back
    assert len(cuts) <= 6


def test_cooldown_is_used_despite_a_sensor_offset():
    """The element probe reading above the water probe at rest must not hide the cool-down."""
    sim = Simulator(element_sensor_offset=1.5)
    p = TestParams(element="element_1", duty_entity="number.element_1_duty",
                   element_sensor="sensor.element_1_temperature", water_sensor="sensor.water_temperature",
                   ambient_sensor="sensor.ambient_temperature", mass=20, max_water_temp=80, cool_minutes=60)
    run = run_test(sim, SimClock(sim), p)
    r = analyze(run, AnalysisOptions(mass=20))
    assert r["tail_minutes"] > 45
    assert r["sensor_offset"] == pytest.approx(1.5, abs=0.2)
    _assert_close(r["power"], 2000)
    _assert_close(r["loss_coefficient"], sim.loss, rel=0.1)


def test_curved_loss_is_recovered():
    """Open vessel: the loss grows faster than the temperature difference."""
    sim = Simulator(loss=1.5, loss_quadratic=0.2, element_sensor_offset=1.5)
    p = TestParams(element="element_1", duty_entity="number.element_1_duty",
                   element_sensor="sensor.element_1_temperature", water_sensor="sensor.water_temperature",
                   ambient_sensor="sensor.ambient_temperature", mass=20, max_water_temp=78, cool_minutes=120)
    run = run_test(sim, SimClock(sim), p)
    r = analyze(run, AnalysisOptions(mass=20))
    for d_t in (30, 50):
        truth = 1.5 * d_t + 0.2 * d_t * d_t
        assert r["loss_watts"][str(d_t)] == pytest.approx(truth, rel=0.08)
    _assert_close(r["power"], 2000)
    _assert_close(r["conductance"], 40)
    assert abs(r["energy_balance"]["residual_pct"]) < 5
