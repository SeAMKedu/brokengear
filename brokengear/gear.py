"""Involute spur-gear geometry with per-tooth state (wear, bending, fracture, ...).

Conventions
-----------
* Millimetres, gear axis = +Z, gear body occupies z in [0, face_width].
  An optional hub extends *downwards* (z in [-hub_height, 0]) so the top face
  (z = face_width) is always free for face/edge features. Print with the
  bottom (z = 0, or the hub end) on the bed.
* Teeth are numbered 0..z-1 counter-clockwise, tooth 0 centred on +X.
* side = +1 is the CCW flank of a tooth, side = -1 the CW flank.
  ``drive_side`` names the loaded flank (default +1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import build123d as bd


def inv(phi: float) -> float:
    """Involute function."""
    return math.tan(phi) - phi


@dataclass
class GearSpec:
    module: float = 2.0
    teeth: int = 24
    face_width: float = 8.0
    pressure_angle: float = 20.0  # degrees
    bore: float | None = 8.0  # None -> solid gear (no bore by design)
    keyway: bool = False
    keyway_width: float | None = None  # default ~ bore/4
    keyway_depth: float | None = None  # radial depth beyond bore, default ~0.45*width
    hub_diameter: float | None = None
    hub_height: float = 0.0
    drive_side: int = +1
    material: str = "plastic"  # "plastic" (molded / printed) or "steel" (machined)
    min_feature: float = 0.4  # smallest printable feature (nozzle width), mm

    @classmethod
    def from_outside_diameter(cls, od: float, teeth: int, **kw) -> "GearSpec":
        return cls(module=od / (teeth + 2), teeth=teeth, **kw)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            pitch_diameter=self.module * self.teeth,
            outside_diameter=self.module * (self.teeth + 2),
            root_diameter=self.module * (self.teeth - 2.5),
        )
        return d


@dataclass
class ToothState:
    index: int
    center: float  # radians
    thick: float = 1.0  # tooth-thickness scale (profile error)
    wear_drive: float = 0.0  # mm of material removed (max) on the loaded flank
    wear_coast: float = 0.0  # mm on the unloaded flank
    bend: float = 0.0  # mm tangential tip displacement (plastic flow / rolled tip)
    tip_r: float | None = None  # truncated tip radius (short shot, melt)
    fracture: dict | None = None  # {"r_drive","r_coast","sag","jag":[...]}
    removed: bool = False  # cleanly gone (flush with root circle)
    owners: list = field(default_factory=list)  # defect ids that changed this tooth

    @property
    def intact_shape(self) -> bool:
        return not self.removed and self.fracture is None


class Gear:
    """Holds the nominal geometry and the mutable per-tooth state."""

    def __init__(self, spec: GearSpec):
        if spec.teeth < 8:
            raise ValueError("need at least 8 teeth")
        self.spec = spec
        m, z = spec.module, spec.teeth
        self.alpha = math.radians(spec.pressure_angle)
        self.r_p = m * z / 2
        self.r_b = self.r_p * math.cos(self.alpha)
        self.r_a = self.r_p + m
        self.r_f = self.r_p - 1.25 * m
        self.pitch = 2 * math.pi / z
        self.ds = spec.drive_side
        self.teeth = [ToothState(i, i * self.pitch) for i in range(z)]
        # nominal half-thickness angle at the pitch circle
        self._psi_base = math.pi / (2 * z) + inv(self.alpha)
        # radius where flank contact with a mating gear begins (approx.)
        self.r_contact = max(self.r_f + 0.35 * m, self.r_b)

    # ------------------------------------------------------------------ flanks
    def psi0(self, r: float) -> float:
        """Nominal half tooth-thickness angle at radius r."""
        r = max(r, self.r_b)
        phi = math.acos(min(1.0, self.r_b / r))
        return self._psi_base - inv(phi)

    def wear_profile(self, r: float) -> float:
        """Relative wear depth along the flank (0..1).

        Abrasive/adhesive wear is lowest at the pitch line (pure rolling) and
        grows towards tip and root where sliding velocity is highest; there is
        none below the start of active contact.
        """
        m = self.spec.module
        rc = self.r_contact
        ramp_end = rc + 0.4 * m
        if r <= rc:
            return 0.0
        if r < ramp_end:  # smooth start of the worn band (leaves a small step at the contact start)
            u = (r - rc) / (ramp_end - rc)
            return 0.4 * u * u * (3 - 2 * u)
        if r < self.r_p:
            span = max(self.r_p - ramp_end, 1e-6)
            return 0.4 - 0.15 * (r - ramp_end) / span
        u = min(1.0, (r - self.r_p) / (self.r_a - self.r_p))
        return 0.25 + 0.75 * u

    def flank_angle(self, t: ToothState, side: int, r: float) -> float:
        psi = self.psi0(r) * t.thick
        w = t.wear_drive if side == self.ds else t.wear_coast
        psi -= w * self.wear_profile(r) / r
        u = max(0.0, (r - self.r_p) / (self.r_a - self.r_p))
        bend = t.bend * u * u * (-self.ds)  # tip pushed away from the load
        return t.center + side * psi + bend / r

    def flank_point(self, t: ToothState, side: int, r: float) -> tuple[float, float]:
        a = self.flank_angle(t, side, r)
        return (r * math.cos(a), r * math.sin(a))

    def flank_normal(self, t: ToothState, side: int, r: float) -> tuple[float, float]:
        """Unit outward normal of the flank surface (pointing into the gap)."""
        h = 0.01 * self.spec.module
        x0, y0 = self.flank_point(t, side, r - h)
        x1, y1 = self.flank_point(t, side, r + h)
        tx, ty = x1 - x0, y1 - y0
        n = math.hypot(tx, ty)
        tx, ty = tx / n, ty / n
        nx, ny = -ty, tx  # rotate +90
        a = self.flank_angle(t, side, r)
        tang = (-math.sin(a) * side, math.cos(a) * side)
        if nx * tang[0] + ny * tang[1] < 0:
            nx, ny = -nx, -ny
        return nx, ny

    def tip_radius(self, t: ToothState) -> float:
        r = min(t.tip_r or self.r_a, self.r_a)
        land = 0.12 * self.spec.module
        while r > self.r_b + 0.2 * self.spec.module:
            w = (self.flank_angle(t, +1, r) - self.flank_angle(t, -1, r)) * r
            if w >= land:
                break
            r -= 0.02 * self.spec.module
        return r

    # --------------------------------------------------------------- profile
    def _flank_edges(self, t: ToothState, side: int, r0: float, r1: float) -> list:
        """Edges along one flank from r0 to r1 (ascending radius)."""
        edges = []
        if r1 <= r0 + 1e-9:
            return edges
        if r0 < self.r_b:
            rb = min(r1, self.r_b)
            edges.append(bd.Line(self.flank_point(t, side, r0), self.flank_point(t, side, rb)))
            r0 = rb
        if r1 > r0 + 1e-6:
            n = 10
            pts = []
            for k in range(n + 1):
                # equal spacing in r^2 ~ equal roll angle
                rr = math.sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * k / n)
                pts.append(bd.Vector(*self.flank_point(t, side, rr)))
            edges.append(bd.Spline(*pts))
        return edges

    def _tooth_edges(self, t: ToothState) -> list:
        rf = self.r_f
        if t.fracture is not None:
            fr = t.fracture
            r_r = fr["r_drive"] if self.ds == -1 else fr["r_coast"]
            r_l = fr["r_drive"] if self.ds == +1 else fr["r_coast"]
            edges = self._flank_edges(t, -1, rf, r_r)
            # jagged fracture line from right flank (r_r) to left flank (r_l)
            a0 = self.flank_angle(t, -1, r_r)
            a1 = self.flank_angle(t, +1, r_l)
            pts = [self.flank_point(t, -1, r_r)]
            jag = fr.get("jag", [])
            n = len(jag) + 1
            for k, j in enumerate(jag, start=1):
                s = k / n
                rr = (1 - s) * r_r + s * r_l - fr.get("sag", 0.0) * math.sin(math.pi * s) + j
                aa = (1 - s) * a0 + s * a1
                pts.append((rr * math.cos(aa), rr * math.sin(aa)))
            pts.append(self.flank_point(t, +1, r_l))
            for p, q in zip(pts[:-1], pts[1:]):
                edges.append(bd.Line(p, q))
            edges += [e.reversed() for e in reversed(self._flank_edges(t, +1, rf, r_l))]
            return edges
        rt = self.tip_radius(t)
        edges = self._flank_edges(t, -1, rf, rt)
        am = 0.5 * (self.flank_angle(t, -1, rt) + self.flank_angle(t, +1, rt))
        edges.append(
            bd.ThreePointArc(
                self.flank_point(t, -1, rt),
                (rt * math.cos(am), rt * math.sin(am)),
                self.flank_point(t, +1, rt),
            )
        )
        edges += [e.reversed() for e in reversed(self._flank_edges(t, +1, rf, rt))]
        return edges

    def profile_face(self) -> bd.Face:
        kept = [t for t in self.teeth if not t.removed]
        if not kept:
            return bd.Face(bd.Wire([bd.Edge.make_circle(self.r_f)]))
        edges = []
        rf = self.r_f
        for k, t in enumerate(kept):
            edges += self._tooth_edges(t)
            nxt = kept[(k + 1) % len(kept)]
            a0 = self.flank_angle(t, +1, rf)
            a1 = self.flank_angle(nxt, -1, rf)
            while a1 <= a0:
                a1 += 2 * math.pi
            am = 0.5 * (a0 + a1)
            edges.append(
                bd.ThreePointArc(
                    (rf * math.cos(a0), rf * math.sin(a0)),
                    (rf * math.cos(am), rf * math.sin(am)),
                    (rf * math.cos(a1), rf * math.sin(a1)),
                )
            )
        wire = bd.Wire(edges)
        return bd.Face(wire)
