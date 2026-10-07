"""Headless checks. Run:  blender -b --factory-startup --python tests/test_core.py -- <scan.ply>

Edits a scan with the core functions, then checks that
  * a full restore gives back the original scan exactly (every splat, every attribute),
  * export writes exactly the visible splats, without bookkeeping columns,
  * the streaming exporter matches the in-memory one byte for byte.
"""
import os, sys, filecmp, tempfile
import bpy
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch
gs_patch.register()
from gs_patch.splats import SplatSet, ADDED_ATTR, FADED_ATTR, ORIG_OPACITY_ATTR, SELECT_ATTR, HIDDEN_ATTR
from gs_patch import core, ply_io, stash
from gs_patch.ops import save

TRACK = {ADDED_ATTR, FADED_ATTR, ORIG_OPACITY_ATTR, SELECT_ATTR, HIDDEN_ATTR}
FAILS = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok:
        FAILS.append(label)


def fingerprint(S):
    names = sorted(k for k in S.arrays if k not in TRACK)
    table = np.concatenate([S.get(k).astype(np.float64).reshape(S.n, -1) for k in names], 1)
    return names, table[np.lexsort(table.T[::-1])]


scan = sys.argv[-1]
bpy.ops.wm.ply_import(filepath=scan)
obj = bpy.context.selected_objects[0]
bpy.context.view_layer.objects.active = obj
S0 = SplatSet.read(obj.data)
names0, fp0 = fingerprint(S0)
rng = np.random.default_rng(3)
anchors = S0.pos[rng.choice(np.nonzero(S0.opacity > 0.6)[0], 4, replace=False)]
view = np.array([0, 0, -1.0], np.float32)

S = SplatSet.read(obj.data)
st = core.Stroke(S); idx, w = core.sphere_hits(S.pos, anchors[0], 0.06, 0.5); st.erase(idx, w)
st.commit_clones_and_erase(); save(obj, S)
S = SplatSet.read(obj.data)
cs, ns = core.surface_frame(S, anchors[1], 0.05, view); cd, nd = core.surface_frame(S, anchors[2], 0.05, view)
st = core.Stroke(S); st.clone(cs, ns, cd, nd, 0.05, 0.3, replace=True, heal=1.0)
st.commit_clones_and_erase(); save(obj, S)
S = SplatSet.read(obj.data)
core.heal_fill(S, np.linalg.norm(S.pos - anchors[3], axis=1) < 0.03); save(obj, S)
bpy.ops.gsp.select_floaters(); bpy.ops.gsp.delete_selected()

S = SplatSet.read(obj.data)
check("edits recorded", S.hidden.any() and S.added.any(),
      f"hidden {int(S.hidden.sum())}, added {int(S.added.sum())}, faded {int(S.faded.sum())}")
check("no original splat lost", int((~S.added).sum()) == S0.n)

tmp = tempfile.mkdtemp()
a, b = os.path.join(tmp, "a.ply"), os.path.join(tmp, "b.ply")
n_a = ply_io.write_splat_ply(a, S.visible_subset())
n_b = ply_io.write_pointcloud_ply(b, obj.data)
check("export writes the visible splats", n_b == int(S.visible.sum()), f"{n_b}")
check("streaming export == in-memory export", n_a == n_b and filecmp.cmp(a, b, shallow=False))
header = open(b, "rb").read(6000).split(b"end_header")[0].decode()
check("no bookkeeping columns in the file", not any(k in header for k in TRACK))

T = stash.read(obj)
core.apply_restore(S, T, np.ones(S.n, bool), np.ones(S.n, bool),
                   np.zeros(0, bool) if T is None else np.ones(T.n, bool))
save(obj, S)
R = SplatSet.read(obj.data).visible_subset()
names1, fp1 = fingerprint(R)
check("full restore is bit-for-bit the original", names0 == names1 and np.array_equal(fp0, fp1),
      f"{R.n} vs {S0.n} splats")

print("ALL PASSED" if not FAILS else f"FAILED: {FAILS}", flush=True)
