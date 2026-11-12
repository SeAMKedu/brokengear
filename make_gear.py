#!/usr/bin/env python3
"""Generate perfect / defective / broken spur gears as STEP files with JSON ground truth.

Examples
--------
  # one perfect gear, module 2, 24 teeth, 8 mm thick, 8 mm bore
  python make_gear.py one --module 2 --teeth 24 --width 8 --bore 8 --condition perfect

  # size by outside diameter instead of module
  python make_gear.py one --od 50 --teeth 20 --condition combined --seed 3

  # hand-picked defects
  python make_gear.py one --teeth 30 --defect broken_tooth:teeth=4 \
        --defect "pitting:teeth=10,11;severity=0.9" --defect eccentric_bore:severity=0.5

  # a dataset: 40 gears mixed across conditions
  python make_gear.py batch --count 40 --out dataset --mix perfect=1,manufacturing=1,in_use=1,combined=1

  # the same dataset as USD for Omniverse / Isaac Sim Replicator (per-defect meshes + labels)
  python make_gear.py batch --count 40 --out dataset --usd

  # list the defect catalog
  python make_gear.py list
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brokengear.build import build_gear, export  # noqa: E402
from brokengear.cli import (add_gear_args, only_override, parse_defect, parse_range,  # noqa: E402
                            spec_from_args)
from brokengear.defects import CATALOG  # noqa: E402
from brokengear.model import CONDITIONS  # noqa: E402


def cmd_one(a):
    spec = spec_from_args(a)
    defects = [parse_defect(d) for d in a.defect] if a.defect else None
    solid, label = build_gear(spec, a.condition, a.seed, defects=defects, severity=a.severity,
                              severity_range=parse_range(a.severity_range),
                              weights_override=only_override(a.only))
    name = a.name or f"gear_m{spec.module:g}_z{spec.teeth}_{label['condition']}_s{a.seed}"
    label = export(solid, label, Path(a.out), name, stl=a.stl, usd=a.usd, spec=spec,
                   usd_physics=a.usd_physics, usd_max_error=a.usd_max_error)
    print_summary(label, Path(a.out))


def print_summary(label, out):
    print(f"{out / label['files']['step']}  [{label['condition']}]")
    for d in label["defects"]:
        flag = "" if d["applied"] else f"  (SKIPPED: {d.get('skip_reason')})"
        cause = f"  <- {d['caused_by']}" if d.get("caused_by") else ""
        teeth = f" teeth={d['teeth']}" if d.get("teeth") and len(d["teeth"]) <= 8 else (
            f" teeth=[{len(d['teeth'])} teeth]" if d.get("teeth") else "")
        print(f"   {d['id']:>3} {d['category'][:4]} {d['type']:<22} s={d['severity']:.2f}{teeth}{cause}{flag}")


def cmd_batch(a):
    out = Path(a.out)
    mix = {}
    for part in a.mix.split(","):
        k, _, v = part.partition("=")
        if k not in CONDITIONS:
            raise SystemExit(f"bad condition '{k}' in --mix")
        mix[k] = float(v or 1)
    rng = random.Random(a.seed)
    defects = [parse_defect(d) for d in a.defect] if a.defect else None
    rows = []
    for k in range(a.count):
        tot = sum(mix.values())
        x, cond = rng.random() * tot, None
        for c, w in mix.items():
            x -= w
            if x <= 0:
                cond = c
                break
        cond = cond or list(mix)[-1]
        if a.vary_size:
            a.teeth = rng.randint(a.teeth_min, a.teeth_max)
        spec = spec_from_args(a)
        seed = rng.randint(0, 10**9)
        name = f"{a.prefix}{k:04d}"
        try:
            solid, label = build_gear(spec, cond, seed, severity=a.severity,
                                      severity_range=parse_range(a.severity_range),
                                      weights_override=only_override(a.only), defects=defects)
        except Exception as e:  # never abort a long batch on one bad sample
            print(f"{name}: FAILED {e}", file=sys.stderr)
            continue
        label = export(solid, label, out, name, stl=a.stl, usd=a.usd, spec=spec,
                       usd_physics=a.usd_physics, usd_max_error=a.usd_max_error)
        print_summary(label, out)
        rows.append(dict(
            name=name, condition=label["condition"], seed=seed, teeth=spec.teeth,
            module=round(spec.module, 4), defects=" ".join(label["defect_types"]),
            damaged_teeth=" ".join(map(str, label["damaged_teeth"])),
        ))
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["name"])
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} gears -> {out}/ (manifest.csv)")


def cmd_list(a):
    for cat in ("manufacturing", "in_use"):
        print(f"\n== {cat} ==")
        for n, t in CATALOG.items():
            if t.category == cat:
                w = ", ".join(f"{k}={v}" for k, v in t.weight.items())
                print(f"  {n:<22} [{t.phase}] {t.description}\n  {'':<22} cause: {t.mechanism}\n  {'':<22} weight: {w}")
    if a.json:
        print(json.dumps({n: dict(category=t.category, phase=t.phase, description=t.description,
                                  mechanism=t.mechanism, weight=t.weight) for n, t in CATALOG.items()}, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("one", help="generate one gear")
    add_gear_args(one)
    one.add_argument("--condition", choices=CONDITIONS, default="perfect")
    one.add_argument("--seed", type=int, default=0)
    one.add_argument("--name", help="output file stem")
    one.set_defaults(func=cmd_one)

    b = sub.add_parser("batch", help="generate a labelled dataset")
    add_gear_args(b)
    b.add_argument("--count", type=int, default=20)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--mix", default="perfect=1,manufacturing=1,in_use=1,combined=1",
                   help="relative share of each condition")
    b.add_argument("--vary-size", action="store_true", help="randomise tooth count per gear")
    b.add_argument("--teeth-min", type=int, default=16)
    b.add_argument("--teeth-max", type=int, default=40)
    b.add_argument("--prefix", default="gear_")
    b.set_defaults(func=cmd_batch)

    ls = sub.add_parser("list", help="show the defect catalog")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
