"""Headless check of Convert to Splats. Run:
    blender -b --factory-startup --python tests/test_convert.py
Converts a moved, rotated and scaled sphere and a checkered metal cube, then checks the
splats sit on the surface and cover it, face the right way, look like the material under
a light on the camera, relight like Lambert shading through Vintage Click's relight formula,
merge and export with a scan as one PLY, convert again in place, bake scene lighting,
reflect the world, and go two-sided."""
import bpy, sys, os, math, tempfile, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gs_patch; gs_patch.register()
from gs_patch.splats import SplatSet
from gs_patch import mesh_sh, mesh_convert
D = tempfile.mkdtemp() + "/"
FAILS = []
def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok: FAILS.append(label)

sc = bpy.context.scene
for o in list(bpy.data.objects): bpy.data.objects.remove(o)
sc.cycles.device = 'CPU'

def principled(name, base, rough, metal=0.0):
    m = bpy.data.materials.new(name)
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*base, 1)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    return m

# A sphere, moved, rotated and scaled: world radius 0.75 around (1, 2, 0.5).
bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.5)
sph = bpy.context.active_object
bpy.ops.object.shade_smooth()
sph.location = (1, 2, 0.5); sph.rotation_euler = (0.3, 0.2, 0.9); sph.scale = (1.5, 1.5, 1.5)
ALB = (0.6, 0.15, 0.05)
sph.data.materials.append(principled("red", ALB, 0.5))
# A metal cube with a procedural checker (no UVs of its own).
bpy.ops.mesh.primitive_cube_add(size=0.6, location=(-1, 0, 0.3))
cube = bpy.context.active_object
m = principled("checker", (1, 1, 1), 0.4, 1.0)
nt = m.node_tree
chk = nt.nodes.new("ShaderNodeTexChecker"); chk.inputs["Scale"].default_value = 3
chk.inputs["Color1"].default_value = (0.9, 0.9, 0.9, 1); chk.inputs["Color2"].default_value = (0.2, 0.2, 0.2, 1)
nt.links.new(chk.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
cube.data.materials.append(m)
cube.data.uv_layers.remove(cube.data.uv_layers[0])
bpy.context.view_layer.update()

ENGINE = sc.render.engine
p = sc.gsp
p.conv_spacing = 0.01
p.conv_degree = 'AUTO'
for o in bpy.data.objects: o.select_set(o in (sph, cube))
bpy.context.view_layer.objects.active = sph
r = bpy.ops.gsp.convert_mesh()
check("convert ran", r == {'FINISHED'}, str(r))
S_obj = next(o for o in bpy.data.objects if o.get(mesh_convert.SOURCE_KEY) == sph.name)
C_obj = next(o for o in bpy.data.objects if o.get(mesh_convert.SOURCE_KEY) == cube.name)
check("meshes hidden", sph.hide_get() and cube.hide_get() and sph.hide_render)
S = SplatSet.read(S_obj.data)
check("degree 3 when the scene has no scans", len(S.sh_names()) == 15, str(len(S.sh_names())))
area = 4 * math.pi * 0.75 ** 2
check("splat count matches spacing", 0.7 < S.n * 0.01 ** 2 / area < 1.6, f"{S.n:,} splats")

c = np.array([1, 2, 0.5])
rad = np.linalg.norm(S.pos - c, axis=1)
# a 48 x 24 UV sphere sags up to r (1 - cos(pi / 24)) = 0.0064 between its vertices
check("splats on the surface", np.abs(rad - 0.75).max() < 0.0065, f"radius {rad.min():.4f}..{rad.max():.4f}")
q = S.get("rotation").astype(np.float64)
w, x, y, z = (q / np.linalg.norm(q, axis=1, keepdims=True)).T
axis3 = np.stack([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)], 1)
radial = (S.pos - c) / rad[:, None]
check("splats lie flat on the surface", np.abs((axis3 * radial).sum(1)).min() > 0.98,
      f"min |cos| {np.abs((axis3 * radial).sum(1)).min():.3f}")
sc_ = S.get("scale")
check("flat splats", np.median(sc_[:, 2] / sc_[:, 0]) < 0.05 and 0.003 < np.median(sc_[:, 0]) < 0.015,
      f"median scale {np.median(sc_, 0)}")
# coverage: random surface points all have a splat within one spacing
from mathutils.kdtree import KDTree
kd = KDTree(S.n)
for i, pt in enumerate(S.pos): kd.insert(pt, i)
kd.balance()
rng = np.random.default_rng(0)
u = rng.normal(size=(3000, 3)); u /= np.linalg.norm(u, axis=1, keepdims=True)
gap = max(kd.find(c + 0.75 * v)[2] for v in u)
check("no holes (seams included)", gap < 0.0085, f"largest gap to a splat {gap:.4f}")

# colour: rebuild what the game sees
dc = S.dc.astype(np.float64); base = 0.5 + mesh_sh.SH_C0 * dc
rest = np.stack([S.get(n) for n in S.sh_names()], 1).astype(np.float64)
def sh_at(i, d, lo, hi):
    B = mesh_sh.sh_basis_full(d)
    return np.einsum('nk,nkc->nc', B[:, lo:hi], rest[i, lo:hi])
idx = np.arange(0, S.n, 37)
facing = base[idx] + sh_at(idx, -radial[idx], 0, 15)
truth = mesh_sh.srgb(np.array(ALB) * (1 - 0.04) + 0.04 * mesh_sh.ggx_headlight(np.array(1.0), 0.5))
err = np.abs(np.median(facing, 0) - truth).max()
check("looks like the material facing the light", err < 0.08, f"{np.median(facing, 0).round(3)} vs {truth.round(3)}")
for ang in (0, 45, 75):
    t = math.radians(ang)
    perp = np.cross(radial[idx], [0.3, 0.5, 0.8]); perp /= np.linalg.norm(perp, axis=1, keepdims=True)
    l = math.cos(t) * radial[idx] + math.sin(t) * perp
    relit = mesh_sh.lin(np.maximum(base[idx] + sh_at(idx, -l, 0, 3), 0))
    lam = np.array(ALB) * 0.96 * math.cos(t)
    rel = np.abs(np.median(relit, 0)[0] - lam[0]) / lam[0]
    check(f"relights like Lambert, light {ang} deg", rel < 0.25, f"red {np.median(relit, 0)[0]:.3f} vs {lam[0]:.3f}")
behind = mesh_sh.lin(np.maximum(base[idx] + sh_at(idx, radial[idx], 0, 3), 0))
check("dark when lit from behind", np.median(behind) < 0.05, f"{np.median(behind):.3f}")

Cs = SplatSet.read(C_obj.data)
cbase = 0.5 + mesh_sh.SH_C0 * Cs.dc
crest = np.stack([Cs.get(n) for n in Cs.sh_names()], 1).astype(np.float64)
cn = Cs.pos - np.array([-1, 0, 0.3]); ax = np.argmax(np.abs(cn), 1)
cnrm = np.zeros_like(cn); cnrm[np.arange(len(cn)), ax] = np.sign(cn[np.arange(len(cn)), ax])
lum = (cbase + np.einsum('nk,nkc->nc', mesh_sh.sh_basis_full(-cnrm), crest)).mean(1)   # facing the camera light
check("checker texture came through", np.percentile(lum, 90) - np.percentile(lum, 10) > 0.2,
      f"10th/90th percentile {np.percentile(lum, 10):.3f} / {np.percentile(lum, 90):.3f}")
shine = np.abs(crest[:, 3:15]).sum((1, 2))
check("metal keeps its shine in bands 2-3", np.median(shine) > 0.2, f"median |bands 2-3| {np.median(shine):.3f}")

# merge with a fake scan (degree 3) and export one PLY
n = 500
scan = {"position": ['FLOAT_VECTOR', rng.normal(size=(n, 3)).astype(np.float32)],
        "scale": ['FLOAT_VECTOR', np.full((n, 3), 0.01, np.float32)],
        "rotation": ['QUATERNION', np.tile([1, 0, 0, 0], (n, 1)).astype(np.float32)],
        "radiance:base": ['FLOAT4', np.tile([0.1, 0.2, 0.3, 0.8], (n, 1)).astype(np.float32)]}
for k in range(15): scan[f"radiance:sh_{k}"] = ['FLOAT_VECTOR', np.zeros((n, 3), np.float32)]
pc = bpy.data.pointclouds.new("scan"); SplatSet(n, scan).write(pc)
scan_obj = bpy.data.objects.new("scan", pc); sc.collection.objects.link(scan_obj)
scan_obj.location = (0.5, 0, 0)
for o in bpy.data.objects: o.select_set(o in (scan_obj, S_obj, C_obj))
bpy.context.view_layer.objects.active = scan_obj
r = bpy.ops.gsp.merge_splats(originals='KEEP')
merged = bpy.context.view_layer.objects.active
check("merge ran", r == {'FINISHED'} and len(merged.data.points) == n + S.n + Cs.n, str(len(merged.data.points)))
path = D + "scene.ply"
r = bpy.ops.gsp.export_ply(filepath=path)
head = open(path, "rb").read(4000).split(b"end_header")[0].decode()
nrest = head.count("f_rest_")
check("one PLY, degree 3 throughout", r == {'FINISHED'} and nrest == 45 and f"element vertex {n + S.n + Cs.n}" in head,
      f"{nrest} f_rest")
bpy.data.objects.remove(merged)

# convert again in place, coarser
p.conv_spacing = 0.02
for o in bpy.data.objects: o.select_set(False)
S_obj.select_set(True); bpy.context.view_layer.objects.active = S_obj
r = bpy.ops.gsp.convert_mesh()
again = [o for o in bpy.data.objects if o.get(mesh_convert.SOURCE_KEY) == sph.name]
check("convert again refreshes the same object", r == {'FINISHED'} and again == [S_obj]
      and 0.15 < len(S_obj.data.points) / S.n < 0.4, f"{len(S_obj.data.points):,} splats")

# degree follows the scene's scans
for k in range(8, 15): pc.attributes.remove(pc.attributes[f"radiance:sh_{k}"])
check("degree matches the scene's scans", mesh_convert.scene_sh_degree(sc) == 2)
bpy.data.objects.remove(scan_obj)

# bake scene lighting: a sun from +X; the lit side is brighter, no view-dependent colour
sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", 'SUN'))
sun.data.energy = 3; sun.rotation_euler = (0, math.radians(90), 0)   # shines towards -X, lighting the +X side
sc.collection.objects.link(sun)
p.conv_lighting = 'BAKED'; p.conv_bake_samples = 16; p.conv_degree = '3'
for o in bpy.data.objects: o.select_set(False)
sph.hide_set(False); sph.select_set(True); bpy.context.view_layer.objects.active = sph
r = bpy.ops.gsp.convert_mesh()
B = SplatSet.read(S_obj.data)
bb = 0.5 + mesh_sh.SH_C0 * B.dc
side = (B.pos[:, 0] - 1) / 0.75
lit, dark = bb[side > 0.7].mean(), bb[side < -0.7].mean()
brest = np.stack([B.get(n) for n in B.sh_names()], 1)
check("baked lighting: lit side brighter", r == {'FINISHED'} and lit > dark + 0.2, f"lit {lit:.3f} dark {dark:.3f}")
check("baked lighting: flat colour", np.abs(brest).max() == 0)
bpy.data.objects.remove(sun)

# reflections: a blue world reflected in a smooth metal sphere
world = bpy.data.worlds.new("w"); sc.world = world
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.1, 0.8, 1)
sph.data.materials[0] = principled("chrome", (0.9, 0.9, 0.9), 0.2, 1.0)
p.conv_lighting = 'HEADLIGHT'; p.conv_reflections = False; p.conv_spacing = 0.03
for o in bpy.data.objects: o.select_set(False)
sph.hide_set(False); sph.select_set(True); bpy.context.view_layer.objects.active = sph
bpy.ops.gsp.convert_mesh()
plain = np.stack([n_ for n_ in (S_obj.data.attributes[f"radiance:sh_{k}"] for k in range(3, 15))], 0)
def bands23(obj):
    Sx = SplatSet.read(obj.data)
    return np.stack([Sx.get(f"radiance:sh_{k}") for k in range(3, 15)], 1).astype(np.float64), Sx
r0, _ = bands23(S_obj)
p.conv_reflections = True; p.conv_probe_res = 32
for o in bpy.data.objects: o.select_set(False)
sph.hide_set(False); sph.select_set(True); bpy.context.view_layer.objects.active = sph
r = bpy.ops.gsp.convert_mesh()
r1, Sx = bands23(S_obj)
diff = (r1 - r0)
blue = np.abs(diff[..., 2]).mean() > 2 * np.abs(diff[..., 0]).mean()
check("reflections add the blue world", r == {'FINISHED'} and np.abs(diff).mean() > 0.01 and blue,
      f"mean |change| r,g,b {np.abs(diff).mean((0, 1)).round(4)}")
leftovers = [o.name for o in bpy.data.objects if o.name.startswith("_gsp")] + \
            [m.name for m in bpy.data.materials if m.name.startswith("_gsp")] + \
            [i.name for i in bpy.data.images if i.name.startswith("_gsp")]
check("no temporary data left behind", not leftovers, str(leftovers))
check("scene settings restored", sc.render.engine == ENGINE and sc.camera is None, sc.render.engine)

# two-sided plane
bpy.ops.mesh.primitive_plane_add(size=0.5, location=(0, -2, 0))
pl = bpy.context.active_object; pl.data.materials.append(principled("leaf", (0.1, 0.5, 0.1), 0.6))
p.conv_reflections = False; p.conv_two_sided = True; p.conv_spacing = 0.01
for o in bpy.data.objects: o.select_set(o == pl)
bpy.ops.gsp.convert_mesh()
P = SplatSet.read(next(o for o in bpy.data.objects if o.get(mesh_convert.SOURCE_KEY) == pl.name).data)
half = P.n // 2
pb = 0.5 + mesh_sh.SH_C0 * P.dc.astype(np.float64)
pr = np.stack([P.get(n) for n in P.sh_names()], 1).astype(np.float64)
up = np.array([[0, 0, 1.0]])
def look(i, d):
    return pb[i] + np.einsum('nk,nkc->nc', mesh_sh.sh_basis_full(np.repeat(d, len(i), 0)), pr[i])
from_top = look(np.arange(half), -up).mean(0)       # camera above looking down at the front
from_below = look(np.arange(half, P.n), up).mean(0)  # camera below looking up at the back
check("two-sided: both faces look the same", np.abs(from_top - from_below).max() < 0.02 and P.n % 2 == 0,
      f"top {from_top.round(3)} below {from_below.round(3)}")

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
