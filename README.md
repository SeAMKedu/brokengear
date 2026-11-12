# brokengear

`brokengear` generates spur gears as STEP files for 3D printing machine-vision
test parts. A gear can be:

- **perfect**,
- given **manufacturing defects**,
- **damaged in use**, or
- **combined**: manufacturing defects that cause the in-use damage.

Every gear comes with a JSON label recording what is wrong with it, where, and
why. You can use that file as ground truth when testing an inspection system.

The defects are based on the ISO 10825 failure classes, the fatigue and
plastic-gear literature, and common moulding and machining errors. See
[BREAKAGE_MODEL.md](BREAKAGE_MODEL.md) for the sources and the full catalog of
30 defect types.

## Requirements

You need [uv](https://docs.astral.sh/uv/). The wrapper script `make_gear.sh`
runs the tool in the pinned `cadgen` / `build123d` environment. The first run
downloads that environment.

## Usage

Make one perfect gear (module 2, 24 teeth, 8 mm thick, 8 mm bore, with a keyway):

```bash
./make_gear.sh one --module 2 --teeth 24 --width 8 --bore 8 --keyway --condition perfect
```

Set the size by outside diameter instead, and pick a random defective condition:

```bash
./make_gear.sh one --od 50 --teeth 20 --condition combined --seed 7 --stl
```

Choose the defects yourself:

```bash
./make_gear.sh one --teeth 30 --defect broken_tooth:teeth=4 --defect "pitting:teeth=10,11;severity=0.9" --defect eccentric_bore
```

Generate a labelled dataset, with each condition equally likely and the tooth count varied:

```bash
./make_gear.sh batch --count 40 --out dataset --keyway --vary-size --mix perfect=1,manufacturing=1,in_use=1,combined=1
```

Print the defect catalog:

```bash
./make_gear.sh list
```

### Options

| Option | Meaning |
|---|---|
| `--module` / `--od`, `--teeth`, `--width`, `--pressure-angle` | Gear size |
| `--bore` (0 = none), `--keyway`, `--keyway-width/-depth` | Bore and keyway |
| `--hub-diameter`, `--hub-height` | Hub extending below the gear |
| `--material plastic\|steel` | Selects the process (moulded or machined) and the failure statistics |
| `--condition perfect\|manufacturing\|in_use\|combined` | Random breakage model |
| `--defect name[:key=val;...]` | Explicit defects. Keys: `teeth=1,2`, `severity=0..1`, `angle_deg=`, `end=top`, `sign=-1` (bore size), or any sampled parameter |
| `--severity` | Force one severity for every defect |
| `--min-feature` | Smallest defect size, default 0.4 mm (the nozzle width) |
| `--stl` | Also write an STL file |
| `--seed` | Makes the output reproducible |

## Output

`<name>.step`, an optional `<name>.stl`, and `<name>.json`. A batch also
writes `manifest.csv`.

The JSON label contains:

- `condition`
- the `gear` parameters
- `defects[]`, where each defect has:
  - `type` and `category`
  - `severity`
  - `teeth` and `tooth_angles_deg`
  - `angle_deg`
  - `params`
  - `caused_by` and `cause_note`
  - `description` and `mechanism`
- `damaged_teeth`
- `volume_mm3`
- `bounding_box_mm`

Coordinate conventions:

- The gear axis is +Z, and the body runs from z = 0 to z = width.
- Tooth 0 is centred on +X, and teeth are numbered counter-clockwise when seen
  from +Z.
- The loaded flank is the CCW flank. Use `--drive-cw` to flip it.

**Printing:** put the bottom face (or the hub end) on the bed. Face defects are
on the top face.

## Layout

```
make_gear.py / make_gear.sh   CLI
brokengear/gear.py            involute geometry and per-tooth state (wear, bend, fracture, truncation)
brokengear/defects.py         defect catalog: sampling and applying each defect
brokengear/model.py           breakage model: conditions, damage chains, causal links
brokengear/build.py           assembly of the solid, boolean checks, labels, export
checks/each_defect.py         builds every defect type once and checks for a valid single solid
examples/                     a perfect reference gear and 12 sample gears
```

## Print plate (Bambu Lab H2D)

```bash
./make_plate.sh --out plate_h2d
```

This fills the 300 × 320 mm area that both H2D nozzles can reach, so the
plate prints in any nozzle mode. It holds 30 hex-packed gears, 40–50 mm in
diameter and 3 mm thick:

- 3 perfect gears.
- The rest split between manufacturing, in-use and combined conditions.
- Gears are picked so every defect type appears at least once.

Each gear has its **ID engraved on the bottom face**, mirrored so it reads
correctly when you flip the gear over. An **arrow points at tooth 0**. The
inspected top face is left clean.

Outputs:

| File | Contents |
|---|---|
| `plate.3mf` | 30 separate objects, ready for Bambu Studio |
| `plate.step` | The whole plate in one STEP file |
| `gears/<ID>.step` and `.json` | Each gear and its ground-truth label |
| `catalog.md` / `catalog.csv` | ID → condition and defects, including the affected teeth |
| `plate_map.svg` | A printable top-view map |

`make_plate.sh` accepts the same gear and defect options as `make_gear.sh`.
**An option you give is fixed for the whole plate; one you leave out varies
per gear.**

| Left out | How it varies |
|---|---|
| Size (`--od`, `--module`, `--teeth`) | Within `--od-min/--od-max` (40–50 mm) and `--module-min/--module-max` (1.5–2.5) |
| `--bore` | Picked from `--bores` (6, 8) |
| `--keyway` | Given to the share set by `--keyway-share` |
| `--material` | Set by `--plastic-share` |

The default thickness is 3 mm. A fixed size packs the plate tighter: 45 mm
gears fit 39 to a plate.

Fixed-size gears with in-use damage only, severity stepping evenly from 0.2
(first ID) to 1.0 (last ID), plus one perfect gear:

```bash
./make_plate.sh --od 45 --teeth 24 --bore 8 --condition in_use --perfect 1 --severity-range 0.2,1.0 --out plate_in_use
```

A severity ladder for one defect type:

```bash
./make_plate.sh --od 45 --teeth 24 --defect broken_tooth --severity-range 0.1,1.0 --perfect 1 --out plate_broken
```

Plate-specific options:

| Option | Meaning |
|---|---|
| `--condition` or `--mix` | Which conditions the non-perfect gears get |
| `--only TYPES` | Restrict to these defect types, e.g. `wear,pitting`, or a whole category with `in_use` |
| `--severity-range LO,HI` | Spread severity over the plate. `--severity-spread random` draws it per defect instead |
| `--defect` | Put this defect on every non-perfect gear |
| `--perfect N` | Number of perfect gears |
| `--count` | Number of gears |
| `--bed 325x320` | Use the single-nozzle area |
| `--gap` | Minimum gap between gears |
| `--prefix A` | IDs become `A01…`, for telling several plates apart |
| `--seed` | Makes the plate reproducible |

The catalog and map also show each gear's severity.
