"""Derived channels: computed from other channels at each base tick.

Params: ``fn`` (a name in ``DERIVED_FUNCTIONS``) and ``inputs`` (channel names).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from clogguard.channels.base import SourceContext, register_source
from clogguard.core.channels import ChannelSpec, SourceKind
from clogguard.core.config import MaterialProfile

# fn(inputs in declared order, material) -> value or None
DerivedFn = Callable[[list[float], MaterialProfile | None], float | None]
DERIVED_FUNCTIONS: dict[str, DerivedFn] = {}
_NEEDS_MATERIAL: set[str] = set()


def register_derived(name: str, needs_material: bool = False) -> Callable[[DerivedFn], DerivedFn]:
    def deco(fn: DerivedFn) -> DerivedFn:
        DERIVED_FUNCTIONS[name] = fn
        if needs_material:
            _NEEDS_MATERIAL.add(name)
        return fn

    return deco


@register_derived("volumetric_flow", needs_material=True)
def volumetric_flow(inputs: list[float], material: MaterialProfile | None) -> float | None:
    """mm3/s = filament velocity (mm/s) x filament cross-section (mm2)."""
    assert material is not None
    return inputs[0] * material.filament_area_mm2


@register_source(SourceKind.DERIVED)
class DerivedSource:
    kind = SourceKind.DERIVED

    def __init__(self, specs: list[ChannelSpec], context: SourceContext | None = None) -> None:
        ctx = context or SourceContext()
        self.specs = list(specs)
        self.material = ctx.material
        for s in specs:
            fn = s.params.get("fn")
            if fn not in DERIVED_FUNCTIONS:
                raise ValueError(
                    f"derived channel {s.name!r}: unknown fn {fn!r}; "
                    f"known: {sorted(DERIVED_FUNCTIONS)}"
                )
            if not s.params.get("inputs"):
                raise ValueError(f"derived channel {s.name!r} needs params.inputs")
            if fn in _NEEDS_MATERIAL and self.material is None:
                raise ValueError(f"derived channel {s.name!r} ({fn}) needs a MaterialProfile")

    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    def inputs(self, name: str) -> list[str]:
        for s in self.specs:
            if s.name == name:
                return list(s.params["inputs"])
        raise KeyError(name)

    def compute(self, values: Mapping[str, float | None]) -> dict[str, float | None]:
        """Compute every derived channel from ``values``; None if any input is missing.
        Derived channels may use earlier derived channels as inputs."""
        known = dict(values)
        out: dict[str, float | None] = {}
        for s in self.specs:
            args = [known.get(i) for i in s.params["inputs"]]
            if any(a is None or a != a for a in args):  # None or NaN
                v = None
            else:
                v = DERIVED_FUNCTIONS[s.params["fn"]]([float(a) for a in args], self.material)  # type: ignore[arg-type]
            out[s.name] = v
            known[s.name] = v
        return out
