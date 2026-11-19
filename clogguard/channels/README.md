# clogguard.channels

One plugin per `SourceKind`, registered with `@register_source(kind)`; `build_sources(config,
SourceContext(...))` groups a `ChannelConfig` by kind and instantiates each plugin.

| Kind | Class | Params | Notes |
| --- | --- | --- | --- |
| moonraker | `MoonrakerSource` | `object`, `field`, optional `index` (list), `key` (dict, dotted) | `subscription()`, `extract(merged_status)` |
| serial | `SerialSource` | `port`, `baud`, optional `key`, `raw_rate_hz` | one JSON object per line with `t_mcu`; `run(sink)`; injectable line reader; real ports need optional `pyserial` |
| replay | `ReplaySource` | optional `column` | `values_at(t)` (zero-order hold), `iter_samples()` |
| derived | `DerivedSource` | `fn`, `inputs` | `volumetric_flow` = e-velocity x filament area (`MaterialProfile`) |

`sim` has no plugin here (the simulator owns it); `build_sources(..., skip_unregistered=True)`
skips such channels.

`Sampler` turns asynchronous samples into one row per base tick (`BASE_RATE_HZ`): zero-order
hold for slow channels, mean within the tick for fast ones (`rate_hz` above the base rate or
`raw_rate_hz` set), derived channels computed per row. `age(name, now)`, `ages(now)` and
`stale(now, max_age_s)` report data freshness. With `keep_raw=True` fast channels are kept at
full rate in `sampler.raw` for `raw/<channel>.parquet`.

`ClockAligner` maps MCU/`eventtime` clocks onto the host clock (minimum-latency offset over a
sliding window).
