# clogguard.safety

The safety gate: the **only** code in ClogGuard that turns a `Command` into a printer RPC.

```python
gate = SafetyGate(service_config, printer_link, notifier)
result = await gate.submit(cmd, now_s=t, decision_t_s=t, data_fresh=True)  # -> CommandResult
gate.reset_for_new_print()
```

## Guarantees

| # | Guarantee | Tested by |
| --- | --- | --- |
| G1 | Only `clogguard/safety` calls `PrinterLink.call`; no other module names the G-code or print-control RPC methods | `tests/safety/test_source_scan.py` (AST scan of `clogguard/**/*.py`) |
| G2 | Fixed templates only (`templates.py`): `SetSpeedFactor` -> `M220 S<int>`; `SetNozzleTemp` -> `SET_HEATER_TEMPERATURE HEATER=extruder TARGET=<x.x>`; `RunPurge` -> the configured macro name; `Pause` -> print pause with `{}`; `Notify` -> `Notifier`, never the printer | `test_gate.py`, `test_fuzz.py` |
| G3 | Every command is re-validated with `parse_command` on its dumped dict; objects built with `model_construct`, dicts and junk are handled; non-finite numbers rejected | `test_invalid_objects_rejected`, fuzz |
| G4 | Reject, never adjust: temperature above `temp_ceiling_c` or below `material.nominal_temp_c` (checked on the raw value **and** on the 1-decimal value actually sent); speed not a configured step and not 100; purge when disabled or when the macro name is a raw G-code word (`M112`, `G28`, ...) | `test_gate.py`, fuzz |
| G5 | Watch mode sends no printer command (Pause included); Notify still works | `test_watch_mode_*`, fuzz |
| G6 | Stale data (anything but `data_fresh is True`, or a non-finite `now_s`) blocks corrections; Pause is still allowed in act mode | `test_stale_data_blocks_corrections_but_not_pause` |
| G7 | At most one correction per `min_action_interval_s` (time going backwards also rejects); at most `max_interventions_per_print` corrective commands; Pause exempt from both | `test_min_interval_*`, `test_intervention_cap_*`, fuzz |
| G8 | Every submission yields a `CommandResult` with `sent_at_s`/`acked_at_s` | all tests; audit roundtrip |

*Corrective* = an escalation relative to what the gate last sent this print: a speed below the
last sent speed, a temperature above the last sent target, or a purge. Restores (towards 100%
or nominal) are not counted, so a verified recovery can always step back down.

## Delivery semantics

* `accepted=False`: rejected, nothing sent; `rejected_reason = "<code>: <detail>"` with codes
  `invalid_command, kind_not_allowed, non_finite, watch_mode, data_stale, temp_above_ceiling,
  temp_below_nominal, speed_not_allowed, purge_disabled, min_interval, intervention_cap`.
* `accepted=True` and `acked_at_s` set: sent and acknowledged.
* `accepted=True`, `acked_at_s=None`: sent but the transport raised or no ack came within
  `command_ack_timeout_s`; `rejected_reason` starts with `not_acked:` (the core `CommandResult`
  has no separate error field). Treat as **not applied** (`was_applied()` is False); the gate
  still counts it for the interval and cap limits. The recovery ladder then only escalates to
  Pause.
* An invalid object is recorded with a placeholder `Notify` command (message starts
  `[invalid command placeholder]`) so the audit log always round-trips.

`acked_at_s = sent_at_s + elapsed seconds on the gate's monotonic clock` (injectable for tests).

## 100% branch coverage

```
PYTHONPATH=$PWD python -m pytest tests/safety --cov=clogguard.safety --cov-branch
```

The fuzz test (`tests/safety/test_fuzz.py`, seed fixed) submits 4,800 random commands over 60
random configs with random timings, freshness flags, transport failures and missing acks, and
checks every RPC sent against the templates and limits.
