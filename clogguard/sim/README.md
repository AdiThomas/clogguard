# clogguard.sim: clog simulator and mock printer

Everything this package produces is **simulated** (`data_source="simulated"`). Use it to build and
test before the hardware exists. Never report its output as real printer performance. The
physics is chosen to be plausible, documented and tunable. It is not precise.

## Usage

```bash
clogguard sim generate --n-normal 20 --n-clog 20 --seed 1 --out runs/sim   # mixed dataset
clogguard sim preset slow_viscous --seed 3 --out runs/sim                  # one preset
clogguard sim list
clogguard mock-printer --scenario slow_viscous --speedup 60 --port 7125    # fake Moonraker
```

```python
from clogguard.sim import generate_run, preset
from clogguard.core.commands import SetSpeedFactor

run = generate_run(preset("slow_viscous", seed=2))            # core Run, 10 Hz
ctl = lambda t, frame: [SetSpeedFactor(percent=70)] if 900 <= t < 901 else []
run2 = generate_run(preset("slow_viscous"), controller=ctl)   # closed loop, no network

from clogguard.sim.mock_moonraker import MockMoonraker
async with MockMoonraker(preset("slow_viscous"), speedup=60) as mock:
    ...  # connect to mock.url; mock.command_log; mock.sim; mock.run_frame() (ground truth)
```

## Files

| File | Contents |
| --- | --- |
| `physics.py` | `PhysicsParams`, pure pressure/flow functions, the stateful `NozzleModel` |
| `printjob.py` | Seeded print-profile generator (`box`, `cylinder`, `bracket`) |
| `simulator.py` | `Simulator`: print job + nozzle model + controls, recorded at 10 Hz |
| `scenarios.py` | `Scenario`, presets, `generate_run`, `generate_dataset` |
| `calibrate.py` | `fit_pressure_model(df)` for TU Darmstadt or baseline-print data |
| `mock_moonraker.py` | Websocket JSON-RPC Moonraker mock in accelerated time |
| `io.py` | Small writer/reader for the core run layout (meta.json + timeseries.parquet) |
| `cli.py` | `clogguard sim ...` |
| `channels_sim.yaml` | `ChannelConfig` for the simulated sensor set (labels have role `label`) |
| `channels_mock_moonraker.yaml` | `ChannelConfig` for logging the mock through Moonraker |

## Model

One step is 0.1 s (`dt_s`). The thermal loop uses `substeps` (2) internal steps.

1. **Print job.** The print job is laid out in *job time* τ. Each step τ advances by
   `dt × speed_factor`, so M220 stretches the print. Commanded filament velocity is the tick mean
   of the job's velocity × speed factor × extrude factor. Retract and unretract moves count in
   `e_velocity_mm_s`. They are left out of the flow that pushes melt through the nozzle.
2. **Restriction.** The clog profile gives the intrinsic progression s(t) (0..severity):
   `none`, `linear`, `exponential` (accelerating, `(e^{ku}-1)/(e^k-1)`) or `step` (5 s rise).
   The target restriction depends on the clog type:
   * `viscous`: `r_bg + s·(Q_ema/Q_ref)^α·exp(−k_v (T − T_nom))·(1 − relief)`. Slowing or
     heating removes it fully.
   * `heat_creep`: `r_bg + s·(Q_ema/Q_ref)^α·exp(+k_h (T − T_nom))·(1 − relief)`. Slowing removes
     it. Heating makes it worse.
   * `debris`: `r_bg + s·(1 − debris_relief)`. Speed and temperature have no effect. Each purge
     removes a small, permanent fraction.

   Restriction r follows the target through a first-order lag (rise 20 s, fall 60 s). The
   effective diameter is `d = d0 (1 − r)`. `true_restriction` = r.
3. **Pressure.** `F_press = K (Q/Q0)^m · mu(T_melt)/mu(T_ref) · L · (d0/d)^n`. The diameter
   exponent n defaults to 3.0. The flow exponent defaults to `m = (n−1)/3`, which is the
   power-law-fluid relation (a Newtonian fluid has m = 1, n = 4). Set `flow_exponent=1.0` to
   get the Q-linear form. Viscosity follows Arrhenius: `mu ∝ exp(Ea/R (1/T − 1/T_ref))`. When
   flow approaches capacity, the melt core is colder than the block:
   `T_melt = T − ΔT (Q/Q_max)^2`.
4. **Capacity and extruder.** Melt capacity soft-limits the flow:
   `Q_cap = Q / (1 + (Q/Q_max(T))^8)^(1/8)`. The force demand is
   `F_demand = F_fric + F_press(Q_cap)`. The drive gear's grip soft-limits the delivered force:
   `F = F_demand / (1 + (F_demand/F_grip)^8)^(1/8)`. Delivered flow solves `F_press(Q_del) = F − F_fric`.
   `true_deficit = 1 − Q_del/Q_cmd` (0 when not extruding).
5. **Thermal.** `C dT/dt = duty·P_heater − (h + h_fan·fan)(T − T_amb) − Q_del·ρ·c_p·(T_melt − T_amb)`.
   A Klipper-style PID with anti-windup sets duty from a smoothed, noisy temperature reading.
   Lower delivered flow therefore means lower duty. `extruder_power` is that duty plus noise.
6. **Defect label.** `true_defect` latches to 1 once the EMA of the deficit (τ 15 s, extruding
   ticks only) has stayed above 0.15 for 20 s. A 15% under-extrusion held for tens of seconds is
   taken as the point where gaps become visible on a layer photo. This is an assumption to
   replace with the campaign's blind ratings. `defect_ratings` carries the label's first time
   under rater `sim_truth`.

### Signals (10 Hz)

| Channel | Model |
| --- | --- |
| `force_g` | lagged (τ 0.15 s) actual force + load-cell zero drift (OU, sd 60 g, τ 600 s) + noise (25 g) |
| `extruder_power` | PID duty averaged over the step + noise (0.01) |
| `tmc_sg_result` | `sg_free(|e_v|) − 25 counts/kg × force`, + noise 8, rounded, 0..510, held 1 s |
| `accel_rms` | `(0.6 + 0.008 v + 3.5e-4 |a|)·(1+N(0,0.08)) + 3 (r−r̄_bg)²(0.5+Q/Q_ref) + 2.5 (1 − grip_eff)` |
| `encoder_slip_mm` | per tick `(Q_cmd − Q_del)/A_f·dt` + noise, quantised to 0.005 mm |

The core channel declaration defines `encoder_slip_mm` per tick ("commanded minus measured
filament advance per tick"). The simulator follows that rather than a running total. Sum it to
get the accumulated slip.

### Normal variation (so a fixed threshold false-alarms)

Each run varies in several ways:

* filament batch viscosity (lognormal sd 0.12)
* slow viscosity drift (OU sd 0.04, τ 10 min)
* background partial restriction (OU mean 0.01, sd 0.015, τ 5 min)
* ambient temperature (run sd 2 °C plus drift)
* heat-loss coefficient (sd 6%)
* load-cell zero drift
* nominal temperature (200 to 215 °C in the dataset)
* operator speed changes (a third of normal runs, 75 to 115%) and ±5 °C temperature tweaks (a
  quarter)
* geometry: the bracket's small wall layers trigger minimum-layer-time slowdowns, so speed
  changes inside the print.

`tests/sim/test_scenarios.py` checks one case. A force threshold low enough to warn 5 minutes
ahead on the slow presets fires on some fault-free prints.

## Parameters and their basis

All live in `PhysicsParams` and can be overridden per scenario (`Scenario(params={...})`).

| Parameter | Default | Basis |
| --- | --- | --- |
| `nozzle_d0_mm` | 0.4 | Standard nozzle (spec) |
| `filament_area_mm2` | 2.405 | 1.75 mm filament |
| `pressure_n` | 3.0 | Brief default. A power-law fluid with PLA flow index ~0.5–0.7 gives n = 3m+1 ≈ 2.5–3.1 |
| `flow_exponent` | None → (n−1)/3 = 0.67 | Power-law consistency with n |
| `pressure_scale_g` | 1600 g at Q0 = 10 mm³/s, 210 °C | Order of magnitude of direct-drive extrusion force (≈15 N, ≈6 MPa nozzle pressure). To be calibrated with `calibrate.py` |
| `activation_energy_j_mol` | 90 kJ/mol | PLA melt flow activation energy (literature range ≈ 80–180 kJ/mol). Gives −37% viscosity per +10 °C at 210 °C |
| `pressure_tau_s` | 0.15 s | Melt compressibility / filament compliance (order of Klipper pressure-advance smoothing) |
| `qmax_ref_mm3_s`, `qmax_slope_per_c` | 22 mm³/s at 210 °C, 2%/°C | High-flow-ish V6-class hotend. Test-part peaks are ~15 mm³/s |
| `melt_dt_at_qmax_c` | 15 °C | Melt core lags block temperature at capacity (assumed) |
| `friction_g`, `friction_per_mm_s_g` | 150 g, 8 g/(mm/s) | Filament path drag (assumed) |
| `grip_force_g` | 6000 g | Typical dual-gear direct-drive push force before grinding (~50–80 N) |
| `grip_sharpness`, `qmax_sharpness` | 8 | Soft-min sharpness. Deficit starts a little before the hard limit |
| `restriction_tau_rise_s/fall_s` | 20 / 60 s | Deposits build faster than they clear (assumed) |
| `flow_ema_tau_s`, `clog_flow_alpha` | 60 s, 1.5 | Over-flow drives viscous/heat-creep growth (assumed). Slowing to 55% cuts growth ≈ 60% |
| `viscous_temp_k_per_c` | 0.06 /°C | +10 °C cuts a cold/viscous restriction ≈ 45% (assumed) |
| `heat_creep_temp_k_per_c` | 0.08 /°C | +10 °C grows heat creep ≈ 2.2× (assumed) |
| `purge_relief` | viscous 0.4, heat creep 0.15, debris 0.1 | Purge helps viscous most and debris "a little". Viscous/heat-creep relief fades with τ 180 s because the cause persists |
| `heat_capacity_j_k`, `heater_w`, `loss_w_k`, `fan_loss_w_k` | 12 J/K, 40 W, 0.085 W/K, 0.025 W/K | V6-class block with sock. Idle duty ≈ 0.4, printing ≈ 0.55–0.6 |
| `cp_j_gk`, `density_g_mm3` | 2.0 J/(g K), 1.24e-3 g/mm³ | PLA. Melting 10 mm³/s takes ≈ 4.5 W |
| `pid_kp/ki/kd` | 22, 1.08, 114 (/255) | Typical Klipper `PID_CALIBRATE` results for a 40 W hotend |
| `temp_noise_c`, `temp_smooth_s` | 0.12 °C, 1 s | Thermistor ADC noise. Klipper `smooth_time` |
| `force_noise_g`, `force_zero_drift_*` | 25 g; 60 g, 600 s | HX711-class load cell with thermal zero drift (assumed) |
| `sg_*` | free 120→260, 25 counts/kg, sd 8, 1 s | StallGuard4 is velocity-dependent and noisy. `drv_status` rate is unverified, so 1 Hz is held |
| `accel_*` | see the signals table | Motion-driven vibration baseline. Restriction and slip add energy (UFRGS dataset shows restricted nozzles change vibration; magnitudes assumed) |
| `encoder_resolution_mm` | 0.005 mm | Optical/magnetic filament encoder class |
| `defect_*` | 0.15 deficit, τ 15 s, 20 s sustain | See "Defect label" |
| `bg_restriction_*`, `viscosity_*`, `ambient_*`, `loss_w_k_run_sd` | see the list above | Normal run-to-run and within-run variation (assumed) |

## Scenarios

Presets: `normal_box`, `normal_cylinder`, `normal_bracket`, `slow_viscous` (linear, 20 min ramp),
`slow_heat_creep` (exponential, ~17 min), `slow_debris` (linear, 20 min), `fast_viscous` (5 min
ramp). All clogs start at 8 min (`onset_s=480`), with severity 0.6.

The defaults are tuned so that slow presets with no intervention reach the defect label
**8–20 min after onset**. Force rises > 15% at least 5 min before the defect, but under 60% in
the first 2 minutes.

`induction_label`:

* `natural`: no manipulated variable.
* `temp_step`: target lowered 5 °C every 4 min down to −15 °C. `manipulated_variables` =
  `extruder_target_c`, `extruder_temp_c`.
* `over_flow`: speed factor raised to 1.4. The flow channels are manipulated.

`induction.method` is always `"simulated"` (or `"none"`). `recoverable` is True for viscous and
heat creep and False for debris. The label and parameters are stored in `notes` and
`induction.schedule`.

`generate_dataset(n_normal, n_clog, seed)` mix:

* **Normal runs:** geometries in rotation; nominal temperature 200/205/210/215 °C; noise level
  0.8–1.3; a third get an operator speed change and a quarter a ±5 °C tweak.
* **Clog runs:**
  * Type cycle viscous 40%, heat creep 30%, debris 30%.
  * Profile cycle linear 50%, exponential 40%, step 10%.
  * Step clogs are always debris (a lodged particle, severity 0.12–0.3), so they may never
    cause a defect.
  * Onset is 5–15 min. Severity is 0.55–0.65 with ramps of 13–27 min.
  * A quarter of viscous runs use `temp_step` (severity × 0.35, because the cold itself does
    most of the work).

## Mock Moonraker

The mock follows the shared wire assumption (`/tmp/wire.md` in the build). It serves JSON-RPC
2.0 at `ws://host:port/websocket` and supports these methods:

* `server.connection.identify`, `server.info`, `printer.info`
* `printer.objects.list`, `printer.objects.query`, `printer.objects.subscribe`. Unknown objects
  are omitted.
* `printer.gcode.script`, which accepts exactly `M220 S<n>`,
  `SET_HEATER_TEMPERATURE HEATER=extruder TARGET=<x>` (above `max_temp_c`=250 is an error) and
  the purge macro. The purge macro answers `"ok"` when the purge has finished. Anything else
  returns error 400.
* `printer.print.pause`, `printer.print.resume`, `printer.print.cancel`. With no active print
  they return error 400.
* Unknown methods return -32601.

On connect it sends `notify_klippy_ready`. It sends `notify_status_update` `[diff, eventtime]`
every simulated 0.1 s, with only changed fields. A nested dict is resent whole when it changes.

`eventtime` = 1000 + simulated seconds since the server started. `print_stats.state` goes
standby → printing (after `start_delay_s` sim seconds) → complete (or paused/cancelled).

Pause freezes the job and extrusion. Complete and cancel set the target to 0 and the fan to 0.

Served objects:

* `load_cell.force_g` is a 1 s mean, like Klipper.
* Virtual `clogguard_sim` serves `{force_g, accel_rms, encoder_slip_mm}` raw at each tick.
* Toolhead x/y are not simulated (fixed at 100, 100). z and e are.

ASSUMPTION: before the print starts, the hotend is already at temperature (as after a
`PRINT_START` heat-up).

## Known limits

* There is no first-layer squish, no pressure advance and no retraction ooze model.
* Heat creep is modelled as a restriction, not as a jam at the heat break.
* Melting heat ignores PLA crystallinity details.
* Accelerometer magnitudes, the defect threshold and every "assumed" row above are placeholders
  until the campaign's baseline prints and the open datasets calibrate them.
