"""Selection volumes: a box, sphere, cylinder or cone you move, turn and scale like any object,
then select or deselect the splats inside it.

A volume is an Empty (so it never renders and no engine draws it as geometry) tagged with its
shape; the add-on draws the shape's wireframe. In the Empty's own space each shape fills the
unit cube's span: the box is -1..1 on every axis, the sphere has radius 1, the cylinder radius 1
from z = -1 to 1, and the cone a base of radius 1 at z = -1 narrowing to its tip at z = 1.
"""

import numpy as np

SHAPE_KEY = "gsp_volume"       # on the Empty: its shape
TARGET_KEY = "gsp_target"      # on the Empty: the splat object it was made for

SHAPES = [
    ('BOX', "Box", "A box: scale each axis for any brick shape", 'MESH_CUBE', 0),
    ('SPHERE', "Sphere", "A sphere: scale unevenly for an ellipsoid", 'MESH_UVSPHERE', 1),
    ('CYLINDER', "Cylinder", "A cylinder along its Z axis", 'MESH_CYLINDER', 2),
    ('CONE', "Cone", "A cone along its Z axis, its tip at +Z", 'MESH_CONE', 3),
]


def shape_of(obj):
    return obj.get(SHAPE_KEY) if obj is not None and obj.type == 'EMPTY' else None


def inside(shape, q):
    """Which points q [N,3] (the volume's own space) are inside the unit shape."""
    x, y, z = q[:, 0], q[:, 1], q[:, 2]
    if shape == 'BOX':
        return (np.abs(x) <= 1) & (np.abs(y) <= 1) & (np.abs(z) <= 1)
    if shape == 'SPHERE':
        return x * x + y * y + z * z <= 1
    r2 = x * x + y * y
    if shape == 'CYLINDER':
        return (r2 <= 1) & (np.abs(z) <= 1)
    if shape == 'CONE':
        rad = (1 - z) / 2                         # 1 at the base (z = -1), 0 at the tip
        return (np.abs(z) <= 1) & (r2 <= rad * rad)
    return np.zeros(len(q), bool)


def to_volume_space(volume, obj, pos):
    """Splat centres (obj's own space) in the volume's own space."""
    M = np.linalg.inv(np.array(volume.matrix_world, np.float64)) @ np.array(obj.matrix_world, np.float64)
    return pos.astype(np.float64) @ M[:3, :3].T + M[:3, 3]


def _circle(z, r=1.0, n=48):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    p = np.stack([np.cos(a) * r, np.sin(a) * r, np.full(n, z)], 1)
    return np.stack([p, np.roll(p, -1, 0)], 1).reshape(-1, 3)


def wire(shape):
    """Line segments (pairs of points, [2M,3]) of the unit shape's wireframe."""
    if shape == 'BOX':
        c = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float64)
        edges = [(i, j) for i in range(8) for j in range(i + 1, 8) if np.sum(c[i] != c[j]) == 1]
        return np.array([c[k] for e in edges for k in e])
    if shape == 'SPHERE':
        ring = _circle(0.0)
        rx = ring[:, [2, 0, 1]]                   # the same circle in the other two planes
        ry = ring[:, [0, 2, 1]]
        return np.concatenate([ring, rx, ry])
    if shape == 'CYLINDER':
        sides = np.array([[[np.cos(a), np.sin(a), -1], [np.cos(a), np.sin(a), 1]]
                          for a in np.linspace(0, 2 * np.pi, 8, endpoint=False)]).reshape(-1, 3)
        return np.concatenate([_circle(-1.0), _circle(1.0), _circle(0.0), sides])
    if shape == 'CONE':
        sides = np.array([[[np.cos(a), np.sin(a), -1], [0, 0, 1]]
                          for a in np.linspace(0, 2 * np.pi, 8, endpoint=False)]).reshape(-1, 3)
        return np.concatenate([_circle(-1.0), _circle(0.0, 0.5), sides])
    return np.zeros((0, 3))
