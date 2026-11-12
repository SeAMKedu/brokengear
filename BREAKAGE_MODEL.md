# Gear breakage model

This explains how `brokengear` decides what is wrong with a gear. Each defect
is based on a documented failure or manufacturing error. Its size scales with
a severity value from 0 to 1. Defects are placed where they physically occur
on a real gear.

## Research basis

| Source | What it gives us |
|---|---|
| ISO 10825-1:2022 *Gears: Wear and damage to gear teeth, Part 1: Nomenclature and characteristics* | The ISO 10825 failure classes: **wear**, **surface fatigue** (pitting, micropitting, spalling), **plastic deformation**, and **tooth breakage** (fatigue, overload, tooth-end breakage). |
| Bending-fatigue literature (Gear Technology, TU München, Politecnico di Torino) | Root cracks start at the **~30° tangent of the root fillet on the loaded flank**. They run either *across the tooth*, which loses one tooth and is the "fail-safe" path, or *radially through the rim*, which is catastrophic. The fracture surface is slanted and rough. |
| VDI 2736 and plastic-gear studies (ZHAW, TUM) | In plastic gears the main failure modes are **wear**, **thermal melting/softening**, **root fatigue cracks**, **creep and plastic deformation**, and flank fatigue. Pitting is rare and occurs mostly with oil lubrication. |
| Gear inspection and hobbing sources (Gear Solutions, AGMA papers) | **Runout from an eccentric blank/bore**, **indexing (pitch) error**, **profile and thickness error** from wrong infeed or a worn hob, **lead error from bore perpendicularity**, and **burrs and nicks** at the face edges. |
| Injection-moulding defect guides (Keyence, Xometry, Firstmold, Elastron) | **Short shot**, **flash** at the parting line, **sink marks** over thick sections, **weld lines** where flow fronts meet, **voids and porosity**, and **warpage**. |
| Hub and keyway failure analyses (Rexnord/Falk, KHK, reducer case studies) | **Hub cracks from the keyway corner** caused by hoop stress or press fits. **Oval or fretted bores** from a loose fit. **Keyways wallowed out** by reversing torque. |

Relative frequencies (`weight` in `make_gear.py list`) are approximate. They
follow the pattern most failure surveys report:

- **Steel gears:** breakage (fatigue and overload) dominates, then surface
  fatigue, then wear.
- **Plastic gears:** wear and thermal damage dominate, with root fatigue second.

Tune the weights for your own test plan by passing `weights_override` to
`build_gear`.

## Defect catalog

### Manufacturing defects

| Type | Geometry produced | Real cause |
|---|---|---|
| `short_shot` | 1–5 adjacent teeth with shortened, rounded tips; worst in the middle of the sector | Melt froze before filling the cavity |
| `flash` | Thin web (≥ min feature) at the parting plane (z = 0) that bridges tooth gaps and overhangs the tips | Worn parting line, high pressure, low clamp force |
| `sink_mark` | Shallow spherical dish on the top face | Thick section shrinks after the skin sets |
| `weld_line` | Thin radial groove on the top face | Melt fronts meeting around the core pin |
| `porosity` | Cluster of open pores on the top face | Gas or moisture in the melt; casting porosity |
| `bore_flash` | Membrane partly closing the bore at the bottom | Worn or short core pin |
| `burr` | Lip above the face at a tooth tip, or a fin on the flank | Exit burr from hobbing; handling nicks |
| `pitch_error` | One tooth shifted 4–16 % of the pitch | Indexing error |
| `tooth_thickness_error` | One tooth (or all teeth) 3–25 % thicker or thinner | Cutter infeed or profile shift, shrinkage |
| `face_wedge` | Top face tilted, so thickness varies by 0.2–0.8 mm | Warpage; blank clamped tilted |
| `missing_bore` | No axle hole | Operation skipped; broken core pin |
| `blind_bore` | Hole only partly through, open at the top | Wrong drill depth; short core |
| `eccentric_bore` | Bore offset from the gear centre | Blank not centred; core shift |
| `tilted_bore` | Bore axis tilted 1.5–6.5° | Bore not perpendicular, causing lead error |
| `bore_size_error` | Bore over- or undersize | Wrong tool or shrinkage |
| `missing_keyway` | Keyway not cut | Operation skipped |

### In-use damage

| Type | Geometry produced | Real cause |
|---|---|---|
| `broken_tooth` | Stub left near the root with a slanted, jagged fracture line: higher on the loaded side, sagging toward the other root | Bending fatigue |
| `tooth_tip_break` | Upper part of a tooth broken off | Overload, shock, or a foreign body in the mesh |
| `missing_teeth` | 1–3 adjacent teeth gone, flush with the root circle | Stripped gear (cascading tooth failure) |
| `chipped_tip` | Ellipsoidal chip at a tooth-tip corner (mostly the top face) | Edge loading, impact, debris |
| `tooth_end_break` | Triangular piece missing at one end of a tooth | Misalignment or lead error edge-loading the tooth |
| `pitting` | Clusters of craters on the loaded flank, just below the pitch line | Rolling-contact fatigue |
| `wear` | Loaded flanks thinned on all teeth: minimal at the pitch line, most at the tip, a small step where contact starts, tips become pointed | Abrasive or adhesive wear |
| `plastic_deformation` | Tips bent or rolled away from the load | Overload, creep |
| `thermal_melt` | 2–6 teeth smeared, bent and shortened (plastic only) | Frictional heating |
| `root_crack` | Crack from the loaded-side root fillet partway across the tooth | Early bending fatigue |
| `rim_crack` | Crack from a tooth root radially toward the bore | Fatigue in a thin rim (catastrophic path) |
| `hub_crack` | Radial crack from the bore or keyway corner | Hoop stress, press fit, shock torque |
| `bore_wear` | Bore worn oval toward one side | Fretting or slipping on the shaft |
| `keyway_damage` | Keyway widened on the loaded side | Loose key under reversing torque |

## Conditions

- **perfect**: the nominal gear.
- **manufacturing**: 1 defect (55 %), 2 (30 %) or 3 (15 %), drawn from the
  process that matches the material: moulded plastic or machined steel.
- **in_use**: 1–3 damages, followed by **damage chains**:

  | Source damage | Possible secondary damage |
  |---|---|
  | broken / missing tooth | chips on the neighbouring teeth from fragments and impact; the next tooth may strip |
  | wear | plastic deformation, pitting |
  | pitting | wear (spalled particles act as abrasive) |
  | thermal melt | wear |

- **combined**: 1–2 manufacturing defects, then **causal links**. These put the
  in-use damage where the defect would cause it on a real gear:

  | Manufacturing defect | In-use consequence (placement) |
  |---|---|
  | eccentric bore | wear or pitting on the teeth *farthest from the axis* (most loaded) |
  | tilted bore / face wedge | tooth-end breakage, pitting or chips at *one end* of the face |
  | thin tooth / short shot | that tooth breaks |
  | pitch error | that tooth chips or breaks |
  | weld line / porosity | rim, root or hub crack *at the same angle* |
  | undersize bore | hub crack (press-fit hoop stress) |
  | oversize bore / missing keyway | bore wear (fretting, slipping) |

  If no link fires, one independent in-use damage is added. A further
  independent damage is added 30 % of the time.

Each label records the link through `caused_by` and `cause_note`.

## Printability rules

- Every defect is at least `--min-feature` (default 0.4 mm). Cracks and
  grooves are that wide, and pits and pores have at least that diameter.
  **Printed cracks are therefore wider than real cracks.** Lower
  `--min-feature` for SLA printing.
- Face features (sink marks, weld lines, pores, burrs, chips, tooth-end
  breaks) sit on the **top face**. Flash sits on the bottom (parting) plane, so
  it prints as part of the first layers. Print bottom-down, or hub-down if the
  gear has a hub.
- With `module < 1` many defects get close to the printer's resolution. A
  module of 1.5–3 works well for FDM.

## Limitations

- Spur gears only. There is no root trochoid or fillet: the root is a
  straight radial line from the root circle up to the base circle.
- Surface texture is not modelled: scuffing, micropitting, discolouration and
  fracture-surface beach marks.
- Warpage is approximated as a planar face wedge. Real warpage is a dish or
  saddle shape.
- Crack openings are exaggerated to the printable width.
