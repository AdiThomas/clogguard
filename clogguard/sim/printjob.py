"""Deterministic print-profile generator for the simulator.

A print job is a sequence of motion phases (constant acceleration pieces of trapezoidal moves,
retractions and dwells) laid out on *job time* ``tau``: the time the print would take at a
100% speed factor. The simulator advances ``tau`` by ``dt * speed_factor`` each step, so slowing
the printer stretches the job in wall time, as on a real machine.

Three geometries of roughly 25 g and 30 to 40 minutes, matching the test part in
``docs/plan.md``:

* ``box``      - square hollow box, 3 perimeters, sparse infill, solid top and bottom.
* ``cylinder`` - round tube made of many short segments (near-constant velocity).
* ``bracket``  - L bracket: a wide base plate, then a narrow wall whose small layers trigger the
  slicer's minimum-layer-time slowdown (natural speed changes).

All values are simulated. Nothing here is measured from a real printer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

GEOMETRIES = ("box", "cylinder", "bracket")

FILAMENT_DIAMETER_MM = 1.75
FILAMENT_AREA_MM2 = math.pi * (FILAMENT_DIAMETER_MM / 2) ** 2
PLA_DENSITY_G_MM3 = 1.24e-3

KIND_CODES = {"travel": 0, "perimeter": 1, "infill": 2, "solid": 3, "retract": 4, "dwell": 5}


@dataclass(frozen=True)
class SlicerSettings:
    """Nominal slicer settings (PrusaSlicer/OrcaSlicer-like defaults for a 0.4 mm nozzle)."""

    layer_h_mm: float = 0.24
    first_layer_h_mm: float = 0.28
    line_w_mm: float = 0.45
    perimeters: int = 3
    solid_layers: int = 4
    infill_density: float = 0.25
    v_outer_mm_s: float = 45.0
    v_inner_mm_s: float = 80.0
    v_infill_mm_s: float = 135.0
    v_solid_mm_s: float = 90.0
    v_travel_mm_s: float = 200.0
    v_first_layer_mm_s: float = 25.0
    accel_mm_s2: float = 3000.0
    square_corner_v_mm_s: float = 5.0
    retract_mm: float = 0.8
    retract_v_mm_s: float = 35.0
    min_layer_time_s: float = 8.0
    fan_full_from_layer: int = 2


@dataclass
class _Builder:
    s: SlicerSettings
    t0: list[float] = field(default_factory=list)
    dur: list[float] = field(default_factory=list)
    v0: list[float] = field(default_factory=list)
    acc: list[float] = field(default_factory=list)
    e_ratio: list[float] = field(default_factory=list)
    e_direct: list[float] = field(default_factory=list)
    kind: list[int] = field(default_factory=list)
    layer: list[int] = field(default_factory=list)
    z: list[float] = field(default_factory=list)
    fan: list[float] = field(default_factory=list)
    t: float = 0.0
    cur_layer: int = 0
    cur_z: float = 0.0
    cur_fan: float = 0.0
    speed_scale: float = 1.0

    def _phase(self, dur: float, v0: float, a: float, er: float, ed: float, kind: str) -> None:
        if dur <= 1e-9:
            return
        self.t0.append(self.t)
        self.dur.append(dur)
        self.v0.append(v0)
        self.acc.append(a)
        self.e_ratio.append(er)
        self.e_direct.append(ed)
        self.kind.append(KIND_CODES[kind])
        self.layer.append(self.cur_layer)
        self.z.append(self.cur_z)
        self.fan.append(self.cur_fan)
        self.t += dur

    def move(self, length: float, v_max: float, kind: str, v_junction: float | None = None) -> None:
        """Trapezoidal move of ``length`` mm. Extruding kinds use the slicer's e-ratio."""
        if length <= 1e-6:
            return
        s = self.s
        a = s.accel_mm_s2
        v_max = v_max * self.speed_scale
        vj = s.square_corner_v_mm_s if v_junction is None else min(v_junction, v_max)
        er = 0.0
        if kind != "travel":
            h = s.first_layer_h_mm if self.cur_layer == 0 else s.layer_h_mm
            er = s.line_w_mm * h / FILAMENT_AREA_MM2
        v_peak = min(v_max, math.sqrt(a * length + vj * vj))
        vj = min(vj, v_peak)
        d_acc = (v_peak * v_peak - vj * vj) / (2 * a)
        d_cruise = max(0.0, length - 2 * d_acc)
        t_acc = (v_peak - vj) / a
        self._phase(t_acc, vj, a, er, 0.0, kind)
        self._phase(d_cruise / v_peak, v_peak, 0.0, er, 0.0, kind)
        self._phase(t_acc, v_peak, -a, er, 0.0, kind)

    def retract(self, sign: float) -> None:
        s = self.s
        self._phase(s.retract_mm / s.retract_v_mm_s, 0.0, 0.0, 0.0, sign * s.retract_v_mm_s,
                    "retract")

    def travel(self, length: float) -> None:
        self.retract(-1.0)
        self.move(length, self.s.v_travel_mm_s, "travel")
        self.retract(+1.0)

    def dwell(self, dur: float) -> None:
        self._phase(dur, 0.0, 0.0, 0.0, 0.0, "dwell")


@dataclass(frozen=True)
class _Section:
    """Cross-section of the part for a range of heights."""

    shape: str  # "rect" or "circle"
    a_mm: float  # width (rect) or diameter (circle)
    b_mm: float  # depth (rect); unused for circle
    z_top_mm: float
    solid: bool = False  # whole section printed solid (e.g. a thin plate)


def _geometry_sections(geometry: str, rng: np.random.Generator) -> list[_Section]:
    j = lambda: float(rng.uniform(0.97, 1.03))  # noqa: E731 - per-dimension jitter
    if geometry == "box":
        return [_Section("rect", 41.0 * j(), 41.0 * j(), 28.0 * j())]
    if geometry == "cylinder":
        return [_Section("circle", 44.0 * j(), 0.0, 30.0 * j())]
    if geometry == "bracket":
        base_top = 6.0 * j()
        return [
            _Section("rect", 75.0 * j(), 42.0 * j(), base_top),
            _Section("rect", 70.0 * j(), 10.0 * j(), base_top + 19.0 * j()),
        ]
    raise ValueError(f"unknown geometry {geometry!r}; expected one of {GEOMETRIES}")


def _perimeter_loop(b: _Builder, sec: _Section, inset: float, v: float) -> None:
    if sec.shape == "rect":
        w, d = sec.a_mm - 2 * inset, sec.b_mm - 2 * inset
        for side in (w, d, w, d):
            b.move(side, v, "perimeter")
    else:
        dia = sec.a_mm - 2 * inset
        n_seg = 96
        seg = math.pi * dia / n_seg
        # Junction velocity for a 3.75-degree turn is close to full speed.
        for _ in range(n_seg):
            b.move(seg, v, "perimeter", v_junction=0.9 * v)


def _infill(b: _Builder, sec: _Section, inset: float, density: float, v: float, kind: str,
            layer: int) -> None:
    lw = b.s.line_w_mm
    spacing = lw / max(density, 1e-3)
    if sec.shape == "rect":
        w, d = sec.a_mm - 2 * inset, sec.b_mm - 2 * inset
        if layer % 2:
            w, d = d, w
        n = max(1, int(d / spacing))
        for _ in range(n):
            b.move(w, v, kind)
            b.move(min(spacing, d), v, kind)
    else:
        r = sec.a_mm / 2 - inset
        n = max(1, int(2 * r / spacing))
        for i in range(n):
            y = -r + (i + 0.5) * (2 * r / n)
            chord = 2 * math.sqrt(max(r * r - y * y, 0.0))
            b.move(chord, v, kind)
            b.move(spacing, v, kind)


def _layer(b: _Builder, sec: _Section, layer: int, n_layers: int, s: SlicerSettings,
           density: float) -> None:
    first = layer == 0
    t_start = b.t
    solid = sec.solid or layer < s.solid_layers or layer >= n_layers - s.solid_layers
    for k in range(s.perimeters):
        v = s.v_first_layer_mm_s if first else (s.v_outer_mm_s if k == 0 else s.v_inner_mm_s)
        _perimeter_loop(b, sec, (k + 0.5) * s.line_w_mm, v)
        if k < s.perimeters - 1:
            b.move(s.line_w_mm, v, "perimeter")
    b.travel(8.0)
    inset = s.perimeters * s.line_w_mm
    if solid:
        v = s.v_first_layer_mm_s if first else s.v_solid_mm_s
        _infill(b, sec, inset, 1.0, v, "solid", layer)
    else:
        _infill(b, sec, inset, density, s.v_infill_mm_s, "infill", layer)
    b.travel(15.0)
    # Minimum layer time: the slicer slows small layers (here by dwelling an equivalent time;
    # PrusaSlicer slows moves instead, which we approximate by a speed scale on the next layer).
    layer_time = b.t - t_start
    if layer_time < s.min_layer_time_s:
        b.speed_scale = max(0.4, layer_time / s.min_layer_time_s)
    else:
        b.speed_scale = 1.0
    b.dwell(0.4)  # layer change: z move


@dataclass(frozen=True)
class PrintJob:
    """Rasterised print profile on a fine job-time grid (``dt`` seconds)."""

    geometry: str
    seed: int
    dt: float
    toolhead_v: np.ndarray  # mm/s
    e_v: np.ndarray  # commanded filament velocity at 100% speed, mm/s (negative = retract)
    accel: np.ndarray  # |toolhead acceleration| mm/s^2
    z: np.ndarray  # mm
    layer: np.ndarray  # int
    fan: np.ndarray  # 0..1
    kind: np.ndarray  # int8, see KIND_CODES
    cum_v: np.ndarray  # cumulative integrals (len n+1) for fast tick averages
    cum_e: np.ndarray
    cum_a: np.ndarray
    cum_ep: np.ndarray  # cumulative printing extrusion (retract/unretract excluded)
    n_layers: int

    @property
    def duration_s(self) -> float:
        return len(self.e_v) * self.dt

    @property
    def filament_mm(self) -> float:
        return float(self.cum_e[-1])

    @property
    def mass_g(self) -> float:
        return self.filament_mm * FILAMENT_AREA_MM2 * PLA_DENSITY_G_MM3

    @property
    def mean_extrusion_flow_mm3_s(self) -> float:
        """Mean volumetric flow over the time spent extruding (reference for clog dynamics)."""
        mask = self.e_v > 0.05
        return float(self.e_v[mask].mean() * FILAMENT_AREA_MM2) if mask.any() else 1.0

    def _cum_at(self, cum: np.ndarray, tau: float) -> float:
        x = min(max(tau / self.dt, 0.0), len(cum) - 1.0)
        i = int(x)
        if i >= len(cum) - 1:
            return float(cum[-1])
        f = x - i
        return float(cum[i] + f * (cum[i + 1] - cum[i]))

    def means(self, tau0: float, tau1: float) -> tuple[float, float, float, float]:
        """Mean (toolhead v, filament v, printing filament v, |accel|) over job time
        [tau0, tau1]. "Printing" excludes retract/unretract moves, which refill the melt zone
        rather than push melt through the nozzle."""
        span = tau1 - tau0
        if span <= 1e-12:
            i = self.index(tau0)
            ep = 0.0 if self.kind[i] == KIND_CODES["retract"] else float(self.e_v[i])
            return float(self.toolhead_v[i]), float(self.e_v[i]), ep, float(self.accel[i])
        c = self._cum_at
        return (
            (c(self.cum_v, tau1) - c(self.cum_v, tau0)) / span,
            (c(self.cum_e, tau1) - c(self.cum_e, tau0)) / span,
            (c(self.cum_ep, tau1) - c(self.cum_ep, tau0)) / span,
            (c(self.cum_a, tau1) - c(self.cum_a, tau0)) / span,
        )

    def index(self, tau: float) -> int:
        return min(max(int(tau / self.dt), 0), len(self.e_v) - 1)


def generate_job(geometry: str = "box", seed: int = 0, dt: float = 0.02,
                 settings: SlicerSettings | None = None) -> PrintJob:
    """Build a deterministic print job for ``geometry`` from ``seed``."""
    rng = np.random.default_rng([seed, GEOMETRIES.index(geometry) if geometry in GEOMETRIES
                                 else 99])
    base = settings or SlicerSettings()
    sv = float(rng.uniform(0.95, 1.05))
    s = SlicerSettings(
        **{**base.__dict__,
           "v_outer_mm_s": base.v_outer_mm_s * sv,
           "v_inner_mm_s": base.v_inner_mm_s * sv,
           "v_infill_mm_s": base.v_infill_mm_s * float(rng.uniform(0.92, 1.08)),
           "v_solid_mm_s": base.v_solid_mm_s * sv,
           "infill_density": base.infill_density * float(rng.uniform(0.9, 1.1))}
    )
    sections = _geometry_sections(geometry, rng)
    fan_level = float(rng.uniform(0.8, 1.0))
    b = _Builder(s)
    z_top = sections[-1].z_top_mm
    n_layers = 1 + math.ceil((z_top - s.first_layer_h_mm) / s.layer_h_mm)
    for layer in range(n_layers):
        z = s.first_layer_h_mm + layer * s.layer_h_mm
        sec = next((c for c in sections if z <= c.z_top_mm + 1e-9), sections[-1])
        b.cur_layer, b.cur_z = layer, z
        b.cur_fan = 0.0 if layer < s.fan_full_from_layer else fan_level
        # The bracket's base top is solid for a few layers before the wall starts.
        if geometry == "bracket" and sec is sections[0] and z > sec.z_top_mm - 4 * s.layer_h_mm:
            sec = _Section(sec.shape, sec.a_mm, sec.b_mm, sec.z_top_mm, solid=True)
        _layer(b, sec, layer, n_layers, s, s.infill_density)

    t0 = np.asarray(b.t0)
    v0 = np.asarray(b.v0)
    acc = np.asarray(b.acc)
    er = np.asarray(b.e_ratio)
    ed = np.asarray(b.e_direct)
    n = math.ceil(b.t / dt)
    tk = (np.arange(n) + 0.5) * dt
    idx = np.clip(np.searchsorted(t0, tk, side="right") - 1, 0, len(t0) - 1)
    v = np.clip(v0[idx] + acc[idx] * (tk - t0[idx]), 0.0, None)
    e = v * er[idx] + ed[idx]
    a = np.abs(acc[idx])
    kind = np.asarray(b.kind, dtype=np.int8)[idx]
    e_print = np.where(kind == KIND_CODES["retract"], 0.0, e)

    def cum(x: np.ndarray) -> np.ndarray:
        return np.concatenate([[0.0], np.cumsum(x) * dt])

    return PrintJob(
        geometry=geometry,
        seed=seed,
        dt=dt,
        toolhead_v=v,
        e_v=e,
        accel=a,
        z=np.asarray(b.z)[idx],
        layer=np.asarray(b.layer)[idx],
        fan=np.asarray(b.fan)[idx],
        kind=kind,
        cum_v=cum(v),
        cum_e=cum(e),
        cum_a=cum(a),
        cum_ep=cum(e_print),
        n_layers=n_layers,
    )
