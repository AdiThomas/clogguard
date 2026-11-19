"""Per-tick accelerometer summaries: ``accel_rms`` and band energies at the base rate.

Definitions (per 10 Hz tick, i.e. one 0.1 s window, about 320 samples at 3200 Hz):

* The mean of each axis inside the window is removed (gravity and sensor offset).
* ``accel_rms`` = sqrt(mean(x'^2 + y'^2 + z'^2)), in the input units (g if the source is in g).
* ``accel_band_<lo>_<hi>`` = the part of that mean square lying in [lo, hi) Hz, summed over the
  three axes, from a one-sided periodogram normalised so the bands add up to ``accel_rms**2``
  (Parseval). The last band includes the Nyquist bin.

Bands that lie entirely above the Nyquist frequency of the input are NaN (e.g. the UFRGS 200 Hz
filtered files cannot fill the upper bands).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from clogguard.core.schema import BASE_RATE_HZ
from clogguard.datasets.tabular import tick_index, tick_times

BANDS_HZ: tuple[tuple[float, float], ...] = ((0, 50), (50, 200), (200, 800), (800, 1600))
BAND_COLUMNS: tuple[str, ...] = tuple(f"accel_band_{lo:g}_{hi:g}" for lo, hi in BANDS_HZ)
ACCEL_COLUMNS: tuple[str, ...] = ("accel_rms", *BAND_COLUMNS)

MIN_SAMPLES_PER_TICK = 8
_CHUNK_TICKS = 2000


def _band_energy(block: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """block: (n_ticks, n_samples, 3) with axis means removed -> (ms_total, bands[n_ticks, B])."""
    n = block.shape[1]
    spec = np.fft.rfft(block, axis=1)
    power = (np.abs(spec) ** 2) / (n * n)
    # one-sided: double every bin except DC and (for even n) Nyquist
    if n % 2 == 0:
        power[:, 1:-1, :] *= 2.0
    else:
        power[:, 1:, :] *= 2.0
    power = power.sum(axis=2)  # sum over axes -> (n_ticks, n_bins)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    nyq = fs / 2.0
    bands = np.full((block.shape[0], len(BANDS_HZ)), np.nan)
    for i, (lo, hi) in enumerate(BANDS_HZ):
        if lo >= nyq:
            continue
        last = i == len(BANDS_HZ) - 1
        mask = (freqs >= lo) & ((freqs <= hi) if last else (freqs < hi))
        bands[:, i] = power[:, mask].sum(axis=1)
    ms = (block**2).sum(axis=2).mean(axis=1)
    return ms, bands


def accel_tick_features(
    t_s: np.ndarray, xyz: np.ndarray, fs: float | None = None, rate_hz: float = BASE_RATE_HZ
) -> pd.DataFrame:
    """Summarise a raw triaxial stream into base-rate rows.

    ``t_s``: sample times in seconds (any origin); ``xyz``: array (n, 3). ``fs`` defaults to the
    median sample rate. Ticks with fewer than ``MIN_SAMPLES_PER_TICK`` samples are dropped.
    """
    t = np.asarray(t_s, dtype=float)
    a = np.asarray(xyz, dtype=float)
    if a.ndim != 2 or a.shape[1] != 3 or a.shape[0] != t.shape[0]:
        raise ValueError("xyz must be (n, 3) and match t_s")
    if t.size == 0:
        return pd.DataFrame(columns=["t", *ACCEL_COLUMNS])
    order = np.argsort(t, kind="stable")
    t, a = t[order] - t[order][0], a[order]
    if fs is None:
        d = np.diff(t)
        d = d[d > 0]
        if d.size == 0:
            raise ValueError("cannot infer sample rate: time does not advance")
        fs = 1.0 / float(np.median(d))
    ticks = tick_index(t, rate_hz)
    starts = np.flatnonzero(np.r_[True, ticks[1:] != ticks[:-1]])
    lengths = np.diff(np.r_[starts, ticks.size])
    tick_ids = ticks[starts]

    rms = np.full(tick_ids.size, np.nan)
    bands = np.full((tick_ids.size, len(BANDS_HZ)), np.nan)
    # Ticks are batched by sample count so jittered streams are still vectorised.
    for n in np.unique(lengths):
        if n < MIN_SAMPLES_PER_TICK:
            continue
        all_sel = np.flatnonzero(lengths == n)
        for c0 in range(0, all_sel.size, _CHUNK_TICKS):  # bound memory on 1e7-sample files
            sel = all_sel[c0 : c0 + _CHUNK_TICKS]
            idx = starts[sel][:, None] + np.arange(n)[None, :]
            block = a[idx]  # (k, n, 3)
            block = block - block.mean(axis=1, keepdims=True)
            ms, b = _band_energy(block, fs)
            rms[sel] = np.sqrt(ms)
            bands[sel] = b
    keep = lengths >= MIN_SAMPLES_PER_TICK
    out = pd.DataFrame({"t": tick_times(tick_ids[keep], rate_hz), "accel_rms": rms[keep]})
    for i, col in enumerate(BAND_COLUMNS):
        out[col] = bands[keep, i]
    return out
