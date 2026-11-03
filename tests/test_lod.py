"""Headless check of Low Detail / Full Detail. Run:
    blender -b --factory-startup --python tests/test_lod.py -- <scan.ply>
Two copies of the scan go to low detail; checks the stand-ins' size, that the brushes refuse
them, that export / merge / bake read the full models, that Full Detail brings them back
cleanly, and that a stand-in survives saving and reopening the file."""
import bpy, sys, os, tempfile, filecmp, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet
from gs_patch import lod, ply_io
D = tempfile.mkdtemp() + "/"
SCAN = sys.argv[sys.argv.index("--") + 1]
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)

for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.wm.ply_import(filepath=SCAN)
A = bpy.context.selected_objects[0]; A.name = "A"
B = bpy.data.objects.new("B", A.data.copy()); bpy.context.scene.collection.objects.link(B)
B.location = (0.5, 0, 0); B.rotation_euler = (0, 0, 1.2)
# some erased splats in A
S = SplatSet.read(A.data); m = np.zeros(S.n, bool); m[:1000] = True; S.hide(m); S.write(A.data); del S
nA, nB = len(A.data.points), len(B.data.points)
ply_io.write_pointcloud_ply(D + "full.ply", A.data)
ctx = bpy.context
for o in (A, B): o.select_set(True)
ctx.view_layer.objects.active = A
r = bpy.ops.gsp.low_detail(fraction=0.1)
check("low detail ran", r == {'FINISHED'})
check("A shows a stand-in", lod.is_proxy(A) and abs(len(A.data.points) - 0.1 * (nA - 1000)) < 2, f"{len(A.data.points):,} of {nA:,}")
check("B shows a stand-in", lod.is_proxy(B) and abs(len(B.data.points) - 0.1 * nB) < 2)
check("the full models are kept", lod.full_data(A).use_fake_user and len(lod.full_data(A).points) == nA)
check("stand-ins carry no SH", not any(a.name.startswith("radiance:sh_") for a in A.data.attributes))
check("brushes refuse a stand-in", not bpy.ops.gsp.select_all.poll() and not bpy.ops.gsp.delete_selected.poll())
ply_io.write_pointcloud_ply(D + "via_proxy.ply", lod.full_data(A))
bpy.ops.gsp.export_ply(filepath=D + "op_export.ply")
check("export uses the full model", filecmp.cmp(D + "full.ply", D + "op_export.ply", shallow=False))
r = bpy.ops.gsp.merge_splats(originals='KEEP')
C = ctx.view_layer.objects.active
check("merge uses the full models", r == {'FINISHED'} and len(C.data.points) == nA + nB and not lod.is_proxy(C),
      f"{len(C.data.points):,}")
bpy.data.objects.remove(C)
bpy.context.view_layer.update()
for o in (A, B): o.select_set(False)
A.select_set(True); ctx.view_layer.objects.active = A
r = bpy.ops.gsp.bake_edits()
check("bake works on the full model behind a stand-in", r == {'FINISHED'} and len(lod.full_data(A).points) == nA - 1000 and lod.is_proxy(A))
# save, reopen, bring back
path = D + "lod.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)
bpy.ops.wm.open_mainfile(filepath=path)
A, B = bpy.data.objects["A"], bpy.data.objects["B"]
check("stand-ins survive reopening", lod.is_proxy(A) and lod.is_proxy(B) and len(lod.full_data(B).points) == nB)
for o in (A, B): o.select_set(True)
bpy.context.view_layer.objects.active = A
r = bpy.ops.gsp.full_detail()
check("full detail brings the models back", r == {'FINISHED'} and not lod.is_proxy(A) and len(A.data.points) == nA - 1000
      and len(B.data.points) == nB)
check("no stand-ins left behind", not any(p.name.endswith("_lod") for p in bpy.data.pointclouds),
      str([p.name for p in bpy.data.pointclouds]))
check("full models no longer pinned", not A.data.use_fake_user and not B.data.use_fake_user)
print("RESULT", "OK" if not FAILS else f"{len(FAILS)} failed: {FAILS}", flush=True)
sys.exit(1 if FAILS else 0)
