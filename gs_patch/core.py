"""Splat editing algorithms: pure NumPy plus mathutils (no bpy context needed).

Every function works in the point cloud's object space.
"""

import numpy as np
from mathutils import Vector
from mathutils.kdtree import KDTree

from .splats import SplatSet, BASE_ATTR


# ---------------------------------------------------------------- basic math
def feather_weight(d, radius, feather):
    """1 inside the hard core, cosine falloff to 0 at the radius."""
    d = np.asarray(d, np.float32)
    w = np.ones_like(d)
    if feather > 0.0:
        r_in = radius * (1.0 - feather)
        t = np.clip((d - r_in) / max(radius - r_in, 1e-12), 0.0, 1.0)
        w = 0.5 * (1.0 + np.cos(np.pi * t))
    w[d > radius] = 0.0
    return w


def quat_mul(a, b):
    """a (4,) times each row of b (N, 4); both w, x, y, z."""
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ], axis=1).astype(np.float32)


def rotation_between(n_from, n_to):
    """Minimal rotation taking n_from onto n_to, as (3x3 matrix, w-x-y-z quaternion)."""
    q = Vector(n_from).normalized().rotation_difference(Vector(n_to).normalized())
    return np.array(q.to_matrix(), np.float32), np.array(q, np.float32)


# ------------------------------------------------ view-dependent colour (SH)
# Real spherical harmonic basis used by 3DGS for bands 1-3, in the order of the
# f_rest / radiance:sh_k coefficients (band 1 = k 0..2, band 2 = 3..7, band 3 = 8..14).
_SH_C1 = 0.4886025119029199
_SH_C2 = (1.0925484305920792, -1.0925484305920792, 0.31539156525252005,
          -1.0925484305920792, 0.5462742152960396)
_SH_C3 = (-0.5900435899266435, 2.890611442640554, -0.4570457994644658, 0.3731763325901154,
          -0.4570457994644658, 1.445305721320277, -0.5900435899266435)
_SH_BANDS = ((0, 3), (3, 8), (8, 15))


def sh_basis(d):
    """Bands 1-3 of the 3DGS SH basis at unit directions d (M, 3); returns [(M,3), (M,5), (M,7)]."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    xx, yy, zz = x * x, y * y, z * z
    b1 = np.stack([-_SH_C1 * y, _SH_C1 * z, -_SH_C1 * x], 1)
    c = _SH_C2
    b2 = np.stack([c[0] * x * y, c[1] * y * z, c[2] * (2 * zz - xx - yy),
                   c[3] * x * z, c[4] * (xx - yy)], 1)
    c = _SH_C3
    b3 = np.stack([c[0] * y * (3 * xx - yy), c[1] * x * y * z, c[2] * y * (4 * zz - xx - yy),
                   c[3] * z * (2 * zz - 3 * xx - 3 * yy), c[4] * x * (4 * zz - xx - yy),
                   c[5] * z * (xx - yy), c[6] * x * (xx - 3 * yy)], 1)
    return [b1, b2, b3]


def sh_rotation_matrices(rot_m):
    """Per-band matrices M so that coefficients c' = M @ c describe the colour rotated by rot_m.

    Rotating a splat by R should give colour f'(d) = f(R^T d). Each band's basis
    functions evaluated at R^T d are an exact linear mix of the same band at d,
    so that mix is recovered by least squares over sample directions. That avoids
    hand-deriving Wigner matrices and matches the basis used above by construction.
    """
    rng = np.random.default_rng(12345)
    d = rng.normal(size=(64, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    rot_m = np.asarray(rot_m, np.float64)
    at_d = sh_basis(d)
    at_rd = sh_basis(d @ rot_m)          # rows are R^T d
    # basis(R^T d) = basis(d) @ X, so f' = sum_m c_m basis_m(R^T d) has coefficients X @ c.
    return [np.linalg.lstsq(a, b, rcond=None)[0] for a, b in zip(at_d, at_rd)]


def rotate_sh(S, idx, rot_m):
    """Higher-order SH of splats idx, rotated by rot_m. Returns {attr name: (len(idx), 3)}."""
    names = S.sh_names()
    if not names:
        return {}
    coeffs = np.stack([S.get(n)[idx] for n in names], axis=1).astype(np.float64)  # (N, K, 3)
    out = coeffs.copy()
    for (lo, hi), M in zip(_SH_BANDS, sh_rotation_matrices(rot_m)):
        if hi > len(names):
            break
        out[:, lo:hi, :] = np.einsum('km,nmc->nkc', M, coeffs[:, lo:hi, :])
    return {n: out[:, i, :].astype(np.float32) for i, n in enumerate(names)}


def fit_frame(points, weights=None, iterations=2):
    """Robust plane fit. Returns centroid, tangent e1, tangent e2, normal, inlier mask.

    Points far off the plane (a pin, a hair, a dust speck) are rejected by a
    median-absolute-deviation test and the plane is refit without them.
    """
    if weights is None:
        weights = np.ones(len(points), np.float32)
    inlier = np.ones(len(points), bool)
    c = points.mean(0)
    evecs = np.eye(3)
    for _ in range(iterations + 1):
        w = weights[inlier]
        c = (points[inlier] * w[:, None]).sum(0) / max(w.sum(), 1e-12)
        x = points[inlier] - c
        cov = (x * w[:, None]).T @ x / max(w.sum(), 1e-12)
        _, evecs = np.linalg.eigh(cov)
        h = (points - c) @ evecs[:, 0]
        med = np.median(h[inlier])
        mad = np.median(np.abs(h[inlier] - med)) + 1e-12
        new = np.abs(h - med) < 3.5 * 1.4826 * mad
        if new.sum() < 6 or np.array_equal(new, inlier):
            break
        inlier = new
    n = evecs[:, 0]
    e1 = evecs[:, 2]
    e2 = np.cross(n, e1)
    return c, e1, e2, n, inlier


def surface_frame(S, center, radius, view_dir, min_opacity=0.15):
    """Anchor point and outward normal of the splat surface near center."""
    view_dir = np.asarray(view_dir, np.float32)
    view_dir = view_dir / max(np.linalg.norm(view_dir), 1e-12)
    cand = S.near(center, radius * 1.5)
    d = np.linalg.norm(S.pos[cand] - center, axis=1)
    m = cand[(d < radius * 1.5) & (S.opacity[cand] > min_opacity)]
    if len(m) < 8:
        return np.asarray(center, np.float32), -view_dir
    c, _, _, n, inlier = fit_frame(S.pos[m], S.opacity[m])
    if np.dot(n, view_dir) > 0:
        n = -n
    return c.astype(np.float32), n.astype(np.float32)


def segment_distance(point, pos, axes):
    """Distance from point to each splat's long-axis segment pos +/- axes."""
    rel = point - pos
    t = np.clip((rel * axes).sum(1) / np.maximum((axes * axes).sum(1), 1e-20), -1.0, 1.0)
    return np.linalg.norm(rel - t[:, None] * axes, axis=1)


def sphere_hits(pos, center, radius, feather, axes=None, cand=None):
    """Splats inside the sphere. With axes, a splat counts if any part of its long axis does.

    cand optionally limits the test to those indices (from a spatial query); the
    returned indices are always into pos.
    """
    if cand is None:
        cand = np.arange(len(pos))
    d = np.linalg.norm(pos[cand] - center, axis=1)
    if axes is not None:
        reach = np.linalg.norm(axes[cand], axis=1)
        close = d < radius + reach
        d = np.full(len(cand), np.inf, np.float32)
        d[close] = segment_distance(center, pos[cand[close]], axes[cand[close]])
    keep = d < radius
    return cand[keep], feather_weight(d[keep], radius, feather)


def sphere_hits_S(S, center, radius, feather, axes=None, reach_cap=0.0, long_idx=None):
    """sphere_hits over a SplatSet using its spatial grid (plus the few very long splats)."""
    cand = S.near(center, radius + reach_cap)
    if axes is not None and long_idx is not None and len(long_idx):
        cand = np.union1d(cand, long_idx[long_idx < S.n])
    return sphere_hits(S.pos, center, radius, feather, axes, cand)


# ---------------------------------------------------------------- floaters
_HALF_NEIGHBOURS = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                    if (dx, dy, dz) > (0, 0, 0)]


def connected_components(pos, cell):
    """Label splats by 26-connected occupancy on a voxel grid of the given cell size."""
    ijk = np.floor((pos - pos.min(0)) / cell).astype(np.int64) + 1
    dims = ijk.max(0) + 2
    key = (ijk[:, 0] * dims[1] + ijk[:, 1]) * dims[2] + ijk[:, 2]
    uk, inv = np.unique(key, return_inverse=True)
    a_list, b_list = [], []
    for dx, dy, dz in _HALF_NEIGHBOURS:
        nk = uk + (dx * dims[1] + dy) * dims[2] + dz
        at = np.clip(np.searchsorted(uk, nk), 0, len(uk) - 1)
        m = uk[at] == nk
        a_list.append(np.nonzero(m)[0])
        b_list.append(at[m])
    a, b = np.concatenate(a_list), np.concatenate(b_list)
    label = np.arange(len(uk))
    while True:
        la, lb = label[a], label[b]
        low = np.minimum(la, lb)
        new = label.copy()
        np.minimum.at(new, la, low)   # hook both roots onto the smaller label
        np.minimum.at(new, lb, low)
        while True:                   # pointer jumping
            nxt = new[new]
            if np.array_equal(nxt, new):
                break
            new = nxt
        if np.array_equal(new, label):
            break
        label = new
    return label[np.asarray(inv).ravel()]


def auto_gap(S):
    scale = S.get("scale")
    if scale is None:
        extent = np.linalg.norm(S.pos.max(0) - S.pos.min(0))
        return float(extent / 150.0)
    return float(4.0 * np.median(scale.max(1)))


def floater_mask(S, gap=0.0, keep_fraction=0.02):
    """Splats in clumps that don't touch the main body.

    Clumps are splats joined by chains with no hole wider than about `gap`. Every
    clump smaller than keep_fraction of the largest one is marked.
    """
    vis = np.nonzero(S.visible)[0]
    if len(vis) == 0:
        return np.zeros(S.n, bool), 0.0
    cell = gap if gap > 0 else auto_gap(S)
    comp = connected_components(S.pos[vis], cell)
    _, inverse, counts = np.unique(comp, return_inverse=True, return_counts=True)
    size = counts[np.asarray(inverse).ravel()]
    out = np.zeros(S.n, bool)
    out[vis] = size < keep_fraction * counts.max()
    return out, cell


def ring_mean_dc(S, center, radius, exclude=None):
    """Opacity-weighted mean DC colour in the shell radius..1.6*radius."""
    dc = S.dc
    if dc is None:
        return None
    cand = S.near(center, radius * 1.6)
    d = np.linalg.norm(S.pos[cand] - center, axis=1)
    m = (d > radius) & (d < radius * 1.6) & (S.opacity[cand] > 0.2)
    if exclude is not None:
        m &= ~exclude[cand]
    m = cand[m]
    if len(m) < 4:
        return None
    w = S.opacity[m]
    return (dc[m] * w[:, None]).sum(0) / w.sum()


# ---------------------------------------------------------------- strokes
class Stroke:
    """Accumulates brush dabs against an unchanged SplatSet, then commits once.

    Keeping the splats fixed during a stroke means indices stay valid, dabs never
    re-sample splats the same stroke has just created, and overlapping dabs
    don't compound (each splat keeps the strongest effect any dab gave it).
    """

    def __init__(self, S):
        self.S = S
        n = S.n
        self.factor = np.ones(n, np.float32)       # opacity multiplier on existing splats
        self.select = np.zeros(n, bool)
        self.deselect = np.zeros(n, bool)
        self.hole = np.zeros(n, bool)
        self.used_src = np.zeros(n, bool)
        self.clone_src, self.clone_pos, self.clone_rot = [], [], []
        self.clone_w, self.clone_dc_shift, self.clone_sh = [], [], []
        self.dirty = True

    # --- dabs
    def erase(self, idx, w, strength=1.0):
        self.factor[idx] = np.minimum(self.factor[idx], 1.0 - w * strength)
        self.dirty = True

    def mark_deselect(self, idx):
        self.deselect[idx] = True
        self.select[idx] = False
        self.dirty = True

    def mark_select(self, idx):
        self.select[idx] = True
        self.dirty = True

    def mark_hole(self, idx):
        self.hole[idx] = True
        self.dirty = True

    def clone(self, cs, ns, cd, nd, radius, feather, replace=True, heal=0.0):
        """Copy the sphere at (cs, ns) onto (cd, nd), reoriented to the target surface."""
        S = self.S
        if replace:
            idx, w = sphere_hits(S.pos, cd, radius, feather, cand=S.near(cd, radius))
            live = S.visible[idx]
            self.erase(idx[live], w[live])
        idx, w = sphere_hits(S.pos, cs, radius, feather, cand=S.near(cs, radius))
        fresh = ~self.used_src[idx] & S.visible[idx]
        idx, w = idx[fresh], w[fresh]
        keep = w > 0.02
        idx, w = idx[keep], w[keep]
        if len(idx) == 0:
            return 0
        self.used_src[idx] = True
        rot_m, rot_q = rotation_between(ns, nd)
        self.clone_src.append(idx)
        self.clone_pos.append((S.pos[idx] - cs) @ rot_m.T + cd)
        rot = S.get("rotation")
        self.clone_rot.append(None if rot is None else quat_mul(rot_q, rot[idx]))
        self.clone_sh.append(rotate_sh(S, idx, rot_m))
        self.clone_w.append(w)
        shift = np.zeros(3, np.float32)
        if heal > 0.0:
            md = ring_mean_dc(S, cd, radius)
            ms = ring_mean_dc(S, cs, radius)
            if md is not None and ms is not None:
                shift = (md - ms) * heal
        self.clone_dc_shift.append(np.broadcast_to(shift, (len(idx), 3)))
        self.dirty = True
        return len(idx)

    # --- preview helpers
    def clone_preview(self):
        if not self.clone_pos:
            return None, None
        pos = np.concatenate(self.clone_pos)
        src = np.concatenate(self.clone_src)
        dc = self.S.dc
        if dc is None:
            return pos, self.S.rgb(src)
        rgb = np.clip(0.5 + 0.28209479 * (dc[src] + np.concatenate(self.clone_dc_shift)), 0, 1)
        return pos, rgb

    # --- commit
    def commit_clones_and_erase(self, feather_opacity=True):
        """Apply erase factors and append clones. Returns (removed, added)."""
        S = self.S
        n0 = S.n
        added = 0
        if self.clone_src:
            src = np.concatenate(self.clone_src)
            overrides = {"position": np.concatenate(self.clone_pos)}
            if S.get("rotation") is not None:
                overrides["rotation"] = np.concatenate(self.clone_rot)
            base = S.base
            if base is not None:
                b = base[src].copy()
                b[:, :3] += np.concatenate(self.clone_dc_shift)
                if feather_opacity:
                    b[:, 3] *= np.concatenate(self.clone_w)
                overrides[BASE_ATTR] = b
            for name in S.sh_names():
                overrides[name] = np.concatenate([sh[name] for sh in self.clone_sh])
            overrides["gsp_selected"] = np.zeros((len(src), 1), bool)
            S.append_copies(src, overrides)
            added = len(src)
        factor = np.concatenate([self.factor, np.ones(S.n - n0, np.float32)])
        gone = factor <= (0.02 if S.base is not None else 0.5)
        S.fade(np.where(gone, 1.0, factor))
        removed = S.hide(gone)
        return removed, added


# ---------------------------------------------------------------- restore
def apply_restore(S, T, remove_added, unfade, bring_back):
    """Undo edits for the marked splats.

    remove_added: mask over S of splats added by edits, to delete
    unfade      : mask over S of faded splats, to return to their original opacity
    bring_back  : mask over the stash T of erased originals, to put back into S
    T is modified in place. Returns (restored, unfaded, removed) counts.
    """
    # Erased splats are hidden in place, so restoring them is un-hiding; splats a
    # 0.1.3 session moved to the separate stash (T) are appended back.
    unfade = unfade & ~S.added & (S.hidden | S.faded)
    back = int((unfade & S.hidden).sum())
    unfaded = int((unfade & ~S.hidden).sum())
    S.unhide(unfade)
    removed = S.hide(remove_added & S.added)
    if T is not None and bring_back.any():
        back += int(bring_back.sum())
        S.append_rows(T.rows(bring_back))
        T.keep(~bring_back, stash=False)
    return back, unfaded, removed


# ---------------------------------------------------------------- heal fill
def _grid_flood_outside(free):
    """Cells of `free` reachable from the grid border through free cells."""
    reach = np.zeros_like(free)
    reach[0, :] = free[0, :]
    reach[-1, :] = free[-1, :]
    reach[:, 0] |= free[:, 0]
    reach[:, -1] |= free[:, -1]
    while True:
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= free
        if np.array_equal(grown, reach):
            return reach
        reach = grown


def _dilate(mask, steps=1):
    for _ in range(steps):
        g = mask.copy()
        g[1:, :] |= mask[:-1, :]
        g[:-1, :] |= mask[1:, :]
        g[:, 1:] |= mask[:, :-1]
        g[:, :-1] |= mask[:, 1:]
        mask = g
    return mask


def _kdtree(points):
    kd = KDTree(len(points))
    for i, p in enumerate(points):
        kd.insert(p, i)
    kd.balance()
    return kd


def _quad_design(u, v):
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=1)


class _Surface:
    """Quadratic height field h(u, v) fitted to a ring of splats, in a tangent frame."""

    def __init__(self, pos, op, ring, toward=None, stiffness=0.0):
        c, e1, e2, n, inl = fit_frame(pos[ring], np.clip(op[ring], 0.05, 1.0))
        if toward is not None and np.dot(n, toward) < 0:
            n, e2 = -n, -e2
        self.c, self.e1, self.e2, self.n = c, e1, e2, n
        rel = pos[ring] - c
        self.u, self.v, h = rel @ e1, rel @ e2, rel @ n
        A = _quad_design(self.u, self.v)
        fit_m = inl & (op[ring] > 0.2)
        if np.count_nonzero(fit_m) < 6:
            fit_m = inl
        if stiffness > 0:
            # Ridge on the curvature terms, scaled to the ring's size, so an uneven rim
            # can't make the surface bulge or dive across the gap it has no data for.
            L2 = max(float(np.percentile(self.u ** 2 + self.v ** 2, 90)), 1e-12)
            Af, hf = A[fit_m], h[fit_m]
            reg = np.diag([0, 0, 0, 1, 1, 1]) * stiffness * len(hf) * L2 * L2
            self.coef = np.linalg.solve(Af.T @ Af + reg, Af.T @ hf)
        else:
            self.coef, *_ = np.linalg.lstsq(A[fit_m], h[fit_m], rcond=None)
        self.resid = h - A @ self.coef
        self.ring = ring

    def uvh(self, pts):
        rel = pts - self.c
        return rel @ self.e1, rel @ self.e2, rel @ self.n

    def height(self, uu, vv):
        return _quad_design(uu, vv) @ self.coef

    def point(self, uu, vv, extra=0.0):
        hh = self.height(uu, vv) + extra
        return (self.c + uu[:, None] * self.e1 + vv[:, None] * self.e2 + hh[:, None] * self.n).astype(np.float32)

    def normal(self, uu, vv):
        coef, n, e1, e2 = self.coef, self.n, self.e1, self.e2
        du = coef[1] + 2 * coef[3] * uu + coef[4] * vv
        dv = coef[2] + coef[4] * uu + 2 * coef[5] * vv
        nn = n[None, :] - du[:, None] * e1[None, :] - dv[:, None] * e2[None, :]
        return nn / np.linalg.norm(nn, axis=1, keepdims=True)


class _Grid:
    def __init__(self, uv_points, cell, margin_cells=2, max_dim=600):
        lo = uv_points.min(0) - margin_cells * cell
        hi = uv_points.max(0) + margin_cells * cell
        cell = float(max(cell, (hi - lo).max() / max_dim))
        self.gmin, self.cell = lo, cell
        self.dims = np.ceil((hi - lo) / cell).astype(int) + 1

    def ij(self, uu, vv):
        ij = np.floor((np.stack([uu, vv], 1) - self.gmin) / self.cell).astype(int)
        return np.clip(ij, 0, self.dims - 1)

    def count(self, uu, vv):
        occ = np.zeros(self.dims, np.int32)
        ij = self.ij(uu, vv)
        np.add.at(occ, (ij[:, 0], ij[:, 1]), 1)
        return occ

    def centres(self, ij):
        return self.gmin + (ij + 0.5) * self.cell


def _spacing(pos, idx, sample=None):
    """Median nearest-neighbour distance among splats idx (measured on `sample` of them)."""
    kd = _kdtree(pos[idx])
    probe = idx if sample is None else sample
    return float(np.median([kd.find_n(p, 2)[1][2] for p in pos[probe]])) + 1e-9


def _populate(S, surf, grid, fill_ij, per_cell, exclude, rng, source=None, source_normal_hint=None,
              roughness=1.0, color_smooth=0.5, heal=1.0, feather=0.3):
    """New splats covering grid cells fill_ij of the surface. Returns (src, overrides, msg) or (None, None, msg)."""
    pos, op, base = S.pos, S.opacity, S.base
    ring, cell = surf.ring, grid.cell
    overrides = {}
    if source is None:
        # ---- grow from the ring: copy nearby rim splats onto the surface, keeping their grain
        counts = rng.poisson(per_cell, len(fill_ij))
        cells = np.repeat(fill_ij, counts, axis=0)
        m = len(cells)
        if m == 0:
            return None, None, "Border too sparse to regrow"
        uvn = grid.gmin + (cells + rng.random((m, 2))) * cell
        kd_uv = KDTree(len(ring))
        for i, (a, bb) in enumerate(zip(surf.u, surf.v)):
            kd_uv.insert((a, bb, 0.0), i)
        kd_uv.balance()
        k = min(10, len(ring))
        donor = np.empty(m, np.int64)
        mean_dc = np.zeros((m, 3), np.float32)
        for j, (a, bb) in enumerate(uvn):
            nb = kd_uv.find_n((a, bb, 0.0), k)
            ids = np.array([x[1] for x in nb])
            wts = 1.0 / (np.array([x[2] for x in nb]) + 1e-9)
            wts /= wts.sum()
            donor[j] = ids[rng.choice(len(ids), p=wts)]
            if base is not None:
                mean_dc[j] = (base[ring[ids], :3] * wts[:, None]).sum(0)
        src = ring[donor]
        overrides["position"] = surf.point(uvn[:, 0], uvn[:, 1], roughness * surf.resid[donor])
        if base is not None:
            nb_ = base[src].copy()
            nb_[:, :3] = (1 - color_smooth) * nb_[:, :3] + color_smooth * mean_dc
            overrides[BASE_ATTR] = nb_
        return src, overrides, "Grown from surroundings"

    # ---- clone from a sample area onto the surface, tone-matched to the ring
    src_pt = np.asarray(source, np.float32)
    fill_uv = grid.centres(fill_ij)
    u0, v0 = fill_uv.mean(0)
    cd = surf.point(np.array([u0]), np.array([v0]))[0]
    nd = surf.normal(np.array([u0]), np.array([v0]))[0]
    reach = float(np.linalg.norm(fill_uv - (u0, v0), axis=1).max()) + cell
    fade = max(2 * cell, feather * reach)
    R = reach + fade
    hint = source_normal_hint if source_normal_hint is not None else -nd
    cs, ns = surface_frame(S, src_pt, R, hint)
    if source_normal_hint is None and np.dot(ns, nd) < 0:
        ns = -ns
    cand = S.near(cs, R * 1.3)
    d = np.linalg.norm(pos[cand] - cs, axis=1)
    sidx = cand[~exclude[cand] & S.visible[cand] & (d < R * 1.3)]
    if len(sidx) == 0:
        return None, None, "No splats found around the sample point"
    rot_m, rot_q = rotation_between(ns, nd)
    mapped = (pos[sidx] - cs) @ rot_m.T + cd
    mu, mv, _ = surf.uvh(mapped)
    kd_f = KDTree(len(fill_uv))
    for i, (a, bb) in enumerate(fill_uv):
        kd_f.insert((a, bb, 0.0), i)
    kd_f.balance()
    dist_fill = np.array([kd_f.find((a, bb, 0.0))[2] for a, bb in zip(mu, mv)], np.float32)
    inside = dist_fill <= 0.75 * cell
    w = np.where(inside, 1.0, feather_weight(dist_fill, fade + 0.75 * cell, 1.0))
    use = w > 0.02
    src, w, mapped = sidx[use], w[use], mapped[use]
    if len(src) == 0:
        return None, None, "Sample area does not cover the gap"
    overrides["position"] = mapped.astype(np.float32)
    rot = S.get("rotation")
    if rot is not None:
        overrides["rotation"] = quat_mul(rot_q, rot[src])
    overrides.update(rotate_sh(S, src, rot_m))
    if base is not None:
        nb_ = base[src].copy()
        if heal > 0:
            ring_w = op[ring]
            ring_dc = (base[ring, :3] * ring_w[:, None]).sum(0) / ring_w.sum()
            outer = ~inside[use]
            sw = nb_[outer, 3] if np.any(outer) else nb_[:, 3]
            sdc = nb_[outer, :3] if np.any(outer) else nb_[:, :3]
            src_dc = (sdc * sw[:, None]).sum(0) / max(sw.sum(), 1e-9)
            nb_[:, :3] += heal * (ring_dc - src_dc)
        nb_[:, 3] *= w
        overrides[BASE_ATTR] = nb_
    return src, overrides, "Filled from sample area"


def _append(S, src, overrides, hide_mask=None):
    """Add the new splats and erase (hide) hide_mask. Returns the number added."""
    overrides["gsp_selected"] = np.zeros((len(src), 1), bool)
    S.append_copies(src, overrides)
    if hide_mask is not None:
        S.hide(np.concatenate([hide_mask, np.zeros(len(src), bool)]))
    return len(src)


def heal_fill(S, hole_mask, border=0.0, roughness=1.0, color_smooth=0.5, density=1.0,
              source=None, source_normal_hint=None, heal=1.0, feather=0.3, seed=0):
    """Delete the hole splats and regrow the surface across the gap.

    source=None   : new splats are grown from the surrounding border (spot heal).
    source=(x,y,z): splats are cloned from around that point into the gap, then
                    tone-matched to the border (healing brush with a sample area).

    Returns (removed, added, message).
    """
    rng = np.random.default_rng(seed)
    pos, op = S.pos, S.opacity
    hole_mask = hole_mask & S.visible
    hole_idx = np.nonzero(hole_mask)[0]
    if len(hole_idx) == 0:
        return 0, 0, "Nothing selected to fill"
    H = pos[hole_idx]
    hc = H.mean(0)
    h_rad = float(np.percentile(np.linalg.norm(H - hc, axis=1), 95)) + 1e-9

    # Existing splats near the hole, with their distance to the nearest hole splat.
    kept = ~hole_mask & S.visible
    lo, hi = H.min(0) - h_rad, H.max(0) + h_rad
    cand = np.nonzero(kept & np.all((pos > lo) & (pos < hi), axis=1))[0]
    if len(cand) < 12:
        return 0, 0, "Not enough surrounding splats to heal from"
    kd_h = _kdtree(H)
    dh = np.array([kd_h.find(p)[2] for p in pos[cand]], np.float32)

    # Local splat spacing, measured on the splats right at the hole's edge.
    edge = cand[np.argsort(dh)[:min(400, len(cand))]]
    spacing = _spacing(pos, cand, edge)

    def surface_for(border_w):
        ring = cand[dh < border_w]
        return _Surface(pos, op, ring) if len(ring) >= 12 else None

    b = border if border > 0 else 10.0 * spacing
    surf = surface_for(b)
    if surf is None:
        return 0, 0, "Not enough surrounding splats to heal from"
    if border <= 0:
        # Size the border from the hole's footprint on the surface (not its full
        # 3D extent, which a long pin would exaggerate).
        hu, hv, _ = surf.uvh(H)
        uv = np.stack([hu, hv], 1)
        foot = float(np.percentile(np.linalg.norm(uv - uv.mean(0), axis=1), 95))
        b = max(4.0 * spacing, 0.75 * foot)
        surf = surface_for(b) or surf

    hu, hv, _ = surf.uvh(H)
    grid = _Grid(np.concatenate([np.stack([surf.u, surf.v], 1), np.stack([hu, hv], 1)]), 2.0 * spacing)
    occupied = grid.count(surf.u, surf.v) > 0
    closed = _dilate(occupied, 1)
    enclosed = ~closed & ~_grid_flood_outside(~closed)
    hole_cells = np.zeros(grid.dims, bool)
    hij = grid.ij(hu, hv)
    hole_cells[hij[:, 0], hij[:, 1]] = True
    fill_ij = np.argwhere(~occupied & (_dilate(hole_cells, 1) | _dilate(enclosed, 1)))
    if len(fill_ij) == 0:
        S.hide(hole_mask)
        return len(hole_idx), 0, "Removed; the surrounding surface already covers the gap"

    per_cell = len(surf.ring) / max(np.count_nonzero(occupied), 1) * density
    src, overrides, msg = _populate(S, surf, grid, fill_ij, per_cell, hole_mask, rng, source,
                                    source_normal_hint, roughness, color_smooth, heal, feather)
    if src is None:
        if source is not None:
            return 0, 0, msg
        S.hide(hole_mask)
        return len(hole_idx), 0, "Removed; " + msg.lower()
    added = _append(S, src, overrides, hole_mask)
    return len(hole_idx), added, "Healed from surroundings" if source is None else msg


def bridge_fill(S, ring, near, in_footprint, toward, edge=None, roughness=1.0, color_smooth=0.5,
                density=1.0, source=None, heal=1.0, feather=0.3, seed=0, stiffness=0.05,
                footprint_is_hole=False):
    """Continue the surface across an empty gap.

    ring        : indices of intact front-surface splats around the gap (the rim)
    near        : indices of all splats in and around the gap, at any depth
    in_footprint: fn(points (M, 3)) -> bool mask, True where the user painted the gap
    toward      : a direction pointing out of the surface, towards the viewer
    edge        : the rim splats right at the hole's edge; they set which layer is the
                  surface, so rim splats well in front of or behind it (a leg or wing
                  crossing the rim) are ignored

    A smooth surface is fitted to the rim and new splats are grown on it wherever
    it passes through the painted footprint and nothing already sits on it. Splats
    deeper inside the gap (the far wall of a pin hole) are left alone; the bridge
    simply covers them. Returns (removed, added, message).
    """
    rng = np.random.default_rng(seed)
    pos, op = S.pos, S.opacity
    if len(ring) < 12:
        return 0, 0, "Not enough intact surface around the gap; paint a little onto it"
    spacing = _spacing(pos, ring, ring[rng.choice(len(ring), min(400, len(ring)), replace=False)])
    if edge is not None and len(edge) >= 12:
        c, _, _, n, inl = fit_frame(pos[edge], np.clip(op[edge], 0.05, 1.0))
        he = (pos[edge][inl] - c) @ n
        mad = float(np.median(np.abs(he - np.median(he)))) * 1.4826
        h = (pos[ring] - c) @ n
        layer = np.abs(h - np.median(he)) < max(4.0 * mad, 3.0 * spacing)
        if layer.sum() >= 12:
            ring = ring[layer]
    surf = _Surface(pos, op, ring, toward, stiffness)
    grid = _Grid(np.stack([surf.u, surf.v], 1), 2.0 * spacing)

    removed = 0

    # Cells that already have surface: compare each cell's outermost splat with how far out
    # the rim's cells typically reach. A pit's top sits well below that, however rough the rim.
    def cell_tops(uu, vv, res):
        ij = grid.ij(uu, vv)
        key = ij[:, 0] * grid.dims[1] + ij[:, 1]
        top = np.full(grid.dims[0] * grid.dims[1], -np.inf)
        np.maximum.at(top, key, res)
        return top.reshape(grid.dims)
    ring_top = cell_tops(surf.u, surf.v, surf.resid)
    ring_cells = np.isfinite(ring_top)
    reach = float(np.percentile(ring_top[ring_cells], 10)) - spacing
    solid_near = near[op[near] > 0.1]
    nu, nv, nh = surf.uvh(pos[solid_near])
    occupied = cell_tops(nu, nv, nh - surf.height(nu, nv)) >= reach

    all_ij = np.argwhere(np.ones(grid.dims, bool))
    cu, cv = grid.centres(all_ij).T
    painted = in_footprint(surf.point(cu, cv)).reshape(grid.dims)
    # footprint_is_hole: the caller already worked out (from the view) which part is missing.
    fill_ij = np.argwhere(painted if footprint_is_hole else painted & ~occupied)
    per_cell = len(ring) / max(np.count_nonzero(ring_cells), 1) * density
    if len(fill_ij) == 0:
        return removed, 0, "The surface under the painted area is already covered"
    src, overrides, msg = _populate(S, surf, grid, fill_ij, per_cell, np.zeros(S.n, bool), rng,
                                    source, None, roughness, color_smooth, heal, feather)
    if src is None:
        return removed, 0, msg
    added = _append(S, src, overrides)
    return removed, added, "Bridged from surroundings" if source is None else "Bridged from sample area"
