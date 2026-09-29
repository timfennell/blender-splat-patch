"""Screen-space picking of splats and GPU overlays for the brush."""

import math

import numpy as np
import gpu
from gpu_extras.batch import batch_for_shader
from bpy_extras import view3d_utils
from mathutils import Vector, Matrix


class Projection:
    """Screen positions and view depth of every splat, cached per view."""

    def __init__(self):
        self.key = None
        self.sx = self.sy = self.depth = self.ok = None

    def update(self, region, rv3d, S, matrix_world, revision):
        key = (tuple(tuple(r) for r in rv3d.perspective_matrix), region.width,
               region.height, revision, tuple(tuple(r) for r in matrix_world))
        if key == self.key:
            return
        self.key = key
        pos = S.pos
        P = np.array(rv3d.perspective_matrix @ matrix_world, np.float32)
        V = np.array(rv3d.view_matrix @ matrix_world, np.float32)
        clip = pos @ P[:, :3].T + P[:, 3]
        w = clip[:, 3]
        self.ok = w > 1e-6
        w = np.where(self.ok, w, 1.0)
        self.sx = (clip[:, 0] / w * 0.5 + 0.5) * region.width
        self.sy = (clip[:, 1] / w * 0.5 + 0.5) * region.height
        self.depth = -(pos @ V[2, :3] + V[2, 3])

    def disc(self, mx, my, radius_px):
        d2 = (self.sx - mx) ** 2 + (self.sy - my) ** 2
        idx = np.nonzero(self.ok & (d2 < radius_px * radius_px))[0]
        return idx, np.sqrt(d2[idx])

    def pick(self, S, mx, my, radius_px, min_opacity=0.2):
        """Object-space point on the front splat surface under the mouse, or None."""
        for r in (max(4.0, radius_px * 0.2), radius_px):
            idx, _ = self.disc(mx, my, r)
            idx = idx[S.opacity[idx] > min_opacity]
            if len(idx) >= 3:
                break
        else:
            return None
        depth = self.depth[idx]
        front = idx[depth <= np.percentile(depth, 10)]
        return S.pos[front].mean(0)


def view_vector_local(region, rv3d, mx, my, matrix_world):
    v = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
    v = matrix_world.inverted().to_3x3() @ v
    return np.array(v.normalized(), np.float32)


def pixel_radius_to_local(region, rv3d, mx, my, radius_px, hit_local, matrix_world):
    hit_w = matrix_world @ Vector(hit_local)
    a = view3d_utils.region_2d_to_location_3d(region, rv3d, (mx, my), hit_w)
    b = view3d_utils.region_2d_to_location_3d(region, rv3d, (mx + radius_px, my), hit_w)
    scale = sum(abs(s) for s in matrix_world.to_scale()) / 3.0
    return max((a - b).length / max(scale, 1e-12), 1e-9)


# ------------------------------------------------------------------ drawing
def _shader(name, fallback):
    try:
        return gpu.shader.from_builtin(name)
    except (ValueError, SystemError):
        return gpu.shader.from_builtin(fallback)


def _basis(normal):
    n = Vector(normal).normalized()
    t = n.orthogonal().normalized()
    return t, n.cross(t)


def circle_coords(center, normal, radius, segments=48):
    t, b = _basis(normal)
    c = Vector(center)
    pts = []
    for i in range(segments + 1):
        a = 2 * math.pi * i / segments
        pts.append(tuple(c + (t * math.cos(a) + b * math.sin(a)) * radius))
    return pts


def draw_lines(strips, color, width=2.0):
    """strips: list of coordinate lists (line strips), in the current matrix space."""
    if not strips:
        return
    shader = _shader('POLYLINE_UNIFORM_COLOR', 'UNIFORM_COLOR')
    coords = []
    for s in strips:
        for a, b in zip(s[:-1], s[1:]):
            coords += [a, b]
    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
    shader.bind()
    try:
        vp = gpu.state.viewport_get()
        shader.uniform_float("viewportSize", (vp[2], vp[3]))
        shader.uniform_float("lineWidth", width)
    except ValueError:
        pass
    shader.uniform_float("color", color)
    batch.draw(shader)


def draw_points(pos, rgba, size=3.0):
    if pos is None or len(pos) == 0:
        return
    shader = _shader('POINT_FLAT_COLOR', 'FLAT_COLOR')
    batch = batch_for_shader(shader, 'POINTS', {"pos": pos, "color": rgba})
    gpu.state.point_size_set(size)
    shader.bind()
    batch.draw(shader)


def cap(pos, rgba, limit=150000):
    if pos is not None and len(pos) > limit:
        step = int(math.ceil(len(pos) / limit))
        pos = pos[::step]
        rgba = rgba[::step] if rgba is not None and len(rgba) > 1 else rgba
    return pos, rgba


def solid_rgba(n, color):
    return np.tile(np.asarray(color, np.float32), (n, 1))


class Overlay:
    """Everything the brush draws, in object space, pushed under matrix_world."""

    def __init__(self):
        self.matrix = Matrix.Identity(4)
        self.rings = []          # (center, normal, radius, rgba, width)
        self.points = []         # (pos, rgba, size)

    def clear(self):
        self.rings = []
        self.points = []

    def draw(self):
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        with gpu.matrix.push_pop():
            gpu.matrix.multiply_matrix(self.matrix)
            for pos, rgba, size in self.points:
                draw_points(pos, rgba, size)
            for center, normal, radius, color, width in self.rings:
                draw_lines([circle_coords(center, normal, radius)], color, width)
        gpu.state.blend_set('NONE')
        gpu.state.point_size_set(1.0)
