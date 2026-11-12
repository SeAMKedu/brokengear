"""Command-line options shared by make_gear.py and make_plate.py."""

from __future__ import annotations

import argparse

from .defects import CATALOG
from .gear import GearSpec


def parse_value(v: str):
    if "," in v:
        return [parse_value(x) for x in v.split(",") if x != ""]
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def parse_defect(text: str):
    """'name' or 'name:key=val;key=v1,v2'  (teeth lists use commas, params use ';')."""
    name, _, rest = text.partition(":")
    name = name.strip()
    if name not in CATALOG:
        raise SystemExit(f"unknown defect '{name}'. Run './make_gear.sh list' for the catalog.")
    params = {}
    if rest:
        for part in rest.split(";"):
            if not part.strip():
                continue
            k, _, v = part.partition("=")
            val = parse_value(v.strip())
            if k.strip() == "teeth" and not isinstance(val, list):
                val = [val]
            params[k.strip()] = val
    return name, params


def parse_range(text: str | None):
    if not text:
        return None
    lo, _, hi = text.partition(",")
    lo, hi = float(lo), float(hi or lo)
    if not (0 <= lo <= hi <= 1):
        raise SystemExit("--severity-range must be 'lo,hi' with 0 <= lo <= hi <= 1")
    return (lo, hi)


def only_override(text: str | None):
    """--only a,b,c  ->  weights_override that disables every other defect type."""
    if not text:
        return None
    keep = set()
    for name in (x.strip() for x in text.split(",") if x.strip()):
        if name in ("manufacturing", "in_use"):
            keep |= {n for n, t in CATALOG.items() if t.category == name}
        elif name in CATALOG:
            keep.add(name)
        else:
            raise SystemExit(f"--only: unknown defect '{name}'")
    return {n: 0.0 for n in CATALOG if n not in keep}


def add_gear_args(p: argparse.ArgumentParser, plate: bool = False):
    """Gear-size, defect and output options.

    In make_plate (plate=True) every size option defaults to None, meaning
    "vary it per gear"; giving the option fixes it for the whole plate.
    """
    d = (lambda single, plate_default=None: plate_default if plate else single)
    g = p.add_argument_group("gear size" + (" (omit to vary per gear)" if plate else ""))
    g.add_argument("--module", type=float, default=d(2.0), help="module m in mm")
    g.add_argument("--od", type=float, help="outside (tip) diameter; overrides --module")
    g.add_argument("--teeth", type=int, default=d(24))
    g.add_argument("--width", "--thickness", dest="width", type=float, default=d(8.0, 3.0),
                   help="face width / thickness (mm)")
    g.add_argument("--pressure-angle", type=float, default=20.0)
    g.add_argument("--bore", type=float, default=d(8.0), help="axle hole diameter, 0 = none")
    g.add_argument("--keyway", action="store_true", help="cut a keyway in the bore")
    g.add_argument("--keyway-width", type=float)
    g.add_argument("--keyway-depth", type=float)
    g.add_argument("--hub-diameter", type=float, help="add a hub below the gear")
    g.add_argument("--hub-height", type=float, default=5.0)
    g.add_argument("--material", choices=["plastic", "steel"], default=d("plastic"),
                   help="selects the manufacturing process / failure statistics "
                        "(plastic = injection moulded, steel = machined)")
    g.add_argument("--min-feature", type=float, default=0.4,
                   help="smallest defect feature in mm (printer nozzle / resolution)")
    g.add_argument("--drive-cw", action="store_true", help="loaded flank is the CW flank")

    f = p.add_argument_group("defects")
    f.add_argument("--severity", type=float, help="force severity 0..1 for all defects")
    f.add_argument("--severity-range", metavar="LO,HI",
                   help="draw severities from this range, e.g. 0.2,1.0"
                        + (" (spread evenly over the plate, see --severity-spread)" if plate else ""))
    f.add_argument("--only", metavar="TYPES",
                   help="restrict random defects to these types (comma list; 'in_use' or "
                        "'manufacturing' selects a whole category)")
    f.add_argument("--defect", action="append",
                   help="explicit defect 'name[:key=val;...]' (repeatable)"
                        + ("; applied to every non-perfect gear" if plate else "; overrides --condition"))

    o = p.add_argument_group("output")
    o.add_argument("--out", default="plate_h2d" if plate else "out", help="output folder")
    o.add_argument("--stl", action="store_true", help="also write STL")


def spec_kwargs(a) -> dict:
    """GearSpec keyword arguments that are fixed on the command line (None values dropped)."""
    kw = dict(
        face_width=a.width, pressure_angle=a.pressure_angle,
        keyway_width=a.keyway_width, keyway_depth=a.keyway_depth,
        hub_diameter=a.hub_diameter, hub_height=a.hub_height if a.hub_diameter else 0.0,
        material=a.material, min_feature=a.min_feature, drive_side=-1 if a.drive_cw else 1,
    )
    if a.bore is not None:
        kw["bore"] = None if a.bore <= 0 else a.bore
    if a.keyway:
        kw["keyway"] = True
    return {k: v for k, v in kw.items() if v is not None}


def spec_from_args(a) -> GearSpec:
    kw = spec_kwargs(a)
    if a.od:
        return GearSpec.from_outside_diameter(a.od, a.teeth, **kw)
    return GearSpec(module=a.module, teeth=a.teeth, **kw)
