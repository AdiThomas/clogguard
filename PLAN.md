# PLAN.md: ClogGuard software build

Status: approved by Ad on 2026-11-16 ("start the plan ... make any assumptions and we can fix
it later"). Phase 0 complete; build running.

Specification: `docs/plan.md`. Build prompt: `MASTER_PROMPT.md`.

## 1. Platform facts confirmed (Phase 0)

| Need | Confirmed | Source |
| --- | --- | --- |
| Heater duty for the zero-cost signal | `extruder.power`, with `temperature`, `target`, `can_extrude` | [Klipper status reference](https://www.klipper3d.org/Status_Reference.html) |
| Actual filament velocity | `motion_report.live_extruder_velocity` (also `live_velocity`, `live_position`) | same |
| Speed and flow multipliers | `gcode_move.speed_factor`, `extrude_factor`, `speed` | same |
| Extruder driver load | TMC objects expose `drv_status` (null when disabled), `run_current`, `temperature`; queried "periodically", **no rate stated** | same |
| Load cell | `load_cell.force_g` (1 s average), `sample_rate`, `is_calibrated`; HX711 80 SPS, HX717 320 SPS | same; [Load cells](https://www.klipper3d.org/Load_Cell.html) |
| Print and pause state | `print_stats.state`, `info.current_layer`; `pause_resume.is_paused`; `webhooks.state` | status reference |
| Live data | Moonraker `ws://host:7125/websocket`, `printer.objects.subscribe` with `{"objects": {name: null or [fields]}}`; updates arrive as `notify_status_update` | [Moonraker web API](https://moonraker.readthedocs.io/en/latest/web_api/) |
| Commands | `printer.gcode.script {"script": ...}` returns "ok" on completion; `printer.print.pause` | same |
| Auth | `server.connection.identify` with `api_key` or `access_token` | same |

Not confirmed (marked ASSUMPTION in code):
- `notify_status_update` params are `[status_diff, eventtime]` (from memory of Moonraker; the
  docs page was truncated before that section).
- `drv_status` update rate. If it is about 1 Hz or slower it cannot carry a 1 s assessment alone.
- `load_cell.force_g` is a 1 s average, too smoothed for fast features; a raw stream needs the
  external-MCU path (`source: serial`).

## 2. Assumptions (fix later)

| # | Assumption | Where it matters |
| --- | --- | --- |
| A1 | Printer is a Klipper direct-drive with Moonraker on the same Pi | logger, service |
| A2 | Base table rate 10 Hz; fast sensors summarised per tick, raw kept separately | schema |
| A3 | PLA max 220 C, nominal 210 C until a datasheet is supplied | `config/service.example.yaml` |
| A4 | Temperature correction changes target via `SET_HEATER_TEMPERATURE HEATER=extruder` | safety translation |
| A5 | Speed correction via `M220 S<pct>`; never above 100% | safety translation |
| A6 | Purge via a single user-installed macro `CLOGGUARD_PURGE`; disabled by default | ladder |
| A7 | Experiment tracking is a local JSON-lines tracker; MLflow export optional | train |
| A8 | Torch CNN is optional (`[deep]` extra); tests do not need it | models |
| A9 | Open datasets cannot be downloaded from the build sandbox; loaders are tested on fixtures | benchmarks |
| A10 | Dashboard is server-rendered FastAPI + vanilla JS, read-only, LAN only | dashboard |

## 3. Interfaces (fixed before building; in `clogguard/core/`)

| Interface | File | Summary |
| --- | --- | --- |
| Channel config | `channels.py` | `ChannelSpec(name, unit, rate_hz, source, role, params)`; roles `signal`, `context`, `label` |
| Run record | `schema.py` | run dir = `meta.json` + `timeseries.parquet` (10 Hz, column `t`) + `raw/` + `photos/` + `audit.jsonl`; `RunMetadata` with `manipulated_variables`, `data_source`, defect ratings |
| Commands | `commands.py` | exactly five: `SetSpeedFactor(10..100)`, `SetNozzleTemp`, `RunPurge`, `Pause`, `Notify`; no G-code field anywhere |
| Events | `events.py` | `Assessment`, `Decision`, `CommandResult`, `Recovery`, `Lifecycle` in `AuditRecord` JSON lines |
| Service config | `config.py` | `ServiceConfig`, `MaterialProfile`, `LadderConfig`, `DetectorConfig`; `temp_ceiling_c = min(max, nominal + raise)` |
| Model API | `model_api.py` | `HealthModel.fit/score_run/make_online/save`; `OnlineScorer.update(rows, now_s) -> Assessment` |

## 4. Workstreams

| ID | Workstream | Owns | Depends on | Wave |
| --- | --- | --- | --- | --- |
| A | Data platform: channel plugins, Moonraker client + logger, run store, validation, blind labelling tool | `clogguard/channels/`, `clogguard/moonraker/`, `clogguard/logger/`, `clogguard/store/`, `clogguard/labeling/` | core | 1 |
| C | Simulator + mock Moonraker server | `clogguard/sim/` | core | 1 |
| H | Test-campaign tooling + Table 2 statistics | `clogguard/campaign/`, `clogguard/eval/` | core | 1 |
| B | Open-data loaders + benchmark | `clogguard/datasets/`, `benchmarks/` | core | 1 |
| I | Design-process templates | `design/` | none | 1 |
| E | Real-time service + safety gate | `clogguard/service/`, `clogguard/safety/` | core (+ A's Moonraker client at merge) | 1 |
| D | Features + models layers 1-7, training, tracking, comparison | `clogguard/features/`, `clogguard/models/`, `clogguard/train/` | A store, C sim | 2 |
| G | Dashboard + alerts | `clogguard/dashboard/` | E events | 2 |
| X | End-to-end tests, README, integration | `tests/e2e/`, `README.md` | all | 3 |
| F | Printer integration (Klipper macros, sensor firmware, install) | `printer/` | concept selection Feb 4 | out of scope |

Dependency graph: core -> {A, C, H, B, I, E} -> D -> X; E -> G -> X. Independent reviewer last.

## 5. Acceptance (from MASTER_PROMPT section 7)

- pytest green, ruff clean, coverage >= 80% on `clogguard/`, 100% branches in `clogguard/safety/`.
- Safety tests: off-allowlist command rejected; over-ceiling temperature rejected (not lowered);
  stale data -> no action; watch mode never sends printer commands.
- e2e: mock printer replays a simulated slow clog -> warn, ladder, verified recovery; audit log
  shows assessments >= 1 Hz and commands acked within 2 s of decision.
- e2e: simulated normal prints with varied flow -> false-intervention count reported.
- `clogguard compare` regenerates the signal x model table.
- Statistics module reproduces 26/30 -> lower bound >= 0.70 and 0/30 -> upper bound < 0.10.

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| Simulator too optimistic, models look better than they will on hardware | Every sim result labelled "simulated"; never mixed with real tables |
| Moonraker message shapes differ from assumption | Client isolated in `clogguard/moonraker/`; mock built from the same assumption so one fix updates both |
| Service blocks the event loop and misses 1 Hz | Inference budget 100 ms; tick timing logged and tested |
| Safety bypass via a second code path | Only `clogguard/safety` imports the RPC client's `call`; reviewer checks |

## 7. Manual steps for the team

1. Confirm printer model, mainboard and extruder driver; edit `config/channels.yaml`.
2. Fill PLA `max_temp_c` from the actual spool datasheet.
3. Install Moonraker API key (if auth enabled) as `MOONRAKER_API_KEY` on the Pi.
4. Fit sensors after concept selection; add their channels.
5. Mount the camera; check every layer is visible.
6. Run the campaign: print, weigh, rate defects blind, attend every clog run.
7. Make every judgment in `design/` templates.
