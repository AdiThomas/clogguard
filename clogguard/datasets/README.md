# clogguard.datasets: open datasets

Registry, downloader and loaders for the four open datasets in `docs/plan.md`. They are for
building, pretraining and benchmarking only. Every converted run has
`data_source="open_dataset"` and a `source_citation`, and must never be reported as ClogGuard
hardware performance.

| Name | Loader | Output |
| --- | --- | --- |
| `ufrgs_vibration` | `ufrgs.py` | 10 Hz `accel_rms`, `accel_band_0_50`, `accel_band_50_200`, `accel_band_200_800`, `accel_band_800_1600` from the raw 3200 Hz stream |
| `tud_force` | `tud.py` | 10 Hz `force_g` + every numeric setting column (sanitised names) |
| `agh_anomaly` | `agh.py` | 10 Hz `accel_x/y/z`, `accel_rms` (bands if >= 400 Hz), `power_w`, `temp_*_c`, `anomaly_label` (ground truth only) |
| `caxton` | `caxton.py` | label table only (no images), filterable by flow-rate class |

## Commands

```bash
clogguard data list                       # names, licences, URLs
clogguard data info ufrgs_vibration       # URL, DOI, licence, APA citation, notes
clogguard data download ufrgs_vibration   # -> data/open/ufrgs_vibration/ + checksums.json + provenance.json
clogguard data convert ufrgs_vibration    # -> runs/open/ufrgs_vibration/<run_id>/{meta.json,timeseries.parquet}
scripts/data/download.sh                  # all datasets; manual ones print instructions
```

`convert` options: `--src`, `--out`, `--limit N`, `--rate-hz` (sample rate when a table has no
time column). For `caxton`, `convert` writes `labels.parquet` and prints the flow-class counts.

## Downloads

- UFRGS: file list and links come from the Zenodo API (record 22716165).
- AGH: GitHub archive zip of the repository, extracted in place.
- TUD and CAXTON: no stable direct links are known, so `download` prints manual steps. Put the
  files in `data/open/<name>/` by hand and run `download` again to record checksums and
  provenance.
- Interrupted downloads resume from `<file>.part` (HTTP Range).
- After the first download, copy the SHA-256 values from `checksums.json` into
  `registry.py` (`RemoteFile.sha256`) so later downloads are verified.

## Conventions

- Tick `t` is the END of each 0.1 s window, so a row only summarises past samples.
- Band energies are mean-square values in the input units (g² if the source is in g) and add up
  to `accel_rms²`. Bands above the input's Nyquist frequency are NaN.
- UFRGS nozzle restriction runs get `induction.method="small_nozzle"` with severity `0.3mm` or
  `0.2mm`. All other faults (temperature, loose head, TUD misconfigurations, AGH malfunctions)
  get `method="none"` with the fault in `part` and `notes`.
- Unknown formats are handled heuristically; every guess is marked `# ASSUMPTION:` in the code.
  The loaders were written against the documented formats and tested only on synthetic
  fixtures (the build sandbox had no internet). Check the column detection on the first real
  file.
