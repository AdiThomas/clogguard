"""Reading and writing one run directory (layout in ``clogguard.core.schema``).

Crash safety: writers append Parquet *parts* under ``<run>/.parts/`` and consolidate them into
the final file at the end. A run whose writer died still loads: the parts are read in order.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from clogguard.core.schema import META_FILE, RAW_DIR, TIMESERIES_FILE, Run, RunMetadata

PARTS_DIR = ".parts"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_meta(run_dir: str | Path) -> RunMetadata:
    return RunMetadata.model_validate_json(Path(run_dir, META_FILE).read_bytes())


def write_meta(run_dir: str | Path, meta: RunMetadata) -> None:
    atomic_write_bytes(Path(run_dir, META_FILE), meta.model_dump_json(indent=2).encode())


def write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    frame.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _stem(name: str) -> str:
    return name.replace("/", "__")


class PartWriter:
    """Append-only Parquet parts for one logical file (``timeseries`` or ``raw/<channel>``)."""

    def __init__(self, run_dir: str | Path, name: str) -> None:
        self.run_dir = Path(run_dir)
        self.name = name
        self.parts_dir = self.run_dir / PARTS_DIR
        self._seq = len(self.part_paths())

    @property
    def final_path(self) -> Path:
        return self.run_dir / f"{self.name}.parquet"

    def part_paths(self) -> list[Path]:
        if not self.parts_dir.is_dir():
            return []
        return sorted(self.parts_dir.glob(f"{_stem(self.name)}.*.parquet"))

    def write(self, frame: pd.DataFrame) -> Path | None:
        if frame.empty:
            return None
        self._seq += 1
        path = self.parts_dir / f"{_stem(self.name)}.{self._seq:06d}.parquet"
        write_parquet(path, frame)
        return path

    def read(self) -> pd.DataFrame | None:
        """Final file if present, else the concatenated parts, else None."""
        if self.final_path.exists():
            return pd.read_parquet(self.final_path)
        parts = self.part_paths()
        if not parts:
            return None
        return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)

    def consolidate(self) -> Path | None:
        """Merge parts into the final file (atomically), then delete the parts."""
        parts = self.part_paths()
        if not parts:
            return self.final_path if self.final_path.exists() else None
        frames = [pd.read_parquet(p) for p in parts]
        if self.final_path.exists():
            frames.insert(0, pd.read_parquet(self.final_path))
        write_parquet(self.final_path, pd.concat(frames, ignore_index=True))
        for p in parts:
            p.unlink()
        _remove_if_empty(self.parts_dir)
        return self.final_path


def _remove_if_empty(d: Path) -> None:
    if d.is_dir() and not any(d.iterdir()):
        d.rmdir()


def consolidate_parts(run_dir: str | Path) -> list[Path]:
    """Consolidate every logical file that has parts (e.g. after a crash)."""
    run_dir = Path(run_dir)
    parts_dir = run_dir / PARTS_DIR
    if not parts_dir.is_dir():
        return []
    names = sorted({p.name.split(".")[0].replace("__", "/") for p in parts_dir.glob("*.parquet")})
    out = [PartWriter(run_dir, n).consolidate() for n in names]
    _remove_if_empty(parts_dir)
    return [p for p in out if p is not None]


def load_frame(run_dir: str | Path) -> pd.DataFrame:
    frame = PartWriter(run_dir, TIMESERIES_FILE.removesuffix(".parquet")).read()
    if frame is None:
        return pd.DataFrame({"t": pd.Series(dtype=float)})
    if "t" in frame.columns:
        frame = frame.sort_values("t", kind="stable").reset_index(drop=True)
    return frame


def load_run(run_dir: str | Path) -> Run:
    return Run(meta=read_meta(run_dir), frame=load_frame(run_dir))


def load_raw(run_dir: str | Path, channel: str) -> pd.DataFrame | None:
    return PartWriter(run_dir, f"{RAW_DIR}/{channel}").read()


def save_run(run: Run, run_dir: str | Path, *, overwrite: bool = False) -> Path:
    run_dir = Path(run_dir)
    if (run_dir / META_FILE).exists() and not overwrite:
        raise FileExistsError(f"{run_dir} already holds a run (pass overwrite=True)")
    run_dir.mkdir(parents=True, exist_ok=True)
    for p in PartWriter(run_dir, "timeseries").part_paths():
        p.unlink()
    write_parquet(run_dir / TIMESERIES_FILE, run.frame)
    write_meta(run_dir, run.meta)
    return run_dir
