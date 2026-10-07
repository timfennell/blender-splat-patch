"""Write a Blender splat point cloud back out as a standard 3DGS PLY.

Blender 5.3's own PLY exporter only writes mesh vertices, so a point cloud comes
out empty. This writes the INRIA layout that Brush, SuperSplat, nerfstudio and
Blender's importer all read.

The file is filled one attribute at a time through a memory map, so exporting a
multi-million-splat scan needs only one attribute's worth of extra memory, not
several copies of the whole cloud.
"""

import numpy as np

from .splats import SplatSet, BASE_ATTR, HIDDEN_ATTR, _LAYOUT


def _logit(p):
    p = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def _write(path, n_total, keep, get, sh_names):
    """keep: bool mask over the n_total splats (or None for all).
    get(name) -> (n_total, width) array for an attribute, or None if absent."""
    n = n_total if keep is None else int(np.count_nonzero(keep))
    k = len(sh_names)
    names = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"]
    names += [f"f_rest_{i}" for i in range(3 * k)]
    names += ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]
    col = {nm: i for i, nm in enumerate(names)}

    header = ["ply", "format binary_little_endian 1.0",
              "comment Exported from Blender by Gaussian Splat Patch",
              f"element vertex {n}"]
    header += [f"property float {nm}" for nm in names]
    header.append("end_header")
    head = ("\n".join(header) + "\n").encode("ascii")
    with open(path, "wb") as f:
        f.write(head)
        f.truncate(len(head) + n * len(names) * 4)
    if n == 0:
        return 0
    out = np.memmap(path, dtype="<f4", mode="r+", offset=len(head), shape=(n, len(names)))

    def put(name, values):
        out[:, col[name]] = values

    def rows(name):
        a = get(name)
        if a is None:
            return None
        return a if keep is None else a[keep]

    pos = rows("position")
    for i, a in enumerate("xyz"):
        put(a, pos[:, i])
    del pos
    for a in ("nx", "ny", "nz"):
        put(a, 0.0)

    base = rows(BASE_ATTR)
    if base is not None:
        for i in range(3):
            put(f"f_dc_{i}", base[:, i])
        put("opacity", _logit(base[:, 3]))
    else:
        for i in range(3):
            put(f"f_dc_{i}", 0.0)
        put("opacity", _logit(np.ones(n, np.float32)))
    del base

    # f_rest is channel-major: all red coefficients, then green, then blue.
    for j, name in enumerate(sh_names):
        sh = rows(name)
        for ch in range(3):
            put(f"f_rest_{ch * k + j}", sh[:, ch])
        del sh

    scale = rows("scale")
    for i in range(3):
        put(f"scale_{i}", np.log(np.maximum(scale[:, i], 1e-12)) if scale is not None else np.log(0.01))
    del scale

    rot = rows("rotation")
    for i in range(4):
        put(f"rot_{i}", rot[:, i] if rot is not None else (1.0 if i == 0 else 0.0))
    del rot

    out.flush()
    del out
    return n


def _sh_sorted(names):
    names = [k for k in names if k.startswith("radiance:sh_")]
    return sorted(names, key=lambda k: int(k.rsplit('_', 1)[1]))


def write_splat_ply(path, S: SplatSet):
    """Write a SplatSet (all of its rows)."""
    return _write(path, S.n, None, S.get, S.sh_names())


def write_pointcloud_ply(path, pc):
    """Write a Blender point cloud straight from its attributes, leaving out erased splats."""
    n = len(pc.points)

    def get(name):
        attr = pc.attributes.get(name)
        if attr is None or attr.domain != 'POINT':
            return None
        width, key, dtype = _LAYOUT[attr.data_type]
        buf = np.empty(n * width, dtype)
        if n:
            attr.data.foreach_get(key, buf)
        return buf.reshape(n, width)

    hidden = get(HIDDEN_ATTR)
    keep = None if hidden is None or not hidden.any() else ~hidden[:, 0]
    return _write(path, n, keep, get, _sh_sorted([a.name for a in pc.attributes]))
