"""Combining several splat objects into one model.

Each object's splats are carried into the target's space: positions through the relative
transform, each splat's rotation turned with it, its size scaled, and its view-dependent
colour (higher SH bands) rotated so it keeps facing the way it did. The bookkeeping
columns (erased, added, faded, original opacity) come along, so the merged model can still
be restored; so do the erased originals in each object's stash.
"""

import numpy as np

from .core import quat_mul, sh_rotation_matrices, _SH_BANDS


def decompose(M):
    """4x4 -> (rotation 3x3, uniform scale, translation, non-uniform?, mirrored?)."""
    M = np.asarray(M, np.float64)
    A = M[:3, :3]
    axes = np.linalg.norm(A, axis=0)
    det = np.linalg.det(A)
    s = float(np.cbrt(abs(det))) or 1.0
    U, _, Vt = np.linalg.svd(A / np.maximum(axes, 1e-12))
    R = U @ Vt
    if np.linalg.det(R) < 0:                 # nearest proper rotation
        U[:, -1] *= -1
        R = U @ Vt
    nonuniform = float(axes.max() / max(axes.min(), 1e-12)) > 1.001
    return R, s, M[:3, 3], nonuniform, det < 0


def quat_from_matrix(R):
    """w, x, y, z."""
    m = R
    t = np.trace(m)
    if t > 0:
        S = np.sqrt(t + 1.0) * 2
        q = [0.25 * S, (m[2, 1] - m[1, 2]) / S, (m[0, 2] - m[2, 0]) / S, (m[1, 0] - m[0, 1]) / S]
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        S = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = [(m[2, 1] - m[1, 2]) / S, 0.25 * S, (m[0, 1] + m[1, 0]) / S, (m[0, 2] + m[2, 0]) / S]
    elif m[1, 1] > m[2, 2]:
        S = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = [(m[0, 2] - m[2, 0]) / S, (m[0, 1] + m[1, 0]) / S, 0.25 * S, (m[1, 2] + m[2, 1]) / S]
    else:
        S = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = [(m[1, 0] - m[0, 1]) / S, (m[0, 2] + m[2, 0]) / S, (m[1, 2] + m[2, 1]) / S, 0.25 * S]
    q = np.array(q, np.float64)
    return q / np.linalg.norm(q)


def _sh_names(rows):
    names = [k for k in rows if k.startswith("radiance:sh_")]
    return sorted(names, key=lambda k: int(k.rsplit('_', 1)[1]))


def transform_rows(rows, M):
    """A copy of splat rows ({name: [data_type, array]}) carried through the 4x4 M.

    Returns (rows, notes): notes lists what could only be approximated (a non-uniform scale
    becomes its average; a mirror becomes the nearest rotation)."""
    R, s, t, nonuniform, mirrored = decompose(M)
    notes = []
    if nonuniform:
        notes.append("non-uniform scale (its average was used)")
    if mirrored:
        notes.append("mirrored (splats can't be mirrored: the nearest rotation was used)")
    out = {k: [dt, a.copy()] for k, (dt, a) in rows.items()}
    identity = np.allclose(R, np.eye(3), atol=1e-7)
    pos = out["position"][1]
    out["position"][1] = (pos.astype(np.float64) @ (R * s).T + t).astype(pos.dtype)
    if "scale" in out:
        out["scale"][1] = (out["scale"][1] * s).astype(out["scale"][1].dtype)
    if "rotation" in out and not identity:
        q = out["rotation"][1]
        out["rotation"][1] = quat_mul(quat_from_matrix(R), q.astype(np.float64)).astype(q.dtype)
    names = _sh_names(out)
    if names and not identity:
        coeffs = np.stack([out[n][1] for n in names], axis=1).astype(np.float64)   # (N, K, 3)
        rot = coeffs.copy()
        for (lo, hi), Mb in zip(_SH_BANDS, sh_rotation_matrices(R)):
            if hi > len(names):
                break
            rot[:, lo:hi, :] = np.einsum('km,nmc->nkc', Mb, coeffs[:, lo:hi, :])
        for i, n in enumerate(names):
            out[n][1] = rot[:, i, :].astype(out[n][1].dtype)
    return out, notes
