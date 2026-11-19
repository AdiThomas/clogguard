# clogguard.campaign: test-campaign tooling

```
clogguard campaign plan --out campaign/                # docs/plan.md test plan, seeded
clogguard campaign plan --write-default plan.yaml      # edit counts, methods, block length
clogguard campaign plan --config plan.yaml --seed 7 --block-length 4 --out campaign/
clogguard campaign checklist --markdown checklist.md   # printable
clogguard campaign checklist --run-dir runs/CG-001-IT --datasheet-max-c 220 \
    --klipper-max-temp-c 220 --supervisor AT [--yes]
clogguard campaign report runs/CG-001-IT [--out report.html]
```

- **plan**: phases run in order (development first, validation last). Within a phase, clog runs
  are grouped into supervised blocks of `block_length` consecutive runs and shuffled together
  with fault-free runs; fault-free runs cycle the three geometries in balanced numbers. Outputs
  `plan.csv`, `plan.md`, `run_sheets.html` (printable, one page per run) and
  `sheets/<run_id>.md|.html`. Method severities and schedules are placeholders (TBD) for the
  team's test plan.
- **checklist**: thermal-runaway protection, Klipper max_temp = material limit, datasheet max
  recorded, supervisor (clog runs), low-voltage wiring, fire safety. Interactive, or `--yes`
  (confirmations printed). Appends a note to `meta.json`, or writes `checklist.json` if the run
  directory has no metadata yet.
- **report**: HTML with embedded PNGs (signals, detector output), alert, defect onset, actions,
  masses; data source in the title.
