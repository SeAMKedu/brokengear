"""Breakage model: decides *which* defects a gear gets, how severe, and where.

Four conditions
  perfect        - nominal gear
  manufacturing  - 1..3 defects from production (process depends on material)
  in_use         - 1..3 service damages, with damage chains (one failure leading to another)
  combined       - manufacturing defects that *cause* in-use damage at a related
                   location (eccentric bore -> localised wear, weld line -> crack, ...)
                   plus occasional independent damage

Relative frequencies come from CATALOG[...].weight[material]. They are
approximations drawn from the failure literature (tooth breakage is the most
common in-service failure of steel gears, followed by surface fatigue and wear;
plastic gears fail mostly by wear, thermal damage and root fatigue). Tune them
with ``weights_override`` for your test plan.
"""

from __future__ import annotations

import math
import random

from .defects import CATALOG

CONDITIONS = ("perfect", "manufacturing", "in_use", "combined")

PHASE_ORDER = ("profile", "solid", "add", "bore", "cut", "post")


# ---------------------------------------------------------------- causal links
def _hint_far_side(ctx, inst):
    """Teeth farthest from the (eccentric) rotation axis carry the most load."""
    a = math.radians(inst["direction_deg"]) + math.pi
    z = ctx.spec.teeth
    c = ctx.nearest_tooth(a)
    span = max(1, z // 8)
    return {"teeth": [(c + k) % z for k in range(-span, span + 1)]}


def _hint_same_teeth(ctx, inst):
    return {"teeth": list(inst.get("teeth", []))}


def _hint_angle(ctx, inst):
    if "angle" in inst:
        a = inst["angle"]
    else:
        a = math.radians(inst.get("angle_deg", 0.0))
    return {"angle": a, "teeth": [ctx.nearest_tooth(a)]}


def _hint_top_end(ctx, inst):
    return {"end": "top"}


# manufacturing defect -> [(in-use defect, probability, hint function, note)]
LINKS = {
    "eccentric_bore": [
        ("wear", 0.55, _hint_far_side, "runout overloads the teeth on the far side of the axis"),
        ("pitting", 0.35, _hint_far_side, "runout raises contact stress on the far-side teeth"),
    ],
    "tilted_bore": [
        ("tooth_end_break", 0.45, None, "lead error from the tilted axis edge-loads one tooth end"),
        ("pitting", 0.35, _hint_top_end, "edge loading concentrates pitting at one end of the face"),
        ("chipped_tip", 0.3, _hint_top_end, "edge loading chips tooth corners"),
    ],
    "face_wedge": [
        ("tooth_end_break", 0.3, None, "uneven face loads one end of the teeth"),
        ("chipped_tip", 0.3, _hint_top_end, "edge loading chips tooth corners"),
    ],
    "tooth_thickness_error": [
        ("broken_tooth", 0.5, _hint_same_teeth, "thin tooth has reduced bending strength"),
    ],
    "short_shot": [
        ("broken_tooth", 0.4, _hint_same_teeth, "incompletely filled tooth is weak"),
        ("chipped_tip", 0.3, None, "mesh impacts at the short teeth"),
    ],
    "pitch_error": [
        ("chipped_tip", 0.45, _hint_same_teeth, "mis-indexed tooth takes impact load"),
        ("broken_tooth", 0.35, _hint_same_teeth, "mis-indexed tooth is overloaded"),
    ],
    "weld_line": [
        ("rim_crack", 0.45, _hint_angle, "crack follows the weak weld plane"),
        ("hub_crack", 0.3, _hint_angle, "crack follows the weak weld plane"),
    ],
    "porosity": [
        ("root_crack", 0.35, _hint_angle, "voids initiate fatigue cracks"),
        ("rim_crack", 0.25, _hint_angle, "voids initiate fatigue cracks"),
    ],
    "sink_mark": [
        ("hub_crack", 0.2, _hint_angle, "internal void under the sink mark weakens the web"),
    ],
    "bore_size_error": [
        ("hub_crack", 0.4, None, "undersize bore -> press fit hoop stress (oversize -> fretting)"),
        ("bore_wear", 0.4, None, "oversize bore lets the gear fret on the shaft"),
    ],
    "missing_keyway": [
        ("bore_wear", 0.6, None, "without a key the gear slips and wears its bore"),
    ],
}

# in-use damage -> secondary damage
CHAINS = {
    "broken_tooth": [("chipped_tip", 0.4, None, "fragments pass through the mesh"),
                     ("missing_teeth", 0.15, None, "load jumps to the next tooth")],
    "missing_teeth": [("chipped_tip", 0.45, None, "impact when the mesh skips the gap")],
    "tooth_tip_break": [("chipped_tip", 0.3, None, "fragments pass through the mesh")],
    "wear": [("plastic_deformation", 0.3, None, "thinned teeth yield"),
             ("pitting", 0.25, None, "surface fatigue on worn flanks")],
    "pitting": [("wear", 0.2, None, "spalled particles act as abrasive")],
    "thermal_melt": [("wear", 0.4, None, "softened flanks wear fast")],
}


def _incompatible(name, chosen, ctx):
    t = CATALOG[name]
    if "bore" in t.needs and ctx.spec.bore is None:
        return True
    if "keyway" in t.needs and not ctx.spec.keyway:
        return True
    if t.group and any(CATALOG[c].group == t.group for c in chosen):
        return True
    no_hole = {"missing_bore"} & set(chosen) or (name == "missing_bore" and set(chosen) & {
        "bore_wear", "hub_crack", "bore_flash", "keyway_damage", "missing_keyway"})
    if no_hole and name in ("bore_wear", "hub_crack", "bore_flash", "keyway_damage", "missing_keyway",
                            "missing_bore"):
        return True
    if name == "bore_flash" and "blind_bore" in chosen or name == "blind_bore" and "bore_flash" in chosen:
        return True
    if name in chosen and name not in ("chipped_tip", "pitting", "burr", "porosity"):
        return True
    return False


def _weights(category, material, chosen, ctx, override):
    out = {}
    for n, t in CATALOG.items():
        if t.category != category or _incompatible(n, chosen, ctx):
            continue
        w = (override or {}).get(n, t.weight.get(material, 0.0))
        if w > 0:
            out[n] = w
    return out


def _choose(rng, weights):
    tot = sum(weights.values())
    x = rng.random() * tot
    for n, w in weights.items():
        x -= w
        if x <= 0:
            return n
    return next(iter(weights))


def _count(rng, table):
    return _choose(rng, {k: v for k, v in table})


class Planner:
    def __init__(self, ctx, rng, severity=None, weights_override=None, severity_range=None):
        self.ctx, self.rng = ctx, rng
        self.severity = severity
        self.severity_range = severity_range
        self.override = weights_override
        self.plan: list[dict] = []

    @property
    def chosen(self):
        return [p["type"] for p in self.plan]

    def _sev(self):
        if self.severity is not None:
            return self.severity
        lo, hi = self.severity_range or (0.25, 1.0)
        return round(self.rng.uniform(lo, hi), 2)

    def add(self, name, hint=None, cause=None, note=None, severity=None, params=None):
        if _incompatible(name, self.chosen, self.ctx):
            return None
        if self.override and self.override.get(name, None) == 0:
            return None  # type excluded (e.g. --only)
        s = severity if severity is not None else self._sev()
        self.ctx.current = name  # lets ctx.pick_teeth know who is claiming teeth
        try:
            p = CATALOG[name].sample(self.ctx, self.rng, s, hint)
        except Exception:
            p = None
        if p is None:
            return None
        if params:
            p.update(params)
        inst = dict(id=f"d{len(self.plan) + 1}", type=name, category=CATALOG[name].category,
                    severity=s, cause=cause, cause_note=note, **p)
        self.plan.append(inst)
        return inst

    def add_random(self, category, n):
        for _ in range(n):
            w = _weights(category, self.ctx.spec.material, self.chosen, self.ctx, self.override)
            if not w:
                return
            for _try in range(5):
                if self.add(_choose(self.rng, w)):
                    break

    def follow_links(self, table, sources):
        for src in list(sources):
            for name, prob, hint_fn, note in table.get(src["type"], []):
                if self.rng.random() >= prob:
                    continue
                if src["type"] == "tooth_thickness_error" and src.get("scale", 1) >= 1:
                    continue
                if src["type"] == "bore_size_error":
                    under = src["delta_diameter"] < 0
                    if (name == "hub_crack") != under:
                        continue
                if name == "chipped_tip" and hint_fn is None and src.get("teeth"):
                    z = self.ctx.spec.teeth
                    t0, t1 = src["teeth"][0], src["teeth"][-1]
                    hint = {"teeth": [(t1 + 1) % z, (t0 - 1) % z]}
                else:
                    hint = hint_fn(self.ctx, src) if hint_fn else None
                new = self.add(name, hint=hint, cause=src["id"], note=note)
                if new:
                    break  # at most one consequence per source


def sample_defects(ctx, condition, rng: random.Random, severity=None, weights_override=None,
                   severity_range=None):
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}")
    P = Planner(ctx, rng, severity, weights_override, severity_range)
    if condition == "perfect":
        return []
    if condition == "manufacturing":
        P.add_random("manufacturing", _count(rng, [(1, 0.55), (2, 0.3), (3, 0.15)]))
    elif condition == "in_use":
        P.add_random("in_use", _count(rng, [(1, 0.5), (2, 0.35), (3, 0.15)]))
        P.follow_links(CHAINS, [p for p in P.plan])
    else:  # combined
        P.add_random("manufacturing", _count(rng, [(1, 0.6), (2, 0.4)]))
        mfg = list(P.plan)
        P.follow_links(LINKS, mfg)
        if not any(p["category"] == "in_use" for p in P.plan) or rng.random() < 0.3:
            P.add_random("in_use", 1)
        P.follow_links(CHAINS, [p for p in P.plan if p["category"] == "in_use"])
    return P.plan


def explicit_defects(ctx, specs, rng, severity=None, severity_range=None):
    """specs: list of (name, dict) e.g. ("broken_tooth", {"teeth": [3], "severity": 0.8})."""
    P = Planner(ctx, rng, severity, severity_range=severity_range)
    for name, kw in specs:
        if name not in CATALOG:
            raise ValueError(f"unknown defect '{name}'. Known: {', '.join(sorted(CATALOG))}")
        kw = dict(kw)
        s = kw.pop("severity", None)
        hint = None
        if "teeth" in kw:
            hint = {"teeth": kw.pop("teeth")}
        if "angle_deg" in kw:
            a = math.radians(kw.pop("angle_deg"))
            hint = dict(hint or {}, angle=a, teeth=(hint or {}).get("teeth", [ctx.nearest_tooth(a)]))
        if "end" in kw:
            hint = dict(hint or {}, end=kw.pop("end"))
        if "sign" in kw:
            hint = dict(hint or {}, sign=kw.pop("sign"))
        if P.add(name, hint=hint, severity=s, params=kw) is None:
            raise ValueError(f"could not place defect '{name}' (conflict or no free teeth)")
    return P.plan
