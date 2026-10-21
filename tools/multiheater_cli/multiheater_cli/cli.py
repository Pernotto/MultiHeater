"""Interactive menu: Configure / Launch test / Recalculate."""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

import questionary
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from . import checks
from . import config as config_mod
from . import storage
from .analysis import AnalysisOptions, analyze, yaml_snippet
from .ha import HAClient, HAError, RealClock
from .runner import TestParams, element_limit, parse_duty_steps, run_test
from .simulator import SimClock, Simulator

console = Console()


# --- prompt helpers -----------------------------------------------------------

def _ask(q):
    """questionary returns None on Ctrl-C; turn that into a clean abort."""
    answer = q.ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _float(prompt: str, default: float | None = None, optional: bool = False) -> float | None:
    def valid(v: str):
        if optional and v.strip() == "":
            return True
        try:
            float(v)
            return True
        except ValueError:
            return "enter a number"

    v = _ask(questionary.text(prompt, default="" if default is None else f"{default:g}", validate=valid))
    return None if v.strip() == "" else float(v)


def _pick_entity(ha, domain: str, prompt: str, suggestion: str | None = None) -> str:
    choices = [questionary.Choice(f"{eid}  ({name})" if name != eid else eid, value=eid)
               for eid, name in ha.list_entities(domain)]
    if not choices:
        return _ask(questionary.text(f"{prompt} (entity_id)", default=suggestion or f"{domain}."))
    default = next((c for c in choices if suggestion and c.value == suggestion), None)
    return _ask(questionary.select(prompt, choices=choices, default=default))


# --- configure ----------------------------------------------------------------

def configure(cfg: config_mod.Config) -> config_mod.Config:
    console.rule("Configure")
    url = _ask(questionary.text("Home Assistant URL", default=cfg.ha_url or "http://homeassistant.local:8123"))
    token = _ask(questionary.password("Long-lived access token (Profile > Security)", default=cfg.token))
    ha = HAClient(url, token)
    try:
        console.print(f"[green]✓[/] {ha.ping()}")
    except HAError as e:
        console.print(f"[red]✗ {e}[/]")
        return cfg
    control = _pick_entity(ha, "sensor", "Water temperature (control sensor)", cfg.control_sensor)
    if ha.get_float(control) is None:
        console.print(f"[yellow]! {control} has no numeric state right now[/]")
    ambient = None
    if _ask(questionary.confirm("Is there an ambient temperature sensor?", default=bool(cfg.ambient_sensor))):
        ambient = _pick_entity(ha, "sensor", "Ambient temperature sensor", cfg.ambient_sensor)
    poll = _float("Poll interval (s)", cfg.poll_interval)
    results = _ask(questionary.text("Results directory", default=cfg.results_dir))
    new = dataclasses.replace(cfg, ha_url=url, token=token, control_sensor=control, ambient_sensor=ambient,
                              poll_interval=poll, results_dir=results)
    config_mod.save(new)
    console.print(f"[green]Saved {config_mod.CONFIG_PATH}[/]")
    return new


# --- launch test --------------------------------------------------------------

def _suggest(ha, domain: str, element: str, suffix: str) -> str | None:
    wanted = f"{domain}.{element.lower().replace(' ', '_')}{suffix}"
    return wanted if any(eid == wanted for eid, _ in ha.list_entities(domain)) else None


def _zero_other_duties(cfg: config_mod.Config, ha, keep: str) -> None:
    candidates = {e["duty_entity"] for e in cfg.elements.values() if e.get("duty_entity")}
    candidates |= {eid for eid, _ in ha.list_entities("number") if "duty" in eid}
    others = sorted(eid for eid in candidates - {keep} if ha.get_float(eid))
    if others and _ask(questionary.confirm(f"Other duty numbers are non-zero ({', '.join(others)}). Set them to 0?",
                                           default=True)):
        for eid in others:
            ha.set_number(eid, 0)


def _live_view(p: TestParams, samples: list[dict], crossings: list[tuple[float, float]]):
    s = samples[-1]
    head = Table.grid(padding=(0, 2))
    head.add_row(
        f"[bold]{s['phase'].upper()}[/]",
        f"t {s['t'] / 60:6.1f} min",
        f"water {s['tw'] if s['tw'] is not None else float('nan'):6.2f} °C",
        f"element {s['tel'] if s['tel'] is not None else float('nan'):6.2f} °C",
        f"duty {s['duty'] * 100:5.1f} %",
    )
    limit = element_limit(s["tw"], p.max_delta, p.t_max) if s["tw"] is not None else float("nan")
    limited = s["tel"] is not None and s["tel"] > limit - p.limit_band and s["phase"] == "heat"
    limit_text = "no element limit" if limit == float("inf") else f"element limit {limit:.0f} °C"
    head.add_row(f"target water {p.max_water_temp:g} °C", limit_text,
                 "[yellow]LIMITED[/]" if limited else "full-on" if s["duty"] > 0 else "", "", "")
    bands = Table("band", "time", box=None)
    for i in range(1, len(crossings)):
        bands.add_row(f"{crossings[i - 1][0]:g}→{crossings[i][0]:g} °C",
                      f"{(crossings[i][1] - crossings[i - 1][1]) / 60:.1f} min")
    return Panel(Group(head, bands), title=f"Calibrating {p.element}  (Ctrl-C: skip to cool-down / stop)")


def launch_test(cfg: config_mod.Config, ha, clock, simulate: bool) -> None:
    console.rule("Launch test")
    mass_g = _float("Water mass (grams; 1 litre ≈ 1000 g)", cfg.water_mass_g)
    mass = mass_g / 1000.0
    other = "Other…"
    element = other
    if cfg.elements:
        element = _ask(questionary.select("Element", choices=[*cfg.elements, other]))
    if element == other:
        element = _ask(questionary.text("Element name", default="element_1"))
    preset = cfg.elements.get(element, {})
    duty_entity = _pick_entity(ha, "number", "Element duty number (0-100 %)",
                               preset.get("duty_entity") or _suggest(ha, "number", element, "_duty"))
    element_sensor = _pick_entity(ha, "sensor", "Element temperature sensor",
                                  preset.get("temperature_sensor") or _suggest(ha, "sensor", element, "_temperature"))
    max_delta = _float("Max element temperature above the water (°C, blank = no limit)",
                       preset.get("max_delta"), optional=True)
    t_max = _float("Absolute element ceiling (°C, blank = none)", preset.get("t_max"), optional=True)
    limited = max_delta is not None or t_max is not None
    start_duty = 100.0
    if limited:
        start_duty = _float("Starting duty (%, a rough guess; it is then adjusted a few points at a time)",
                            preset.get("start_duty") or 70)
    nominal = _float("Nominal element power (W, blank = unknown)", preset.get("nominal_power"), optional=True)
    ambient = None
    if not cfg.ambient_sensor:
        ambient = _float("Ambient temperature (°C)", 20)
    steps_text = _ask(questionary.text(
        "Duty steps 'water°C:duty%' (e.g. 50:60,65:30, blank = always 100 %)",
        validate=lambda v: _valid_steps(v)))
    max_water = _float("Stop heating at water temperature (°C)", 80)
    cool = _float("Cool-down logging (minutes, ≥ 30 recommended)", 60)

    p = TestParams(
        element=element, duty_entity=duty_entity, element_sensor=element_sensor,
        water_sensor=cfg.control_sensor, mass=mass, max_delta=max_delta, t_max=t_max, start_duty=start_duty,
        max_water_temp=max_water,
        cool_minutes=cool, ambient_sensor=cfg.ambient_sensor, ambient=ambient, nominal_power=nominal,
        duty_steps=parse_duty_steps(steps_text), poll_interval=cfg.poll_interval,
    )
    tw = ha.get_float(p.water_sensor)
    tel = ha.get_float(p.element_sensor)
    console.print(f"Water now {tw} °C, element now {tel} °C")
    if tw is None or tel is None:
        console.print("[red]Sensor unavailable, aborting[/]")
        return
    _zero_other_duties(cfg, ha, duty_entity)
    start_text = f"{start_duty:g} %" if limited else "full duty, no element limit"
    if not _ask(questionary.confirm(f"Start heating {element} at {start_text}?", default=True)):
        return

    samples: list[dict] = []
    crossings: list[tuple[float, float]] = []  # (5 °C boundary, time it was crossed)
    next_boundary = (tw // 5 + 1) * 5

    with Live(console=console, refresh_per_second=4, transient=False) as live:
        def on_sample(s):
            nonlocal next_boundary
            samples.append(s)
            if s["phase"] == "heat" and s["tw"] is not None and s["tw"] >= next_boundary:
                crossings.append((next_boundary, s["t"]))
                next_boundary += 5
            live.update(_live_view(p, samples, crossings))

        run = run_test(ha, clock, p, on_sample=on_sample)
        # Save before anything else: nothing after this point may cost the recording.
        run["simulated"] = simulate
        path = storage.new_path(cfg.results_dir, element)
        storage.save(path, run)

    console.print(f"[green]Saved {path.resolve()}[/]")
    for n in run["notes"]:
        console.print(f"• {n}")
    if not simulate:
        # Remember the answers as defaults for the next run.
        cfg.water_mass_g = mass_g
        cfg.elements[element] = {"duty_entity": duty_entity, "temperature_sensor": element_sensor,
                                 "max_delta": max_delta, "t_max": t_max, "start_duty": start_duty,
                                 "nominal_power": nominal}
        config_mod.save(cfg)
    try:
        result = analyze(run, AnalysisOptions(mass=mass))
    except ValueError as e:
        console.print(f"[red]Analysis failed: {e}[/]")
        return
    run["analysis"] = result
    storage.save(path, run)
    print_report(result, run["config"])


def _valid_steps(v: str):
    try:
        parse_duty_steps(v)
        return True
    except ValueError:
        return "format: 50:60,65:30 (duty 0-100)"


# --- test configuration -------------------------------------------------------

def test_configuration(cfg: config_mod.Config, ha) -> None:
    console.rule("Test configuration")
    results = checks.check_config(cfg, ha)
    marks = {checks.OK: "[green]✓[/]", checks.WARN: "[yellow]![/]", checks.FAIL: "[red]✗[/]"}
    t = Table(box=None)
    t.add_column("")
    t.add_column("check")
    t.add_column("result")
    for c in results:
        t.add_row(marks[c.level], c.name, c.detail)
    console.print(t)
    summary = {
        checks.OK: "[green]Everything is in order.[/]",
        checks.WARN: "[yellow]Usable, but look at the warnings first.[/]",
        checks.FAIL: "[red]Not ready: fix the failed checks before launching a test.[/]",
    }
    console.print(summary[checks.worst(results)])


# --- recalculate --------------------------------------------------------------

def recalculate(cfg: config_mod.Config) -> None:
    console.rule("Recalculate")
    files = storage.list_results(cfg.results_dir)
    if not files:
        console.print(f"No results in {cfg.results_dir}/")
        return
    path = Path(_ask(questionary.select("Result file", choices=[str(f) for f in files])))
    run = storage.load(path)
    c = run["config"]
    prev = run.get("analysis", {}).get("options", {})
    opts = AnalysisOptions(
        mass=_float("Water mass (grams)", c["mass"] * 1000.0) / 1000.0,
        ambient=_float("Ambient override (°C, blank = logged)", optional=True),
        window_s=_float("Smoothing window (s)", prev.get("window_s", 120)),
        trim_start_s=_float("Trim start (s)", prev.get("trim_start_s", 0)),
        trim_end_s=_float("Trim end (s)", prev.get("trim_end_s", 0)),
        tail_margin=_float("Cooling tail margin T_el - T_w (°C)", prev.get("tail_margin", 1.0)),
    )
    try:
        result = analyze(run, opts)
    except ValueError as e:
        console.print(f"[red]Analysis failed: {e}[/]")
        return
    print_report(result, c)
    if _ask(questionary.confirm("Save this analysis into the result file?", default=False)):
        run["analysis"] = result
        storage.save(path, run)
        console.print(f"[green]Saved {path}[/]")


# --- report -------------------------------------------------------------------

def print_report(r: dict, c: dict) -> None:
    t = Table(title=f"Fit for {c['element']}  (mass {r['options']['mass'] * 1000:g} g)")
    t.add_column("parameter")
    t.add_column("value", justify="right")
    t.add_row("P_full (power)", f"{r['power']:.0f} W")
    if r.get("power_factor"):
        t.add_row("power factor", f"{r['power_factor']:.3f}  (of {c['nominal_power']:g} W)")
    t.add_row("h (conductance)", f"{r['conductance']:.2f} W/K")
    t.add_row("C_el (heat capacity)", f"{r['heat_capacity']:.0f} J/K")
    if r.get("loss_quadratic"):
        t.add_row("vessel loss", f"{r['loss_coefficient']:.2f} W/K + {r['loss_quadratic']:.4f} W/K²")
        lw = r["loss_watts"]
        t.add_row("  loss at 30 / 50 °C above ambient", f"{lw['30']:.0f} W / {lw['50']:.0f} W")
    else:
        t.add_row("k (vessel loss)", f"{r['loss_coefficient']:.2f} W/K")
    if abs(r.get("sensor_offset") or 0) >= 0.2:
        t.add_row("sensor offset at rest", f"{r['sensor_offset']:+.2f} °C (element vs water, removed)")
    if r.get("max_delta") is not None:
        t.add_row("max_delta", f"{r['max_delta']:g} °C above the water")
    if r.get("t_max") is not None:
        t.add_row("absolute ceiling", f"{r['t_max']:g} °C")
    t.add_row("R² water / element", f"{r['r2']['water']:.4f} / {r['r2']['element']:.4f}")
    if r["r2"].get("loss") is not None:
        t.add_row("R² loss curve", f"{r['r2']['loss']:.4f}")
    t.add_row("energy balance residual", f"{r['energy_balance']['residual_pct']:.1f} %")
    t.add_row("cooling tail", f"{r['tail_minutes']:.1f} min")
    t.add_row("limited regime seen", "yes" if r["limited_regime"] else "no")
    console.print(t)

    b = Table(title="Heating by band")
    for col in ("water °C", "time (min)", "measured °C/min", "model °C/min"):
        b.add_column(col, justify="right")
    fmt = lambda v, f: "—" if v is None else f.format(v)  # noqa: E731
    for band in r["bands"]:
        if band["seconds"] is None and band["measured_k_min"] is None:
            continue
        b.add_row(f"{band['from']:g}–{band['to']:g}", fmt(band["seconds"] and band["seconds"] / 60, "{:.1f}"),
                  fmt(band["measured_k_min"], "{:.3f}"), fmt(band["model_k_min"], "{:.3f}"))
    console.print(b)
    for w in r["warnings"]:
        console.print(f"[yellow]! {w}[/]")
    console.print(Panel(yaml_snippet(r, c["element"]), title="multi_heater YAML"))


# --- main ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="multiheater", description=__doc__)
    ap.add_argument("--simulate", action="store_true", help="use a simulated Home Assistant (no hardware)")
    ap.add_argument("--sim-mass", type=float, default=20.0, help="simulated water mass in kg (default 20)")
    args = ap.parse_args(argv)

    cfg = config_mod.load()
    sim = None
    if args.simulate:
        sim = Simulator(mass=args.sim_mass)
        cfg = config_mod.Config(ha_url="sim", token="sim", control_sensor="sensor.water_temperature",
                                ambient_sensor="sensor.ambient_temperature", poll_interval=5.0,
                                results_dir=cfg.results_dir, water_mass_g=args.sim_mass * 1000,
                                elements={e.name: {"duty_entity": f"number.{e.name}_duty",
                                                   "temperature_sensor": f"sensor.{e.name}_temperature",
                                                   } for e in sim.elements})
        console.print(f"[magenta]Simulation mode: {args.sim_mass:g} kg water, elements "
                      f"{', '.join(e.name for e in sim.elements)}[/]")

    while True:
        try:
            choice = _ask(questionary.select(
                "MultiHeater calibration",
                choices=["Configure", "Test configuration", "Launch test", "Recalculate", "Quit"]))
            if choice == "Quit":
                return 0
            if choice == "Configure":
                if sim:
                    console.print("Configuration is fixed in simulation mode.")
                else:
                    cfg = configure(cfg)
            elif choice == "Test configuration":
                test_configuration(cfg, sim or HAClient(cfg.ha_url, cfg.token))
            elif choice == "Launch test":
                if sim:
                    launch_test(cfg, sim, SimClock(sim), simulate=True)
                    sim = Simulator(mass=args.sim_mass)  # fresh cold tank for the next run
                elif not cfg.complete:
                    console.print("[yellow]Run Configure first.[/]")
                else:
                    launch_test(cfg, HAClient(cfg.ha_url, cfg.token), RealClock(), simulate=False)
            elif choice == "Recalculate":
                recalculate(cfg)
        except KeyboardInterrupt:
            console.print("\n[dim]cancelled[/]")
        except HAError as e:
            console.print(f"[red]{e}[/]")


if __name__ == "__main__":
    sys.exit(main())
