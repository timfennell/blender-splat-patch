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
    d = np.linalg.norm(S.pos - center, axis=1)
    m = (d < radius * 1.5) & (S.opacity > min_opacity)
    if np.count_nonzero(m) < 8:
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


def sphere_hits(pos, center, radius, feather, axes=None):
    """Splats inside the sphere. With axes, a splat counts if any part of its long axis does."""
    d = np.linalg.norm(pos - center, axis=1)
    if axes is not None:
        reach = np.linalg.norm(axes, axis=1)
        near = np.nonzero(d < radius + reach)[0]
        d_near = segment_distance(center, pos[near], axes[near])
        d = np.full(len(pos), np.inf, np.float32)
        d[near] = d_near
    idx = np.nonzero(d < radius)[0]
    return idx, feather_weight(d[idx], radius, feather)


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
    if S.n == 0:
        return np.zeros(0, bool), 0.0
    cell = gap if gap > 0 else auto_gap(S)
    comp = connected_components(S.pos, cell)
    _, inverse, counts = np.unique(comp, return_inverse=True, return_counts=True)
    size = counts[inverse]
    return size < keep_fraction * counts.max(), cell


def ring_mean_dc(S, center, radius, exclude=None):
    """Opacity-weighted mean DC colour in the shell radius..1.6*radius."""
    dc = S.dc
    if dc is None:
        return None
    d = np.linalg.norm(S.pos - center, axis=1)
    m = (d > radius) & (d < radius * 1.6) & (S.opacity > 0.2)
    if exclude is not None:
        m &= ~exclude
    if np.count_nonzero(m) < 4:
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
        self.hole = np.zeros(n, bool)
        self.used_src = np.zeros(n, bool)
        self.clone_src, self.clone_pos, self.clone_rot = [], [], []
        self.clone_w, self.clone_dc_shift = [], []
        self.dirty = True

    # --- dabs
    def erase(self, idx, w, strength=1.0):
        self.factor[idx] = np.minimum(self.factor[idx], 1.0 - w * strength)
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
            idx, w = sphere_hits(S.pos, cd, radius, feather)
            self.erase(idx, w)
        idx, w = sphere_hits(S.pos, cs, radius, feather)
        fresh = ~self.used_src[idx]
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
            overrides["gsp_selected"] = np.zeros((len(src), 1), bool)
            S.append_copies(src, overrides)
            added = len(src)
        factor = np.concatenate([self.factor, np.ones(S.n - n0, np.float32)])
        base = S.base
        if base is not None:
            base[:, 3] *= factor
            keep = factor > 0.02
        else:
            keep = factor > 0.5
        removed = int(np.count_nonzero(~keep))
        if removed:
            S.keep(keep)
        return removed, added


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
    hole_idx = np.nonzero(hole_mask)[0]
    if len(hole_idx) == 0:
        return 0, 0, "Nothing selected to fill"
    H = pos[hole_idx]
    hc = H.mean(0)
    h_rad = float(np.percentile(np.linalg.norm(H - hc, axis=1), 95)) + 1e-9

    # Existing splats near the hole, with their distance to the nearest hole splat.
    kept = ~hole_mask
    lo, hi = H.min(0) - h_rad, H.max(0) + h_rad
    cand = np.nonzero(kept & np.all((pos > lo) & (pos < hi), axis=1))[0]
    if len(cand) < 12:
        return 0, 0, "Not enough surrounding splats to heal from"
    kd_h = _kdtree(H)
    dh = np.array([kd_h.find(p)[2] for p in pos[cand]], np.float32)

    # Local splat spacing, measured on the splats right at the hole's edge.
    edge = cand[np.argsort(dh)[:min(400, len(cand))]]
    kd_c = _kdtree(pos[cand])
    spacing = float(np.median([kd_c.find_n(p, 2)[1][2] for p in pos[edge]])) + 1e-9

    def frame_for(border_w):
        ring = cand[dh < border_w]
        if len(ring) < 12:
            return None
        c, e1, e2, n, inl = fit_frame(pos[ring], np.clip(op[ring], 0.05, 1.0))
        return ring, c, e1, e2, n, inl

    b = border if border > 0 else 10.0 * spacing
    fr = frame_for(b)
    if fr is None:
        return 0, 0, "Not enough surrounding splats to heal from"
    if border <= 0:
        # Size the border from the hole's footprint on the surface (not its full
        # 3D extent, which a long pin would exaggerate).
        _, c, e1, e2, n, _ = fr
        uv = np.stack([(H - c) @ e1, (H - c) @ e2], 1)
        foot = float(np.percentile(np.linalg.norm(uv - uv.mean(0), axis=1), 95))
        b = max(4.0 * spacing, 0.75 * foot)
        fr = frame_for(b) or fr
    ring, c, e1, e2, n, inl = fr

    # Height field over the ring: quadratic surface plus per-splat residual (grain).
    rel = pos[ring] - c
    u, v, h = rel @ e1, rel @ e2, rel @ n
    A = _quad_design(u, v)
    fit_m = inl & (op[ring] > 0.2)
    if np.count_nonzero(fit_m) < 6:
        fit_m = inl
    coef, *_ = np.linalg.lstsq(A[fit_m], h[fit_m], rcond=None)
    resid = h - A @ coef

    # Occupancy grid in the tangent plane.
    cell = 2.0 * spacing
    uvH = np.stack([(H - c) @ e1, (H - c) @ e2], 1)
    all_uv = np.concatenate([np.stack([u, v], 1), uvH])
    gmin = all_uv.min(0) - 2 * cell
    dims = np.minimum(np.ceil((all_uv.max(0) + 2 * cell - gmin) / cell).astype(int), 600)
    cell = float(max((all_uv.max(0) + 2 * cell - gmin).max() / dims.max(), cell))
    dims = np.ceil((all_uv.max(0) + 2 * cell - gmin) / cell).astype(int) + 1

    def to_cell(uv):
        ij = np.floor((uv - gmin) / cell).astype(int)
        return np.clip(ij, 0, dims - 1)

    ring_ij = to_cell(np.stack([u, v], 1))
    occ = np.zeros(dims, np.int32)
    np.add.at(occ, (ring_ij[:, 0], ring_ij[:, 1]), 1)
    occupied = occ > 0
    closed = _dilate(occupied, 1)
    enclosed = ~closed & ~_grid_flood_outside(~closed)
    hole_cells = np.zeros(dims, bool)
    hij = to_cell(uvH)
    hole_cells[hij[:, 0], hij[:, 1]] = True
    fill = ~occupied & (_dilate(hole_cells, 1) | _dilate(enclosed, 1))
    fill_ij = np.argwhere(fill)
    if len(fill_ij) == 0:
        # Hole was already covered by the border; just delete it.
        S.keep(kept)
        return len(hole_idx), 0, "Removed; the surrounding surface already covers the gap"

    per_cell = len(ring) / max(np.count_nonzero(occupied), 1) * density

    def surface_point(uu, vv):
        return _quad_design(uu, vv) @ coef

    def surface_normal(uu, vv):
        du = coef[1] + 2 * coef[3] * uu + coef[4] * vv
        dv = coef[2] + coef[4] * uu + 2 * coef[5] * vv
        nn = n[None, :] - du[:, None] * e1[None, :] - dv[:, None] * e2[None, :]
        return nn / np.linalg.norm(nn, axis=1, keepdims=True)

    base = S.base
    overrides = {}
    if source is None:
        # ---- grow from the border
        counts = rng.poisson(per_cell, len(fill_ij))
        cells = np.repeat(fill_ij, counts, axis=0)
        m = len(cells)
        if m == 0:
            S.keep(kept)
            return len(hole_idx), 0, "Removed; border too sparse to regrow"
        uvn = gmin + (cells + rng.random((m, 2))) * cell
        kd_uv = KDTree(len(ring))
        for i, (a, bb) in enumerate(zip(u, v)):
            kd_uv.insert((a, bb, 0.0), i)
        kd_uv.balance()
        k = min(10, len(ring))
        donor = np.empty(m, np.int64)
        mean_dc = np.zeros((m, 3), np.float32)
        for j, (a, bb) in enumerate(uvn):
            nb = kd_uv.find_n((a, bb, 0.0), k)
            ids = np.array([x[1] for x in nb])
            ds = np.array([x[2] for x in nb]) + 1e-9
            wts = 1.0 / ds
            wts /= wts.sum()
            donor[j] = ids[rng.choice(len(ids), p=wts)]
            if base is not None:
                mean_dc[j] = (base[ring[ids], :3] * wts[:, None]).sum(0)
        src = ring[donor]
        hh = surface_point(uvn[:, 0], uvn[:, 1]) + roughness * resid[donor]
        overrides["position"] = (c + uvn[:, :1] * e1 + uvn[:, 1:] * e2 + hh[:, None] * n).astype(np.float32)
        if base is not None:
            nb_ = base[src].copy()
            nb_[:, :3] = (1 - color_smooth) * nb_[:, :3] + color_smooth * mean_dc
            overrides[BASE_ATTR] = nb_
        msg = "Healed from surroundings"
    else:
        # ---- clone from a sample area into the gap, tone-matched to the border
        src_pt = np.asarray(source, np.float32)
        fill_uv = gmin + (fill_ij + 0.5) * cell
        u0, v0 = fill_uv.mean(0)
        cd = c + u0 * e1 + v0 * e2 + surface_point(np.array([u0]), np.array([v0]))[0] * n
        nd = surface_normal(np.array([u0]), np.array([v0]))[0]
        reach = float(np.linalg.norm(fill_uv - (u0, v0), axis=1).max()) + cell
        fade = max(2 * cell, feather * reach)
        R = reach + fade
        hint = source_normal_hint if source_normal_hint is not None else -nd
        cs, ns = surface_frame(S, src_pt, R, hint)
        if source_normal_hint is None and np.dot(ns, nd) < 0:
            ns = -ns
        d = np.linalg.norm(pos - cs, axis=1)
        sidx = np.nonzero(kept & (d < R * 1.3))[0]
        if len(sidx) == 0:
            return 0, 0, "No splats found around the sample point"
        rot_m, rot_q = rotation_between(ns, nd)
        mapped = (pos[sidx] - cs) @ rot_m.T + cd
        muv = np.stack([(mapped - c) @ e1, (mapped - c) @ e2], 1)
        kd_f = KDTree(len(fill_uv))
        for i, (a, bb) in enumerate(fill_uv):
            kd_f.insert((a, bb, 0.0), i)
        kd_f.balance()
        dist_fill = np.array([kd_f.find((a, bb, 0.0))[2] for a, bb in muv], np.float32)
        inside = dist_fill <= 0.75 * cell
        w = np.where(inside, 1.0, feather_weight(dist_fill, fade + 0.75 * cell, 1.0))
        use = w > 0.02
        src, w, mapped = sidx[use], w[use], mapped[use]
        if len(src) == 0:
            return 0, 0, "Sample area does not cover the gap"
        overrides["position"] = mapped.astype(np.float32)
        rot = S.get("rotation")
        if rot is not None:
            overrides["rotation"] = quat_mul(rot_q, rot[src])
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
        msg = "Filled from sample area"

    overrides["gsp_selected"] = np.zeros((len(src), 1), bool)
    S.append_copies(src, overrides)
    added = len(src)
    S.keep(np.concatenate([kept, np.ones(added, bool)]))
    return len(hole_idx), added, msg
