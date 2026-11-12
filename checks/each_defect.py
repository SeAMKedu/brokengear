"""Build one gear per catalog defect and report whether it applied and stays one valid solid."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brokengear.build import build_gear, export
from brokengear.defects import CATALOG
from brokengear.gear import GearSpec

out = Path(sys.argv[1] if len(sys.argv) > 1 else "tmp/each")
bad = 0
for name, t in CATALOG.items():
    mat = "steel" if t.weight.get("plastic", 0) == 0 else "plastic"
    spec = GearSpec(module=2, teeth=24, face_width=8, bore=8, keyway=True, material=mat)
    t0 = time.time()
    try:
        solid, label = build_gear(spec, defects=[(name, {})], severity=0.8, seed=1)
        d = label["defects"][0]
        ok = d["applied"] and solid.is_valid and len(solid.solids()) == 1
        export(solid, label, out, name)
        print(f"{'OK ' if ok else 'BAD'} {name:<22} {time.time()-t0:5.1f}s vol={solid.volume:8.0f} {d.get('skip_reason','')}")
        bad += not ok
    except Exception as e:
        bad += 1
        print(f"ERR {name:<22} {type(e).__name__}: {e}")
print("failures:", bad)
sys.exit(1 if bad else 0)
