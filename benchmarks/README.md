# Benchmarks on open data

Benchmarks answer narrow questions with open datasets before ClogGuard hardware data exists.
Every results file states its data source. None of them is real-printer performance.

## `ufrgs_vibration.py`

**Question:** can a hotend accelerometer tell a restricted nozzle from a healthy printer and
from other faults (wrong temperature, loose head)?

**Method:**
1. Each UFRGS file is converted to 10 Hz runs (`accel_rms` plus four band energies,
   `clogguard.datasets.ufrgs`). The file holding prints 11 and 12 is split at its largest time
   gap.
2. Sliding windows (default 5 s, step 2.5 s) are summarised: log mean, log std and log 90th
   percentile of each channel, plus the mean share of each band.
3. A HistGradientBoosting classifier (`--model rf` for a random forest) is evaluated with
   leave-one-print-out cross-validation. Folds are grouped by run, never by window.
4. The report gives a six-class confusion matrix, per-class precision, recall and F1, a grouped
   view (healthy / restricted nozzle / other fault) and a per-print majority vote.

**Limits:** 27 prints is a small sample, and windows from one print are correlated. Read the
print-level results first. A different printer, part (3DBenchy) and fault set than ClogGuard's,
so a good score here shows the signal *can* carry the information, not that it will on our
printer.

### Run on the real data

```bash
clogguard data download ufrgs_vibration          # about 1.1 GB into data/open/ufrgs_vibration/
python -m benchmarks.ufrgs_vibration             # -> benchmarks/results/ufrgs_vibration.md
python -m benchmarks.ufrgs_vibration --model rf --window-s 10 --step-s 5
python -m benchmarks.ufrgs_vibration --limit 6   # quick check on the first 6 files
```

The results header reads "Data source: open dataset (UFRGS, CC BY 4.0) - not ClogGuard
hardware".

### Pipeline check without the data

```bash
python -m benchmarks.ufrgs_vibration --fixture   # -> benchmarks/results/ufrgs_vibration_FIXTURE.md
```

`--fixture` writes synthetic 3200 Hz files with invented class signatures to a temporary
directory and runs the same pipeline. The output is labelled SYNTHETIC and only shows that the
code runs end to end. Its numbers mean nothing about real vibration data.
`ufrgs_vibration_FIXTURE.md` in `results/` was generated this way.
