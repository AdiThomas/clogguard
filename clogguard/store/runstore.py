"""``RunStore``: discover, filter, load and save run directories under one root."""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clogguard.core.schema import META_FILE, Run, RunMetadata
from clogguard.store.io import load_run, read_meta, save_run, write_meta

log = logging.getLogger(__name__)

Filter = str | Collection[str] | None


def _matches(value: str, wanted: Filter) -> bool:
    if wanted is None:
        return True
    if isinstance(wanted, str):
        return value == wanted
    return value in wanted


class RunStore:
    """Every directory below ``root`` that contains ``meta.json`` is a run (any depth)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ discovery
    def run_dirs(self) -> Iterator[Path]:
        if not self.root.is_dir():
            return
        for meta in sorted(self.root.rglob(META_FILE)):
            rel = meta.relative_to(self.root).parts
            if any(part.startswith(".") for part in rel[:-1]):
                continue  # skip hidden dirs (.parts, .git ...)
            yield meta.parent

    def iter_metas(
        self, *, purpose: Filter = None, data_source: Filter = None, induction: Filter = None
    ) -> Iterator[tuple[Path, RunMetadata]]:
        """Lazily yield ``(run_dir, meta)`` matching the filters. Unreadable metas are skipped
        with a warning."""
        for d in self.run_dirs():
            try:
                meta = read_meta(d)
            except (ValidationError, ValueError, OSError) as exc:
                log.warning("skipping %s: unreadable meta.json (%s)", d, exc)
                continue
            if (
                _matches(meta.purpose, purpose)
                and _matches(meta.data_source, data_source)
                and _matches(meta.induction.method, induction)
            ):
                yield d, meta

    def list(
        self, *, purpose: Filter = None, data_source: Filter = None, induction: Filter = None
    ) -> list[RunMetadata]:
        """Metadata of matching runs (time series not loaded), sorted by start time."""
        metas = [
            m
            for _, m in self.iter_metas(
                purpose=purpose, data_source=data_source, induction=induction
            )
        ]
        return sorted(metas, key=lambda m: (m.started_at, m.run_id))

    def iter_runs(
        self, *, purpose: Filter = None, data_source: Filter = None, induction: Filter = None
    ) -> Iterator[Run]:
        """Lazily load matching runs one at a time."""
        for d, _ in self.iter_metas(purpose=purpose, data_source=data_source, induction=induction):
            yield load_run(d)

    def run_ids(self) -> list[str]:
        return sorted(m.run_id for _, m in self.iter_metas())

    def path(self, run_id: str) -> Path:
        """Directory of ``run_id``: ``root/run_id`` if it holds that run, else searched."""
        direct = self.root / run_id
        if (direct / META_FILE).exists():
            return direct
        found = [d for d, m in self.iter_metas() if m.run_id == run_id]
        if not found:
            raise KeyError(f"run {run_id!r} not found under {self.root}")
        if len(found) > 1:
            raise KeyError(f"run id {run_id!r} is duplicated: {[str(p) for p in found]}")
        return found[0]

    def __contains__(self, run_id: object) -> bool:
        try:
            self.path(str(run_id))
        except KeyError:
            return False
        return True

    # ------------------------------------------------------------------ read / write
    def load(self, run_id: str) -> Run:
        return load_run(self.path(run_id))

    def load_meta(self, run_id: str) -> RunMetadata:
        return read_meta(self.path(run_id))

    def save(self, run: Run, *, overwrite: bool = False) -> Path:
        """Write ``run`` to its existing directory, or to ``root/<run_id>`` if new."""
        try:
            target = self.path(run.run_id)
        except KeyError:
            target = self.root / run.run_id
        return save_run(run, target, overwrite=overwrite)

    def update_meta(self, run_id: str, /, **changes: Any) -> RunMetadata:
        """Validate and write metadata changes; returns the new metadata."""
        if "run_id" in changes and changes["run_id"] != run_id:
            raise ValueError("run_id cannot be changed")
        d = self.path(run_id)
        data = read_meta(d).model_dump()
        unknown = set(changes) - set(data)
        if unknown:
            raise ValueError(f"unknown metadata fields: {sorted(unknown)}")
        data.update(changes)
        meta = RunMetadata.model_validate(data)
        write_meta(d, meta)
        return meta
