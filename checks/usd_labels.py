"""Check the USD export's defect segmentation.

1. A perfect gear must come out with no defect mesh at all.
2. Every catalog defect, built alone, must get its own non-empty mesh.
3. Random combined gears: every applied defect should get a mesh, and little
   area should end up in ``defect_unattributed``.

Prints the changed area per defect so odd results stand out.
"""
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pxr import Usd, UsdGeom  # noqa: E402

from brokengear.build import build_gear  # noqa: E402
from brokengear.defects import CATALOG  # noqa: E402
from brokengear.gear import GearSpec  # noqa: E402
from brokengear.usd import export_usd  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/usd_check")
out.mkdir(parents=True, exist_ok=True)
n_random = int(sys.argv[2]) if len(sys.argv) > 2 else 12


def areas(path):
    """mesh prim name -> area in mm^2."""
    st = Usd.Stage.Open(str(path))
    res = {}
    for p in st.Traverse():
        if p.IsA(UsdGeom.Mesh):
            m = UsdGeom.Mesh(p)
            v = np.array(m.GetPointsAttr().Get()) * 1000
            t = np.array(m.GetFaceVertexIndicesAttr().Get()).reshape(-1, 3)
            res[p.GetName()] = 0.5 * np.linalg.norm(
                np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]]), axis=1).sum()
    return res


def run(name, spec, **kw):
    solid, label = build_gear(spec, **kw)
    label = dict(name=name, **label)
    path = out / f"{name}.usd"
    export_usd(path, solid, label, spec)
    return label, areas(path)


bad = 0
spec = GearSpec(module=2, teeth=24, face_width=8, bore=8, keyway=True)

label, a = run("perfect", spec, condition="perfect", seed=0)
extra = [k for k in a if k != "intact"]
print(f"{'OK ' if not extra else 'BAD'} perfect                meshes={sorted(a)}")
bad += bool(extra)

for name, t in CATALOG.items():
    mat = "steel" if t.weight.get("plastic", 0) == 0 else "plastic"
    s = GearSpec(module=2, teeth=24, face_width=8, bore=8, keyway=True, material=mat)
    t0 = time.time()
    label, a = run(f"one_{name}", s, defects=[(name, {})], severity=0.8, seed=1)
    d = label["defects"][0]
    if not d["applied"]:
        print(f"--- {name:<22} not applied: {d.get('skip_reason')}")
        continue
    mine = a.get(f"{d['id']}_{name}", 0.0)
    unatt = a.get("defect_unattributed", 0.0)
    ok = mine > 0 and unatt == 0
    bad += not ok
    print(f"{'OK ' if ok else 'BAD'} {name:<22} {time.time() - t0:4.1f}s defect area={mine:8.2f} mm2"
          + (f" unattributed={unatt:.2f}" if unatt else ""))

rng = random.Random(7)
tot_def = tot_un = 0.0
for k in range(n_random):
    s = GearSpec(module=2, teeth=rng.randint(16, 36), face_width=8, bore=8, keyway=rng.random() < 0.6,
                 material=rng.choice(["plastic", "steel"]))
    label, a = run(f"combined_{k:02d}", s, condition="combined", seed=rng.randint(0, 10**9))
    applied = [d for d in label["defects"] if d["applied"]]
    missing = [f"{d['id']}_{d['type']}" for d in applied if f"{d['id']}_{d['type']}" not in a]
    un = a.get("defect_unattributed", 0.0)
    dsum = sum(v for n, v in a.items() if n not in ("intact", "marking"))
    tot_def += dsum
    tot_un += un
    print(f"    combined_{k:02d} {', '.join(d['type'] for d in applied)}"
          + (f"\n        no mesh for: {missing}" if missing else "")
          + (f"\n        unattributed {un:.2f} of {dsum:.2f} mm2" if un else ""))
print(f"\nrandom gears: {tot_un:.1f} of {tot_def:.1f} mm2 defect area unattributed "
      f"({100 * tot_un / max(tot_def, 1e-9):.1f} %)")
print("failures:", bad)
sys.exit(1 if bad else 0)
