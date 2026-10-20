"""Headless check of Merge Splat Objects. Run:
    blender -b --factory-startup --python tests/test_merge.py -- <scan.ply>
Merges a 10% sample of the scan with a moved, rotated and scaled copy of itself, some splats
erased, and checks counts, world positions, orientations, sizes, view-dependent colour, the
erase history and the export."""
import bpy, sys, os, math, tempfile, numpy as np
from mathutils import Matrix, Vector, Euler
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet, HIDDEN_ATTR
from gs_patch import core, ply_io
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
keep = np.arange(0, S.n, 10)                       # a 10% sample, to keep the test quick
pc = bpy.data.pointclouds.new("A"); SplatSet(len(keep), {k: [dt, a[keep]] for k, (dt, a) in S.arrays.items()}).write(pc)
bpy.data.objects.remove(full)
sc = bpy.context.scene
A = bpy.data.objects.new("A", pc); sc.collection.objects.link(A)
B = bpy.data.objects.new("B", pc.copy()); sc.collection.objects.link(B)
A.location = (0.1, -0.05, 0.02); A.rotation_euler = (0.2, 0.0, 0.4)
B.location = (0.6, 0.3, -0.1); B.rotation_euler = (0.7, -0.4, 1.9); B.scale = (1.6, 1.6, 1.6)
bpy.context.view_layer.update()
# erase a few hundred splats in A, as the brush would
SA = SplatSet.read(A.data); mask = np.zeros(SA.n, bool); mask[:500] = True; SA.hide(mask); SA.write(A.data)
nA, nB = len(A.data.points), len(B.data.points)

for o in (A, B): o.select_set(True)
bpy.context.view_layer.objects.active = A
r = bpy.ops.gsp.merge_splats(originals='HIDE')
C = bpy.context.view_layer.objects.active
check("merge ran", r == {'FINISHED'} and C.name == "A_merged", str(r))
check("splat count", len(C.data.points) == nA + nB, f"{len(C.data.points)} vs {nA + nB}")
check("originals hidden", A.hide_get() and B.hide_get())
SC = SplatSet.read(C.data)
SBo = SplatSet.read(B.data)
# positions: merged B rows in world space match B's world positions
MC, MB = np.array(C.matrix_world), np.array(B.matrix_world)
def world(M, p): return p @ M[:3, :3].T + M[:3, 3]
err = np.abs(world(MC, SC.pos[nA:]) - world(MB, SBo.pos)).max()
check("positions in world space", err < 1e-4, f"max error {err:.2e}")
check("erased splats keep their history", int(SC.hidden[:nA].sum()) == 500 and np.allclose(SC.orig_opacity[:500], SA.orig_opacity[:500]))
# rotation: each splat's world orientation is the same
def qmat(q):
    q = q / np.linalg.norm(q, axis=1, keepdims=True); w, x, y, z = q.T
    return np.stack([np.stack([1-2*(y*y+z*z), 2*(x*y-w*z), 2*(x*z+w*y)], -1),
                     np.stack([2*(x*y+w*z), 1-2*(x*x+z*z), 2*(y*z-w*x)], -1),
                     np.stack([2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y)], -1)], 1)
def unit(M): A3 = M[:3, :3]; return A3 / np.linalg.norm(A3, axis=0)
sel = np.arange(0, nB, 997)
RwB = unit(MB) @ qmat(SBo.get("rotation")[sel].astype(np.float64))
RwC = unit(MC) @ qmat(SC.get("rotation")[nA:][sel].astype(np.float64))
check("splat orientations", np.abs(RwB - RwC).max() < 1e-4, f"{np.abs(RwB - RwC).max():.2e}")
scl = np.abs(SC.get("scale")[nA:] * np.linalg.norm(MC[:3, 0]) - SBo.get("scale") * np.linalg.norm(MB[:3, 0])).max()
check("splat sizes", scl < 1e-5, f"{scl:.2e}")
# view-dependent colour: same in every world direction
names = SBo.sh_names()
cB = np.stack([SBo.get(n)[sel] for n in names], 1).astype(np.float64)
cC = np.stack([SC.get(n)[nA:][sel] for n in names], 1).astype(np.float64)
rng = np.random.default_rng(1); d = rng.normal(size=(50, 3)); d /= np.linalg.norm(d, axis=1, keepdims=True)
def colour(c, dirs):
    b = np.concatenate(core.sh_basis(dirs), 1)              # (M, 15)
    return np.einsum('mk,nkc->nmc', b, c)
fB = colour(cB, d @ unit(MB))                              # local direction = R_B^T d
fC = colour(cC, d @ unit(MC))
check("view-dependent colour", np.abs(fB - fC).max() < 1e-3, f"max colour difference {np.abs(fB - fC).max():.2e}")
# export: exactly the visible splats
path = D + "merged.ply"
n = ply_io.write_pointcloud_ply(path, C.data)
check("export writes the visible splats", n == nA + nB - 500, f"{n}")
bpy.ops.wm.ply_import(filepath=path)
check("export re-imports", len(bpy.context.selected_objects[0].data.points) == n)
bpy.data.objects.remove(bpy.context.selected_objects[0])
print("RESULT", "OK" if not FAILS else f"{len(FAILS)} failed: {FAILS}", flush=True)
sys.exit(1 if FAILS else 0)
