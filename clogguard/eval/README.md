# clogguard.eval: Table 2 statistics

- `runs.py`: read-only loader. Any directory containing `meta.json` is a run (`RunMetadata`,
  optional `timeseries.parquet`, optional `audit.jsonl` of `AuditRecord`). `load_runs(root)`.
- `stats.py`: exact one-sided Clopper-Pearson bounds, Wilson (display only), Mann-Whitney U,
  seeded bootstrap CIs, `min_successes(n, target)` (26 for 30, 18 for 20 at 0.70) and
  `max_failures(n, limit)` (0 for 30, 1 for 50 at 0.10).
- `criteria.py`: one function per criterion (`lead_time`, `recovery_rate`, `false_alarms`,
  `filament_waste`, `part_quality`, `response_time`, `hardware_cost`, `install_time`), each
  returning `CriterionResult(value, target, meets_target, supported_95, n, notes, data_source)`.
- `detection.py`: offline detector evaluation for models: `evaluate_alerts(scores, metas,
  threshold, persist_s)` and `sweep(...)` (lead time vs false alarms table).
- `report.py`: Markdown + JSON report. Refuses mixed data sources unless `allow_mixed`; any
  non-real section is labelled "SIMULATED / OPEN DATASET - not a performance claim" in the title
  and on every row.

```
clogguard eval table2 --runs runs/ --bom bom.csv --install install.csv --out report.md
```

Rules implemented (docs/plan.md): lead time only on watch-only clog runs (acted runs excluded
with a reason; no alert = lead 0, late alert = negative); recovery on `validation_clog`, mode
`act`, `induction.recoverable` true, success = no rater marked a defect; false alarms on
`validation_clean`, any accepted non-Notify command is an intervention, Notify alone is a false
alert reported separately. Interpretations are marked `# ASSUMPTION:` in the code.
