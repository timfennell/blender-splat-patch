"""Headless check of the selection volumes. Run:
    blender -b --factory-startup --python tests/test_volumes.py -- <scan.ply>
Adds each shape, turns and scales it unevenly, and checks Select / Deselect / Only against the
shape worked out directly; that erased splats and a work area's locked surroundings are never
selected; and that a low-detail stand-in is refused."""
import bpy, sys, os, math, numpy as np
from mathutils import Euler, Vector
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet
from gs_patch import lod, volumes
SCAN = sys.argv[sys.argv.index("--") + 1]
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)

for o in list(bpy.data.objects): bpy.data.objects.remove(o)
bpy.ops.wm.ply_import(filepath=SCAN)
A = bpy.context.selected_objects[0]
A.location = (0.3, -0.2, 0.1); A.rotation_euler = (0.2, 0.0, 0.7)
ctx = bpy.context
ctx.view_layer.update()
S0 = SplatSet.read(A.data)
Mw = np.array(A.matrix_world)
world = S0.pos @ Mw[:3, :3].T + Mw[:3, 3]
centre = np.median(world, 0)
# erase a few so we can check they're never selected
m = np.zeros(S0.n, bool); m[::50] = True; S0.hide(m); S0.write(A.data)
hidden = m

def expect(vol):
    q = volumes.to_volume_space(vol, A, S0.pos)
    return volumes.inside(vol[volumes.SHAPE_KEY], q) & ~hidden

def select_A():
    for o in ctx.view_layer.objects: o.select_set(False)
    A.select_set(True); ctx.view_layer.objects.active = A

for shape in ('BOX', 'SPHERE', 'CYLINDER', 'CONE'):
    select_A()
    ctx.scene.cursor.location = Vector(centre.tolist())
    r = bpy.ops.gsp.add_volume(shape=shape)
    vol = ctx.view_layer.objects.active
    check(f"{shape}: added and active", r == {'FINISHED'} and volumes.shape_of(vol) == shape
          and vol.hide_render and ctx.scene.gsp.volume == vol)
    vol.rotation_euler = Euler((0.4, -0.3, 1.1))
    vol.scale = Vector(vol.scale) * Vector((2.0, 1.0, 0.6))
    ctx.view_layer.update()
    want = expect(vol)
    r = bpy.ops.gsp.volume_select(action='ONLY')
    sel = SplatSet.read(A.data).selected.copy()
    check(f"{shape}: Only selects exactly the inside", r == {'FINISHED'} and np.array_equal(sel, want)
          and 0 < want.sum() < S0.n, f"{int(want.sum()):,}")
    check(f"{shape}: erased splats stay unselected", not sel[hidden].any())
    # select something else, then add and remove with the volume
    select_A(); bpy.ops.gsp.select_all(action='SELECT')
    ctx.view_layer.objects.active = vol
    bpy.ops.gsp.volume_select(action='DESELECT')
    sel = SplatSet.read(A.data).selected
    check(f"{shape}: Deselect clears only the inside", not sel[want].any() and sel[~want & ~hidden].all())
    bpy.ops.gsp.volume_select(action='SELECT')
    sel = SplatSet.read(A.data).selected
    check(f"{shape}: Select adds it back", sel[~hidden].all())
    r = bpy.ops.gsp.remove_volume()
    check(f"{shape}: removed", r == {'FINISHED'} and not any(volumes.shape_of(o) for o in bpy.data.objects))

# the cone points its tip along +Z: a point just under the tip is inside, beside it isn't
q = np.array([[0, 0, 0.9], [0.2, 0, 0.9], [0.9, 0, -0.9], [0, 0, -1.1]])
check("cone shape", list(volumes.inside('CONE', q)) == [True, False, True, False])

# a work area: its locked surroundings are never selected
select_A()
bpy.ops.gsp.low_detail(fraction=0.1)
bpy.ops.gsp.add_volume(shape='BOX')
vol = ctx.view_layer.objects.active
check("Select refused on a low-detail stand-in", not bpy.ops.gsp.volume_select.poll())
select_A()
ctx.scene.cursor.location = Vector(centre.tolist())
size = float(max(A.dimensions)) * 0.15
bpy.ops.gsp.focus_workarea(radius=size, fraction=0.1)
vol.scale = (size * 3, size * 3, size * 3); ctx.view_layer.update()
ctx.view_layer.objects.active = vol
r = bpy.ops.gsp.volume_select(action='ONLY')
H = SplatSet.read(A.data)
check("work area: only unlocked splats selected", r == {'FINISHED'} and H.selected.any()
      and not H.selected[H.locked].any(), f"{int(H.selected.sum()):,}")
print("RESULT", "OK" if not FAILS else f"FAILED {FAILS}")
