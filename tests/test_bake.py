"""Headless check of Bake Edits. Run:
    blender -b --factory-startup --python tests/test_bake.py -- <scan.ply>
Edits a 10% sample of the scan (erases, fades, added splats, stashed originals), then bakes and
checks the model keeps exactly the visible splats, drops the bookkeeping and the stash,
exports the same file as before the bake, and saves smaller."""
import bpy, sys, os, tempfile, filecmp, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet, _TRACKING, ADDED_ATTR, set_from_rows
from gs_patch import ply_io, stash
D = tempfile.mkdtemp() + "/"
SCAN = sys.argv[sys.argv.index("--") + 1]
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)

for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.wm.ply_import(filepath=SCAN)
full = bpy.context.selected_objects[0]
S = SplatSet.read(full.data)
keep = np.arange(0, S.n, 10)
pc = bpy.data.pointclouds.new("A"); SplatSet(len(keep), {k: [dt, a[keep]] for k, (dt, a) in S.arrays.items()}).write(pc)
bpy.data.objects.remove(full)
A = bpy.data.objects.new("A", pc); bpy.context.scene.collection.objects.link(A)
# edits as the brushes leave them
S = SplatSet.read(A.data)
m = np.zeros(S.n, bool); m[:3000] = True; S.hide(m)                       # erased
f = np.zeros(S.n, bool); f[5000:6000] = True
S.orig_opacity[f] = S.base[f, 3]; S.base[f, 3] *= 0.5; S.faded[f] = True    # faded
extra = {k: [dt, a[7000:7500].copy()] for k, (dt, a) in S.arrays.items()}  # added (cloned)
extra["position"][1][:] += 0.01
extra[ADDED_ATTR][1][:] = True
S2 = set_from_rows({k: [dt, np.concatenate([S.arrays[k][1], extra[k][1]])] for k, (dt, _) in S.arrays.items()})
S2.write(A.data)
stash.write(A, set_from_rows({k: [dt, a[100:600].copy()] for k, (dt, a) in S.arrays.items()}))  # stashed originals
n_all, n_visible = S2.n, int((~S2.hidden).sum())
before = D + "before.ply"; ply_io.write_pointcloud_ply(before, A.data)
bpy.ops.wm.save_as_mainfile(filepath=D + "before.blend")
size_before = os.path.getsize(D + "before.blend")

bpy.context.view_layer.objects.active = A; A.select_set(True)
r = bpy.ops.gsp.bake_edits()
check("bake ran", r == {'FINISHED'}, str(r))
check("only the visible splats are left", len(A.data.points) == n_visible, f"{len(A.data.points)} of {n_all}, {n_visible} visible")
left = [n for n in _TRACKING if A.data.attributes.get(n) is not None]
check("no edit bookkeeping left", not left, str(left))
check("the stash is gone", stash.get(A) is None and not any(p.name.endswith("_gsp_stash") for p in bpy.data.pointclouds))
after = D + "after.ply"; ply_io.write_pointcloud_ply(after, A.data)
check("export is unchanged by the bake", filecmp.cmp(before, after, shallow=False))
bpy.ops.wm.save_as_mainfile(filepath=D + "after.blend")
size_after = os.path.getsize(D + "after.blend")
check("the .blend is smaller", size_after < size_before, f"{size_before / 1e6:.1f} MB -> {size_after / 1e6:.1f} MB")
# the brush still works on a baked model (its bookkeeping comes back empty)
S3 = SplatSet.read(A.data)
check("a baked model reads back for editing", S3.n == n_visible and not S3.hidden.any())
print("RESULT", "OK" if not FAILS else f"{len(FAILS)} failed: {FAILS}", flush=True)
sys.exit(1 if FAILS else 0)
