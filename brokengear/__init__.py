"""brokengear - parametric spur gears with realistic manufacturing defects and in-use damage.

Exports STEP (and optionally STL) plus a JSON ground-truth label per gear,
intended for 3D-printing test pieces for machine-vision inspection. With
--usd it also writes USD with per-defect meshes and semantic labels for
synthetic-data training in NVIDIA Omniverse / Isaac Sim (see brokengear.usd).
"""

from .gear import GearSpec, ToothState, Gear
from .defects import CATALOG
from .model import sample_defects, CONDITIONS
from .build import build_gear

__all__ = [
    "GearSpec",
    "ToothState",
    "Gear",
    "CATALOG",
    "sample_defects",
    "CONDITIONS",
    "build_gear",
]
