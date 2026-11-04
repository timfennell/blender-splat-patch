"""Splats from a mesh, part 2: the Blender side.

A temporary world-space copy of the mesh gets its own UV layout (Smart UV Project, so
texel density is even), Cycles bakes its materials into that layout (diffuse colour,
glossy colour = F0, roughness, shading normal; or the fully lit result), and every
covered texel becomes one flat splat. The source mesh and its materials are untouched:
the bake works on copies, and the textures still read the mesh's own UV maps.
"""

import math
import os
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import mesh_sh
from .splats import SplatSet, is_splat_object

SOURCE_KEY = "gsp_mesh_source"      # on a converted splat object: the mesh it came from
MAX_SPLATS = 30_000_000
MAX_RES = 8192
OPACITY = 0.995


class ConvertError(Exception):
    pass


# ------------------------------------------------------------------ settings
class Settings:
    def __init__(self, spacing=0.0, degree=3, lighting='HEADLIGHT', gain=1.0, shine=1.0,
                 reflections=False, reflect_strength=1.0, probe_res=64, bake_samples=64,
                 two_sided=False, contact_shadows=True, shadow_strength=1.0, shadow_reach=0.0):
        self.spacing = spacing
        self.degree = degree
        self.lighting = lighting
        self.gain = gain
        self.shine = shine
        self.reflections = reflections
        self.reflect_strength = reflect_strength
        self.probe_res = probe_res
        self.bake_samples = bake_samples
        self.two_sided = two_sided
        self.contact_shadows = contact_shadows
        self.shadow_strength = shadow_strength
        self.shadow_reach = shadow_reach


def scene_sh_degree(scene, default=3):
    """The highest SH degree among the scene's splat objects (what a merge must match)."""
    best = None
    for ob in scene.objects:
        if is_splat_object(ob) and SOURCE_KEY not in ob and "gsp_shadow_source" not in ob:
            k = sum(1 for a in ob.data.attributes if a.name.startswith("radiance:sh_"))
            d = int(round(math.sqrt(k + 1))) - 1
            best = d if best is None else max(best, d)
    return default if best is None else best


def auto_spacing(obj):
    """Splat spacing when none is set: 1/400 of the object's diagonal."""
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c[i] for c in corners) for i in range(3)))
    hi = Vector((max(c[i] for c in corners) for i in range(3)))
    return max((hi - lo).length / 400.0, 1e-6)


# --------------------------------------------------------- scene bookkeeping
class _SceneState:
    """Saves what the conversion changes in the scene and puts it back."""

    def __init__(self, context):
        self.scene = context.scene
        self.view_layer = context.view_layer
        self.engine = self.scene.render.engine
        self.samples = self.scene.cycles.samples if hasattr(self.scene, "cycles") else None
        self.active = self.view_layer.objects.active
        self.selected = [o for o in self.view_layer.objects if o.select_get()]
        r = self.scene.render
        self.render = (r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
                       r.image_settings.file_format, r.image_settings.color_depth,
                       r.film_transparent, self.scene.camera)
        self.hidden = []                # (object, hide_render) we changed

    def hide_render(self, obj):
        self.hidden.append((obj, obj.hide_render))
        obj.hide_render = True

    def restore(self):
        s = self.scene
        s.render.engine = self.engine
        if self.samples is not None:
            s.cycles.samples = self.samples
        r = s.render
        (r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
         r.image_settings.file_format, r.image_settings.color_depth,
         r.film_transparent, s.camera) = self.render
        # The temp objects are gone by now, and the view layer may still list them as None
        # until it is updated.
        self.view_layer.update()
        for o, h in self.hidden:
            try:
                o.hide_render = h
            except ReferenceError:
                pass
        for o in self.view_layer.objects:
            if o is not None:
                o.select_set(False)
        for o in self.selected:
            try:
                o.select_set(True)
            except (ReferenceError, RuntimeError):
                pass
        try:
            if self.active is not None:
                self.view_layer.objects.active = self.active
        except (ReferenceError, RuntimeError):
            pass


# ------------------------------------------------------------- temp geometry
def _temp_copy(context, obj):
    """A copy of the evaluated mesh (modifiers applied) on a temp object placed where obj
    is. It stays in obj's own space so Generated and Object texture coordinates match."""
    dg = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=dg)
    me.name = "_gsp_bake_mesh"
    src = obj.data
    if hasattr(src, "texspace_location"):
        me.use_auto_texspace = False
        me.texspace_location = src.texspace_location
        me.texspace_size = src.texspace_size
    # Object-linked materials don't come with the mesh: take what the slots show.
    mats = [s.material for s in obj.material_slots]
    me.materials.clear()
    for m in mats:
        me.materials.append(m)
    tmp = bpy.data.objects.new("_gsp_bake", me)
    context.scene.collection.objects.link(tmp)
    tmp.matrix_world = obj.matrix_world
    return tmp


def _to_world(M, pts=None, normals=None):
    """Points through the 4x4 M; normals through its inverse transpose (renormalised)."""
    M = np.asarray(M, np.float64)
    if pts is not None:
        return pts @ M[:3, :3].T + M[:3, 3]
    Nm = np.linalg.inv(M[:3, :3]).T
    n = normals @ Nm.T
    return n / np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-12)


def _remove_temp(tmp):
    me = tmp.data
    bpy.data.objects.remove(tmp)
    if me.users == 0:
        bpy.data.meshes.remove(me)


def _bake_uvs(context, tmp, spacing):
    """Give tmp an even-density UV layout for baking; return (uv layer name, resolution)."""
    me = tmp.data
    render_uv = next((u for u in me.uv_layers if u.active_render), None)
    me.calc_loop_triangles()
    vt = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get("vertices", vt)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    P = _to_world(np.array(tmp.matrix_world), pts=co.reshape(-1, 3).astype(np.float64))[vt].reshape(-1, 3, 3)
    area = 0.5 * np.linalg.norm(np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]), axis=1).sum()
    if area <= 0:
        raise ConvertError("the mesh has no surface area")
    est = area / spacing ** 2
    if est > MAX_SPLATS:
        raise ConvertError(f"about {est / 1e6:.0f} million splats at this spacing (the limit is "
                           f"{MAX_SPLATS / 1e6:.0f} million): increase Splat Spacing")
    res_guess = min(MAX_RES, max(64, int(math.sqrt(area / 0.65) / spacing)))
    uv = me.uv_layers.new(name="_gsp_bake")
    if render_uv is not None:
        render_uv.active_render = True        # textures keep reading the mesh's own UVs
    me.uv_layers.active = uv
    vl = context.view_layer
    for o in vl.objects:
        o.select_set(False)
    tmp.select_set(True)
    vl.objects.active = tmp
    with context.temp_override(active_object=tmp, object=tmp, selected_objects=[tmp],
                               selected_editable_objects=[tmp]):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.reveal()
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=min(0.05, 4.0 / res_guess),
                                 area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
        bpy.ops.object.mode_set(mode='OBJECT')
    me.calc_loop_triangles()
    tris = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get("loops", tris)
    uvs = np.empty(len(me.loops) * 2, np.float32)
    uv = me.uv_layers["_gsp_bake"]
    uv.data.foreach_get("uv", uvs)
    t = uvs.reshape(-1, 2)[tris].reshape(-1, 3, 2).astype(np.float64)
    uv_area = 0.5 * np.abs((t[:, 1, 0] - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1])
                           - (t[:, 2, 0] - t[:, 0, 0]) * (t[:, 1, 1] - t[:, 0, 1])).sum()
    res = int(math.ceil(math.sqrt(area / max(uv_area, 1e-9)) / spacing))
    if res > MAX_RES:
        raise ConvertError(f"the bake would need {res} px textures (the limit is {MAX_RES}): "
                           "increase Splat Spacing")
    return max(res, 16)


def _geometry(tmp, res):
    """Per covered texel: position, smooth normal, triangle frame (rotation, scale), texel."""
    me = tmp.data
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    loops = np.empty(nt * 3, np.int32)
    me.loop_triangles.foreach_get("loops", loops)
    verts = np.empty(nt * 3, np.int32)
    me.loop_triangles.foreach_get("vertices", verts)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    M = np.array(tmp.matrix_world, np.float64)
    co = _to_world(M, pts=co.reshape(-1, 3).astype(np.float64))
    cn = np.empty(len(me.loops) * 3, np.float32)
    me.corner_normals.foreach_get("vector", cn)
    cn = _to_world(M, normals=cn.reshape(-1, 3).astype(np.float64))
    uvs = np.empty(len(me.loops) * 2, np.float32)
    me.uv_layers["_gsp_bake"].data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2).astype(np.float64)

    P = co[verts].reshape(nt, 3, 3)
    N = cn[loops].reshape(nt, 3, 3)
    UV = uvs[loops].reshape(nt, 3, 2) * res
    tri, bary, px, py = mesh_sh.rasterize_uv(UV, res)
    if len(tri) == 0:
        raise ConvertError("no texels were covered (is the mesh too small for the spacing?)")
    pos = np.einsum('sk,skc->sc', bary, P[tri])
    nrm = np.einsum('sk,skc->sc', bary, N[tri])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
    rot, scale = mesh_sh.triangle_frames(P, UV)
    return pos, nrm, rot[tri], scale[tri], px, py


# -------------------------------------------------------------------- baking
class _Baker:
    """Bakes passes of tmp's materials (on copies of them) into one float image."""

    def __init__(self, context, tmp, res):
        self.context = context
        self.tmp = tmp
        self.img = bpy.data.images.new("_gsp_bake", res, res, alpha=True, float_buffer=True)
        self.img.colorspace_settings.name = 'Non-Color'
        self.res = res
        self.mats = []
        me = tmp.data
        if len(me.materials) == 0:
            me.materials.append(None)
        for i, m in enumerate(me.materials):
            if m is None:
                m = bpy.data.materials.new("_gsp_default")
            else:
                m = m.copy()
            m.name = "_gsp_bake_mat"
            n = m.node_tree.nodes.new("ShaderNodeTexImage")
            n.image = self.img
            n.select = True
            m.node_tree.nodes.active = n
            me.materials[i] = m
            self.mats.append(m)

    def bake(self, kind, px, py, samples=4, **kw):
        sc = self.context.scene
        sc.render.engine = 'CYCLES'
        sc.cycles.samples = samples
        vl = self.context.view_layer
        for o in vl.objects:
            o.select_set(False)
        self.tmp.select_set(True)
        vl.objects.active = self.tmp
        with self.context.temp_override(active_object=self.tmp, object=self.tmp,
                                        selected_objects=[self.tmp], selected_editable_objects=[self.tmp]):
            bpy.ops.object.bake(type=kind, margin=2, margin_type='EXTEND', use_clear=True,
                                target='IMAGE_TEXTURES', use_selected_to_active=False, **kw)
        buf = np.empty(self.res * self.res * 4, np.float32)
        self.img.pixels.foreach_get(buf)
        return buf.reshape(-1, 4)[py * self.res + px, :3].astype(np.float64)

    def free(self):
        for m in self.mats:
            bpy.data.materials.remove(m)
        bpy.data.images.remove(self.img)


# --------------------------------------------------------------- the probe
# Probe faces: (forward, right, up). Texel (i, j) of an n x n face (j counted from the
# bottom row, as Blender stores pixels) looks along forward + s right + t up, with s, t
# the texel centre mapped to [-1, 1]. The render camera uses the same axes.
_FACES = np.array([
    ((1, 0, 0), (0, -1, 0), (0, 0, 1)), ((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
    ((0, 1, 0), (1, 0, 0), (0, 0, 1)), ((0, -1, 0), (-1, 0, 0), (0, 0, 1)),
    ((0, 0, 1), (1, 0, 0), (0, -1, 0)), ((0, 0, -1), (1, 0, 0), (0, 1, 0))], np.float64)


def _face_dirs(n):
    """Unit directions of every texel of the six n x n faces, face by face, row by row."""
    c = (np.arange(n) + 0.5) / n * 2 - 1
    s, t = np.meshgrid(c, c)                          # rows: t, columns: s
    out = [f + s[..., None] * r + t[..., None] * u for f, r, u in _FACES]
    d = np.concatenate([o.reshape(-1, 3) for o in out])
    return d / np.linalg.norm(d, axis=1, keepdims=True)


def _cube_index(R, n):
    """Texel of six n x n faces nearest each direction R (..., 3)."""
    proj = R @ _FACES[:, 0].T                         # (..., 6) along each face's forward
    k = np.argmax(proj, -1)
    f = np.maximum(np.take_along_axis(proj, k[..., None], -1)[..., 0], 1e-9)
    s = (R * _FACES[k, 1]).sum(-1) / f
    t = (R * _FACES[k, 2]).sum(-1) / f
    i = np.clip(((s + 1) * 0.5 * n).astype(np.int64), 0, n - 1)
    j = np.clip(((t + 1) * 0.5 * n).astype(np.int64), 0, n - 1)
    return k * n * n + j * n + i


class Probe:
    """The surroundings seen from one point: six 90-degree renders, kept at full size
    (to light the mesh) and shrunk and blurred for each roughness (for reflections)."""

    LEVELS = np.array([0.15, 0.3, 0.5, 0.75, 1.0])

    def __init__(self, full, small=16):
        self.full = full                              # (6, res, res, 3) linear
        res = full.shape[1]
        small = min(small, res)
        b = res // small
        rad = full[:, :small * b, :small * b].reshape(6, small, b, small, b, 3).mean((2, 4))
        rad = rad.reshape(-1, 3).astype(np.float64)
        self.n = small
        self.radiance = rad
        d = _face_dirs(small)
        self.dirs = d
        cosm = np.clip(d @ d.T, -1, 1)
        # texel solid angles: d omega = dA / |p|^3 on the unit cube face
        c = (np.arange(small) + 0.5) / small * 2 - 1
        s, t = np.meshgrid(c, c)
        self.solid = np.tile(((2.0 / small) ** 2 / (1 + s * s + t * t) ** 1.5).ravel(), 6)
        levels = []
        for r in self.LEVELS:
            a = r * r
            kappa = min(2.0 / max(a * a, 1e-4), 400.0)
            w = np.exp(kappa * (cosm - 1)) * self.solid
            w /= w.sum(1, keepdims=True)
            levels.append(w @ rad)
        self.levels = np.stack(levels)                # (L, 6 n n, 3)

    def __call__(self, R, rough):
        """Radiance from directions R (N, M, 3), blurred for each splat's roughness (N,)."""
        idx = _cube_index(R, self.n)
        lv = np.interp(np.clip(rough, self.LEVELS[0], 1.0), self.LEVELS, np.arange(len(self.LEVELS)))
        l0 = np.floor(lv).astype(np.int64)
        l1 = np.minimum(l0 + 1, len(self.LEVELS) - 1)
        f = (lv - l0)[:, None, None]
        return self.levels[l0[:, None], idx] * (1 - f) + self.levels[l1[:, None], idx] * f

    def equirect(self, w=1024, h=512):
        """The full-size probe as an equirectangular map in Blender's World layout."""
        u = (np.arange(w) + 0.5) / w
        v = (np.arange(h) + 0.5) / h
        phi = -(u - 0.5) * 2 * np.pi                  # Blender: u = -atan2(y, x) / 2pi + 0.5
        el = (v - 0.5) * np.pi
        P, E = np.meshgrid(phi, el)                   # rows: v (bottom up), columns: u
        d = np.stack([np.cos(E) * np.cos(P), np.cos(E) * np.sin(P), np.sin(E)], -1)
        res = self.full.shape[1]
        return self.full.reshape(-1, 3)[_cube_index(d, res)]   # (h, w, 3)


def spirula_available():
    return hasattr(bpy.types.Scene, "spirula")


class _Restorer:
    """Attribute changes undone in reverse order (for the probe's temporary settings)."""

    def __init__(self):
        self.saved = []

    def set(self, owner, attr, value):
        self.saved.append((owner, attr, getattr(owner, attr)))
        setattr(owner, attr, value)

    def undo(self):
        while self.saved:
            owner, attr, value = self.saved.pop()
            try:
                setattr(owner, attr, value)
            except (ReferenceError, TypeError, AttributeError):
                pass


def _render_faces(context, center, res, folder, engine, transparent=True):
    sc = context.scene
    cam_data = bpy.data.cameras.new("_gsp_probe")
    cam_data.angle = math.pi / 2
    cam_data.clip_start = 1e-4
    cam_data.clip_end = 1e6
    cam = bpy.data.objects.new("_gsp_probe", cam_data)
    sc.collection.objects.link(cam)
    ch = _Restorer()
    r = sc.render
    try:
        ch.set(r, "engine", engine)
        ch.set(sc, "camera", cam)
        ch.set(r, "resolution_x", res)
        ch.set(r, "resolution_y", res)
        ch.set(r, "resolution_percentage", 100)
        ch.set(r.image_settings, "file_format", 'OPEN_EXR')
        ch.set(r.image_settings, "color_depth", '32')
        ch.set(r, "film_transparent", transparent)
        if engine == 'CYCLES':
            ch.set(sc.cycles, "samples", 32)
        if engine == 'SPIRULA':
            s = sc.spirula
            # A clean look at the surroundings: no lens character, filter or film.
            for attr in ("lens", "lens_filter", "film"):
                if hasattr(s, attr):
                    ch.set(s, attr, 'NONE')
            ch.set(s, "dof_samples", 1)
        faces = []
        for k, (f, rt, up) in enumerate(_FACES):
            Rm = Matrix((Vector(rt), Vector(up), -Vector(f))).transposed()   # columns: x, y, z
            cam.matrix_world = Matrix.Translation(center) @ Rm.to_4x4()
            path = os.path.join(folder, f"face{k}.exr")
            ch.set(r, "filepath", path)
            bpy.ops.render.render(write_still=True)
            img = bpy.data.images.load(path)
            px = np.empty(res * res * 4, np.float32)
            img.pixels.foreach_get(px)
            bpy.data.images.remove(img)
            os.remove(path)
            faces.append(px.reshape(res, res, 4))
        return np.stack(faces)                        # (6, res, res, 4) premultiplied
    finally:
        ch.undo()
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)


def render_probe(context, center, hide, res=64, engine=None):
    """The surroundings from center with `hide` hidden: the scene's own engine (or the
    one given), with any uncovered background filled from the World by Cycles."""
    sc = context.scene
    engine = engine or sc.render.engine
    ch = _Restorer()
    folder = tempfile.mkdtemp(prefix="gsp_probe_")
    try:
        for o in hide:
            ch.set(o, "hide_render", True)
        faces = _render_faces(context, center, res, folder, engine)
        rgb, alpha = faces[..., :3], faces[..., 3:4]
        if (alpha < 0.999).any():
            # Where nothing covers the view, the World shows: Cycles renders it alone.
            for o in sc.objects:
                if not o.hide_render and o.type != 'CAMERA':
                    ch.set(o, "hide_render", True)
            world = _render_faces(context, center, min(res, 32), folder, 'CYCLES', transparent=False)[..., :3]
            if world.shape[1] != res:
                k = res // world.shape[1]
                world = world.repeat(k, 1).repeat(k, 2)
                if world.shape[1] != res:            # res not a multiple of 32
                    idx = (np.arange(res) * world.shape[1] // res)
                    world = world[:, idx][:, :, idx]
            rgb = rgb + (1 - alpha) * world
    finally:
        ch.undo()
        try:
            os.rmdir(folder)
        except OSError:
            pass
    return Probe(np.maximum(rgb, 0).astype(np.float64))


# ------------------------------------------------------- light from the scan
SHADOW_KEY = "gsp_shadow_source"     # on a contact-shadow layer: the mesh casting it


def _is_scan(ob):
    return is_splat_object(ob) and SOURCE_KEY not in ob and SHADOW_KEY not in ob


def _bbox_world(obj):
    corners = np.array([list(obj.matrix_world @ Vector(c)) for c in obj.bound_box])
    return corners.min(0), corners.max(0)


def _scan_world_world(probe):
    """A temporary World that shines the probe on the mesh."""
    w = bpy.data.worlds.new("_gsp_scan_world")
    eq = probe.equirect()
    h, wd = eq.shape[:2]
    img = bpy.data.images.new("_gsp_scan_env", wd, h, alpha=False, float_buffer=True)
    img.colorspace_settings.name = 'Non-Color'
    px = np.ones((h, wd, 4), np.float32)
    px[..., :3] = eq
    img.pixels.foreach_set(px.ravel())
    img.update()
    img.pack()                  # Cycles reads a packed image's pixels, not the blank generated one
    nt = w.node_tree
    bg = nt.nodes.get("Background") or nt.nodes.new("ShaderNodeBackground")
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = img
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = 1.0
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_WORLD')
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    return w, img


def contact_shadows(context, obj, probe, reach, strength, degree, report=print):
    """Splat rows that darken the scans where obj blocks their light.

    The scan surface near obj is sampled on an even grid (one point per cell, from the
    most opaque scan splat in it, whose thinnest axis gives the surface normal). At each
    point, the share of the light reaching it (the probe's surroundings, cos-weighted)
    that the mesh now blocks becomes the opacity of a flat black splat laid just over the
    scan there. One splat per cell, so they don't stack up where scan splats overlap.
    The scans themselves aren't changed."""
    from mathutils.bvhtree import BVHTree
    dg = context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        M = obj.matrix_world
        verts = [M @ v.co for v in me.vertices]
        polys = [list(p.vertices) for p in me.polygons]
    finally:
        ev.to_mesh_clear()
    if not polys:
        return None
    bvh = BVHTree.FromPolygons(verts, polys)
    lo, hi = _bbox_world(obj)
    centre = (lo + hi) / 2
    size = float(np.linalg.norm(hi - lo))
    radius = 0.5 * size
    reach = reach if reach > 0 else 0.5 * size
    cell = size / 60.0
    dirs = mesh_sh.fibonacci_dirs(256)
    env_l = probe.levels[2] @ np.array([0.2126, 0.7152, 0.0722])     # rough 0.5 luminance
    w_env = np.maximum(env_l[_cube_index(dirs, probe.n)], 1e-6)
    pts, nrms = [], []
    for ob in context.scene.objects:
        if not _is_scan(ob) or ob.hide_get() or ob.hide_render:
            continue
        S = SplatSet.read(ob.data)
        Mo = np.array(ob.matrix_world, np.float64)
        pos = _to_world(Mo, pts=S.pos.astype(np.float64))
        near = np.all((pos > lo - reach) & (pos < hi + reach), axis=1) & S.visible & (S.opacity > 0.3)
        idx = np.nonzero(near)[0]
        if len(idx) == 0:
            continue
        # one splat per grid cell: the most opaque
        key = np.floor((pos[idx] - (lo - reach)) / cell).astype(np.int64)
        key = (key[:, 0] * 1_000_003 + key[:, 1]) * 1_000_033 + key[:, 2]
        order = np.lexsort((-S.opacity[idx], key))
        _, first = np.unique(key[order], return_index=True)
        idx = idx[order[first]]
        sc_ = S.get("scale")[idx].astype(np.float64)
        thin = np.argmin(sc_, 1)
        n = _quat_mat(S.get("rotation")[idx])[np.arange(len(idx)), :, thin]   # column = axis
        n = _to_world(Mo, normals=n)
        n *= np.where(((centre - pos[idx]) * n).sum(1) < 0, -1.0, 1.0)[:, None]
        pts.append(pos[idx])
        nrms.append(n)
    if not pts:
        return None
    pts = np.concatenate(pts)
    nrms = np.concatenate(nrms)
    # Only directions that can meet the mesh's bounding sphere need a ray.
    to_c = centre - pts
    dist = np.linalg.norm(to_c, axis=1)
    u = to_c / np.maximum(dist, 1e-12)[:, None]
    cone = np.cos(np.arcsin(np.clip(radius / np.maximum(dist, 1e-12), 0, 1)))
    cosd = nrms @ dirs.T                                                  # (P, D)
    weight = np.maximum(cosd, 0) * w_env
    total = weight.sum(1)
    test = (cosd > 0) & ((u @ dirs.T) >= cone[:, None] - 1e-6)
    lift = 1e-3 * size
    shade = np.zeros(len(pts))
    vdirs = [Vector(d) for d in dirs]
    for i in np.nonzero(test.any(1))[0]:
        o = Vector(pts[i] + nrms[i] * lift)
        blocked = 0.0
        for j in np.nonzero(test[i])[0]:
            if bvh.ray_cast(o, vdirs[j], dist[i] + radius)[0] is not None:
                blocked += weight[i, j]
        shade[i] = blocked / max(total[i], 1e-12)
    a = np.clip(shade * strength, 0, 0.97)
    keep = a > 0.01
    if not keep.any():
        return None
    p_, n_, a_ = pts[keep], nrms[keep], a[keep]
    m = len(p_)
    report(f"{obj.name}: contact shadow of {m:,} splats")
    # a frame with the normal as third axis
    ref = np.where(np.abs(n_[:, 2:3]) < 0.9, np.array([[0, 0, 1.0]]), np.array([[1.0, 0, 0]]))
    u1 = np.cross(ref, n_)
    u1 /= np.linalg.norm(u1, axis=1, keepdims=True)
    u2 = np.cross(n_, u1)
    quat = mesh_sh.matrix_to_quat(np.stack([u1, u2, n_], 2))
    sigma = cell * 0.75
    rows = {
        "position": ['FLOAT_VECTOR', (p_ + n_ * lift).astype(np.float32)],
        "scale": ['FLOAT_VECTOR', np.tile([sigma, sigma, sigma * 0.02], (m, 1)).astype(np.float32)],
        "rotation": ['QUATERNION', quat.astype(np.float32)],
        "radiance:base": ['FLOAT4', np.concatenate([
            np.full((m, 3), -0.5 / mesh_sh.SH_C0, np.float32), a_[:, None].astype(np.float32)], 1)],
    }
    for kk in range((degree + 1) ** 2 - 1):
        rows[f"radiance:sh_{kk}"] = ['FLOAT_VECTOR', np.zeros((m, 3), np.float32)]
    return rows


def _quat_mat(q):
    q = q.astype(np.float64)
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    w, x, y, z = q.T
    return np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
                     np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
                     np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], 1)


# ------------------------------------------------------------------- convert
def convert(context, obj, st: Settings, report=print):
    """Build splat rows for mesh obj. Returns ({name: [data_type, array]}, info dict);
    info["shadow"] holds contact-shadow rows when lit from the scan."""
    spacing = st.spacing if st.spacing > 0 else auto_spacing(obj)
    sc = context.scene
    if st.lighting == 'SCAN' and not spirula_available():
        raise ConvertError("Light from Scan renders the scan with Spirula Render: install and enable it")
    state = _SceneState(context)
    ch = _Restorer()
    tmp = None
    baker = None
    shadow = None
    temp_world = None
    hide_src = _Restorer()
    try:
        # The source sits exactly on the copy being baked: on flat faces every ray from the
        # copy would hit it (a black bake). It's out of the renders until we're done.
        hide_src.set(obj, "hide_render", True)
        tmp = _temp_copy(context, obj)
        res = _bake_uvs(context, tmp, spacing)
        pos, gnrm, rot, scale, px, py = _geometry(tmp, res)
        n = len(pos)
        report(f"{obj.name}: {n:,} splats from a {res} px layout")
        degree = st.degree
        K = (degree + 1) ** 2 - 1
        lo, hi = _bbox_world(obj)
        center = Vector(((lo + hi) / 2).tolist())
        own = [obj, tmp] + [o for o in sc.objects
                            if o.get(SOURCE_KEY) == obj.name or o.get(SHADOW_KEY) == obj.name]
        probe = None
        if st.lighting == 'SCAN':
            report(f"{obj.name}: rendering the scan around it")
            probe = render_probe(context, center, own, res=max(32, st.probe_res), engine='SPIRULA')
        elif st.lighting == 'HEADLIGHT' and st.reflections and degree >= 2:
            probe = render_probe(context, center, own, res=max(16, st.probe_res))
        baker = _Baker(context, tmp, res)
        nrm = baker.bake('NORMAL', px, py, normal_space='OBJECT',
                         normal_r='POS_X', normal_g='POS_Y', normal_b='POS_Z') * 2 - 1
        nrm = _to_world(np.array(obj.matrix_world), normals=nrm)
        # A baked normal facing away from the geometry (bad normal map) falls back to it.
        bad = (nrm * gnrm).sum(1) < 0.05
        nrm[bad] = gnrm[bad]
        dc = np.empty((n, 3), np.float32)
        rest = np.zeros((n, K, 3), np.float32)
        if st.lighting == 'BAKED':
            lit = baker.bake('COMBINED', px, py, samples=st.bake_samples)
            dc[:] = (mesh_sh.srgb(st.gain * lit) - 0.5) / mesh_sh.SH_C0
        elif st.lighting == 'SCAN':
            f0 = np.clip(baker.bake('GLOSSY', px, py, pass_filter={'COLOR'}), 0, 1)
            rough = np.clip(baker.bake('ROUGHNESS', px, py)[:, 0], 0, 1)
            # Cycles lights the mesh with the scan as its World. The splats can't be seen by
            # Cycles (they'd draw as balls), so they're hidden; the scene's lamps only join
            # in when Spirula is relighting the scan with them too.
            temp_world = _scan_world_world(probe)
            ch.set(sc, "world", temp_world[0])
            for o in sc.objects:
                if is_splat_object(o) and not o.hide_render:
                    ch.set(o, "hide_render", True)
                if o.type == 'LIGHT' and not o.hide_render and not sc.spirula.use_relight:
                    ch.set(o, "hide_render", True)
            lit = baker.bake('DIFFUSE', px, py, samples=st.bake_samples,
                             pass_filter={'DIRECT', 'INDIRECT', 'COLOR'})
            lit += baker.bake('EMIT', px, py)
            ch.undo()
            diffuse = st.gain * np.maximum(lit, 0)
            # Reflections of the scan go in bands 1-3 with the base: a scan lit by its own
            # surroundings is relit from its base colour alone, so the bands are free for them.
            fitter = mesh_sh.SurroundFitter(degree)
            for s0 in range(0, n, 4000):
                sl = slice(s0, min(n, s0 + 4000))
                base, r = fitter.fit(nrm[sl], diffuse[sl], f0[sl], rough[sl], probe,
                                     st.gain * st.reflect_strength)
                dc[sl] = (base - 0.5) / mesh_sh.SH_C0
                rest[sl, :fitter.K, :] = r
            if st.contact_shadows:
                report(f"{obj.name}: contact shadows")
                shadow = contact_shadows(context, obj, probe, st.shadow_reach, st.shadow_strength,
                                         degree, report)
        else:
            albedo = np.clip(baker.bake('DIFFUSE', px, py, pass_filter={'COLOR'}), 0, None)
            f0 = np.clip(baker.bake('GLOSSY', px, py, pass_filter={'COLOR'}), 0, 1)
            rough = np.clip(baker.bake('ROUGHNESS', px, py)[:, 0], 0, 1)
            fitter = mesh_sh.EnvFitter(degree) if probe is not None else None
            chunk = 20000 if probe is None else 4000
            for s0 in range(0, n, chunk):
                sl = slice(s0, min(n, s0 + chunk))
                b, k, g2, g3 = mesh_sh.headlight_profiles(albedo[sl], f0[sl], rough[sl],
                                                          st.gain, st.shine)
                d, r = mesh_sh.zonal_to_sh(nrm[sl], b, k, g2, g3, degree)
                if fitter is not None:
                    mu = nrm[sl] @ (-fitter.d).T
                    H = st.gain * (albedo[sl, None, :] * np.maximum(mu, 0)[..., None]
                                   + f0[sl, None, :] * np.where(mu > 0, mesh_sh.ggx_headlight(
                                       mu, rough[sl, None]), 0)[..., None])
                    env = fitter.fit(nrm[sl], f0[sl], rough[sl], H, probe, st.reflect_strength)
                    r[:, fitter.lo:fitter.hi, :] += env.astype(np.float32)
                dc[sl] = d
                rest[sl] = r
    finally:
        ch.undo()
        hide_src.undo()
        if baker is not None:
            baker.free()
        if temp_world is not None:
            bpy.data.worlds.remove(temp_world[0])
            bpy.data.images.remove(temp_world[1])
        if tmp is not None:
            _remove_temp(tmp)
        state.restore()

    quat = mesh_sh.matrix_to_quat(rot).astype(np.float32)
    rows = {
        "position": ['FLOAT_VECTOR', pos.astype(np.float32)],
        "scale": ['FLOAT_VECTOR', scale.astype(np.float32)],
        "rotation": ['QUATERNION', quat],
        "radiance:base": ['FLOAT4', np.concatenate([dc, np.full((n, 1), OPACITY, np.float32)], 1)],
    }
    for k in range(K):
        rows[f"radiance:sh_{k}"] = ['FLOAT_VECTOR', np.ascontiguousarray(rest[:, k, :])]
    if st.two_sided:
        rows = _two_sided(rows, gnrm, scale, degree)
    return rows, {"splats": len(rows["position"][1]), "spacing": spacing, "res": res, "shadow": shadow}


def _two_sided(rows, gnrm, scale, degree):
    """Add a back face: each splat again, nudged behind the surface, colour mirrored."""
    eps = (scale[:, 2] * 2.0)[:, None]
    front = {k: [dt, a.copy()] for k, (dt, a) in rows.items()}
    back = {k: [dt, a.copy()] for k, (dt, a) in rows.items()}
    front["position"][1] = (rows["position"][1] + gnrm * eps).astype(np.float32)
    back["position"][1] = (rows["position"][1] - gnrm * eps).astype(np.float32)
    # Mirroring the profile through the surface flips the odd bands (1 and 3).
    for k in range(3):
        name = f"radiance:sh_{k}"
        if name in back:
            back[name][1] = -back[name][1]
    for k in range(8, 15):
        name = f"radiance:sh_{k}"
        if name in back:
            back[name][1] = -back[name][1]
    return {k: [dt, np.concatenate([front[k][1], back[k][1]])] for k, (dt, _) in rows.items()}


def write_object(context, obj, rows, key=SOURCE_KEY, suffix="_splats"):
    """Create or refresh obj's converted splat object (or, with key=SHADOW_KEY, its
    contact-shadow layer); rows None removes it. Returns the object or None."""
    target = next((o for o in context.scene.objects
                   if o.get(key) == obj.name and is_splat_object(o)), None)
    if rows is None:
        if target is not None:
            data = target.data
            bpy.data.objects.remove(target)
            if data.users == 0:
                bpy.data.pointclouds.remove(data)
        return None
    S = SplatSet(len(rows["position"][1]), rows)
    if target is None:
        pc = bpy.data.pointclouds.new(obj.name + suffix)
        S.write(pc)
        target = bpy.data.objects.new(obj.name + suffix, pc)
        for coll in obj.users_collection:
            coll.objects.link(target)
        target[key] = obj.name
    else:
        old = target.data
        pc = bpy.data.pointclouds.new(obj.name + suffix)
        S.write(pc)
        target.data = pc
        if old.users == 0:
            bpy.data.pointclouds.remove(old)
    target.matrix_world = Matrix.Identity(4)
    return target
