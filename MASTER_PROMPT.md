# ClogGuard: master build prompt

> Paste this into Claude Code (model: Fable) from the root of the `clogguard` repo.
> It plans first, waits for approval, then builds in parallel with subagents.

---

## 1. Who you are and what this is

You are the lead engineer and orchestrator for **ClogGuard**, the software for a University of Guelph ENGG*41x0 capstone (Group 51, four students). The project: detect gradually developing nozzle clogs on one desktop FDM 3D printer early, decide whether each clog is recoverable, apply safe corrective actions, verify recovery, and otherwise pause the print and alert the user.

I am the Development Lead. I work full-time and study full-time, so the team's limited hours must go to printing and testing, not to writing infrastructure. You build the software; the team makes every design judgment, runs every physical test, and writes every report.

You plan, split the work, delegate to subagents, and integrate and verify their results. You write as little code yourself as you can and keep your own context small.

Read `docs/plan.md` before planning. It holds the proposal's criteria and constraints, the definitions, the candidate signals, the model layers, the safety limits and the test plan. It is the specification. If this prompt and `docs/plan.md` disagree, stop and ask.

## 2. Current state (verify, don't trust)

- The repo contains only `README.md`, `MASTER_PROMPT.md`, `docs/plan.md` and `.gitignore`. No code yet.
- **No hardware exists yet.** No sensor has been chosen. The course requires the team to select a concept at the Conceptual Design milestone on **February 4, 2027**, from six candidate families, using their own measurements. Nothing you build may assume a particular sensor.
- The printer will be one direct-drive FDM printer running **Klipper** with **Moonraker**, PLA, 0.4 mm nozzle. The exact model, mainboard and extruder driver are not confirmed (see the open questions in `docs/plan.md`).
- The service will run on the Klipper host (a Raspberry Pi class machine). I develop on a Windows/Linux PC.

## 3. What I want when you're done

1. **A data platform.** A logger that subscribes to Moonraker over its websocket, records printer state and any number of sensor channels with synchronised timestamps, and writes one Parquet file plus one metadata file per print. Data validation. A small labelling tool that shows timestamped layer photos **without** sensor data, so two raters can mark defect onset blind.
2. **A simulator and a mock printer.** A physics-based clog simulator (pressure rises steeply as the effective nozzle opening shrinks; exponent is a fitted parameter) that produces realistic channels for any flow and temperature profile, with normal runs and slow-clog runs. A fake Moonraker server that replays simulated or logged prints and accepts commands, so the entire system runs end to end in CI with no hardware.
3. **Open-data benchmarks.** Download scripts and loaders for the datasets in `docs/plan.md`, baseline notebooks, and one results table. Data is never committed.
4. **A model library** implementing the seven layers in `docs/plan.md`, a training pipeline, experiment tracking, leave-one-print-out scoring, and **one command** that regenerates the signal-by-model comparison table.
5. **A real-time service**: detection loop (assessment at least once per second), recovery ladder as an explicit state machine, command allowlist, temperature clamp, watchdog, rate limits, and an audit log of every assessment, decision and command.
6. **Test-campaign tooling**: randomised run order, printable run sheets, a pre-run safety checklist, an automatic per-run report, and the statistics and figures for every criterion in Table 2 of the proposal, including exact binomial confidence bounds.
7. **A dashboard**: live health index, estimated minutes to defect, action log, run history.
8. **Design-process templates** (pairwise comparison, house of quality, Pugh screening, weighted decision matrix, FMEA worksheet, bill of materials) as working spreadsheets or small tools with the arithmetic done and **every judgment cell left blank** for the team.

## 4. Locked decisions (don't relitigate without asking)

- **Sensor-agnostic by construction.** Every signal is a named **channel** with a unit, a sample rate and a source, declared in a config file. Adding or removing a sensor is a config change. Channel sources are plugins: Moonraker object fields, a serial/USB microcontroller stream, a file replay, the simulator.
- **Python 3.12.** `pyproject.toml` with extras: the runtime service has a light dependency set that installs on a Pi; `[train]` adds scikit-learn, LightGBM, PyTorch and MLflow; `[dev]` adds pytest and ruff.
- **Storage:** Parquet (pyarrow) per run, JSON metadata sidecar with a versioned schema. Fields are listed in `docs/plan.md`.
- **Models, in this order**, each required to beat the one before it on held-out prints before it is adopted: expected-signal regression, EWMA/CUSUM alarm, anomaly model on normal data only, gradient-boosted classifier, small 1D CNN, time-to-defect trend estimate, recoverability rule. The simple baseline is a first-class deliverable, not a stepping stone.
- **Evaluation rules:** split by print, never by window. Features use past data only. Simulated and open-data results are always labelled as such and never mixed into tables of real-printer performance.
- **Induction-variable guard:** runs carry a `manipulated_variables` field. The training and scoring code must exclude those channels as model inputs for those runs, and must refuse to report a headline score from runs where it did not.
- **Safety model (non-negotiable):**
  - The service reaches the printer **only** through the Moonraker API and can send **only** an allowlist: set speed factor, set nozzle temperature, run a named purge macro, pause, notify. No arbitrary G-code path exists anywhere in the code.
  - Every temperature command is clamped to the configured filament maximum. A command above it is rejected and logged, never silently lowered.
  - The service never reads or writes printer configuration and has no way to disable thermal-runaway protection.
  - Missing or stale data means no action. A crash leaves the printer running as a stock printer.
  - One action per verification window; a cap on interventions per print.
  - The model's output selects **when** to climb the ladder. It never produces a command.
- **No cloud.** Everything runs on the local network.
- **Not yours to write:** the team's reports, the concept selection, any judgment in the design templates, and any claim of real-printer performance.

## 5. How to work

### Phase 0: plan (before any code)

1. Read `docs/plan.md` in full.
2. Check the current Moonraker and Klipper documentation for: the websocket subscription API, which printer objects expose extruder velocity, heater power, temperatures, toolhead position and print state, how load-cell and accelerometer data can be read, and whether TMC driver load values are readable at a useful rate. Record what you confirmed and what you could not, with links. Do not rely on memory for API details.
3. Write `PLAN.md` at the repo root with:
   - The workstreams below, each with scope, owned directories, interfaces, acceptance tests and an estimate.
   - A dependency graph and which workstreams run in parallel.
   - The interfaces agreed **before** building: the channel config schema, the run file and metadata schema, the model interface (fit, score, predict-one-tick), the service's internal events, and the dashboard API.
   - Risks and open questions for me.
   - A "Manual steps for the team" list: everything that needs hands, hardware or a human judgment.
4. **Stop and wait for my approval of `PLAN.md`.** Do not start Phase 1 until I say go.

### Phase 1+: build with subagents

| ID | Workstream | Owns | Depends on |
| --- | --- | --- | --- |
| A | Data platform: channel plugins, Moonraker logger, run store, metadata schema, validation, blind labelling tool | `clogguard/channels/`, `clogguard/logger/`, `clogguard/store/`, `tools/label/` | interfaces only |
| B | Open-data benchmark: download scripts, loaders, notebooks, results table | `benchmarks/`, `scripts/data/` | A's run schema |
| C | Simulator and mock Moonraker server | `clogguard/sim/`, `tests/mock_moonraker/` | A's channel schema |
| D | Features and models: layers 1 to 7, training pipeline, tracking, comparison table command | `clogguard/features/`, `clogguard/models/`, `clogguard/train/` | A, C |
| E | Real-time service: loop, state machine, allowlist, clamp, watchdog, audit log | `clogguard/service/`, `clogguard/safety/` | D's model interface, C's mock |
| H | Test-campaign tooling and Table 2 statistics | `clogguard/campaign/`, `clogguard/eval/` | A's run schema |
| G | Dashboard and alerts | `dashboard/` | E's events |
| I | Design-process templates | `design/` | none |

Workstream F (printer integration: Klipper snippets, macros, sensor firmware, install script) is **out of scope** until I tell you which concept was selected. Leave a stub directory and a README listing what it will need.

Rules for delegation:

- **One subagent per workstream**, in parallel where the graph allows, each with `isolation: "worktree"`. A subagent edits only the directories it owns; anything outside goes in its report as a request.
- Give each subagent a **self-contained brief**: goal, owned paths, interfaces, acceptance commands, the conventions below, the relevant parts of `docs/plan.md`, and "return a summary of at most 200 words plus the list of files changed". They start cold.
- **Interfaces first.** Publish the schemas from Phase 0 before any workstream builds against them. Changes route back through you.
- After each workstream finishes, merge its worktree, run **all** checks, and fix integration breaks.
- When everything is merged, run one **independent reviewer subagent** that has not seen the build. It reviews: (1) the safety model, by trying to find any path from model output or config to a non-allowlisted command or an over-limit temperature; (2) data leakage, by checking splits, look-ahead features and the induction-variable guard; (3) honesty of every generated table and figure label. It returns a ranked list. Fix the critical ones.

### Token efficiency

- Your context is for planning, interfaces and integration. Delegate reading and writing.
- Subagent reports are short summaries, never file dumps.
- Grep before you read. Read files in parts.
- Don't re-read files you just wrote. Don't re-run unchanged test suites.
- Batch independent tool calls.

## 6. Conventions

- Type hints everywhere, ruff clean, pytest for every module. Tests run with no network and no hardware.
- Timestamps are monotonic seconds from print start plus one wall-clock anchor per run. Units are in channel names or metadata, never implied.
- Config is files, validated at load, with a documented example.
- Every figure and table generated by code states its data source (real printer, open dataset, simulated) in its title or caption.
- Dataset download scripts record the URL, licence, citation and checksum. `data/` is gitignored.
- Commits are small and conventional (`feat(service): ...`), one workstream per branch, with a PR description listing tests run.
- Never commit secrets, data, or anything from an employer.

## 7. Definition of done

- [ ] `pytest` passes; ruff clean; coverage of at least 80% on `clogguard/`, and 100% of branches in `clogguard/safety/`
- [ ] Tests prove a non-allowlisted command is rejected, an over-limit temperature is rejected, and stale data produces no action
- [ ] End-to-end test in CI: mock printer replays a simulated slow clog; the service warns, climbs the ladder, verifies recovery, and the audit log shows assessments at least once per second and commands within 2 s of each decision
- [ ] End-to-end test in CI: mock printer replays simulated normal prints with varied flow; the false-intervention count is reported
- [ ] One command regenerates the signal-by-model comparison table from stored runs
- [ ] The Table 2 statistics module reproduces known binomial bounds in its tests (for example, 26 of 30 supports 70% at one-sided 95%; 0 of 30 supports a rate under 10%)
- [ ] Open-data loaders run from a clean checkout with one documented command each
- [ ] The service's runtime dependencies install on a Raspberry Pi OS image (verify in an arm64 container)
- [ ] CI green
- [ ] README: architecture diagram, quick start on a PC with the simulator, how to add a channel, how to log a real print
- [ ] Final report to me: what is done, what has only run against the simulator, what the team must do by hand, and known limitations. Do not call anything "working" on a printer; nothing here has touched one.

## 8. When to stop and ask me

- Before Phase 1 (plan approval).
- If the Moonraker or Klipper documentation contradicts an assumption in this prompt or in `docs/plan.md`.
- Before adding any command to the allowlist or any second path to the printer.
- If a dataset's licence is unclear or forbids this use.
- If a workstream blows past twice its estimate.

Start with Phase 0 now.
