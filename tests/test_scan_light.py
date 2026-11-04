"""Headless check of Convert to Splats > Light from Scan (needs Spirula Render installed). Run:
    blender -b --factory-startup --python tests/test_scan_light.py
A white ball inside a splat "room" (an enclosing sphere of splats) must come out as bright
as the room, so a converted asset matches a scan's exposure with no tuning; a bright orange
window must light and tint the side facing it; a splat floor must get a contact shadow layer
that fades with distance, while the scan itself is untouched; a chrome ball must reflect the
room; and the scene must be put back as it was."""
import bpy, sys, os, math, addon_utils, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet
from gs_patch import mesh_sh, mesh_convert as mc
addon_utils.enable("bl_ext.user_default.spirula_render", default_set=False)
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)
check("Spirula Render available", mc.spirula_available())

sc = bpy.context.scene
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
sc.cycles.device = 'CPU'

def splat_object(name, pos, nrm, sigma, display):
    """Flat splats facing along nrm, colour = display value (no view dependence)."""
    n = len(pos)
    ref = np.where(np.abs(nrm[:, 2:3]) < 0.9, [[0, 0, 1.0]], [[1.0, 0, 0]])
    u1 = np.cross(ref, nrm); u1 /= np.linalg.norm(u1, axis=1, keepdims=True)
    u2 = np.cross(nrm, u1)
    q = mesh_sh.matrix_to_quat(np.stack([u1, u2, nrm], 2))
    rows = {"position": ['FLOAT_VECTOR', pos.astype(np.float32)],
            "scale": ['FLOAT_VECTOR', np.tile([sigma, sigma, sigma * 0.02], (n, 1)).astype(np.float32)],
            "rotation": ['QUATERNION', q.astype(np.float32)],
            "radiance:base": ['FLOAT4', np.concatenate([(np.asarray(display) - 0.5) / mesh_sh.SH_C0,
                                                         np.full((n, 1), 0.99)], 1).astype(np.float32)]}
    for k in range(15): rows[f"radiance:sh_{k}"] = ['FLOAT_VECTOR', np.zeros((n, 3), np.float32)]
    pc = bpy.data.pointclouds.new(name); SplatSet(n, rows).write(pc)
    ob = bpy.data.objects.new(name, pc); sc.collection.objects.link(ob)
    return ob

d = mesh_sh.fibonacci_dirs(60000)
ROOM = 0.6
room_col = np.full((len(d), 3), ROOM)
room = splat_object("room", 4.0 * d, -d, 0.07, room_col)

def principled(name, base, rough, metal=0.0):
    m = bpy.data.materials.new(name); p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*base, 1); p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal; return m
bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.4, location=(0, 0, 0.4))
ball = bpy.context.active_object; bpy.ops.object.shade_smooth()
ball.data.materials.append(principled("white", (1, 1, 1), 0.8))
sun = bpy.data.objects.new("lamp", bpy.data.lights.new("lamp", 'POINT')); sc.collection.objects.link(sun)
sun.location = (0, 0, 3)                       # must NOT light the ball: Spirula isn't relighting
sc.world = bpy.data.worlds.new("user world")
user_world = sc.world

p = sc.gsp
p.conv_lighting = 'SCAN'; p.conv_spacing = 0.02; p.conv_bake_samples = 64; p.conv_probe_res = 64
p.conv_degree = '3'
def convert(ob):
    for o in bpy.data.objects: o.select_set(False)
    ob.hide_set(False); ob.select_set(True); bpy.context.view_layer.objects.active = ob
    r = bpy.ops.gsp.convert_mesh()
    return r, next(o for o in bpy.data.objects if o.get(mc.SOURCE_KEY) == ob.name)

def looks(obj, view_from=None):
    """Display colour of each splat seen face-on (or from direction view_from)."""
    S = SplatSet.read(obj.data)
    base = 0.5 + mesh_sh.SH_C0 * S.dc.astype(np.float64)
    rest = np.stack([S.get(n) for n in S.sh_names()], 1).astype(np.float64)
    c = S.pos - np.array(ball.location)
    nrm = c / np.linalg.norm(c, axis=1, keepdims=True)
    v = -nrm if view_from is None else np.repeat(-np.asarray(view_from, float)[None], S.n, 0)
    return base + np.einsum('nk,nkc->nc', mesh_sh.sh_basis_full(v)[:, :rest.shape[1]], rest), S, nrm

# 1. exposure: a white ball in a uniform room comes out as bright as the room
p.conv_contact_shadows = False
r, conv = convert(ball)
col, S, nrm = looks(conv)
expect = mesh_sh.srgb(mesh_sh.lin(ROOM) * 0.96)        # Cycles' white diffuse colour is 0.96
med = np.median(col, 0)
check("converted", r == {'FINISHED'})
check("white ball in a uniform room matches the room's brightness", np.abs(med - expect).max() < 0.06,
      f"{med.round(3)} vs {expect:.3f}")
check("the lamp didn't light it (Spirula isn't relighting)", np.abs(med - expect).max() < 0.06)
check("user's World and lamp are back", sc.world == user_world and not sun.hide_render
      and not room.hide_render and ball.hide_render)

# flat faces too: the mesh being baked mustn't be shadowed by its own (coincident) source
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(1.2, 0, 0.25)); cube = bpy.context.active_object
cube.data.materials.append(principled("white cube", (1, 1, 1), 0.8))
r, cc = convert(cube)
Sc = SplatSet.read(cc.data)
topm = Sc.pos[:, 2] > 0.499
tb = 0.5 + mesh_sh.SH_C0 * Sc.dc.astype(np.float64)[topm]
trest = np.stack([Sc.get(n) for n in Sc.sh_names()], 1).astype(np.float64)[topm]
face_on = tb + np.einsum('nk,nkc->nc', mesh_sh.sh_basis_full(np.tile([[0, 0, -1.0]], (topm.sum(), 1))), trest)
check("flat top face lit like the ball", np.abs(np.median(face_on, 0) - med).max() < 0.08,
      f"cube top {np.median(face_on, 0).round(3)}, ball {med.round(3)}")
bpy.data.objects.remove(cc); bpy.data.objects.remove(cube)

# 2. a bright orange window on the +X side lights and tints that side
win = (d[:, 0] > 0.75)
room_col[win] = (1.0, 0.55, 0.15)
room.data.attributes["radiance:base"].data.foreach_set(
    "vector", np.concatenate([(room_col - 0.5) / mesh_sh.SH_C0, np.full((len(d), 1), 0.99)], 1).astype(np.float32).ravel())
room.data.update_tag()
room.data["gsp_revision"] = int(room.data.get("gsp_revision", 0)) + 1   # as gs_patch's edits do: Spirula re-reads it
r, conv = convert(ball)
col, S, nrm = looks(conv)
px, nx = col[nrm[:, 0] > 0.8].mean(0), col[nrm[:, 0] < -0.8].mean(0)
# the window is about as bright as the room overall but orange: red rises, blue falls
check("window side brighter in red", px[0] > nx[0] + 0.1, f"+X {px.round(3)}  -X {nx.round(3)}")
check("window side tinted orange", px[0] - px[2] > 0.15 and abs(nx[0] - nx[2]) < 0.05,
      f"+X r-b {px[0] - px[2]:.3f}, -X r-b {nx[0] - nx[2]:.3f}")

# 3. a splat floor gets a contact shadow layer that fades with distance; the scan is untouched
g = np.arange(-2.5, 2.5, 0.04)
X, Y = np.meshgrid(g, g); keep = X ** 2 + Y ** 2 < 2.5 ** 2
fpos = np.stack([X[keep], Y[keep], np.zeros(keep.sum())], 1)
floor = splat_object("floor", fpos, np.tile([0, 0, 1.0], (len(fpos), 1)), 0.035, np.full((len(fpos), 3), 0.5))
before = {a.name: a.data[0:].__len__() for a in floor.data.attributes}
fb = np.empty(len(fpos) * 4, np.float32); floor.data.attributes["radiance:base"].data.foreach_get("vector", fb)
p.conv_contact_shadows = True
import time; t = time.time()
r, conv = convert(ball)
print(f"   (convert with contact shadows: {time.time() - t:.1f} s)")
sh = next((o for o in bpy.data.objects if o.get(mc.SHADOW_KEY) == ball.name), None)
check("contact shadow layer made", r == {'FINISHED'} and sh is not None)
T = SplatSet.read(sh.data)
dist = np.linalg.norm(T.pos[:, :2], axis=1)
a = T.opacity
inner, outer = a[dist < 0.1].mean(), a[(dist > 0.5) & (dist < 0.7)].mean() if ((dist > 0.5) & (dist < 0.7)).any() else 0.0
check("shadow dark at the contact, fading outwards", inner > 0.3 and outer < inner * 0.5,
      f"opacity near {inner:.3f}, at 0.5-0.7 {outer:.3f}, {T.n:,} shadow splats")
check("shadow splats are black and just above the floor",
      np.allclose(0.5 + mesh_sh.SH_C0 * T.dc, 0, atol=1e-4) and T.pos[:, 2].min() > 0 and T.pos[:, 2].max() < 0.01)
fa = np.empty(len(fpos) * 4, np.float32); floor.data.attributes["radiance:base"].data.foreach_get("vector", fa)
check("the scan itself is unchanged", np.array_equal(fa, fb) and len(floor.data.points) == len(fpos))
col, S, nrm = looks(conv)
top, bottom = col[nrm[:, 2] > 0.8].mean(), col[nrm[:, 2] < -0.8].mean()
check("underside darker (it faces the shadowed, darker floor)", bottom < top - 0.05, f"top {top:.3f} bottom {bottom:.3f}")

# 4. a chrome ball reflects the room (in bands 1-3), orange towards the window
ball.data.materials[0] = principled("chrome", (0.95, 0.95, 0.95), 0.15, 1.0)
p.conv_contact_shadows = False
r, conv = convert(ball)
S = SplatSet.read(conv.data)
rest = np.stack([S.get(n) for n in S.sh_names()], 1).astype(np.float64)
check("chrome: reflection in the view-dependent colour", np.abs(rest).mean() > 0.02, f"mean |rest| {np.abs(rest).mean():.3f}")
col_px, _, nrm = looks(conv)                         # face-on: a +X-facing splat mirrors the +X room
w_side = col_px[nrm[:, 0] > 0.9].mean(0); o_side = col_px[nrm[:, 0] < -0.9].mean(0)
check("chrome: mirrors the orange window on that side", w_side[0] - w_side[2] > 0.2 and w_side[0] > o_side[0] + 0.1,
      f"window side {w_side.round(3)}, far side {o_side.round(3)}")

left = [o.name for o in bpy.data.objects if o.name.startswith("_gsp")] + \
       [m.name for m in bpy.data.materials if m.name.startswith("_gsp")] + \
       [i.name for i in bpy.data.images if i.name.startswith("_gsp")] + \
       [w.name for w in bpy.data.worlds if w.name.startswith("_gsp")]
check("no temporary data left behind", not left, str(left))
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
