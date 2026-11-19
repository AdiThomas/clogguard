# clogguard.service

The real-time service: subscribes to Moonraker, builds base-rate rows, runs the online scorer
at least once per second of printer time, feeds the recovery ladder, and passes every decision
through the safety gate (`clogguard/safety`). Every assessment, decision, command result,
recovery and lifecycle event goes to `runs/<run_id>/audit.jsonl` and to an in-process
`EventBus` for the dashboard.

```
clogguard serve --config config/service.yaml --channels config/channels.yaml [--model PATH]
```

`mode: watch` (default) assesses and alerts only; the ladder runs on paper and each
would-have-done action appears as a Notify. `mode: act` lets the gate send corrections.

| Module | What |
| --- | --- |
| `ladder.py` | `RecoveryLadder`: pure, deterministic state machine (`step`, `on_result`, `take_queued`, `drain_events`); full rules in its docstring |
| `runtime.py` | `Service(link, channels, config, scorer_factory).run()` / `.stop()` |
| `rows.py` | `StatusRowBuilder` (minimal; `RowSource` protocol for a fuller sampler) |
| `audit.py` | `AuditWriter`, `make_run_id(started_at, filename)`, `find_logger_run` (logger dir reuse rule), `update_meta` |
| `bus.py` | `EventBus`: bounded queues, slow subscribers drop oldest |
| `link.py` | `MoonrakerLink` (wraps `clogguard.moonraker.MoonrakerClient`, imported lazily) and `InMemoryLink` (test fake) |
| `cli.py` | `clogguard serve` |

Key behaviour:

* Time base `t_s = eventtime - print_start_eventtime`; ticks are driven by eventtimes, so an
  accelerated mock printer works. Ack timeout and staleness watchdog use the monotonic clock.
* Stale or missing data, a missing signal channel, a scorer error, or a scorer over its
  100 ms budget -> `unknown` assessment, hold, `Lifecycle(data_stale)`.
* Paused printer -> assessments continue, decisions hold. Disconnect -> hold, log, re-subscribe
  when the link is back; the same print continues.
* Logger run-dir reuse: a `runs_dir/*/meta.json` with the same `gcode_file` base name and
  `started_at` within 10 s. At print end the service writes `service_active`, `service_mode`,
  `alert_time_s` (first warning) and appends printer actions.
* Shutdown sends nothing; the printer continues as a stock printer.

Tests (simulated data only): `tests/service/`.
