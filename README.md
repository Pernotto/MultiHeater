# MultiHeater

- **Purpose:** One ESPHome `climate` that heats a body of water to a target temperature using several heating elements. It drives each element's PWM output directly (e.g. `slow_pwm` on a relay or SSR). It uses a physical model to:
  - supply exactly the power needed,
  - keep every element within a set margin above the water (`max_delta`), with an optional absolute ceiling,
  - stop early enough that the heat still stored in the elements doesn't overshoot the target.
- Includes the `multiheater` CLI, which measures each element's parameters through Home Assistant.

## How it works
You can configure one or more elements. The model gives each element its own temperature and lets them all heat the same water:

```
element : C_el · dT_el/dt = duty · P_full − h · (T_el − T_w)
water   : m·c  · dT_w/dt  = Σ h · (T_el − T_w) − k · (T_w − T_amb)
```

| parameter | YAML key | meaning |
|---|---|---|
| `P_full` | `power` | element power at 100 % duty (W) |
| `h` | `conductance` | element → water heat transfer (W/K) |
| `C_el` | `heat_capacity` | element heat capacity (J/K); stored heat = `C_el·(T_el − T_w)` |
| ΔT_max | `max_delta` | how far the element may be above the water (°C). Optional absolute ceiling: `max_temperature` |
| `k`, `k2` | `loss_coefficient`, `loss_quadratic` | vessel heat loss to ambient: `k·ΔT + k2·ΔT²`. The squared term matters for an open vessel, where evaporation makes the loss grow faster than the temperature difference |
| `m` | `water_mass` / `water_mass_sensor` | grams of water, fixed or from a sensor |

Every second the controller computes:

```
P_req = k·(target − T_amb) + (m·c·(target − T_w) − Σ C_el·(T_el − T_w)) / approach_time
```

- **Holding correction:** the heat an element keeps while it holds the target is not counted as surplus, otherwise the water settles below the target.
- **Feed-forward** covers the losses at the target. The proportional term closes the missing energy in about `approach_time`, after subtracting the heat already stored in the elements. That subtraction is what prevents overshoot.
- **Allocation:** power goes to the element with the most available power first. Each element's limit slides with the water: `limit = T_w + max_delta`, capped by `max_temperature` if set. The available power is `min(P_full, h·(limit − T_w))`. On top of that, a hard limiter fades the duty to 0 over the last `limit_band` °C below the limit.
- **Safety:** all outputs are set to 0 at boot, in mode OFF, when the water sensor reads NaN, when no valid water mass is known, and per element when its own sensor reads NaN or reaches its limit.
- **Hardware fuse:** still fit a hardware thermal fuse on every element. Software isn't a safety device.

## Installation
```yaml
external_components:
  - source: github://Pernotto/MultiHeater
    components: [ multi_heater ]
```

## Workflow
1. Flash [`calibration.yaml`](calibration.yaml). It has no `multi_heater`. Each element's PWM is exposed as a `number` (0–100 %). It also has two firmware safety cuts, which you should keep:
   - each element's duty goes to 0 when its sensor is lost or the element passes an absolute cut set far above normal operation (a backstop against a dry or faulty element)
   - `api: on_client_disconnected` sets everything to 0 once no API client is left
2. Run the CLI once per element (see below).
3. Paste the printed values into [`example.yaml`](example.yaml) and flash it. The raw PWM entities disappear and `multi_heater` takes over.

### Configuration
```yaml
climate:
  - platform: multi_heater
    name: "Boiler"
    sensor: water_temp            # water temperature (control sensor)
    water_mass: 45000             # grams: fixed value, or fallback until the sensor reports
    water_mass_sensor: scale_g    # optional sensor in grams; the last valid reading is kept
    loss_coefficient: 2.3         # W/K, from the CLI
    loss_quadratic: 0             # optional W/K², from the CLI (open vessels)
    ambient_temperature: 20       # fallback / fixed value
    ambient_sensor: room_temp     # optional
    default_target_temperature: 60
    approach_time: 300s           # larger = gentler approach
    max_power: 3000               # optional total budget (Σ duty·power), W
    integral_gain: 0.2            # optional W/(K·s), removes what the model gets wrong near the target
    integral_zone: 1              # °C below target from which the integral acts (frozen further away)
    limit_band: 5                 # °C below the element limit where duty fades to 0
    min_duty: 2%                  # smaller duties are dropped
    elements:
      - output: el1_pwm           # any FloatOutput, typically slow_pwm
        temperature_sensor: el1_temp
        max_delta: 50             # element may be 50 °C above the water
        max_temperature: 150      # optional absolute ceiling
        power: 1850               # from the CLI
        conductance: 38           # from the CLI
        heat_capacity: 1900       # from the CLI
        enable_switch:            # optional HA switch (restore_mode default RESTORE_DEFAULT_ON)
          name: "Element 1 enabled"
      - output: el2_pwm
        temperature_sensor: el2_temp
        max_delta: 50
        power: 920
        conductance: 20
        heat_capacity: 900
        enabled: false            # static alternative to enable_switch
```
- `elements:` takes one or more entries; the two shown are only an example. Each element needs its own output and temperature sensor, and its own calibration run with the CLI.
- A disabled element is switched off immediately and skipped by the allocation.
- `get_commanded_power()` and `get_element_duty(i)` are available for display lambdas; `i` is the element's position in `elements:`, counting from 0.
- The element temperature is whatever that element's sensor reads, so `h` and `C_el` are effective values for that sensor placement. Re-calibrate if you move the sensor.

## Calibration CLI
```bash
cd tools/multiheater_cli
uv venv && uv pip install -e .       # or: pip install -e .
multiheater                          # real Home Assistant
multiheater --simulate               # no hardware: simulated tank and elements
```
Menu:
- **Configure**
  - Home Assistant URL and long-lived access token (Profile → Security)
  - water temperature sensor
  - optional ambient sensor
  - poll interval
  - results directory

  Saved to `~/.multiheater.json` (mode 0600; override with `MULTIHEATER_CONFIG`).
- **Test configuration**: a pre-flight check to run before a test.
  - Home Assistant is reachable and the token works.
  - The water, ambient and element sensors exist and have plausible values.
  - Each duty number is 0–100 %, currently 0 and writable (it only ever writes 0).
  - Each element is within its `max_delta` of the water and below its ceiling.
  - At least one probe value changed in the last 2 minutes. Home Assistant only timestamps value changes, so a single steady sensor is not treated as stale.
- **Launch test**
  - Asks for:
    - water mass in grams
    - element name, its duty `number` and its temperature `sensor`
    - optional element limits: maximum temperature above the water (`max_delta`) and an absolute ceiling (blank = none)
    - nominal power (optional, for the power factor)
    - optional duty steps (`50:60,65:30` means 60 % from 50 °C water, 30 % from 65 °C)
    - stop temperature
    - cool-down duration
  - By default there is no element limit: the test runs at 100 % (or the stepped duty), which is fine for plain water.
  - If you enter a limit (maximum excess over the water and/or an absolute ceiling), it heats from a rough starting duty you enter (default 70 %). Every 10 s it compares the element's excess over its limit, averaged over 30 s, and moves the duty by 2 points: down when over, up when more than 5 °C under and no longer climbing. The duty never jumps, because the element answers a power change several seconds late and a duty tied to the instantaneous reading swings between 0 and 100 %. If the element is ever 15 °C over its limit, or the firmware cuts the heater, the duty goes to 0 at once and resumes 10 points lower.
  - It shows the time taken per 5 °C band.
  - Then turns the element off and logs the coast and passive cooling.
  - Ctrl-C once skips to the cool-down; Ctrl-C again stops. The element is always set to 0 at the end.
  - Whatever was recorded is always saved, also after an interruption or when Home Assistant becomes unreachable (it retries for 30 polls first).
  - The run is saved to `results/<element>_<date>.json` and analysed automatically.
- **Recalculate**: re-runs the analysis on a saved file with different options: mass, ambient, smoothing window, trims, and the cooling-tail margin. It is for debugging.

The analysis first measures the constant offset between the element and water sensors at rest (from the end of the cool-down) and removes it. It then fits the vessel loss from the cooling tail (a curve `k·ΔT + k2·ΔT²` when the cool-down spans at least 10 °C, otherwise a single coefficient), `h` from the water equation, and `P_full`/`C_el` from the element equation. It prints R², an energy-balance check, measured vs modelled heating rate per band, and a ready-to-paste YAML snippet.

Tips:
- Log at least 30–60 min of cool-down, or `k` is unreliable.
- Duty steps give the fit more varied data.
- Stirring (or natural convection) should be the same as in normal use.

## Development
- `cd tools/multiheater_cli && uv pip install -e '.[dev]' && pytest`: the fit recovers the simulated parameters within 5 %.
- `esphome run example_host.yaml`: a desktop simulation of the controller with a scaled-down plant. It heats to 60 °C, then disables element 1 and raises the target to 70 °C. `SIM` log lines show the water temperature and its peak, the element temperatures and the duties.

## License
MIT, see [LICENSE](LICENSE).
