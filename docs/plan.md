# ClogGuard specification

Condensed from the Group 51 ENGG*41x0 proposal (November 6, 2026) and the project plan (November 16, 2026). This file is the specification the software is built against.

## Scope

One direct-drive FDM printer running Klipper and Moonraker, PLA, 0.4 mm nozzle, gradually developing partial clogs. PETG is a stretch goal. Out of scope: hard blockages, other failure modes, multi-material, other printers, commercialisation.

The proposal preselects no sensing approach. Concept selection is due at the Conceptual Design milestone, February 4, 2027.

## Criteria (proposal Table 2)

| Criterion | Target |
| --- | --- |
| Warning lead time | Alert at least 5 min before the first visible defect |
| Recovery rate | At least 70% of recoverable clog runs finish with no visible defect |
| False alarms | At most 1 unnecessary intervention per 10 normal prints |
| Filament waste | Down at least 50% against clog runs with no intervention |
| Part quality | Recovered parts within 5% of the mass of a defect-free reference |
| Response time | Condition assessed at least once per second; command sent within 2 s of a decision |
| Hardware cost | Under $400 per printer; project hardware allowance $350 plus 10% |
| Installation | Installed and calibrated by a technician in under 3 hours |

## Constraints (proposal Table 1)

- Firmware thermal-runaway protection stays enabled and independent of the added system.
- The hotend never exceeds the filament manufacturer's maximum rated temperature.
- Added electronics run on low-voltage DC only; no mains modification.
- No mainboard replacement; no change to the user's slicer workflow.
- Tested and demonstrated within the 12-week term.

## Milestones (proposal Table 5)

| Milestone | Date |
| --- | --- |
| Conceptual Design | February 4, 2027 |
| Design | February 19, 2027 |
| Functional prototype | March 12, 2027 |
| Testing and validation | March 29, 2027 |
| Final delivery | April 11, 2027 |

## Definitions

- **Visible defect onset:** the first timestamped layer photo in which two raters, each blind to the sensor data, mark under-extrusion. The earlier time is used.
- **Recoverable clog:** an induced restriction of a stated method and severity that the allowed actions can reverse. Qualifying methods and levels are listed in the test plan before validation.
- **Unnecessary intervention:** any slow-down, temperature change, purge or pause during a print with no induced fault.

## Candidate signals

All are logged together in a January bake-off; the measurements inform the concept selection.

| Signal | Added hardware | Family |
| --- | --- | --- |
| Hotend heater power | None; reported by Klipper | New, zero cost |
| Extruder driver load | None if a TMC driver is used; read rate unverified | Motor current |
| Extrusion force | Load cell and amplifier between extruder and hotend | Extrusion force |
| Vibration | ADXL345 on the hotend | Vibration |
| Filament motion | Encoder wheel on the filament | Filament motion |
| Camera | Existing | Labels and verification only |

## Clog induction methods

| Method | Note |
| --- | --- |
| Lower nozzle temperature in steps | Reversible. Temperature must be hidden from the model for these runs. |
| Flow beyond hotend capacity | Flow rate must be hidden from the model for these runs. |
| Smaller nozzles (0.3, 0.25, 0.2 mm) | Fixed severity; calibration of signal against opening size. |
| Reduced heatsink fan (heat creep) | Realistic; supervised only. |
| Contaminated or degraded filament | Least repeatable; reserved for final validation. |

Every clog run has a team member present.

## Run record

One Parquet file of time series per print plus a metadata file with: run ID, date, operator, part, spool, nozzle, induction method and schedule, manipulated variables, alert time, defect onset time, actions taken, outcome, part mass, wasted filament mass, photo paths, software version, config hash.

## Model layers

1. **Expected-signal model.** Regression of each signal on volumetric flow, nozzle temperature and acceleration, fitted on fault-free prints once per material and nozzle. The residual is the health index.
2. **Statistical alarm.** EWMA or CUSUM on the residual. The baseline every later layer must beat.
3. **Anomaly model.** Isolation forest or small autoencoder on windowed features, trained on normal prints only.
4. **Supervised classifier.** Gradient-boosted trees on window features; outputs clog probability and severity.
5. **Sequence model.** Small 1D CNN on raw windows, pretrained on open and simulated data. Only if layer 4 plateaus.
6. **Time-to-defect estimate.** Trend of the health index extrapolated to the level at which defects appeared in no-intervention runs; minutes remaining with a range.
7. **Recoverability decision.** A rule on severity and rate of rise; a classifier only if the data supports it.

Rules: split by print, never by window; leave-one-print-out during development; validation prints untouched until the end; past data only in features; under 100 ms per inference on the Klipper host.

## Recovery ladder

1. Warn: notify with estimated minutes to defect.
2. Slow down: reduce the speed factor in steps.
3. Raise nozzle temperature in small steps, never above the filament maximum.
4. Park and purge (optional).
5. Pause and alert.

After each step, wait a set window. If the health index is back in the normal band, log a verified recovery and step down. Step sizes, windows and the cap on attempts are tuning parameters.

## Hard limits

- Moonraker API only; allowlist of: set speed factor, set nozzle temperature, run purge macro, pause, notify.
- Temperature clamp in the service, and Klipper's maximum temperature set to the same value as a second check.
- No access to printer configuration.
- No action on missing or stale data; a crash leaves a stock printer.
- One action per verification window; cap on interventions per print.
- Audit log of every assessment, decision and command.

## Test plan

Test part: about 25 g, 30 to 40 minutes, with varied flow. Three geometries in the fault-free set.

| Purpose | Runs |
| --- | --- |
| Fault-free baseline | 12 |
| Induction method trials | 8 |
| Clog runs, no intervention | 15 |
| Recovery tuning | 10 |
| Final validation, fault-free | 30 |
| Final validation, clogs with the system on | 30 |

Pass rules:

| Criterion | Meets target | Supported at one-sided 95% |
| --- | --- | --- |
| Lead time | Median of 5 min or more, measured in watch-only mode | 5 min or more in at least 90% of runs (proposed) |
| Recovery | 21 of 30 | 26 of 30, or 18 of 20 |
| False alarms | 3 or fewer of 30 | 0 of 30, or at most 1 of 50 |
| Waste | Mean reduced by half | Rank test against the no-intervention runs |

Lead time is measured only on runs where the system does not intervene.

## Open datasets

Used for building, pretraining and benchmarking. Never reported as real-printer performance.

| Dataset | Contents | Licence | URL |
| --- | --- | --- | --- |
| TU Darmstadt hotend force data | Synchronised hotend force under misconfigured extrusion multiplier, speed, temperature, line width, layer height; acquisition code | CC BY 4.0 | https://tudatalib.ulb.tu-darmstadt.de/handle/tudatalib/4596 |
| UFRGS vibration dataset | ADXL345 at 3200 Hz, Ender 3 V2, 27 prints: healthy, two wrong temperatures, loose head, nozzle restricted to 0.3 and 0.2 mm | CC BY 4.0 | https://zenodo.org/records/22716165 |
| CAXTON images | 1,272,273 nozzle-camera images labelled with flow rate, speed, Z offset, temperature | Check record | https://www.repository.cam.ac.uk/items/6d77cd6d-8569-4bf4-9d5f-311ad2a49ac8 |
| AGH anomaly data | Head accelerometer, power, temperatures, provoked malfunctions | CC BY 4.0 | https://github.com/joanna-/3D-Printing-Data |

## Open questions

- Printer model, mainboard and extruder driver.
- Camera position and whether it sees each layer.
- Whether the 5-minute warning must hold in every run or a stated fraction.
- Whether a restriction induced by temperature or nozzle size counts as a recoverable clog.
- Hardware budget: $385 or $1,200 (the proposal states both).
- Course rules on AI tools and how use is disclosed.
