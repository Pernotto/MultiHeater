"""Fit the two-node thermal model to a recorded calibration run.

    element : C_el * dT_el/dt = duty * P_full - h * (T_el - T_w)
    water   : m*c  * dT_w/dt  = h * (T_el - T_w) - k * (T_w - T_amb)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

C_WATER = 4186.0  # J/(kg*K)
BAND_WIDTH = 5.0  # degC


@dataclass
class AnalysisOptions:
    mass: float  # kg
    ambient: float | None = None  # overrides the logged ambient when set
    window_s: float = 120.0  # smoothing window for derivatives
    trim_start_s: float = 0.0
    trim_end_s: float = 0.0
    tail_margin: float = 1.0  # cooling tail = samples where T_el - T_w (offset removed) < margin
    iterations: int = 5
    # Constant difference between the element and water sensors at rest. None = measured from
    # the end of the cool-down.
    sensor_offset: float | None = None
    loss_window_s: float = 600.0  # smoothing window for the slow cooling curve
    quadratic_loss: bool = True  # fit loss = k1*dT + k2*dT^2 when the cool-down spans enough range


def _windowed(t: np.ndarray, y: np.ndarray, half: float) -> tuple[np.ndarray, np.ndarray]:
    """Centered windowed linear fit: returns (smoothed value, slope) per sample."""
    n = len(t)
    val = np.full(n, np.nan)
    slope = np.full(n, np.nan)
    lo = hi = 0
    for i in range(n):
        while t[lo] < t[i] - half:
            lo += 1
        while hi < n and t[hi] <= t[i] + half:
            hi += 1
        tt = t[lo:hi]
        yy = y[lo:hi]
        if len(tt) < 3:
            continue
        tc = tt - tt.mean()
        den = float((tc * tc).sum())
        if den <= 0:
            continue
        b = float((tc * (yy - yy.mean())).sum()) / den
        slope[i] = b
        val[i] = yy.mean() + b * (t[i] - tt.mean())
    return val, slope


def _r2(y: np.ndarray, y_hat: np.ndarray) -> float:
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def _arrays(samples: list[dict], ambient_override: float | None, default_ambient: float | None):
    t = np.array([s["t"] for s in samples], dtype=float)
    tw = np.array([s["tw"] for s in samples], dtype=float)
    tel = np.array([s["tel"] for s in samples], dtype=float)
    duty = np.array([s["duty"] for s in samples], dtype=float)
    phase = np.array([s["phase"] for s in samples])
    if ambient_override is not None:
        ta = np.full(len(t), float(ambient_override))
    else:
        raw = [s.get("tamb") for s in samples]
        fallback = default_ambient if default_ambient is not None else 20.0
        ta = np.array([fallback if v is None else v for v in raw], dtype=float)
    return t, tw, tel, duty, ta, phase


def analyze(run: dict, opts: AnalysisOptions) -> dict:
    cfg = run["config"]
    samples = [s for s in run["samples"] if s.get("tw") is not None and s.get("tel") is not None]
    if len(samples) < 20:
        raise ValueError(f"not enough valid samples ({len(samples)})")
    t, tw, tel, duty, ta, phase = _arrays(samples, opts.ambient, cfg.get("ambient"))
    t = t - t[0]
    half = opts.window_s / 2
    mc = opts.mass * C_WATER
    warnings: list[str] = []

    tw_s, dtw = _windowed(t, tw, half)
    tel_s, dtel = _windowed(t, tel, half)
    duty_s, _ = _windowed(t, duty, half)
    ta_s, _ = _windowed(t, ta, half)
    # At rest the two sensors do not read the same. Without removing that offset the element
    # never looks "back in equilibrium" and the whole cool-down would be ignored.
    offset = opts.sensor_offset
    if offset is None:
        offset = 0.0
        resting = np.flatnonzero((phase == "cool") & (duty == 0))
        if len(resting) and t[resting[-1]] - t[resting[0]] >= 1200:
            last = resting[t[resting] >= t[resting[-1]] - 600]
            offset = float(np.median(tel[last] - tw[last]))
        elif len(resting):
            warnings.append("cool-down shorter than 20 min: the offset between the sensors could not be measured")
    gap = tel_s - tw_s - offset

    valid = ~(np.isnan(dtw) | np.isnan(dtel) | np.isnan(duty_s))
    valid &= (t >= opts.trim_start_s) & (t <= t[-1] - opts.trim_end_s)
    # Window edges have half-empty windows, which bias the fits.
    valid &= (t >= half) & (t <= t[-1] - half)
    # The firmware hard cut overrode the logged duty around these samples.
    cut = np.array([bool(s.get("cut")) for s in samples])
    if cut.any():
        warnings.append(f"{int(cut.sum())} samples hit the firmware T_max cut; excluded from the fit")
        for i in np.flatnonzero(cut):
            valid &= np.abs(t - t[i]) > half + 1e-9

    # Cooling tail: element off and back in equilibrium with the water.
    cool_idx = np.flatnonzero((phase == "cool") & valid)
    tail = np.array([], dtype=int)
    if len(cool_idx):
        settled = cool_idx[(gap[cool_idx] < opts.tail_margin) & (duty[cool_idx] == 0)]
        if len(settled):
            tail = np.arange(settled[0], cool_idx[-1] + 1)
    tail_duration = float(t[tail[-1]] - t[tail[0]]) if len(tail) > 1 else 0.0
    if tail_duration < 600:
        warnings.append(
            f"cooling tail only {tail_duration / 60:.1f} min; loss coefficient k is unreliable "
            "(use a longer cool-down)"
        )

    k = 0.0  # loss = k*dT + k2*dT^2, dT = water - ambient
    k2 = 0.0
    h = 0.0
    fit = valid.copy()

    def loss(d_t):
        return k * d_t + k2 * d_t * d_t

    # The cooling curve is slow, so its slope is taken over a long window, kept inside the tail.
    tail_fit = np.array([], dtype=int)
    if tail_duration > 0:
        long_half = opts.loss_window_s / 2
        tw_l, dtw_l = _windowed(t, tw, long_half)
        inside = tail[(t[tail] >= t[tail[0]] + long_half) & (t[tail] <= t[tail[-1]] - long_half)]
        inside = inside[~np.isnan(dtw_l[inside])]
        d_t_tail = tw_l[inside] - ta_s[inside]
        if opts.quadratic_loss and len(inside) > 20 and float(d_t_tail.max() - d_t_tail.min()) >= 10.0:
            tail_fit = inside

    for _ in range(opts.iterations):
        if len(tail_fit):
            # Loss seen while cooling, corrected for the little heat still coming from the element.
            measured = h * gap[tail_fit] - mc * dtw_l[tail_fit]
            d_t = tw_l[tail_fit] - ta_s[tail_fit]
            (k, k2), *_ = np.linalg.lstsq(np.column_stack([d_t, d_t * d_t]), measured, rcond=None)
            if k2 < 0:  # no curvature in the data: fall back to a straight line
                k, k2 = float((d_t * measured).sum() / (d_t * d_t).sum()), 0.0
            elif k < 0:  # all curvature: keep it physical
                k, k2 = 0.0, float((d_t * d_t * measured).sum() / (d_t**4).sum())
            k, k2 = float(k), float(k2)
        elif tail_duration > 0:
            # Single coefficient: integral energy balance over the tail (robust to sensor quantisation).
            i0, i1 = tail[0], tail[-1]
            seg = slice(i0, i1 + 1)
            int_gap = float(np.trapezoid(tel[seg] - tw[seg] - offset, t[seg]))
            int_loss = float(np.trapezoid(tw[seg] - ta[seg], t[seg]))
            if int_loss > 0:
                k = max(0.0, (h * int_gap - mc * (tw_s[i1] - tw_s[i0])) / int_loss)
        # h: water equation, regression through the origin.
        y = mc * dtw[fit] + loss(tw_s[fit] - ta_s[fit])
        x = gap[fit]
        h = float((x * y).sum() / (x * x).sum())

    y_water = mc * dtw[fit] + loss(tw_s[fit] - ta_s[fit])
    r2_water = _r2(y_water, h * gap[fit])
    r2_loss = None
    if len(tail_fit):
        measured = h * gap[tail_fit] - mc * dtw_l[tail_fit]
        r2_loss = _r2(measured, loss(tw_l[tail_fit] - ta_s[tail_fit]))

    # P_full, C_el: element equation, duty*P - C*dT_el/dt = h*(T_el - T_w)
    if not (fit & (duty_s > 0)).any():
        raise ValueError("no heating samples")
    x_el = np.column_stack([duty_s[fit], -dtel[fit]])
    y_el = h * gap[fit]
    (p_full, c_el), *_ = np.linalg.lstsq(x_el, y_el, rcond=None)
    r2_el = _r2(y_el, x_el @ np.array([p_full, c_el]))

    # Limited regime = element inside the limiter's fade band, where duty < requested.
    # The limit is the water temperature + max_delta, capped by the optional absolute t_max.
    max_delta = cfg.get("max_delta")
    t_max = cfg.get("t_max")
    band = cfg.get("limit_band", 5.0)
    limit = np.full(len(t), np.inf)
    if max_delta is not None:
        limit = np.minimum(limit, tw + max_delta)
    if t_max is not None:
        limit = np.minimum(limit, t_max)
    limited = bool(np.any(tel[phase == "heat"] > (limit - band)[phase == "heat"]))
    if not limited and (max_delta is not None or t_max is not None):
        warnings.append("element never reached its limit: the limited regime was not observed")

    # Energy balance over the whole run.
    idx = np.flatnonzero(fit)
    seg = slice(idx[0], idx[-1] + 1)
    e_water = mc * float(tw_s[idx[-1]] - tw_s[idx[0]])
    e_elec = float(np.trapezoid(duty[seg] * p_full, t[seg]))
    e_loss = float(np.trapezoid(loss(tw[seg] - ta[seg]), t[seg]))
    e_store = c_el * float(gap[idx[-1]] - gap[idx[0]])
    e_resid = e_elec - (e_water + e_loss + e_store)

    bands = _bands(t, tw_s, dtw, gap, ta_s, phase, fit, mc, h, loss)

    result = {
        "options": asdict(opts),
        "loss_coefficient": k,
        "loss_quadratic": k2,
        # "curve" needs a cool-down spanning at least 10 degC; otherwise a single coefficient is fitted.
        "loss_model": "curve" if len(tail_fit) else "single coefficient",
        "loss_watts": {str(d): float(loss(float(d))) for d in (20, 30, 40, 50, 60)},  # by degC above ambient
        "sensor_offset": offset,
        "conductance": h,
        "power": float(p_full),
        "heat_capacity": float(c_el),
        "max_delta": max_delta,
        "t_max": t_max,
        "power_factor": float(p_full) / cfg["nominal_power"] if cfg.get("nominal_power") else None,
        "r2": {"water": r2_water, "element": r2_el, "loss": r2_loss},
        "energy_balance": {
            "electrical_j": e_elec,
            "water_j": e_water,
            "loss_j": e_loss,
            "stored_j": e_store,
            "residual_pct": 100.0 * e_resid / e_elec if e_elec else float("nan"),
        },
        "tail_minutes": tail_duration / 60,
        "limited_regime": limited,
        "bands": bands,
        "warnings": warnings,
    }
    if float(p_full) <= 0 or float(c_el) <= 0 or h <= 0:
        result["warnings"].append("non-physical fit result (<= 0); check the recording")
    return result


def _bands(t, tw_s, dtw, gap, ta_s, phase, fit, mc, h, loss) -> list[dict]:
    """Time to cross each 5 degC band while heating, plus measured vs modelled rate."""
    heat = np.flatnonzero(phase == "heat")
    if len(heat) == 0:
        return []
    tws = tw_s[heat]
    ok = ~np.isnan(tws)
    if not ok.any():
        return []
    start = np.floor(np.nanmin(tws) / BAND_WIDTH) * BAND_WIDTH
    end = np.nanmax(tws)
    out = []
    lo = start
    while lo < end:
        hi = lo + BAND_WIDTH
        in_band = heat[(tws >= lo) & (tws < hi)]
        in_fit = in_band[fit[in_band]]
        entry = {"from": float(lo), "to": float(hi), "seconds": None, "measured_k_min": None, "model_k_min": None}
        crossed_lo = heat[tws >= lo]
        crossed_hi = heat[tws >= hi]
        if len(crossed_lo) and len(crossed_hi) and np.nanmin(tws) <= lo:
            entry["seconds"] = float(t[crossed_hi[0]] - t[crossed_lo[0]])
        if len(in_fit):
            entry["measured_k_min"] = float(np.mean(dtw[in_fit]) * 60)
            model = (h * gap[in_fit] - loss(tw_s[in_fit] - ta_s[in_fit])) / mc
            entry["model_k_min"] = float(np.mean(model) * 60)
        out.append(entry)
        lo = hi
    return out


def yaml_snippet(result: dict, element_name: str) -> str:
    lines = [
        "    loss_coefficient: {:.2f}   # W/K (vessel)".format(result["loss_coefficient"]),
        *(["    loss_quadratic: {:.4f}    # W/K^2 (vessel)".format(result["loss_quadratic"])]
          if result.get("loss_quadratic") else []),
        "    elements:",
        f"      - output: {element_name}_pwm          # adjust to your id",
        f"        temperature_sensor: {element_name}_temp",
        *(["        max_delta: {:g}".format(result["max_delta"])] if result.get("max_delta") is not None else []),
        *(["        max_temperature: {:g}".format(result["t_max"])] if result.get("t_max") is not None else []),
        "        power: {:.0f}{}".format(
            result["power"],
            f"   # power_factor {result['power_factor']:.3f}" if result.get("power_factor") else "",
        ),
        "        conductance: {:.2f}".format(result["conductance"]),
        "        heat_capacity: {:.0f}".format(result["heat_capacity"]),
    ]
    return "\n".join(lines)
