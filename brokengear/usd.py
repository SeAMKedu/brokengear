"""USD export for NVIDIA Omniverse / Isaac Sim (Replicator) synthetic-data training.

Each gear becomes one ``<name>.usd`` file:

    /<name>                 Xform, kind=component, rigid body, class "gear"
        Looks/Material      one neutral UsdPreviewSurface for the whole gear
        intact              Mesh: every surface that matches the perfect gear
        d2_tooth_tip_break  Mesh per defect: the surfaces that defect changed
        marking             Mesh: engraved ID / arrow (plate gears only)

Defect surfaces are found geometrically: the gear is compared against a
perfect twin built from the same GearSpec. A triangle whose centre lies more
than ``TOL_MM`` away from the perfect surface belongs to a defect. Which
defect it belongs to is decided from where it is (tooth, web or bore) and the
defect's recorded teeth / angle; see ``_attribute``. Triangles that changed
but cannot be tied to one defect go to ``defect_unattributed``.

Semantic labels are written twice so both old and new Replicator versions
read them: the ``UsdSemantics.LabelsAPI`` schema (Isaac Sim 5+) and the
legacy ``SemanticsAPI`` attributes (Isaac Sim 4.x). The label types are
``class`` (``gear`` / the defect type / ``marking``), ``defect`` (``defect``
or ``intact``) and ``category`` (``manufacturing`` / ``in_use``). The full
JSON ground truth sits in the root prim's ``customData["brokengear"]``.

The stage is in metres with +Z up (Isaac Sim defaults); the gear keeps the
JSON label's frame (axis +Z, tooth 0 on +X), only scaled from mm to m.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from .defects import CATALOG

TESS_TOL = 0.01      # mm, chord tolerance (same as the STL export)
TESS_ANG = 0.1       # rad
MAX_EDGE = 0.75      # mm, triangles are split until no edge is longer
TOL_MM = 0.03        # mm, deviation that counts as "changed"
SIMPLIFY_ERR = 0.01  # mm, max shape change allowed when decimating (0 = off)
K_NEAR = 32          # candidate reference triangles per query point

DENSITY = {"plastic": 1240.0, "steel": 7850.0}   # kg/m^3 (PLA / steel)
COLOR = {"plastic": (0.55, 0.57, 0.60), "steel": (0.62, 0.63, 0.65)}

BORE_TYPES = {n for n, t in CATALOG.items() if t.phase == "bore"} | {"hub_crack", "bore_flash"}
WEB_TYPES = {"face_wedge", "weld_line", "sink_mark", "porosity", "rim_crack", "hub_crack"}
EVERYWHERE = {"face_wedge"}  # tilts the whole top face


# ------------------------------------------------------------------ meshing
def _subdivide(v, f, max_edge):
    """Split triangles at their longest edge until every edge is <= max_edge.

    Returns (vertices, nodes, node_parent, leaf): every triangle ever made, as
    a binary split tree. Roots (parent -1) are the input triangles and leaves
    are the final pieces; new vertices are appended after the input ones.
    Children always come after their parent."""
    verts = [np.asarray(v, float)]
    nv = len(v)
    allv = verts[0]
    cache: dict[int, int] = {}
    nodes = [np.asarray(f, np.int64)]
    parent = [np.full(len(f), -1, np.int64)]
    leaf = []
    lim = max_edge * max_edge
    cur, cur_idx, total = nodes[0], np.arange(len(f)), len(f)
    while len(cur):
        p = allv[cur]                                       # (n, 3, 3)
        e = np.stack([((p[:, 0] - p[:, 1]) ** 2).sum(1), ((p[:, 1] - p[:, 2]) ** 2).sum(1),
                      ((p[:, 2] - p[:, 0]) ** 2).sum(1)], axis=1)
        i = e.argmax(axis=1)
        split = e[np.arange(len(cur)), i] > lim
        leaf.append((cur_idx, ~split))
        if not split.any():
            break
        t, i = cur[split], i[split]
        rot = np.stack([t[np.arange(len(t)), (i + k) % 3] for k in range(3)], axis=1)  # longest edge = (a,b)
        a_, b_, c_ = rot[:, 0], rot[:, 1], rot[:, 2]
        key = np.minimum(a_, b_) * (1 << 31) + np.maximum(a_, b_)
        uk, inv = np.unique(key, return_inverse=True)
        mids = np.empty(len(uk), np.int64)
        new = []
        for j, k in enumerate(uk.tolist()):
            m = cache.get(k)
            if m is None:
                m = cache[k] = nv + len(new)
                new.append(k)
            mids[j] = m
        if new:
            nk = np.asarray(new, np.int64)
            lo, hi = nk >> 31, nk & ((1 << 31) - 1)
            nvs = (allv[lo] + allv[hi]) / 2
            verts.append(nvs)
            allv = np.vstack([allv, nvs])
            nv += len(new)
        m = mids[inv]
        kids = np.concatenate([np.stack([a_, m, c_], 1), np.stack([m, b_, c_], 1)])
        par = np.concatenate([cur_idx[split], cur_idx[split]])
        nodes.append(kids)
        parent.append(par)
        cur, cur_idx = kids, np.arange(total, total + len(kids))
        total += len(kids)
    nodes = np.vstack(nodes)
    is_leaf = np.zeros(len(nodes), bool)
    for idx, mask in leaf:
        is_leaf[idx[mask]] = True
    return allv, nodes, np.concatenate(parent), is_leaf


def _merge_labels(nodes, parent, leaf, leaf_labels):
    """Largest triangles of the split tree whose pieces all share one label.
    Returns (triangles, labels)."""
    codes, lab_int = np.unique(np.asarray(leaf_labels, dtype=object).astype(str), return_inverse=True)
    lab = np.full(len(nodes), -1, np.int64)            # -1 unset, -2 mixed
    lab[np.flatnonzero(leaf)] = lab_int
    par = parent.tolist()
    labl = lab.tolist()
    for k in range(len(nodes) - 1, -1, -1):   # children come after parents
        q = par[k]
        if q < 0:
            continue
        lk, lq = labl[k], labl[q]
        if lk == -2 or (lq >= 0 and lq != lk):
            labl[q] = -2
        elif lq == -1:
            labl[q] = lk
    lab = np.asarray(labl)
    ok = lab >= 0
    top = ok & ((parent < 0) | (lab[np.maximum(parent, 0)] == -2))
    return nodes[top], codes[lab[top]].astype(object)


def _faces_mesh(solid, max_edge=MAX_EDGE):
    """Per-face tessellation. Vertices are not shared between B-rep faces, so
    normals stay sharp at the gear's real edges and smooth across a face.

    Returns (vertices, nodes, parent, leaf): the split trees of all faces in
    one vertex array (see ``_subdivide``). Leaves are the sampling triangles."""
    verts, nodes, parent, leaf, voff, noff = [], [], [], [], 0, 0
    for face in solid.faces():
        vs, tr = face.tessellate(TESS_TOL, TESS_ANG)
        if not tr:
            continue
        v = np.array([tuple(p) for p in vs], float)
        v2, n2, p2, l2 = _subdivide(v, np.asarray(tr, np.int64), max_edge)
        verts.append(v2)
        nodes.append(n2 + voff)
        parent.append(np.where(p2 < 0, -1, p2 + noff))
        leaf.append(l2)
        voff += len(v2)
        noff += len(n2)
    return np.vstack(verts), np.vstack(nodes), np.concatenate(parent), np.concatenate(leaf)


def _vertex_normals(v, t):
    n = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])  # area weighted
    vn = np.zeros_like(v)
    for k in range(3):
        np.add.at(vn, t[:, k], n)
    ln = np.linalg.norm(vn, axis=1, keepdims=True)
    return vn / np.where(ln == 0, 1, ln)


# ------------------------------------------------------------------ distance
def _closest_on_tri(p, a, b, c):
    """Closest points on triangles (a,b,c) to points p; all arrays (..., 3)."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(-1), (ac * ap).sum(-1)
    bp = p - b
    d3, d4 = (ab * bp).sum(-1), (ac * bp).sum(-1)
    cp = p - c
    d5, d6 = (ab * cp).sum(-1), (ac * cp).sum(-1)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    eps = 1e-30
    denom = va + vb + vc
    v = vb / np.where(np.abs(denom) < eps, eps, denom)
    w = vc / np.where(np.abs(denom) < eps, eps, denom)
    res = a + ab * v[..., None] + ac * w[..., None]       # inside the face

    def put(mask, val):
        nonlocal res
        res = np.where(mask[..., None], val, res)

    # edges (later regions overwrite earlier ones in Ericson's order, so go backwards)
    t_bc = (d4 - d3) / np.where(np.abs((d4 - d3) + (d5 - d6)) < eps, eps, (d4 - d3) + (d5 - d6))
    put((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0), b + (c - b) * t_bc[..., None])
    t_ac = d2 / np.where(np.abs(d2 - d6) < eps, eps, d2 - d6)
    put((vb <= 0) & (d2 >= 0) & (d6 <= 0), a + ac * t_ac[..., None])
    t_ab = d1 / np.where(np.abs(d1 - d3) < eps, eps, d1 - d3)
    put((vc <= 0) & (d1 >= 0) & (d3 <= 0), a + ab * t_ab[..., None])
    put((d6 >= 0) & (d5 <= d6), c)
    put((d3 >= 0) & (d4 <= d3), b)
    put((d1 <= 0) & (d2 <= 0), a)
    return res


class _Surface:
    """Point-to-surface distance against a reference triangle mesh."""

    def __init__(self, solid):
        self.v, nodes, _, leaf = _faces_mesh(solid)
        self.t = nodes[leaf]
        self.tree = cKDTree(self.v[self.t].mean(axis=1))

    def distance(self, pts, chunk=4096):
        out = np.empty(len(pts))
        k = min(K_NEAR, len(self.t))
        for s in range(0, len(pts), chunk):
            p = pts[s:s + chunk]
            _, idx = self.tree.query(p, k=k)
            tri = self.t[idx]                          # (n, k, 3)
            a, b, c = self.v[tri[..., 0]], self.v[tri[..., 1]], self.v[tri[..., 2]]
            q = _closest_on_tri(p[:, None, :], a, b, c)
            out[s:s + chunk] = np.sqrt(((q - p[:, None, :]) ** 2).sum(-1)).min(axis=1)
        return out


# ------------------------------------------------------------------ attribution
def _zone(c, spec, label):
    m = spec.module
    r_root = spec.module * spec.teeth / 2 - 1.25 * m
    r = math.hypot(c[0], c[1])
    if r >= r_root - 0.3 * m:
        return "tooth"
    g = label["gear"]
    rb = (g.get("bore") or 0) / 2
    if rb:
        ecc = max([d["params"].get("offset", 0) for d in label["defects"]
                   if d["type"] == "eccentric_bore" and d["applied"]] + [0])
        kd = (g.get("keyway_depth") or 0.45 * (g.get("keyway_width") or max(1.0, rb / 2))) if g.get("keyway") else 0
        if r <= rb + kd + ecc + 1.0:
            return "bore"
    if c[2] < 0:  # hub
        return "bore"
    return "web"


def _ang_dist(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _attribute(centres, spec, label):
    """Defect id for each changed triangle centre (or None)."""
    applied = [d for d in label["defects"] if d["applied"]]
    z = spec.teeth
    pitch = 360.0 / z
    out = []
    for c in centres:
        zone = _zone(c, spec, label)
        ang = math.degrees(math.atan2(c[1], c[0])) % 360
        tooth = int(round(ang / pitch)) % z
        best, best_key = None, None
        for d in applied:
            typ, teeth = d["type"], d.get("teeth") or []
            if zone == "tooth":
                ok = typ in EVERYWHERE or (teeth and any(min((tooth - t) % z, (t - tooth) % z) <= 1
                                                          for t in teeth))
                if typ in BORE_TYPES or (typ in WEB_TYPES and not teeth and typ not in EVERYWHERE):
                    ok = False
            elif zone == "bore":
                ok = typ in BORE_TYPES or typ in EVERYWHERE
            else:
                ok = typ in WEB_TYPES or typ in EVERYWHERE or typ == "flash"
            if not ok:
                continue
            exact = 0 if (not teeth or tooth in teeth) else 1
            near = _ang_dist(ang, d["angle_deg"]) if "angle_deg" in d else 0.0
            broad = 1 if typ in EVERYWHERE else 0
            key = (broad, exact, len(teeth) if teeth else 0, near, -d["severity"])
            if best_key is None or key < best_key:
                best, best_key = d["id"], key
        if best is None:  # zone rules found nothing: any defect on this tooth
            for d in applied:
                if tooth in (d.get("teeth") or []):
                    best = d["id"]
                    break
        if best is None and len(applied) == 1:
            best = applied[0]["id"]
        out.append(best)
    return out


# ------------------------------------------------------------------ USD writing
def _ident(s):
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    return s if s and not s[0].isdigit() else f"_{s}"


def _labels(prim, **types):
    from pxr import Sdf, UsdSemantics
    for i, (typ, value) in enumerate(types.items()):
        vals = value if isinstance(value, list) else [value]
        UsdSemantics.LabelsAPI.Apply(prim, typ).CreateLabelsAttr(vals)
        # legacy Isaac Sim 4.x "SemanticsAPI" (single value per instance)
        inst = "Semantics" if i == 0 else f"Semantics_{i}"
        prim.AddAppliedSchema(f"SemanticsAPI:{inst}")
        prim.CreateAttribute(f"semantic:{inst}:params:semanticType", Sdf.ValueTypeNames.String).Set(typ)
        prim.CreateAttribute(f"semantic:{inst}:params:semanticData", Sdf.ValueTypeNames.String).Set(",".join(vals))


def _compact(v, t):
    used = np.unique(t)
    remap = np.full(len(v), -1, np.int64)
    remap[used] = np.arange(len(used))
    return v[used], remap[t]


def _simplify(v, t, max_err):
    """Quadric decimation (meshoptimizer) of one label region.

    Open borders are locked. Vertices are not shared between B-rep faces or
    between label regions, so that keeps every sharp CAD edge and every label
    boundary exactly where it was; only the inside of each face is thinned,
    and no point moves more than ``max_err`` mm."""
    import meshoptimizer as mo
    v, t = _compact(v, t)
    idx = t.reshape(-1).astype(np.uint32)
    out = np.zeros_like(idx)
    n = mo.simplify(out, idx, v.astype(np.float32), target_index_count=0, target_error=max_err,
                    options=mo.SIMPLIFY_LOCK_BORDER | mo.SIMPLIFY_ERROR_ABSOLUTE)
    return v, out[:n].reshape(-1, 3).astype(np.int64)


def _mesh(stage, path, v, t, physics, max_err=SIMPLIFY_ERR):
    from pxr import UsdGeom, UsdPhysics, Vt, Gf
    if max_err > 0:
        v, t = _simplify(v, t, max_err)
    pv, tt = _compact(v, t)
    pv = pv / 1000.0
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pv.astype(np.float32)))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(tt), 3, np.int32)))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(tt.reshape(-1).astype(np.int32)))
    mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(_vertex_normals(pv, tt).astype(np.float32)))
    mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateOrientationAttr(UsdGeom.Tokens.rightHanded)
    lo, hi = pv.min(axis=0), pv.max(axis=0)
    mesh.CreateExtentAttr([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))])
    if physics:
        UsdPhysics.CollisionAPI.Apply(mesh.GetPrim())
        UsdPhysics.MeshCollisionAPI.Apply(mesh.GetPrim()).CreateApproximationAttr("convexDecomposition")
    return mesh


def export_usd(path: Path, solid, label, spec, perfect=None, unmarked=None, physics=True,
               max_err=SIMPLIFY_ERR):
    """Write ``path`` (.usd / .usda). ``spec`` is the GearSpec the gear was built
    from (not the rounded one in the label). ``perfect`` is the defect-free twin
    (built here if None); ``unmarked`` is the same gear without the ID engraving
    (plate gears)."""
    from pxr import Kind, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade

    if perfect is None:
        from .build import build_gear
        perfect = build_gear(spec, "perfect", 0)[0]
    # classify small triangles, then merge them back into the largest uniform ones
    v, nodes, parent, leaf = _faces_mesh(solid)
    ft = nodes[leaf]
    centres = v[ft].mean(axis=1)
    fgroup = np.full(len(ft), "intact", dtype=object)
    if unmarked is not None:
        mark = _Surface(unmarked).distance(centres) > TOL_MM
        fgroup[mark] = "marking"
    else:
        mark = np.zeros(len(ft), bool)
    changed = (_Surface(perfect).distance(centres) > TOL_MM) & ~mark
    ids = _attribute(centres[changed], spec, label)
    fgroup[np.flatnonzero(changed)] = [i or "unattributed" for i in ids]
    t, group = _merge_labels(nodes, parent, leaf, fgroup)

    by_id = {d["id"]: d for d in label["defects"]}
    name = _ident(label.get("id") or label["name"])
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, f"/{name}")
    rp = root.GetPrim()
    stage.SetDefaultPrim(rp)
    Usd.ModelAPI(rp).SetKind(Kind.Tokens.component)
    _labels(rp, **{"class": "gear", "condition": label["condition"]})
    rp.SetCustomDataByKey("brokengear", json.dumps(label))
    if physics:
        UsdPhysics.RigidBodyAPI.Apply(rp)
        UsdPhysics.MassAPI.Apply(rp).CreateDensityAttr(DENSITY.get(spec.material, 1240.0))

    mat = UsdShade.Material.Define(stage, f"/{name}/Looks/Material")
    sh = UsdShade.Shader.Define(stage, f"/{name}/Looks/Material/Shader")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(COLOR.get(spec.material, COLOR["plastic"]))
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.55 if spec.material == "plastic" else 0.35)
    sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0 if spec.material == "plastic" else 1.0)
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(rp).Bind(mat)

    counts = {}
    for grp in ["intact", "marking"] + [d["id"] for d in label["defects"]] + ["unattributed"]:
        sel = group == grp
        if not sel.any():
            continue
        counts[grp] = int(sel.sum())
        if grp in ("intact", "marking"):
            prim_name, lab = grp, dict(**{"class": "gear" if grp == "intact" else "marking"},
                                       defect="intact")
        elif grp == "unattributed":
            prim_name, lab = "defect_unattributed", {"class": "defect", "defect": "defect"}
        else:
            d = by_id[grp]
            prim_name = f"{grp}_{d['type']}"
            lab = {"class": d["type"], "defect": "defect", "category": d["category"]}
        mesh = _mesh(stage, f"/{name}/{_ident(prim_name)}", v, t[sel], physics, max_err)
        mp = mesh.GetPrim()
        _labels(mp, **lab)
        if grp in by_id:
            d = by_id[grp]
            mp.SetCustomDataByKey("defect", json.dumps({k: d[k] for k in (
                "id", "type", "category", "severity", "teeth", "caused_by", "description") if k in d}))
    stage.GetRootLayer().Save()
    return counts


def export_plate_usd(path: Path, gears):
    """``gears``: list of (id, usd file relative to path's folder, (x_mm, y_mm))."""
    from pxr import Gf, Kind, Usd, UsdGeom, UsdPhysics
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdPhysics.SetStageKilogramsPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/plate")
    stage.SetDefaultPrim(root.GetPrim())
    Usd.ModelAPI(root.GetPrim()).SetKind(Kind.Tokens.assembly)
    for gid, rel, (x, y) in gears:
        x_ = UsdGeom.Xform.Define(stage, f"/plate/{_ident(gid)}")
        x_.GetPrim().GetReferences().AddReference(rel)
        x_.AddTranslateOp().Set(Gf.Vec3d(x / 1000.0, y / 1000.0, 0.0))
    stage.GetRootLayer().Save()
