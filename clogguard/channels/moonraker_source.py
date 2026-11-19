"""Moonraker channels: map a merged printer status dict onto channel values.

Params: ``object`` (Klipper object name, e.g. ``extruder`` or ``"tmc2209 extruder"``),
``field``, optional ``index`` (element of a list field, e.g. ``toolhead.position[2]``) and
optional ``key`` (entry of a dict field, e.g. ``drv_status.sg_result``; dotted keys descend
further).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clogguard.channels.base import SourceContext, register_source, to_float
from clogguard.core.channels import ChannelSpec, SourceKind
from clogguard.moonraker.status import get_path


@register_source(SourceKind.MOONRAKER)
class MoonrakerSource:
    kind = SourceKind.MOONRAKER

    def __init__(self, specs: list[ChannelSpec], context: SourceContext | None = None) -> None:
        for s in specs:
            if "object" not in s.params or "field" not in s.params:
                raise ValueError(f"moonraker channel {s.name!r} needs params.object and .field")
            if "index" in s.params and not isinstance(s.params["index"], int):
                raise ValueError(f"moonraker channel {s.name!r}: params.index must be an int")
        self.specs = list(specs)
        self._paths: dict[str, tuple[str | int, ...]] = {s.name: self._path(s) for s in specs}

    @staticmethod
    def _path(spec: ChannelSpec) -> tuple[str | int, ...]:
        p = spec.params
        path: list[str | int] = [str(p["object"]), str(p["field"])]
        if "index" in p:
            path.append(int(p["index"]))
        if "key" in p:
            path.extend(str(p["key"]).split("."))
        return tuple(path)

    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def subscription(self) -> dict[str, list[str]]:
        """Objects and fields to pass to ``printer.objects.subscribe``."""
        objs: dict[str, list[str]] = {}
        for s in self.specs:
            fields = objs.setdefault(str(s.params["object"]), [])
            if s.params["field"] not in fields:
                fields.append(str(s.params["field"]))
        return objs

    def extract(self, status: Mapping[str, Any]) -> dict[str, float | None]:
        """Channel values from a full (merged) status; None where missing or non-numeric."""
        return {name: to_float(get_path(status, *path)) for name, path in self._paths.items()}


def merge_subscriptions(*subs: Mapping[str, list[str] | None]) -> dict[str, list[str] | None]:
    """Union of subscription dicts; ``None`` (all fields) wins over a field list."""
    out: dict[str, list[str] | None] = {}
    for sub in subs:
        for obj, fields in sub.items():
            if obj in out and out[obj] is None:
                continue
            if fields is None:
                out[obj] = None
                continue
            cur = out.setdefault(obj, [])
            assert cur is not None
            cur.extend(f for f in fields if f not in cur)
    return out
