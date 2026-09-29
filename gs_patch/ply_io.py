"""Write a Blender splat point cloud back out as a standard 3DGS PLY.

Blender 5.3's own PLY exporter only writes mesh vertices, so a point cloud comes
out empty. This writes the INRIA layout that Brush, SuperSplat, nerfstudio and
Blender's importer all read.
"""

import numpy as np

from .splats import SplatSet


def _logit(p):
    p = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(p / (1.0 - p))


def write_splat_ply(path, S: SplatSet):
    n = S.n
    cols, names = [], []

    def add(name, values):
        names.append(name)
        cols.append(np.asarray(values, np.float32).reshape(n))

    pos = S.pos
    for i, a in enumerate("xyz"):
        add(a, pos[:, i])
    for a in ("nx", "ny", "nz"):
        add(a, np.zeros(n))

    base = S.base
    dc = base[:, :3] if base is not None else np.zeros((n, 3))
    for i in range(3):
        add(f"f_dc_{i}", dc[:, i])

    sh = [S.get(k) for k in S.sh_names()]
    k = len(sh)
    # f_rest is channel-major: all red coefficients, then green, then blue.
    for ch in range(3):
        for j in range(k):
            add(f"f_rest_{ch * k + j}", sh[j][:, ch])

    add("opacity", _logit(base[:, 3] if base is not None else np.ones(n)))

    scale = S.get("scale")
    if scale is None:
        scale = np.full((n, 3), 0.01, np.float32)
    for i in range(3):
        add(f"scale_{i}", np.log(np.maximum(scale[:, i], 1e-12)))

    rot = S.get("rotation")
    if rot is None:
        rot = np.tile(np.array([1, 0, 0, 0], np.float32), (n, 1))
    for i in range(4):
        add(f"rot_{i}", rot[:, i])

    header = ["ply", "format binary_little_endian 1.0",
              "comment Exported from Blender by Gaussian Splat Patch",
              f"element vertex {n}"]
    header += [f"property float {nm}" for nm in names]
    header.append("end_header")
    data = np.stack(cols, axis=1).astype("<f4")
    with open(path, "wb") as f:
        f.write(("\n".join(header) + "\n").encode("ascii"))
        f.write(data.tobytes())
    return n
