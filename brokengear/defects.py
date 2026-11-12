"""Defect catalog: each entry knows how to *sample* its parameters and how to *apply* itself.

Every defect has
  category  - "manufacturing" or "in_use"
  phase     - when it is applied while building the solid:
                profile  : edits the 2D tooth outline (per-tooth state)
                solid    : edits the freshly extruded blank (face wedge)
                add      : unions material (burrs, flash)
                bore     : changes how the bore / keyway are cut
                cut      : subtracts material (chips, pits, cracks, ...)
                post     : unions after the bore is cut (flash inside the bore)
  group     - mutually exclusive group (only one defect of a group per gear)

Severity ``s`` is in [0, 1] and scales the physical size of the defect.
All sizes respect ``spec.min_feature`` so the defect survives FDM/SLA printing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import build123d as bd

TAU = 2 * math.pi


# --------------------------------------------------------------------------- helpers
def _pt(r, a):
    return (r * math.cos(a), r * math.sin(a))


def prism(points, z0, z1):
    """Extrude a closed 2D polygon (absolute XY coords) between z0 and z1."""
    # build123d extrudes along the face normal: force CCW winding so it goes +Z
    area2 = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))
    if area2 < 0:
        points = points[::-1]
    poly = bd.Polygon(*points, align=None)
    return bd.Pos(0, 0, z0) * bd.extrude(poly, amount=z1 - z0)


def tooth_sector(ctx, t, r_in, half_angle=None):
    g = ctx.gear
    h = half_angle if half_angle is not None else g.pitch / 2
    r_out = (g.r_a + 2 * g.spec.module) / math.cos(h)
    c = t.center
    pts = [_pt(r_in, c - h), _pt(r_out, c - h), _pt(r_out, c + h), _pt(r_in, c + h)]
    return prism(pts, ctx.zmin - 1, ctx.zmax + 1)


def thin_path(center_pts, w0, w1):
    """Polygon around a polyline, width tapering from w0 to w1 (a crack)."""
    n = len(center_pts)
    left, right = [], []
    for k, (x, y) in enumerate(center_pts):
        if k == 0:
            dx, dy = center_pts[1][0] - x, center_pts[1][1] - y
        elif k == n - 1:
            dx, dy = x - center_pts[k - 1][0], y - center_pts[k - 1][1]
        else:
            dx = center_pts[k + 1][0] - center_pts[k - 1][0]
            dy = center_pts[k + 1][1] - center_pts[k - 1][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        w = (w0 + (w1 - w0) * k / (n - 1)) / 2
        left.append((x + nx * w, y + ny * w))
        right.append((x - nx * w, y - ny * w))
    return left + right[::-1]


def wiggle_line(rng, p0, p1, n, amp):
    """Polyline from p0 to p1 with random lateral wiggle (crack path)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / L, dx / L
    pts = []
    for k in range(n + 1):
        s = k / n
        off = 0.0 if k in (0, n) else rng.uniform(-amp, amp)
        pts.append((p0[0] + dx * s + nx * off, p0[1] + dy * s + ny * off))
    return pts


# --------------------------------------------------------------------------- registry
@dataclass
class DefectType:
    name: str
    category: str
    phase: str
    description: str
    mechanism: str
    weight: dict  # relative frequency per material {"plastic": w, "steel": w}
    sample: Callable
    apply: Callable
    group: str | None = None
    needs: tuple = ()  # requirements: "bore", "keyway"
    tags: list = field(default_factory=list)


CATALOG: dict[str, DefectType] = {}


def defect(name, category, phase, weight, description, mechanism, group=None, needs=()):
    def deco(cls):
        CATALOG[name] = DefectType(
            name, category, phase, description, mechanism, weight,
            cls.sample, cls.apply, group, needs,
        )
        return cls
    return deco


# =========================================================================== PROFILE
@defect(
    "broken_tooth", "in_use", "profile", {"plastic": 0.16, "steel": 0.24},
    "Tooth fractured off near the root, leaving a stub with a rough, slanted fracture line.",
    "Bending fatigue: crack starts in the root fillet (~30 deg tangent) on the loaded flank and "
    "runs across the tooth towards the opposite root (ISO 10825 'fatigue breakage').",
)
class BrokenTooth:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = 2 if (s > 0.85 and rng.random() < 0.35) else 1
        teeth = ctx.pick_teeth(n, consecutive=True, hint=hint)
        if not teeth:
            return None
        m = ctx.spec.module
        fr = []
        for _ in teeth:
            fr.append(dict(
                r_drive=ctx.gear.r_f + rng.uniform(0.12, 0.35) * m,
                r_coast=ctx.gear.r_f + rng.uniform(0.0, 0.25) * m,
                sag=rng.uniform(0.0, 0.25) * m,
                jag=[rng.uniform(-0.08, 0.08) * m for _ in range(rng.randint(3, 7))],
            ))
        return dict(teeth=teeth, fracture=fr)

    @staticmethod
    def apply(ctx, inst):
        for i, fr in zip(inst["teeth"], inst["fracture"]):
            ctx.gear.teeth[i].fracture = fr


@defect(
    "tooth_tip_break", "in_use", "profile", {"plastic": 0.06, "steel": 0.08},
    "Upper part of a tooth broken off (partial breakage) with a jagged fracture.",
    "Overload / shock or a foreign body in the mesh breaks the tooth above the pitch line.",
)
class ToothTipBreak:
    @staticmethod
    def sample(ctx, rng, s, hint):
        teeth = ctx.pick_teeth(1, hint=hint)
        if not teeth:
            return None
        g, m = ctx.gear, ctx.spec.module
        r1 = g.r_a - (0.35 + 1.0 * s) * m
        r2 = r1 + rng.uniform(-0.35, 0.35) * m
        return dict(teeth=teeth, fracture=[dict(
            r_drive=max(r1, g.r_b + 0.1 * m), r_coast=max(r2, g.r_b + 0.1 * m),
            sag=rng.uniform(-0.1, 0.15) * m,
            jag=[rng.uniform(-0.07, 0.07) * m for _ in range(rng.randint(3, 6))],
        )])

    apply = BrokenTooth.apply


@defect(
    "missing_teeth", "in_use", "profile", {"plastic": 0.08, "steel": 0.07},
    "One or more consecutive teeth completely gone, flush with the root circle.",
    "Stripped gear: after one tooth fails the load jumps to its neighbours, which shear off in turn.",
)
class MissingTeeth:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = 1 + int(s * 2.99 * rng.random())
        teeth = ctx.pick_teeth(n, consecutive=True, hint=hint)
        return dict(teeth=teeth) if teeth else None

    @staticmethod
    def apply(ctx, inst):
        for i in inst["teeth"]:
            ctx.gear.teeth[i].removed = True


@defect(
    "wear", "in_use", "profile", {"plastic": 0.14, "steel": 0.08},
    "All teeth thinned on the loaded flank (less at pitch line, more at tip and root); tips become pointed.",
    "Abrasive / adhesive wear from sliding contact, dry running or contaminated lubricant.",
)
class Wear:
    @staticmethod
    def sample(ctx, rng, s, hint):
        m = ctx.spec.module
        w = max(ctx.spec.min_feature * 0.6, (0.08 + 0.32 * s) * m)
        coast = w * rng.choice([0.0, 0.0, 0.3, 0.6])  # reversing drives wear both flanks
        teeth = [t.index for t in ctx.gear.teeth if not t.removed and t.fracture is None]
        if hint and hint.get("teeth"):  # localised wear (e.g. eccentric bore high side)
            teeth = [i for i in hint["teeth"] if i in teeth]
        jitter = {i: rng.uniform(0.85, 1.15) for i in teeth}
        return dict(teeth=teeth, depth_drive=round(w, 3), depth_coast=round(coast, 3), jitter=jitter)

    @staticmethod
    def apply(ctx, inst):
        for i in inst["teeth"]:
            t = ctx.gear.teeth[i]
            j = inst["jitter"][i]
            t.wear_drive += inst["depth_drive"] * j
            t.wear_coast += inst["depth_coast"] * j


@defect(
    "plastic_deformation", "in_use", "profile", {"plastic": 0.07, "steel": 0.03},
    "Tooth tips bent / rolled over in the sliding direction.",
    "Material flow under overload (steel: rolling/peening; plastic: creep under static load).",
)
class PlasticDeformation:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = rng.randint(1, 4)
        teeth = ctx.pick_teeth(n, consecutive=True, hint=hint, allow_shared=True)
        if not teeth:
            return None
        bend = max(ctx.spec.min_feature, (0.12 + 0.35 * s) * ctx.spec.module)
        return dict(teeth=teeth, bend=round(bend, 3))

    @staticmethod
    def apply(ctx, inst):
        for i in inst["teeth"]:
            ctx.gear.teeth[i].bend += inst["bend"]


@defect(
    "thermal_melt", "in_use", "profile", {"plastic": 0.08, "steel": 0.0},
    "Group of teeth softened, smeared and shortened by frictional heat.",
    "Plastic gears running dry / overloaded exceed their glass transition / melt temperature.",
)
class ThermalMelt:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = rng.randint(2, 6)
        teeth = ctx.pick_teeth(n, consecutive=True, hint=hint, allow_shared=True)
        if not teeth:
            return None
        m = ctx.spec.module
        prof = []
        for k in range(len(teeth)):
            f = math.sin(math.pi * (k + 0.5) / len(teeth))  # worst in the middle
            prof.append(dict(bend=(0.15 + 0.4 * s) * m * f, trunc=(0.15 + 0.6 * s) * m * f))
        return dict(teeth=teeth, profile=prof)

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        for i, p in zip(inst["teeth"], inst["profile"]):
            t = g.teeth[i]
            t.bend += p["bend"]
            t.tip_r = min(t.tip_r or g.r_a, g.r_a - p["trunc"])
            t.thick *= 1.04


@defect(
    "short_shot", "manufacturing", "profile", {"plastic": 0.13, "steel": 0.0},
    "Teeth in one sector incompletely formed (shortened, rounded tips).",
    "Injection moulding: melt froze before filling the cavity far from the gate "
    "(low pressure/temperature, poor venting).",
)
class ShortShot:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = rng.randint(1, 5)
        teeth = ctx.pick_teeth(n, consecutive=True, hint=hint)
        if not teeth:
            return None
        g, m = ctx.gear, ctx.spec.module
        depth = []
        for k in range(len(teeth)):
            f = math.sin(math.pi * (k + 0.5) / len(teeth))
            depth.append(round((0.3 + 1.4 * s) * m * f, 3))
        return dict(teeth=teeth, depth=depth)

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        for i, d in zip(inst["teeth"], inst["depth"]):
            g.teeth[i].tip_r = max(g.r_f + 0.4 * ctx.spec.module, g.r_a - d)


@defect(
    "pitch_error", "manufacturing", "profile", {"plastic": 0.04, "steel": 0.09},
    "Single tooth displaced around the circumference (uneven spacing).",
    "Indexing error of the hobbing/shaping machine, cutter runout, or mould insert error.",
)
class PitchError:
    @staticmethod
    def sample(ctx, rng, s, hint):
        teeth = ctx.pick_teeth(1, hint=hint)
        if not teeth:
            return None
        frac = (0.04 + 0.12 * s) * rng.choice([-1, 1])
        return dict(teeth=teeth, shift_pitch_fraction=round(frac, 3))

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        for i in inst["teeth"]:
            g.teeth[i].center += inst["shift_pitch_fraction"] * g.pitch


@defect(
    "tooth_thickness_error", "manufacturing", "profile", {"plastic": 0.06, "steel": 0.09},
    "Tooth (or all teeth) too thick or too thin.",
    "Wrong cutter infeed / profile shift, worn cutter, or mould shrinkage not compensated.",
)
class ToothThicknessError:
    @staticmethod
    def sample(ctx, rng, s, hint):
        if hint or rng.random() < 0.6:
            teeth = ctx.pick_teeth(1, hint=hint, allow_shared=True)
            scale = 1 + (0.08 + 0.17 * s) * (-1 if hint else rng.choice([-1, 1]))
        else:
            teeth = [t.index for t in ctx.gear.teeth]
            scale = 1 + (0.03 + 0.07 * s) * rng.choice([-1, 1])
        return dict(teeth=teeth, scale=round(scale, 3)) if teeth else None

    @staticmethod
    def apply(ctx, inst):
        for i in inst["teeth"]:
            ctx.gear.teeth[i].thick *= inst["scale"]


# =========================================================================== SOLID
@defect(
    "face_wedge", "manufacturing", "solid", {"plastic": 0.06, "steel": 0.05},
    "Top face not parallel to the bottom face (thickness varies across the gear).",
    "Warpage from uneven cooling (moulding) or blank clamped tilted while facing (machining).",
)
class FaceWedge:
    @staticmethod
    def sample(ctx, rng, s, hint):
        drop = max(ctx.spec.min_feature * 0.5, 0.2 + 0.6 * s)
        drop = min(drop, 0.25 * ctx.b)
        return dict(drop=round(drop, 3), direction_deg=round(rng.uniform(0, 360), 1))

    @staticmethod
    def apply(ctx, inst):
        R = ctx.gear.r_a + 2
        beta = math.degrees(math.atan(inst["drop"] / (2 * R)))
        box = bd.Box(8 * R, 8 * R, 4 * R, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
        cutter = bd.Rot(0, 0, inst["direction_deg"]) * (bd.Pos(-R, 0, ctx.b) * (bd.Rot(0, beta, 0) * box))
        ctx.wedge = (inst["drop"], math.radians(inst["direction_deg"]), R)
        ctx.cut(cutter, inst)


# =========================================================================== ADD
@defect(
    "burr", "manufacturing", "add", {"plastic": 0.04, "steel": 0.13},
    "Small raised burrs on tooth edges at the face (or gate vestige on moulded parts).",
    "Hobbing/shaping exit burr not removed by deburring; nicks from handling.",
)
class Burr:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = rng.randint(1, 4)
        teeth = ctx.pick_teeth(n, consecutive=False, hint=hint, allow_shared=True)
        if not teeth:
            return None
        h = max(ctx.spec.min_feature, 0.3 + 0.5 * s)
        kinds = [rng.choice(["axial", "flank"]) for _ in teeth]
        return dict(teeth=teeth, height=round(h, 3), kinds=kinds)

    @staticmethod
    def apply(ctx, inst):
        g, m = ctx.gear, ctx.spec.module
        for i, kind in zip(inst["teeth"], inst["kinds"]):
            t = g.teeth[i]
            rt = g.tip_radius(t)
            if kind == "axial":
                # lip standing above the top face along the tip edge
                r0, r1 = rt - 0.45 * m, rt - 0.05 * m
                a0 = g.flank_angle(t, -1, r1) + 0.1 * m / r1
                a1 = g.flank_angle(t, +1, r1) - 0.1 * m / r1
                pts = [_pt(r0, a0), _pt(r1, a0), _pt(r1, a1), _pt(r0, a1)]
                x, y = _pt(r1, t.center)
                body = prism(pts, ctx.ztop(x, y) - 0.5, ctx.ztop(x, y) + inst["height"])
            else:
                # fin sticking out of the drive flank into the gap, at the top edge
                side = g.ds
                r = g.r_p
                px, py = g.flank_point(t, side, r)
                nx, ny = g.flank_normal(t, side, r)
                tx, ty = -ny, nx
                L, w = 0.6 * m, inst["height"]
                pts = [
                    (px - nx * 0.3 - tx * L / 2, py - ny * 0.3 - ty * L / 2),
                    (px + nx * w - tx * L / 2, py + ny * w - ty * L / 2),
                    (px + nx * w + tx * L / 2, py + ny * w + ty * L / 2),
                    (px - nx * 0.3 + tx * L / 2, py - ny * 0.3 + ty * L / 2),
                ]
                zt = ctx.ztop(px, py)
                body = prism(pts, zt - max(ctx.spec.min_feature * 1.5, 0.8), zt)
            ctx.add(body, inst)


@defect(
    "flash", "manufacturing", "add", {"plastic": 0.12, "steel": 0.0},
    "Thin web of excess material at the parting plane bridging tooth gaps in one sector.",
    "Injection moulding: worn/misaligned parting line, too high injection pressure or low clamp force.",
)
class Flash:
    @staticmethod
    def sample(ctx, rng, s, hint):
        t = max(ctx.spec.min_feature, 0.3 + 0.3 * s)
        span = (0.06 + 0.25 * s) * TAU
        start = hint.get("angle", rng.uniform(0, TAU)) if hint else rng.uniform(0, TAU)
        ext = (0.05 + 0.25 * s) * ctx.spec.module
        return dict(thickness=round(t, 3), start_deg=round(math.degrees(start), 1),
                    span_deg=round(math.degrees(span), 1), overhang=round(ext, 3))

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        a0 = math.radians(inst["start_deg"])
        a1 = a0 + math.radians(inst["span_deg"])
        r0, r1 = g.r_f - 0.5, g.r_a + inst["overhang"]
        n = max(4, int((a1 - a0) / 0.05))
        outer = [_pt(r1, a0 + (a1 - a0) * k / n) for k in range(n + 1)]
        inner = [_pt(r0, a1 - (a1 - a0) * k / n) for k in range(n + 1)]
        ctx.add(prism(outer + inner, ctx.zmin_body, ctx.zmin_body + inst["thickness"]), inst)
        inst["teeth"] = ctx.teeth_in_arc(a0, a1)


# =========================================================================== BORE
@defect(
    "missing_bore", "manufacturing", "bore", {"plastic": 0.04, "steel": 0.05},
    "Axle hole not made at all (solid blank).",
    "Drilling/boring operation skipped, or core pin missing / broken in the mould.",
    group="bore", needs=("bore",),
)
class MissingBore:
    @staticmethod
    def sample(ctx, rng, s, hint):
        return dict()

    @staticmethod
    def apply(ctx, inst):
        ctx.bore["kind"] = "missing"


@defect(
    "blind_bore", "manufacturing", "bore", {"plastic": 0.03, "steel": 0.04},
    "Bore drilled only part way (blind), opening on the top face.",
    "Drill depth setting wrong / broken drill; short core pin in the mould.",
    group="bore", needs=("bore",),
)
class BlindBore:
    @staticmethod
    def sample(ctx, rng, s, hint):
        return dict(depth_fraction=round(0.85 - 0.6 * s, 3))

    @staticmethod
    def apply(ctx, inst):
        ctx.bore["kind"] = "blind"
        ctx.bore["depth"] = inst["depth_fraction"] * (ctx.zmax - ctx.zmin)


@defect(
    "eccentric_bore", "manufacturing", "bore", {"plastic": 0.07, "steel": 0.12},
    "Bore off-centre relative to the teeth (radial runout).",
    "Blank not centred when cutting teeth, or bore drilled off-axis; mould core shift.",
    group="bore", needs=("bore",),
)
class EccentricBore:
    @staticmethod
    def sample(ctx, rng, s, hint):
        rb = ctx.spec.bore / 2
        room = ctx.gear.r_f - rb - 2.0
        e = min(max(ctx.spec.min_feature * 0.75, (0.04 + 0.12 * s) * rb * 2), 0.3 * room)
        return dict(offset=round(e, 3), direction_deg=round(rng.uniform(0, 360), 1))

    @staticmethod
    def apply(ctx, inst):
        a = math.radians(inst["direction_deg"])
        ctx.bore["center"] = (inst["offset"] * math.cos(a), inst["offset"] * math.sin(a))


@defect(
    "tilted_bore", "manufacturing", "bore", {"plastic": 0.03, "steel": 0.06},
    "Bore axis not perpendicular to the gear faces (wobble).",
    "Blank clamped tilted / poor bore perpendicularity, giving lead (helix) error and axial runout.",
    group="bore", needs=("bore",),
)
class TiltedBore:
    @staticmethod
    def sample(ctx, rng, s, hint):
        H = ctx.zmax - ctx.zmin
        # tilt so the hole shifts at least ~min_feature across the part
        tilt = max(math.degrees(math.atan(ctx.spec.min_feature / H)), 1.5 + 5.0 * s)
        return dict(tilt_deg=round(tilt, 2), direction_deg=round(rng.uniform(0, 360), 1))

    @staticmethod
    def apply(ctx, inst):
        ctx.bore["tilt"] = (inst["tilt_deg"], inst["direction_deg"])


@defect(
    "bore_size_error", "manufacturing", "bore", {"plastic": 0.06, "steel": 0.07},
    "Bore oversize or undersize.",
    "Wrong drill/reamer, tool wear, or mould shrinkage not compensated.",
    group="bore", needs=("bore",),
)
class BoreSizeError:
    @staticmethod
    def sample(ctx, rng, s, hint):
        sign = hint.get("sign") if hint else rng.choice([-1, 1])
        d = ctx.spec.bore
        delta = max(ctx.spec.min_feature, (0.05 + 0.15 * s) * d) * sign
        return dict(delta_diameter=round(delta, 3))

    @staticmethod
    def apply(ctx, inst):
        ctx.bore["diameter"] = ctx.spec.bore + inst["delta_diameter"]


@defect(
    "bore_wear", "in_use", "bore", {"plastic": 0.05, "steel": 0.04},
    "Bore worn oval / enlarged on one side.",
    "Fretting or rotation on a loose fit (gear slipping on the shaft), loose set screw.",
    group="bore", needs=("bore",),
)
class BoreWear:
    @staticmethod
    def sample(ctx, rng, s, hint):
        d = ctx.spec.bore
        return dict(ovality=round(max(ctx.spec.min_feature, (0.06 + 0.2 * s) * d), 3),
                    direction_deg=round(rng.uniform(0, 180), 1))

    @staticmethod
    def apply(ctx, inst):
        ctx.bore["oval"] = (inst["ovality"], inst["direction_deg"])


@defect(
    "missing_keyway", "manufacturing", "bore", {"plastic": 0.02, "steel": 0.05},
    "Keyway not cut.",
    "Broaching/slotting operation skipped.",
    group="keyway", needs=("bore", "keyway"),
)
class MissingKeyway:
    @staticmethod
    def sample(ctx, rng, s, hint):
        return dict()

    @staticmethod
    def apply(ctx, inst):
        ctx.keyway["present"] = False


@defect(
    "keyway_damage", "in_use", "bore", {"plastic": 0.03, "steel": 0.04},
    "Keyway widened / deformed on the loaded side.",
    "Key working loose under reversing or shock torque, wallowing the keyway out.",
    group="keyway", needs=("bore", "keyway"),
)
class KeywayDamage:
    @staticmethod
    def sample(ctx, rng, s, hint):
        return dict(widen=round(max(ctx.spec.min_feature, (0.15 + 0.5 * s) * ctx.keyway["width"]), 3))

    @staticmethod
    def apply(ctx, inst):
        ctx.keyway["widen"] = inst["widen"]


# =========================================================================== CUT
@defect(
    "chipped_tip", "in_use", "cut", {"plastic": 0.06, "steel": 0.10},
    "Corner of a tooth tip chipped away at the face edge.",
    "Edge loading from misalignment, impact, or debris (e.g. fragments of a broken tooth) in the mesh.",
)
class ChippedTip:
    @staticmethod
    def sample(ctx, rng, s, hint):
        n = rng.randint(1, 3)
        teeth = ctx.pick_teeth(n, consecutive=False, hint=hint, allow_shared=True)
        if not teeth:
            return None
        m = ctx.spec.module
        chips = [dict(radius=round(max(ctx.spec.min_feature * 2, (0.35 + 0.6 * s) * m * rng.uniform(0.8, 1.2)), 3),
                      end=hint.get("end", "top") if hint else ("top" if rng.random() < 0.8 else "bottom"),
                      flank=rng.choice([-1, 0, 1])) for _ in teeth]
        return dict(teeth=teeth, chips=chips)

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        for i, c in zip(inst["teeth"], inst["chips"]):
            t = g.teeth[i]
            if not t.intact_shape:
                continue
            rt = g.tip_radius(t)
            a = t.center if c["flank"] == 0 else g.flank_angle(t, c["flank"], rt - 0.1 * ctx.spec.module)
            r = c["radius"]
            x, y = _pt(rt + 0.15 * r, a)
            z = ctx.ztop(x, y) + 0.15 * r if c["end"] == "top" else ctx.zmin_body - 0.15 * r
            sph = bd.Pos(x, y, z) * bd.scale(bd.Sphere(r), by=(1.0, 0.8, 1.2))
            ctx.cut(sph & tooth_sector(ctx, t, g.r_p - 0.5 * ctx.spec.module), inst)


@defect(
    "tooth_end_break", "in_use", "cut", {"plastic": 0.04, "steel": 0.07},
    "A triangular piece broken from one end of a tooth (tooth end breakage).",
    "Edge loading: misalignment, shaft deflection or lead error concentrates load at one end of the face.",
)
class ToothEndBreak:
    @staticmethod
    def sample(ctx, rng, s, hint):
        teeth = ctx.pick_teeth(1, hint=hint)
        if not teeth:
            return None
        return dict(teeth=teeth,
                    length_fraction=round(min(0.85, 0.3 + 0.5 * s), 3),
                    start_height_fraction=round(rng.uniform(0.1, 0.6), 3))

    @staticmethod
    def apply(ctx, inst):
        g, m = ctx.gear, ctx.spec.module
        t = g.teeth[inst["teeth"][0]]
        x0 = g.r_f + inst["start_height_fraction"] * (g.r_a - g.r_f)
        L = inst["length_fraction"] * ctx.b
        zt = ctx.b
        x_far = g.r_a + 2 * m
        z_far = zt - L * (x_far - x0) / (g.r_a - x0)
        # tooth-local profile in the XZ plane, extruded tangentially (Y)
        poly = bd.Polygon((x0, zt + 2), (x0, zt), (x_far, z_far), (x_far, zt + 2), align=None)
        wedge = bd.extrude(bd.Plane.XZ * poly, amount=2 * m, both=True)
        cutter = bd.Rot(0, 0, math.degrees(t.center)) * wedge
        ctx.cut(cutter & tooth_sector(ctx, t, g.r_f - 0.5), inst)
        inst["end"] = "top"


@defect(
    "pitting", "in_use", "cut", {"plastic": 0.03, "steel": 0.13},
    "Small craters on the loaded flanks, clustered just below the pitch line.",
    "Surface (contact) fatigue: sub-surface cracks under rolling contact release small particles.",
)
class Pitting:
    @staticmethod
    def sample(ctx, rng, s, hint):
        g, m = ctx.gear, ctx.spec.module
        n_teeth = max(1, int(1 + s * rng.uniform(1, 6)))
        teeth = ctx.pick_teeth(n_teeth, consecutive=rng.random() < 0.6, hint=hint, allow_shared=True)
        if not teeth:
            return None
        bias = hint.get("end") if hint else None  # "top" -> edge loaded
        pits = []
        for i in teeth:
            for _ in range(rng.randint(2, 3 + int(6 * s))):
                rr = max(ctx.spec.min_feature * 0.75, rng.uniform(0.08, 0.16 + 0.08 * s) * m)
                r = g.r_p - rng.uniform(0.05, 0.35) * m
                if bias == "top":
                    zf = rng.uniform(0.65, 0.95)
                else:
                    zf = rng.uniform(0.1, 0.9)
                pits.append(dict(tooth=i, r=round(r, 3), z_fraction=round(zf, 3), radius=round(rr, 3)))
        return dict(teeth=teeth, pits=pits)

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        spheres = None
        for p in inst["pits"]:
            t = g.teeth[p["tooth"]]
            if not t.intact_shape:
                continue
            side = g.ds
            x, y = g.flank_point(t, side, p["r"])
            nx, ny = g.flank_normal(t, side, p["r"])
            rr = p["radius"]
            z = ctx.zmin_body + rr + p["z_fraction"] * (ctx.b - 2 * rr)
            sph = bd.Pos(x + nx * 0.35 * rr, y + ny * 0.35 * rr, z) * bd.Sphere(rr)
            spheres = sph if spheres is None else spheres + sph
        if spheres is not None:
            ctx.cut(spheres, inst)


def _crack_cut(ctx, inst, pts, z0, z1):
    w0 = max(ctx.spec.min_feature, 0.05 * ctx.spec.module)
    poly = thin_path(pts, w0, w0 * 0.6)
    ctx.cut(prism(poly, z0, z1), inst)


@defect(
    "root_crack", "in_use", "cut", {"plastic": 0.06, "steel": 0.06},
    "Visible crack starting in the root fillet of the loaded flank and growing across the tooth.",
    "Early stage of tooth bending fatigue (precursor of broken_tooth).",
)
class RootCrack:
    @staticmethod
    def sample(ctx, rng, s, hint):
        teeth = ctx.pick_teeth(1, hint=hint)
        if not teeth:
            return None
        return dict(teeth=teeth, length_fraction=round(0.3 + 0.45 * s, 3),
                    depth_fraction=round(rng.choice([1.0, rng.uniform(0.4, 1.0)]), 3),
                    seed=rng.randint(0, 10**6))

    @staticmethod
    def apply(ctx, inst):
        import random
        g, m = ctx.gear, ctx.spec.module
        rng = random.Random(inst["seed"])
        t = g.teeth[inst["teeth"][0]]
        r0 = g.r_f + 0.18 * m
        sx, sy = g.flank_point(t, g.ds, r0)
        nx, ny = g.flank_normal(t, g.ds, r0)
        ex, ey = g.flank_point(t, -g.ds, g.r_f + 0.02 * m)
        ex, ey = ex * 0.97, ey * 0.97  # curve downwards toward the rim
        f = inst["length_fraction"]
        p0 = (sx + nx * 0.6, sy + ny * 0.6)
        p1 = (sx + (ex - sx) * f, sy + (ey - sy) * f)
        pts = wiggle_line(rng, p0, p1, 5, 0.04 * m)
        z1 = ctx.zmax + 1
        z0 = ctx.zmin_body - 1 if inst["depth_fraction"] >= 1 else ctx.b - inst["depth_fraction"] * ctx.b
        _crack_cut(ctx, inst, pts, z0, z1)


@defect(
    "rim_crack", "in_use", "cut", {"plastic": 0.03, "steel": 0.03},
    "Crack from a tooth root running radially into the rim toward the bore.",
    "Fatigue crack in a thin rim; path turns radially instead of across the tooth (catastrophic mode).",
)
class RimCrack:
    @staticmethod
    def sample(ctx, rng, s, hint):
        teeth = ctx.pick_teeth(1, hint=hint, allow_shared=True)
        if not teeth:
            return None
        return dict(teeth=teeth, length_fraction=round(0.3 + 0.4 * s, 3), seed=rng.randint(0, 10**6))

    @staticmethod
    def apply(ctx, inst):
        import random
        g, m = ctx.gear, ctx.spec.module
        rng = random.Random(inst["seed"])
        t = g.teeth[inst["teeth"][0]]
        a = g.flank_angle(t, g.ds, g.r_f) + g.ds * 0.15 * m / g.r_f
        r_in = ctx.inner_radius()
        L = inst["length_fraction"] * (g.r_f - r_in)
        p0 = _pt(g.r_f + 0.6, a)
        p1 = _pt(g.r_f - L, a + rng.uniform(-0.03, 0.03))
        pts = wiggle_line(rng, p0, p1, 6, 0.05 * m)
        _crack_cut(ctx, inst, pts, ctx.zmin - 1, ctx.zmax + 1)
        inst["angle_deg"] = round(math.degrees(a) % 360, 1)


@defect(
    "hub_crack", "in_use", "cut", {"plastic": 0.03, "steel": 0.03},
    "Radial crack starting at the bore (at the keyway corner if there is one).",
    "Hoop stress from press/interference fit or shock torque concentrated at the keyway corner.",
    needs=("bore",),
)
class HubCrack:
    @staticmethod
    def sample(ctx, rng, s, hint):
        a = hint.get("angle") if hint and "angle" in hint else rng.uniform(0, TAU)
        return dict(length_fraction=round(0.25 + 0.4 * s, 3), angle=a, seed=rng.randint(0, 10**6))

    @staticmethod
    def apply(ctx, inst):
        import random
        if ctx.bore["kind"] == "missing":
            raise ValueError("no bore")
        g, m = ctx.gear, ctx.spec.module
        rng = random.Random(inst["seed"])
        cx, cy = ctx.bore["center"]
        rb = ctx.bore["diameter"] / 2
        if ctx.keyway["present"]:
            kw = ctx.keyway
            sgn = rng.choice([-1, 1])
            x0, y0 = rb + kw["depth"], sgn * (kw["width"] / 2 + (kw["widen"] if sgn == g.ds else 0))
            a = math.atan2(y0, x0) + sgn * 0.35
            p0 = (cx + x0 - 0.4, cy + y0)
        else:
            a = inst["angle"]
            p0 = (cx + (rb - 0.5) * math.cos(a), cy + (rb - 0.5) * math.sin(a))
        L = inst["length_fraction"] * (g.r_f - rb)
        p1 = (p0[0] + L * math.cos(a), p0[1] + L * math.sin(a))
        pts = wiggle_line(rng, p0, p1, 5, 0.05 * m)
        _crack_cut(ctx, inst, pts, ctx.zmin - 1, ctx.zmax + 1)
        inst["angle_deg"] = round(math.degrees(a) % 360, 1)


@defect(
    "weld_line", "manufacturing", "cut", {"plastic": 0.08, "steel": 0.0},
    "Thin radial groove/line on the top face where two melt fronts met.",
    "Injection moulding with flow split around the core (or multiple gates); a weak plane that often "
    "starts later cracks.",
)
class WeldLine:
    @staticmethod
    def sample(ctx, rng, s, hint):
        a = hint.get("angle") if hint and "angle" in hint else rng.uniform(0, TAU)
        return dict(angle=a, depth=round(max(0.2, 0.2 + 0.3 * s), 3))

    @staticmethod
    def apply(ctx, inst):
        g = ctx.gear
        a = inst["angle"]
        r0, r1 = ctx.inner_radius() + 0.6, g.r_f - 0.1 * ctx.spec.module
        w = ctx.spec.min_feature
        pts = [_pt(r0, a), _pt(r1, a)]
        poly = thin_path(pts, w, w)
        zc = ctx.ztop(*_pt((r0 + r1) / 2, a))
        ctx.cut(prism(poly, zc - inst["depth"], ctx.zmax + 1), inst)
        inst["angle_deg"] = round(math.degrees(a) % 360, 1)


@defect(
    "sink_mark", "manufacturing", "cut", {"plastic": 0.12, "steel": 0.0},
    "Shallow dish-shaped depression on the face over a thick section.",
    "Injection moulding: thick section shrinks after the skin solidifies (insufficient hold pressure/time).",
)
class SinkMark:
    @staticmethod
    def sample(ctx, rng, s, hint):
        g = ctx.gear
        r_in = ctx.inner_radius() + 0.5
        band = g.r_f - 0.3 * ctx.spec.module - r_in
        if band < 2:
            return None
        foot = min(band * (0.15 + 0.15 * s), 2.5 + 3.5 * s)
        rc = r_in + foot + rng.uniform(0, max(0.0, band - 2 * foot))
        a = hint.get("angle") if hint and "angle" in hint else rng.uniform(0, TAU)
        return dict(radius_position=round(rc, 3), angle=a, footprint=round(foot, 3),
                    depth=round(max(0.2, 0.2 + 0.4 * s), 3))

    @staticmethod
    def apply(ctx, inst):
        a, h = inst["footprint"], inst["depth"]
        R = (a * a + h * h) / (2 * h)
        x, y = _pt(inst["radius_position"], inst["angle"])
        z = ctx.ztop(x, y) + R - h
        ctx.cut(bd.Pos(x, y, z) * bd.Sphere(R), inst)
        inst["angle_deg"] = round(math.degrees(inst["angle"]) % 360, 1)


@defect(
    "porosity", "manufacturing", "cut", {"plastic": 0.06, "steel": 0.03},
    "Cluster of small open pores / voids on the top face.",
    "Trapped gas or moisture in the melt (moulding), gas porosity (castings / sintered parts).",
)
class Porosity:
    @staticmethod
    def sample(ctx, rng, s, hint):
        g = ctx.gear
        r_in = ctx.inner_radius() + 0.8
        r_out = g.r_f - 0.2 * ctx.spec.module
        if r_out - r_in < 1:
            return None
        a0 = hint.get("angle") if hint and "angle" in hint else rng.uniform(0, TAU)
        n = rng.randint(3, 4 + int(10 * s))
        pores = []
        for _ in range(n):
            pr = max(ctx.spec.min_feature * 0.6, rng.uniform(0.25, 0.4 + 0.4 * s))
            pores.append(dict(r=round(rng.uniform(r_in + pr, r_out - pr), 3),
                              angle=a0 + rng.gauss(0, 0.25), radius=round(pr, 3)))
        return dict(pores=pores, angle=a0)

    @staticmethod
    def apply(ctx, inst):
        body = None
        for p in inst["pores"]:
            x, y = _pt(p["r"], p["angle"])
            s = bd.Pos(x, y, ctx.ztop(x, y) + 0.2 * p["radius"]) * bd.Sphere(p["radius"])
            body = s if body is None else body + s
        ctx.cut(body, inst)
        inst["angle_deg"] = round(math.degrees(inst["angle"]) % 360, 1)


# =========================================================================== POST
@defect(
    "bore_flash", "manufacturing", "post", {"plastic": 0.04, "steel": 0.0},
    "Thin membrane partly closing the bore at the bottom face.",
    "Moulding flash around a worn or short core pin.",
    needs=("bore",),
)
class BoreFlash:
    @staticmethod
    def sample(ctx, rng, s, hint):
        return dict(thickness=round(max(ctx.spec.min_feature, 0.3 + 0.3 * s), 3),
                    coverage=round(0.3 + 0.7 * s, 3), direction_deg=round(rng.uniform(0, 360), 1))

    @staticmethod
    def apply(ctx, inst):
        if ctx.bore["kind"] in ("missing", "blind"):
            raise ValueError("no through bore")
        cx, cy = ctx.bore["center"]
        r = ctx.bore["diameter"] / 2 + 0.3
        disk = bd.Pos(cx, cy, ctx.zmin) * bd.Cylinder(r, inst["thickness"],
                                                      align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
        cov = inst["coverage"]
        keep = bd.Box(2 * r * cov, 2 * r + 2, inst["thickness"] + 2,
                      align=(bd.Align.MAX, bd.Align.CENTER, bd.Align.CENTER))
        keep = bd.Pos(cx, cy, ctx.zmin) * (bd.Rot(0, 0, inst["direction_deg"]) * (bd.Pos(r, 0, 0) * keep))
        ctx.add(disk & keep, inst)
