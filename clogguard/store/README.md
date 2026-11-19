# clogguard.store

```python
store = RunStore("runs")                       # any dir below root holding meta.json is a run
store.list(purpose="induction_trial", data_source="real", induction={"temp_step"})
run = store.load("20261119T120000_cube")       # Run(meta, frame)
for run in store.iter_runs(data_source="simulated"): ...   # lazy
store.save(run)                                # root/<run_id>/
store.update_meta(run_id, part_mass_g=12.4)    # validated against RunMetadata
issues = validate_run(run, run_dir=store.path(run.run_id), channels=channel_config)
```

`validate_run` returns `Issue(code, severity, message)` items for: missing/NaN/non-monotonic
`t`; base-rate gaps (> 1.5 periods, warning); declared channels absent; label channels in a
non-simulated run (from the channel config, else columns prefixed `true_`/`label_`); bad defect
ratings (no rater, duplicate rater, outside the run, not on a photo time); open-dataset run
without citation; manipulated variables not in the table; listed photo files missing.

`PartWriter` / `consolidate_parts` implement the crash-safe append used by the logger.
