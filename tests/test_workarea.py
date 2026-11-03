"""Headless check of the regional work area. Run:
    blender -b --factory-startup --python tests/test_workarea.py -- <scan.ply>
Opens a full-detail sphere on a low-detail scan; checks what's inside and outside, that the
tools ignore the locked surroundings, that erases, clones and removals go back into the full
model (on sync, on moving the area, before a render, on export and on bake), and that leaving
it brings back a stand-in built from the edited model."""
import bpy, sys, os, tempfile, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet, FULL_INDEX_ATTR, HIDDEN_ATTR, ADDED_ATTR
from gs_patch import lod, ops, ply_io
D = tempfile.mkdtemp() + "/"
SCAN = sys.argv[sys.argv.index("--") + 1]
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)

def col(pc, name, width=1, key="value", dtype=bool):
    a = np.empty(len(pc.points) * width, dtype)
    pc.attributes[name].data.foreach_get(key, a)
    return a.reshape(-1, width)

for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.wm.ply_import(filepath=SCAN)
A = bpy.context.selected_objects[0]; A.name = "A"
ctx = bpy.context
ctx.view_layer.objects.active = A
full0 = A.data
pos0 = col(full0, "position", 3, "vector", np.float32)
n0 = len(pos0)
centre = np.median(pos0, axis=0)
extent = float(np.max(pos0.max(0) - pos0.min(0)))
R = 0.15 * extent
want_in = int((np.linalg.norm(pos0 - centre, axis=1) < R).sum())
bpy.ops.gsp.low_detail(fraction=0.1)
ctx.scene.cursor.location = A.matrix_world @ __import__("mathutils").Vector(centre.tolist())
r = bpy.ops.gsp.focus_workarea(radius=R * A.matrix_world.to_scale()[0], fraction=0.1)
check("focus ran", r == {'FINISHED'} and lod.is_workarea(A) and not lod.is_proxy(A))
full = lod.full_data(A)
check("full model behind it", full == full0 and len(full.points) == n0)
H = SplatSet.read(A.data)
fi = H.get(FULL_INDEX_ATTR)[:, 0]
locked = H.locked
check("every splat in the sphere at full detail", int((fi >= 0).sum()) == want_in, f"{want_in:,}")
check("only locked stand-in splats outside", locked.sum() == (fi < 0).sum() and locked.sum() > 0
      and np.all(np.linalg.norm(H.pos[locked] - centre, axis=1) >= R * 0.999), f"{int(locked.sum()):,} locked")
check("region rows are exact copies", np.array_equal(H.pos[fi >= 0], pos0[fi[fi >= 0]]))
check("SH kept inside", any(a.name.startswith("radiance:sh_") for a in A.data.attributes))
check("tools see only the region", np.array_equal(H.visible, fi >= 0)
      and np.all(fi[H.near(centre, extent * 10)] >= 0))
check("the brushes accept a work area", bpy.ops.gsp.select_all.poll())
bpy.ops.gsp.select_all(action='SELECT')
H = SplatSet.read(A.data)
check("select all skips locked", not H.selected[H.locked].any() and H.selected[~H.locked].all())
bpy.ops.gsp.select_all(action='DESELECT')

# erase 500 region splats, clone 300, as the brush would (write + revision)
H = SplatSet.read(A.data)
region = np.nonzero(~H.locked)[0]
erase = region[:500]
m = np.zeros(H.n, bool); m[erase] = True
H.hide(m)
H.append_copies(region[1000:1300], {"position": H.pos[region[1000:1300]] + 0.001})
erased_full = H.get(FULL_INDEX_ATTR)[erase, 0].copy()
H.write(A.data); ops.bump_revision(A)
check("clones are new to the full model", np.all(H.get(FULL_INDEX_ATTR)[-300:, 0] == -1))
check("sync wrote", lod.sync(A))
check("erases reached the full model", col(full, HIDDEN_ATTR)[erased_full, 0].all()
      and int(col(full, HIDDEN_ATTR).sum()) == 500)
check("clones appended to the full model", len(full.points) == n0 + 300
      and int(col(full, ADDED_ATTR).sum()) == 300)
fi = col(A.data, FULL_INDEX_ATTR, dtype=np.int32)[:, 0]
check("clones got their rows", np.array_equal(fi[-300:], np.arange(n0, n0 + 300)))
check("sync again is a no-op", not lod.sync(A))

# remove 100 of the synced clones (what Restore does), then move the area
H = SplatSet.read(A.data)
keep = np.ones(H.n, bool); keep[-100:] = False
H.keep(keep, stash=False); H.write(A.data); ops.bump_revision(A)
fpos = col(full, "position", 3, "vector", np.float32)
before = fpos[: n0 + 200].copy()
lod.sync(A)
check("removed clones leave the full model", len(full.points) == n0 + 200
      and np.array_equal(col(full, "position", 3, "vector", np.float32), before))
fi = col(A.data, FULL_INDEX_ATTR, dtype=np.int32)[:, 0]
H = SplatSet.read(A.data)
check("rows still line up", np.array_equal(H.pos[fi >= 0], col(full, "position", 3, "vector", np.float32)[fi[fi >= 0]]))

# an edit then a render: render_pre writes it back
H = SplatSet.read(A.data)
reg = np.nonzero(H.visible)[0]
m = np.zeros(H.n, bool); m[reg[2000:2100]] = True
H.hide(m); H.write(A.data); ops.bump_revision(A)
ops.sync_workareas()
check("render handler syncs", int(col(full, HIDDEN_ATTR).sum()) == 600)
check("render handler registered", ops.sync_workareas in bpy.app.handlers.render_pre)

# moving the area writes back and re-centres
H = SplatSet.read(A.data)
reg = np.nonzero(H.visible)[0]
m = np.zeros(H.n, bool); m[reg[3000:3050]] = True
H.hide(m); H.write(A.data); ops.bump_revision(A)
c2 = centre + np.array([R * 0.5, 0, 0])
lod.open_workarea(A, c2, R, 0.1)
check("moving writes back first", int(col(full, HIDDEN_ATTR).sum()) == 650)
check("moved", np.allclose(lod.workarea(A)[0], c2))
check("old work cloud removed", sum(1 for pc in bpy.data.pointclouds if lod.WORK_KEY in pc) == 1)

# export reads the synced full model
H = SplatSet.read(A.data)
reg = np.nonzero(H.visible)[0]
m = np.zeros(H.n, bool); m[reg[:10]] = True
H.hide(m); H.write(A.data); ops.bump_revision(A)
r = bpy.ops.gsp.export_ply(filepath=D + "w.ply")
check("export syncs and writes the visible full model", r == {'FINISHED'}
      and int(col(full, HIDDEN_ATTR).sum()) == 660)

# bake keeps the work area
r = bpy.ops.gsp.bake_edits()
check("bake on a work area", r == {'FINISHED'} and lod.is_workarea(A)
      and len(lod.full_data(A).points) == n0 + 200 - 660, f"{len(lod.full_data(A).points):,}")
H = SplatSet.read(A.data)
fi = H.get(FULL_INDEX_ATTR)[:, 0]
check("rows line up after bake", np.array_equal(H.pos[fi >= 0],
      col(lod.full_data(A), "position", 3, "vector", np.float32)[fi[fi >= 0]]))

# save / reopen with unsynced edits
m = np.zeros(H.n, bool); m[np.nonzero(fi >= 0)[0][:5]] = True
H.hide(m); H.write(A.data); ops.bump_revision(A)
path = D + "w.blend"
bpy.ops.wm.save_as_mainfile(filepath=path)
bpy.ops.wm.open_mainfile(filepath=path)
A = bpy.data.objects["A"]
check("work area survives reopening", lod.is_workarea(A))
check("unsynced edits sync after reopening", lod.sync(A) and int(col(lod.full_data(A), HIDDEN_ATTR).sum()) == 5)

# leave: stand-in again, region mark gone
bpy.context.view_layer.objects.active = A
r = bpy.ops.gsp.leave_workarea()
full = lod.full_data(A)
check("leave gives a stand-in", r == {'FINISHED'} and lod.is_proxy(A))
check("region mark removed", full.attributes.get(lod.IN_WORK_ATTR) is None)
check("no work clouds left", not any(lod.WORK_KEY in pc for pc in bpy.data.pointclouds))
A.select_set(True)
bpy.ops.gsp.full_detail()
check("full detail after", not lod.is_standin(A) and len(A.data.points) == n0 + 200 - 660)
check("no stand-ins left", len(bpy.data.pointclouds) == 1, f"{[p.name for p in bpy.data.pointclouds]}")
print("RESULT", "OK" if not FAILS else f"FAILED {FAILS}")
