#!/usr/bin/env python3
"""Fill a print bed with a labelled test set of gears (perfect, defective, broken).

Each gear gets an ID engraved on its BOTTOM face (mirrored, so it reads
correctly when the gear is flipped over) and an arrow pointing at tooth 0.
The plate map (SVG), catalog (Markdown + CSV) and per-gear JSON link each ID
to its ground truth.

Default: Bambu Lab H2D, using the 300 x 320 mm zone that both nozzles reach,
so the plate prints in any nozzle mode.

  ./make_plate.sh --out plate_h2d

Takes the same gear and defect options as make_gear.py. A size option you give
is fixed for the whole plate; one you leave out varies per gear. Example: fixed
45 mm / 24-tooth gears, in-use damage only, severity stepping 0.2 -> 1.0:

  ./make_plate.sh --od 45 --teeth 24 --bore 8 --condition in_use --perfect 1 --severity-range 0.2,1.0 --out plate_in_use

Add --usd for gears/<ID>.usd and a plate.usd that places them (Omniverse / Isaac Sim).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build123d as bd  # noqa: E402

from brokengear.build import build_gear, plan_only  # noqa: E402
from brokengear.usd import export_plate_usd, export_usd  # noqa: E402
from brokengear.cli import add_gear_args, only_override, parse_defect, parse_range, spec_kwargs  # noqa: E402
from brokengear.defects import CATALOG  # noqa: E402
from brokengear.gear import GearSpec  # noqa: E402
from brokengear.model import CONDITIONS  # noqa: E402

SHORT = {  # compact names for the plate map
    "broken_tooth": "broken tooth", "tooth_tip_break": "tip break", "missing_teeth": "missing teeth",
    "wear": "wear", "plastic_deformation": "bent tips", "thermal_melt": "melted teeth",
    "short_shot": "short shot", "pitch_error": "pitch err", "tooth_thickness_error": "thickness err",
    "face_wedge": "face wedge", "burr": "burr", "flash": "flash", "missing_bore": "no bore",
    "blind_bore": "blind bore", "eccentric_bore": "eccentric bore", "tilted_bore": "tilted bore",
    "bore_size_error": "bore size", "bore_wear": "oval bore", "missing_keyway": "no keyway",
    "keyway_damage": "keyway dmg", "chipped_tip": "chipped tip", "tooth_end_break": "end break",
    "pitting": "pitting", "root_crack": "root crack", "rim_crack": "rim crack", "hub_crack": "hub crack",
    "weld_line": "weld line", "sink_mark": "sink mark", "porosity": "porosity", "bore_flash": "bore flash",
}


def hex_layout(bed_w, bed_h, d, gap):
    """Centres of a hex-packed grid of discs of diameter d inside bed_w x bed_h."""
    p = d + gap
    dy = p * math.sqrt(3) / 2
    rows = int((bed_h - d) // dy) + 1
    pts = []
    for r in range(rows):
        off = (p / 2) if r % 2 else 0.0
        cols = int((bed_w - d - off) // p) + 1
        for c in range(cols):
            pts.append((d / 2 + off + c * p, d / 2 + r * dy))
    # centre the block on the bed
    if pts:
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        ox = (bed_w - (max(xs) - min(xs))) / 2 - min(xs)
        oy = (bed_h - (max(ys) - min(ys))) / 2 - min(ys)
        pts = [(x + ox, y + oy) for x, y in pts]
    # order: top row first, left to right (as you look at the plate from the front)
    pts.sort(key=lambda p_: (-round(p_[1], 3), p_[0]))
    return pts


def plate_spec(rng, a):
    """GearSpec for one slot: options given on the command line are fixed, the rest vary."""
    kw = spec_kwargs(a)
    m_lo, m_hi = a.module_min, a.module_max

    def z_range(od_lo, od_hi, m1, m2):
        lo = max(10, math.ceil(od_lo / m2 - 2))
        return lo, max(lo, math.floor(od_hi / m1 - 2))

    if a.od:
        z = a.teeth or rng.randint(*z_range(a.od, a.od, m_lo, m_hi))
        spec_size = dict(od=a.od, z=z)
    elif a.module:
        z = a.teeth or rng.randint(*z_range(a.od_min, a.od_max, a.module, a.module))
        spec_size = dict(m=a.module, z=z)
    elif a.teeth:
        spec_size = dict(od=round(rng.uniform(a.od_min, a.od_max), 1), z=a.teeth)
    else:
        od = round(rng.uniform(a.od_min, a.od_max), 1)
        spec_size = dict(od=od, z=rng.randint(*z_range(od, od, m_lo, m_hi)))
    # draw order (material, bore, keyway) is kept so existing seeds reproduce earlier plates
    if a.material is None:
        kw["material"] = "plastic" if rng.random() < a.plastic_share else "steel"
    if "bore" not in kw:
        kw["bore"] = rng.choice(a.bores)
    if "keyway" not in kw:
        kw["keyway"] = rng.random() < a.keyway_share and bool(kw["bore"])
    if "od" in spec_size:
        return GearSpec.from_outside_diameter(spec_size["od"], spec_size["z"], **kw)
    return GearSpec(module=spec_size["m"], teeth=spec_size["z"], **kw)


def slot_diameter(a):
    """Largest outside diameter a slot must hold."""
    if a.od:
        return a.od
    if a.module and a.teeth:
        return a.module * (a.teeth + 2)
    return a.od_max


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_gear_args(p, plate=True)
    v = p.add_argument_group("ranges used for options left out")
    v.add_argument("--od-min", type=float, default=40.0)
    v.add_argument("--od-max", type=float, default=50.0)
    v.add_argument("--module-min", type=float, default=1.5, help="keep defects printable")
    v.add_argument("--module-max", type=float, default=2.5)
    v.add_argument("--bores", default="6,8", help="bore diameters to choose from (when --bore is not given)")
    v.add_argument("--keyway-share", type=float, default=0.5, help="share with keyway (when --keyway not given)")
    v.add_argument("--plastic-share", type=float, default=0.6,
                   help="share using moulded-plastic statistics (when --material not given)")
    pl = p.add_argument_group("plate")
    pl.add_argument("--bed", default="300x320", help="usable bed WxH in mm (H2D both-nozzle zone 300x320)")
    pl.add_argument("--gap", type=float, default=4.0, help="min gap between gears (mm)")
    pl.add_argument("--count", type=int, help="number of gears (default: as many as fit)")
    pl.add_argument("--perfect", type=int, default=3, help="number of perfect gears")
    pl.add_argument("--condition", choices=[c for c in CONDITIONS if c != "perfect"],
                    help="make every non-perfect gear this condition (shortcut for --mix)")
    pl.add_argument("--mix", help="relative share of the defective conditions "
                                  "(default manufacturing=3,in_use=4,combined=4)")
    pl.add_argument("--severity-spread", choices=["even", "random"], default="even",
                    help="with --severity-range: 'even' gives each defective gear one severity, stepping "
                         "evenly from LO (first ID) to HI (last ID); 'random' draws per defect")
    pl.add_argument("--seed", type=int, default=2026)
    pl.add_argument("--prefix", default="", help="ID prefix, e.g. 'A' -> A01, A02, ...")
    a = p.parse_args(argv)
    a.bores = [float(x) for x in a.bores.split(",")]
    bed_w, bed_h = (float(x) for x in a.bed.lower().split("x"))
    sev_range = parse_range(a.severity_range)
    override = only_override(a.only)
    defects = [parse_defect(d) for d in a.defect] if a.defect else None

    if a.mix:
        mix = {k: float(v) for k, v in (x.split("=") for x in a.mix.split(","))}
    elif a.condition:
        mix = {a.condition: 1.0}
    else:
        mix = {"manufacturing": 3.0, "in_use": 4.0, "combined": 4.0}
    if override and not defects:
        for c in mix:
            cats = {"manufacturing": ["manufacturing"], "in_use": ["in_use"],
                    "combined": ["manufacturing", "in_use"]}[c]
            if not any(CATALOG[n].category in cats and override.get(n, 1) != 0 for n in CATALOG):
                raise SystemExit(f"--only leaves no defect types for condition '{c}'")

    d_slot = slot_diameter(a)
    slots = hex_layout(bed_w, bed_h, d_slot, a.gap)
    if not slots:
        raise SystemExit(f"a {d_slot:g} mm gear does not fit on a {a.bed} bed")
    n = min(a.count or len(slots), len(slots))
    slots = slots[:n]
    rng = random.Random(a.seed)

    # ---- condition per slot: perfect ones first, the rest by the mix
    perfect = min(a.perfect, n)
    rest = n - perfect
    tot = sum(mix.values())
    conds = []
    for k, w in mix.items():
        conds += [k] * round(rest * w / tot)
    while len(conds) < rest:
        conds.append(max(mix, key=mix.get))
    conds = ["perfect"] * perfect + conds[:rest]
    order = list(range(n))
    rng.shuffle(order)  # scatter conditions over the plate
    slot_cond = {order[i]: conds[i] for i in range(n)}

    # ---- severity per slot
    defective = [i for i in range(n) if slot_cond[i] != "perfect"]
    slot_sev = {i: a.severity for i in range(n)}
    if a.severity is None and sev_range and a.severity_spread == "even":
        lo, hi = sev_range
        for k, i in enumerate(defective):
            slot_sev[i] = round(lo + (hi - lo) * (k / max(1, len(defective) - 1)), 3)

    # ---- greedy coverage: for each defective slot try candidates, keep the one adding most new types
    covered: set[str] = set()
    chosen = {}
    for i in range(n):
        c = slot_cond[i]
        if c == "perfect":
            chosen[i] = (plate_spec(rng, a), c, rng.randint(0, 10**9))
            continue
        best = None
        for _ in range(40):
            spec = plate_spec(rng, a)
            seed = rng.randint(0, 10**9)
            try:
                plan = plan_only(spec, c, seed, severity=slot_sev[i], weights_override=override,
                                 severity_range=sev_range, defects=defects)
            except Exception:
                continue
            types = {d["type"] for d in plan}
            score = (len(types - covered) * 10 - 1.5 * max(0, len(types) - 3) + rng.random()
                     - (100 if not types else 0))
            if best is None or score > best[0]:
                best = (score, spec, seed, types)
        if best is None:
            raise SystemExit("could not place the requested defects on these gears")
        _, spec, seed, types = best
        covered |= types
        chosen[i] = (spec, c, seed)

    out = Path(a.out)
    (out / "gears").mkdir(parents=True, exist_ok=True)
    rows, placed, labels, usd_gears = [], [], [], []
    width = max(2, len(str(n)))
    for i, (x, y) in enumerate(slots):
        spec, cond, seed = chosen[i]
        gid = f"{a.prefix}{i + 1:0{width}d}"
        if cond == "perfect":
            solid, label = build_gear(spec, "perfect", seed, marking=gid)
        else:
            solid, label = build_gear(spec, cond, seed, marking=gid, severity=slot_sev[i],
                                      weights_override=override, severity_range=sev_range,
                                      defects=defects)
        solid.label = gid
        bd.export_step(solid, str(out / "gears" / f"{gid}.step"))
        if a.stl:
            bd.export_stl(solid, str(out / "gears" / f"{gid}.stl"), tolerance=0.01, angular_tolerance=0.1)
        label = dict(id=gid, plate_position_mm=[round(x, 2), round(y, 2)], seed=seed, **{
            k: v for k, v in label.items() if k != "seed"})
        if a.usd:
            label["files"] = dict(step=f"{gid}.step", usd=f"{gid}.usd")
        (out / "gears" / f"{gid}.json").write_text(json.dumps(label, indent=2), encoding="utf-8")
        if a.usd:
            # twins without the ID engraving: perfect (finds defects) and this gear (finds the marking)
            perfect = build_gear(spec, "perfect", seed)[0]
            unmarked = perfect if cond == "perfect" else build_gear(
                spec, cond, seed, severity=slot_sev[i], weights_override=override,
                severity_range=sev_range, defects=defects)[0]
            export_usd(out / "gears" / f"{gid}.usd", solid, label, spec, perfect=perfect,
                       unmarked=unmarked, physics=a.usd_physics, max_err=a.usd_max_error)
            usd_gears.append((gid, f"./gears/{gid}.usd", (x, y)))
        moved = bd.Pos(x, y, 0) * solid
        moved.label = gid
        placed.append(moved)
        labels.append(label)
        ap = [d for d in label["defects"] if d["applied"]]
        rows.append(dict(
            id=gid, condition=label["condition"],
            severity=max((d["severity"] for d in ap), default=""),
            outside_diameter=round(spec.module * (spec.teeth + 2), 2), teeth=spec.teeth,
            module=round(spec.module, 3), thickness=spec.face_width, bore=spec.bore, keyway=spec.keyway,
            process="moulded plastic" if spec.material == "plastic" else "machined steel",
            defects="; ".join(_describe(d) for d in ap) or "none",
            x_mm=round(x, 1), y_mm=round(y, 1),
        ))
        sv = f"s={rows[-1]['severity']:.2f}" if ap else "      "
        print(f"{gid}  {label['condition']:<13} {sv}  {rows[-1]['defects']}")

    # ---- plate files
    plate = bd.Compound(children=placed)
    plate.label = "plate"
    bd.export_step(plate, str(out / "plate.step"))
    mesher = bd.Mesher()
    for s in placed:
        mesher.add_shape(s, linear_deflection=0.01, angular_deflection=0.15, part_number=s.label)
    mesher.write(str(out / "plate.3mf"))
    if a.usd:
        export_plate_usd(out / "plate.usd", usd_gears)

    with open(out / "catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    write_markdown(out / "catalog.md", rows, a, bed_w, bed_h)
    write_svg(out / "plate_map.svg", rows, labels, bed_w, bed_h)

    cats = set()
    for c in mix:
        cats |= {"combined": {"manufacturing", "in_use"}}.get(c, {c})
    allowed = {n_ for n_, t in CATALOG.items()
               if t.category in cats and (not override or override.get(n_, 1) != 0)
               and not ("keyway" in t.needs and not any(r["keyway"] for r in rows))
               and not ("bore" in t.needs and not any(r["bore"] for r in rows))}
    missing = sorted(allowed - covered) if not defects else []
    print(f"\n{n} gears, {len(covered)} defect types on the plate"
          + (f"; allowed but not on this plate: {', '.join(missing)}" if missing else ""))
    print(f"-> {out}/plate.3mf, plate.step, " + ("plate.usd, " if a.usd else "")
          + "plate_map.svg, catalog.md, catalog.csv, gears/")


def _describe(d):
    s = SHORT.get(d["type"], d["type"])
    if d.get("teeth") and len(d["teeth"]) <= 6:
        s += " (t" + ",".join(map(str, d["teeth"])) + ")"
    elif d.get("teeth"):
        s += " (all)" if len(d["teeth"]) > 12 else f" ({len(d['teeth'])} teeth)"
    if d.get("caused_by"):
        s += f" <-{d['caused_by']}"
    return f"{d['id']} {s}"


def write_markdown(path, rows, a, bed_w, bed_h):
    lines = [
        "# Gear test plate",
        "",
        f"{len(rows)} gears, OD {min(r['outside_diameter'] for r in rows):g}–"
        f"{max(r['outside_diameter'] for r in rows):g} mm, {a.width:g} mm thick, "
        f"laid out on a {bed_w:g} x {bed_h:g} mm area. Seed {a.seed}.",
        "",
        "**Identifying a gear:** flip it over. The engraved number is the ID in the table below. "
        "The arrow points at **tooth 0**. Teeth are numbered counter-clockwise **as seen from the top** "
        "(so clockwise when you look at the bottom). `t3,4` means teeth 3 and 4. `<-d1` means the damage "
        "was caused by defect d1 of the same gear. Full details are in `gears/<ID>.json`.",
        "",
        "| ID | Condition | Severity | OD / teeth | Bore | Process | Defects |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        bore = f"{r['bore']:g}{' +key' if r['keyway'] else ''}" if r["bore"] else "-"
        sev = f"{r['severity']:.2f}" if r["severity"] != "" else "-"
        lines.append(f"| **{r['id']}** | {r['condition']} | {sev} | {r['outside_diameter']:g} / {r['teeth']} | {bore} | "
                     f"{r['process']} | {r['defects']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_svg(path, rows, labels, bed_w, bed_h):
    S = 3.0  # px per mm
    pad = 20
    W, H = bed_w * S + 2 * pad, bed_h * S + 2 * pad + 40
    colors = {"perfect": "#2e7d32", "manufacturing": "#1565c0", "in_use": "#c62828", "combined": "#6a1b9a"}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
           f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="Helvetica,Arial,sans-serif">',
           f'<rect width="100%" height="100%" fill="white"/>',
           f'<rect x="{pad}" y="{pad}" width="{bed_w * S}" height="{bed_h * S}" fill="#f4f4f4" stroke="#999"/>',
           f'<text x="{pad}" y="{H - 22}" font-size="13">Top view of the plate as placed (front of printer at the '
           f'bottom). Arrow = tooth 0. Colour = condition:</text>']
    lx = pad
    for k, c in colors.items():
        out.append(f'<rect x="{lx}" y="{H - 14}" width="10" height="10" fill="{c}"/>'
                   f'<text x="{lx + 14}" y="{H - 5}" font-size="12">{k}</text>')
        lx += 130
    for r, lab in zip(rows, labels):
        x = pad + r["x_mm"] * S
        y = pad + (bed_h - r["y_mm"]) * S
        R = r["outside_diameter"] / 2 * S
        c = colors.get(r["condition"], "#333")
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{R:.1f}" fill="white" stroke="{c}" stroke-width="3"/>')
        out.append(f'<path d="M{x + R * 0.95:.1f},{y:.1f} l{-R * 0.16:.1f},{-R * 0.08:.1f} '
                   f'l0,{R * 0.16:.1f} z" fill="{c}"/>')
        out.append(f'<text x="{x:.1f}" y="{y - R * 0.35:.1f}" font-size="20" font-weight="bold" '
                   f'text-anchor="middle" fill="{c}">{r["id"]}</text>')
        types = list(dict.fromkeys(SHORT.get(d["type"], d["type"]) for d in lab["defects"] if d["applied"]))
        types = types or ["perfect"]
        if r["severity"] != "":
            types = [f"s = {r['severity']:.2f}"] + types
        for k, t in enumerate(types[:5]):
            out.append(f'<text x="{x:.1f}" y="{y - R * 0.05 + k * 13:.1f}" font-size="11" '
                       f'text-anchor="middle">{t}</text>')
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    main()
