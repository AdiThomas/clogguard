# clogguard.logger

Records every print on the real printer as a run directory (layout: `clogguard/core/schema.py`).

```
clogguard log --service-config config/service.yaml --channels config/channels.yaml \
    --purpose baseline --operator Ad [--webcam-url http://pi/webcam/?action=snapshot \
    --photo-on-layer --photo-interval 30] [--flush-interval 10] [--max-runs 1]
```

- Subscribes through `MoonrakerClient`, watches `print_stats.state`. `printing` opens
  `runs/<YYYYMMDDTHHMMSS>_<gcode stem>/` and writes `meta.json` (`data_source="real"`,
  channels, software version, config hash). Pause/resume stays in the same run.
- `complete` -> outcome `completed`, `cancelled` -> `cancelled`, `error` -> `failed`,
  `standby` without an end state -> `cancelled` (assumption). Stopping the logger mid-print
  leaves outcome empty with a note.
- Rows at 10 Hz are buffered and flushed as Parquet parts every `--flush-interval` seconds,
  then consolidated into `timeseries.parquet` (and `raw/<channel>.parquet`). A crash loses at
  most one flush interval; `RunStore` loads unconsolidated parts.
- Photos via an injectable async snapshot function (default: HTTP GET of `--webcam-url`), at
  start, each layer change and/or every K seconds; listed in `meta.photos`.
- `audit.jsonl` and the service's meta fields (`service_active`, `service_mode`,
  `alert_time_s`, `actions`) are never overwritten.
- A failing serial reader (e.g. `pyserial` missing) stops the logger with the error rather
  than silently recording without that sensor.
