"""Build a gear solid from a GearSpec and a list of defect instances."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import build123d as bd

from .defects import CATALOG
from .gear import Gear, GearSpec
from .model import PHASE_ORDER, explicit_defects, sample_defects

# defects that claim a tooth exclusively (its outline is replaced)
EXCLUSIVE = {"broken_tooth", "tooth_tip_break", "missing_teeth", "short_shot", "pitch_error",
             "tooth_end_break", "root_crack"}
DESTROYING = {"broken_tooth", "tooth_tip_break", "missing_teeth"}


class Ctx:
    def __init__(self, spec: GearSpec):
        self.spec = spec
        self.gear = Gear(spec)
        self.b = spec.face_width
        self.zmin_body = 0.0
        self.zmin = -spec.hub_height if spec.hub_diameter else 0.0
        self.zmax = self.b
        self.fate: dict[int, str] = {}
        self.destroyed: set[int] = set()
        self.wedge = None
        self.solid = None
        self.current = None  # defect type being sampled
        kw = spec.keyway_width or max(1.0, round((spec.bore or 0) / 4, 1))
        kd = spec.keyway_depth or round(0.45 * kw, 2)
        self.bore = dict(kind="normal" if spec.bore else "none", diameter=spec.bore or 0.0,
                         center=(0.0, 0.0), tilt=None, oval=None, depth=None)
        self.keyway = dict(present=bool(spec.keyway and spec.bore), width=kw, depth=kd, widen=0.0)

    # ---------------------------------------------------------------- queries
    def nearest_tooth(self, angle):
        z = self.spec.teeth
        return int(round((angle % (2 * math.pi)) / self.gear.pitch)) % z

    def teeth_in_arc(self, a0, a1):
        return [t.index for t in self.gear.teeth
                if (t.center - a0) % (2 * math.pi) <= (a1 - a0)]

    def inner_radius(self):
        r = (self.spec.bore or 0) / 2
        if self.keyway["present"]:
            r += self.keyway["depth"]
        return r

    def ztop(self, x, y):
        if not self.wedge:
            return self.b
        drop, d, R = self.wedge
        u = x * math.cos(d) + y * math.sin(d)
        return self.b - drop * (u + R) / (2 * R)

    def pick_teeth(self, n, consecutive=True, hint=None, allow_shared=False):
        z = self.spec.teeth
        rng = self._rng
        name = self.current
        exclusive = name in EXCLUSIVE and not allow_shared

        def ok(i):
            if i in self.destroyed:
                return False
            if exclusive and i in self.fate and not (hint and hint.get("teeth") and i in hint["teeth"]):
                return False
            return True

        start = None
        if hint and hint.get("teeth"):
            cand = [i for i in hint["teeth"] if ok(i)]
            if cand:
                start = cand[len(cand) // 2] if len(hint["teeth"]) > 2 else cand[0]
                if not consecutive:
                    rng.shuffle(cand)
                    picked = cand[:n]
                    self._claim(picked, exclusive, name)
                    return picked
        if consecutive:
            starts = [start] if start is not None else rng.sample(range(z), z)
            for s0 in starts:
                run = [(s0 + k) % z for k in range(n)]
                if all(ok(i) for i in run):
                    self._claim(run, exclusive, name)
                    return run
            return []
        free = [i for i in range(z) if ok(i)]
        if not free:
            return []
        picked = rng.sample(free, min(n, len(free)))
        self._claim(picked, exclusive, name)
        return sorted(picked)

    def _claim(self, teeth, exclusive, name):
        for i in teeth:
            if exclusive:
                self.fate[i] = name
            if name in DESTROYING:
                self.destroyed.add(i)

    # ---------------------------------------------------------------- booleans
    def _check(self, res, inst):
        solids = res.solids()
        if len(solids) != 1:
            # keep the largest piece only if the rest is negligible
            vols = sorted((s.volume for s in solids), reverse=True)
            if len(vols) == 0 or (len(vols) > 1 and vols[1] > 0.5):
                raise ValueError(f"operation split the part into {len(vols)} pieces")
            res = max(solids, key=lambda s: s.volume)
        if abs(res.volume - self.solid.volume) < 1e-6:
            raise ValueError("operation had no effect (feature misses the part)")
        return res

    def cut(self, tool, inst):
        self.solid = self._check(self.solid - tool, inst)

    def add(self, tool, inst):
        self.solid = self._check(self.solid + tool, inst)


def _bore_cutter(ctx):
    bs, kw = ctx.bore, ctx.keyway
    if bs["kind"] in ("none", "missing"):
        return None
    r = bs["diameter"] / 2
    H = ctx.zmax - ctx.zmin
    if bs["kind"] == "blind":
        z0, z1 = ctx.zmax - bs["depth"], ctx.zmax + 1
    else:
        z0, z1 = ctx.zmin - 1, ctx.zmax + 1
    if bs["tilt"]:  # longer so it still clears both faces when tilted
        z0, z1 = z0 - H, z1 + H
    align = (bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)
    if bs["oval"]:
        ov, d = bs["oval"]
        ell = bd.Ellipse(r + ov / 2, r)
        hole = bd.Rot(0, 0, d) * (bd.Pos(ov / 2, 0, z0) * bd.extrude(ell, amount=z1 - z0))
    else:
        hole = bd.Pos(0, 0, z0) * bd.Cylinder(r, z1 - z0, align=align)
    if kw["present"]:
        w, depth, widen = kw["width"], kw["depth"], kw["widen"]
        y0, y1 = -w / 2, w / 2
        if ctx.spec.drive_side > 0:
            y1 += widen
        else:
            y0 -= widen
        key = bd.Pos(0, (y0 + y1) / 2, z0) * bd.Box(r + depth, y1 - y0, z1 - z0,
                                                    align=(bd.Align.MIN, bd.Align.CENTER, bd.Align.MIN))
        hole = hole + key
    if bs["tilt"]:
        tilt, d = bs["tilt"]
        zc = (ctx.zmin + ctx.zmax) / 2
        hole = bd.Pos(0, 0, zc) * (bd.Rot(0, 0, d) * (bd.Rot(0, tilt, 0) * (bd.Rot(0, 0, -d) * (bd.Pos(0, 0, -zc) * hole))))
    cx, cy = bs["center"]
    return bd.Pos(cx, cy, 0) * hole


def build_gear(spec: GearSpec, condition="perfect", seed=0, defects=None, severity=None,
               weights_override=None, marking=None, severity_range=None):
    """Return (solid, label_dict).

    defects: optional explicit list of (name, params) - overrides the random model.
    marking: optional ID text engraved on the bottom face (with a tooth-0 arrow).
    """
    rng = random.Random(seed)
    ctx = Ctx(spec)
    ctx._rng = rng

    # sampling (the Ctx tracks which teeth are taken)
    if defects:
        plan = explicit_defects(ctx, defects, rng, severity, severity_range)
        condition = _condition_of(plan)
    else:
        plan = sample_defects(ctx, condition, rng, severity, weights_override, severity_range)

    by_phase = {p: [d for d in plan if CATALOG[d["type"]].phase == p] for p in PHASE_ORDER}

    for inst in by_phase["profile"]:
        CATALOG[inst["type"]].apply(ctx, inst)
        inst["applied"] = True
    face = ctx.gear.profile_face()
    ctx.solid = bd.extrude(face, amount=ctx.b).solids()[0]
    if spec.hub_diameter and spec.hub_height > 0:
        hub = bd.Pos(0, 0, -spec.hub_height) * bd.Cylinder(
            spec.hub_diameter / 2, spec.hub_height + 0.01,
            align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
        ctx.solid = (ctx.solid + hub).solids()[0]

    def run(phase):
        for inst in by_phase[phase]:
            try:
                CATALOG[inst["type"]].apply(ctx, inst)
                inst["applied"] = True
            except Exception as e:  # keep going, record why
                inst["applied"] = False
                inst["skip_reason"] = str(e)

    run("solid")
    run("add")
    for inst in by_phase["bore"]:
        CATALOG[inst["type"]].apply(ctx, inst)
        inst["applied"] = True
    cutter = _bore_cutter(ctx)
    if cutter is not None:
        ctx.solid = ctx._check(ctx.solid - cutter, None)
    run("cut")
    run("post")

    marking_info = engrave_marking(ctx, marking) if marking else None

    solid = ctx.solid
    label = make_label(ctx, plan, condition, seed)
    if marking_info:
        label["marking"] = marking_info
    return solid, label


def engrave_marking(ctx, text, depth=0.4):
    """Engrave an ID and a tooth-0 arrow into the BOTTOM face (z = 0).

    The text is mirrored so it reads correctly when the gear is flipped over.
    The top face (where face defects live) stays untouched, so the marking is
    not part of what the vision system is tested on.
    """
    g, spec = ctx.gear, ctx.spec
    depth = min(depth, 0.3 * ctx.b)
    r_in = ctx.inner_radius() if ctx.bore["kind"] not in ("none",) else 0.0
    if ctx.bore["kind"] != "none":
        r_in += math.hypot(*ctx.bore["center"]) + 0.5
    if spec.hub_diameter and spec.hub_height > 0:  # engrave the web bottom, outside the hub
        r_in = max(r_in, spec.hub_diameter / 2 + 0.5)
    r_out = g.r_f - 0.6 * spec.module
    band = r_out - r_in
    if band < 4:
        raise ValueError("not enough room between bore and teeth for the ID marking")
    z0 = ctx.zmin_body - 1
    # ID text on the -X side, reading along the radius
    size = min(6.0, max(3.0, band * 0.55))
    txt = bd.Text(text, font_size=size, font="Arial", font_style=bd.FontStyle.BOLD)
    txt = bd.mirror(txt, about=bd.Plane.YZ)  # readable from below
    w = txt.bounding_box().size.X
    while w > band * 0.95 and size > 2.5:
        size -= 0.25
        txt = bd.mirror(bd.Text(text, font_size=size, font="Arial", font_style=bd.FontStyle.BOLD),
                        about=bd.Plane.YZ)
        w = txt.bounding_box().size.X
    cx = -(r_in + band / 2)
    text_cut = bd.Pos(cx, 0, z0) * bd.extrude(txt, amount=1 + depth)
    # arrow on the +X side pointing at tooth 0
    L = min(4.0, band * 0.6)
    tip = r_out - 0.2
    tri = bd.Polygon((tip, 0), (tip - L, L * 0.45), (tip - L, -L * 0.45), align=None)
    arrow_cut = bd.Pos(0, 0, z0) * bd.extrude(tri, amount=1 + depth)
    if ctx.zmin < ctx.zmin_body:  # never cut into the hub
        hub = bd.Pos(0, 0, z0 - 1) * bd.Cylinder(spec.hub_diameter / 2 + 0.01, 2 + depth + 1,
                                                 align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
        text_cut, arrow_cut = text_cut - hub, arrow_cut - hub
    zb = ctx.zmin_body
    ctx.solid = ctx._check(ctx.solid - text_cut - arrow_cut, None)
    return dict(text=text, face="bottom", depth_mm=depth, text_center_mm=[round(cx, 2), 0.0, zb],
                arrow="points from the centre toward tooth 0 (+X)",
                note="text is mirrored so it reads correctly with the gear upside down")


def plan_only(spec: GearSpec, condition, seed, severity=None, weights_override=None,
              severity_range=None, defects=None):
    """Sample the defect plan build_gear(...) would use with the same arguments, without building."""
    rng = random.Random(seed)
    ctx = Ctx(spec)
    ctx._rng = rng
    if defects:
        return explicit_defects(ctx, defects, rng, severity, severity_range)
    return sample_defects(ctx, condition, rng, severity, weights_override, severity_range)


def _condition_of(plan):
    cats = {d["category"] for d in plan}
    if not cats:
        return "perfect"
    if cats == {"manufacturing"}:
        return "manufacturing"
    if cats == {"in_use"}:
        return "in_use"
    return "combined"


def _jsonable(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def make_label(ctx, plan, condition, seed):
    g = ctx.gear
    out = []
    for d in plan:
        t = CATALOG[d["type"]]
        rec = dict(id=d["id"], type=d["type"], category=d["category"], severity=d["severity"],
                   applied=d.get("applied", False))
        if d.get("skip_reason"):
            rec["skip_reason"] = d["skip_reason"]
        if d.get("cause"):
            rec["caused_by"] = d["cause"]
            rec["cause_note"] = d.get("cause_note")
        if d.get("teeth"):
            rec["teeth"] = sorted(d["teeth"])
            rec["tooth_angles_deg"] = [round(math.degrees(g.teeth[i].center) % 360, 2) for i in rec["teeth"]]
        if "angle_deg" in d:
            rec["angle_deg"] = d["angle_deg"]
        rec["description"] = t.description
        rec["mechanism"] = t.mechanism
        skip = {"id", "type", "category", "severity", "cause", "cause_note", "applied", "teeth",
                "angle_deg", "angle", "skip_reason", "seed", "jitter"}
        rec["params"] = _jsonable({k: v for k, v in d.items() if k not in skip})
        out.append(rec)
    applied = [r for r in out if r["applied"]]
    bb = ctx.solid.bounding_box()
    return dict(
        condition=_condition_of([r for r in applied]) if plan else "perfect",
        requested_condition=condition,
        seed=seed,
        gear=_jsonable(ctx.spec.to_dict()),
        defects=out,
        defect_types=sorted({r["type"] for r in applied}),
        damaged_teeth=sorted({i for r in applied for i in r.get("teeth", [])
                              if r["type"] not in ("wear", "tooth_thickness_error") or len(r.get("teeth", [])) < ctx.spec.teeth}),
        volume_mm3=round(ctx.solid.volume, 2),
        bounding_box_mm=dict(min=_jsonable(tuple(bb.min)), max=_jsonable(tuple(bb.max))),
        conventions=dict(
            units="mm", axis="+Z", body_z=[0, ctx.b], hub_z=[ctx.zmin, 0] if ctx.zmin < 0 else None,
            tooth_numbering="tooth 0 centred on +X, numbered counter-clockwise seen from +Z",
            drive_flank="CCW flank (+1)" if ctx.spec.drive_side > 0 else "CW flank (-1)",
            print_orientation="bottom face (z=0 / hub end) on the bed; face defects are on the top face",
        ),
    )


def export(solid, label, out_dir: Path, name: str, stl=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    solid.label = name
    step_path = out_dir / f"{name}.step"
    bd.export_step(solid, str(step_path))
    files = {"step": step_path.name}
    if stl:
        stl_path = out_dir / f"{name}.stl"
        bd.export_stl(solid, str(stl_path), tolerance=0.01, angular_tolerance=0.1)
        files["stl"] = stl_path.name
    label = dict(name=name, files=files, **label)
    (out_dir / f"{name}.json").write_text(json.dumps(label, indent=2), encoding="utf-8")
    return label
