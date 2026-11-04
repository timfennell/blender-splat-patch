"""Splats from a mesh, part 1: geometry and view-dependent colour (pure NumPy).

A mesh becomes splats the way Mesh2Splat does it: give the mesh a UV layout with even
texel density, and put one flat splat on every texel the surface covers. Each splat lies
in its triangle's plane, sized to the texel's footprint there, so the splats tile the
surface with no training.

The colour is what a scan of that surface would have learnt with the light fixed to the
camera (how the bee was captured), because that is what Vintage Click's relighting
expects. Its relight kernel reads, per light:
  - diffuse: base + band 1 looked up from the light's direction, made linear, times the
    light's irradiance;
  - shine: bands 2-3 looked up at the half vector, added in display space.
With the light on the camera, a surface's look depends only on mu = n . (to camera), so
the fits are 1-D (zonal) and turn into SH coefficients through the addition theorem:
a zonal profile g_l P_l(mu) has band-l coefficients g_l 4 pi / (2l + 1) Y_l(-n), in the
3DGS basis (whose direction argument runs camera -> splat).
"""

import numpy as np

SH_C0 = 0.28209479177387814
_C1 = 0.4886025119029199


def sh_basis_full(d):
    """Bands 1-3 of the 3DGS SH basis at unit directions d (M, 3) -> (M, 15)."""
    x, y, z = d[:, 0], d[:, 1], d[:, 2]
    xx, yy, zz = x * x, y * y, z * z
    return np.stack([
        -_C1 * y, _C1 * z, -_C1 * x,
        1.0925484305920792 * x * y, -1.0925484305920792 * y * z,
        0.31539156525252005 * (2 * zz - xx - yy), -1.0925484305920792 * x * z,
        0.5462742152960396 * (xx - yy),
        -0.5900435899266435 * y * (3 * xx - yy), 2.890611442640554 * x * y * z,
        -0.4570457994644658 * y * (4 * zz - xx - yy),
        0.3731763325901154 * z * (2 * zz - 3 * xx - 3 * yy),
        -0.4570457994644658 * x * (4 * zz - xx - yy), 1.445305721320277 * z * (xx - yy),
        -0.5900435899266435 * x * (xx - 3 * yy)], 1)


_BANDS = ((0, 3), (3, 8), (8, 15))


def srgb(x):
    x = np.maximum(x, 0.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def lin(c):
    c = np.maximum(c, 0.0)
    return np.where(c <= 0.04045, c / 12.92, np.power((c + 0.055) / 1.055, 2.4))


def _dlin(c):
    """d lin / d c (0 where the game's clamp cuts it off)."""
    return np.where(c > 0.04045, 2.4 / 1.055 * np.power((np.maximum(c, 0) + 0.055) / 1.055, 1.4),
                    np.where(c > 0, 1 / 12.92, 0.0))


def ggx_headlight(mu, rough):
    """Specular reflectance (per unit F0) of a GGX surface lit from the camera, in units
    where a white Lambert surface facing the light is 1. Light = view, so h = view and
    the Fresnel term is just F0."""
    mu = np.maximum(mu, 1e-6)
    a = np.maximum(rough, 0.02) ** 2
    a2 = a * a
    g1 = 2 * mu / (mu + np.sqrt(a2 + (1 - a2) * mu * mu))
    return a2 * g1 * g1 / (4 * mu * (mu * mu * (a2 - 1) + 1) ** 2)


# Gauss-Legendre nodes over mu in [-1, 1]: the zonal fits integrate over the sphere.
_MU, _W = np.polynomial.legendre.leggauss(48)
_P2 = 0.5 * (3 * _MU ** 2 - 1)
_P3 = 0.5 * (5 * _MU ** 3 - 3 * _MU)
_FRONT = _MU > 0


def fit_diffuse(H):
    """Base value b and band-1 strength k so the game's relit diffuse
    lin(max(b + k mu, 0)) best matches the linear headlight response H(mu).

    H: (..., len(_MU)) linear response at the nodes (0 behind the surface).
    Gauss-Newton on the two parameters, all splats and channels at once."""
    s = srgb(H[..., -1])                          # facing value: a good start
    b = 0.3 * s
    k = 0.75 * s
    w = _W
    for _ in range(14):
        x = b[..., None] + k[..., None] * _MU
        r = lin(x) - H
        dy = _dlin(x)
        jk = dy * _MU
        a11 = (w * dy * dy).sum(-1) + 1e-9
        a12 = (w * dy * jk).sum(-1)
        a22 = (w * jk * jk).sum(-1) + 1e-9
        g1 = (w * dy * r).sum(-1)
        g2 = (w * jk * r).sum(-1)
        det = a11 * a22 - a12 * a12
        b = b - (a22 * g1 - a12 * g2) / det
        k = k - (a11 * g2 - a12 * g1) / det
    return b, np.maximum(k, 0.0)


# The shine shape for highlights narrower than bands 2-3 can draw. Bands 2-3 integrate to
# zero over the sphere, so any lobe they make dips below zero somewhere; this is the
# mix of P2 and P3 whose dip in front of the surface is smallest (to 37 % of the peak,
# near 70 degrees off the normal), scaled to 1 at the pole. Half its peak is ~30 degrees.
_TEMPLATE = np.array([np.cos(0.7848), np.sin(0.7848)])
_TEMPLATE = _TEMPLATE / _TEMPLATE.sum()
_T_NODES = _TEMPLATE[0] * _P2 + _TEMPLATE[1] * _P3
_FIT23 = None
DIP = 0.35       # the shine may darken a splat by at most this share of its diffuse look


def _fit23_matrix():
    global _FIT23
    if _FIT23 is None:
        wt = _W * np.where(_FRONT, 1.0, 0.1)
        A = np.stack([_P2, _P3], 1)
        _FIT23 = np.linalg.solve(A.T @ (wt[:, None] * A), (A * wt[:, None]).T)   # (2, nodes)
    return _FIT23


def headlight_profiles(albedo, f0, rough, gain=1.0, shine=1.0):
    """Zonal coefficients for splats lit from the camera.

    albedo, f0: (N, 3) linear; rough: (N,). Returns base (N, 3) display value, k1 (N, 3)
    band-1 strength, and g2, g3 (N, 3): bands 2-3 zonal strengths (P_l(mu) units).
    """
    mu = _MU
    D = gain * albedo[..., None] * np.maximum(mu, 0)                            # (N, 3, nodes)
    P = gain * f0[..., None] * np.where(_FRONT, ggx_headlight(mu[None, :], rough[:, None]), 0)[:, None, :]
    H = D + P
    # The game relights bands 0-1 as diffuse light, so they only get the broad part of
    # the shine (its projection onto P0 + P1, i.e. its energy). A narrow highlight's
    # brightness would otherwise turn into a glaring diffuse lobe under every lamp.
    p0 = 0.5 * (_W * P).sum(-1)
    p1 = 1.5 * (_W * P * _MU).sum(-1)
    broad = np.where(_FRONT, np.maximum(p0[..., None] + p1[..., None] * _MU, 0.0), 0.0)
    b, k = fit_diffuse(D + broad)
    diffuse = b[..., None] + k[..., None] * mu                                  # display, at the nodes
    # What bands 0-1 leave out of the scan's look, in display space, goes to bands 2-3.
    resid = np.where(_FRONT, srgb(H) - diffuse, 0.0)
    g = np.einsum('ln,...n->...l', _fit23_matrix(), resid)                      # (N, 3, 2)
    # A highlight narrower than the bands can draw keeps its brightness, up to display
    # white (scaled by its brightest channel, so a metal keeps its colour), drawn as the
    # widest-dipping-least lobe they hold; `shine` scales that peak.
    peak = np.maximum(resid[..., -1], 0.0)
    top = np.maximum(peak.max(-1, keepdims=True), 1.0)
    want = peak / top * shine
    have = g[..., 0] + g[..., 1]
    boost = np.maximum(want - have, 0.0)
    # Keep the dip in front from darkening the splat by more than DIP of its diffuse look
    # (bands 2-3 must dip somewhere; past that, edges of shiny things turn black).
    # The floor follows the diffuse look's brightness, not each channel, so a white
    # highlight on red plastic stays white (its dip may push blue to 0 at the rim).
    luma = np.maximum(np.einsum('c,ncm->nm', np.array([0.2126, 0.7152, 0.0722]), diffuse), 0.0)
    floor = -DIP * np.repeat(luma[:, None, :], 3, 1)                            # (N, 3, nodes)
    g_nodes = g[..., 0:1] * _P2 + g[..., 1:2] * _P3
    # the fitted shine itself, scaled down where it dips past the floor
    scale = np.where(_FRONT & (g_nodes < 0), floor / np.minimum(g_nodes, -1e-12), np.inf).min(-1)
    g = g * np.clip(scale, 0.0, 1.0)[..., None]
    g_nodes = g[..., 0:1] * _P2 + g[..., 1:2] * _P3
    # then as much boost as the room left above the floor allows
    room = np.where(_FRONT & (_T_NODES < 0), (g_nodes - floor) / np.maximum(-_T_NODES, 1e-12), np.inf)
    boost = np.minimum(boost, np.maximum(room.min(-1), 0.0))
    g = g + boost[..., None] * _TEMPLATE
    return b, k, g[..., 0], g[..., 1]


def zonal_to_sh(n, base, k1, g2, g3, degree):
    """SH for splats whose colour is a zonal profile around their normal n (N, 3).

    Returns dc (N, 3) (the PLY's f_dc) and rest (N, K, 3) for K = (degree+1)^2 - 1."""
    K = (degree + 1) ** 2 - 1
    Y = sh_basis_full(-n)                                                       # (N, 15)
    rest = np.zeros((len(n), K, 3), np.float32)
    four_pi = 4 * np.pi
    for (lo, hi), g, l in zip(_BANDS, (k1, g2, g3), (1, 2, 3)):
        if hi > K:
            break
        rest[:, lo:hi, :] = (four_pi / (2 * l + 1)) * Y[:, lo:hi, None] * g[:, None, :]
    dc = (base - 0.5) / SH_C0
    return dc.astype(np.float32), rest


def fibonacci_dirs(m):
    i = np.arange(m) + 0.5
    z = 1 - 2 * i / m
    r = np.sqrt(1 - z * z)
    p = np.pi * (3 - 5 ** 0.5) * i
    return np.stack([r * np.cos(p), r * np.sin(p), z], 1)


class EnvFitter:
    """Fits a reflection of the surroundings into bands 2-3, or 1-3 (added to the shine).

    env(dirs (M, 3), rough (N,)) -> (N, M, 3) linear radiance arriving from dirs, blurred
    for each splat's roughness. Directions sample the whole sphere; the reflection is 0
    behind the surface, and the fit is a plain projection (a fixed pseudo-inverse)."""

    def __init__(self, degree, m=192, first=3):
        """first: the first rest coefficient it may use (3 = band 2, 0 = band 1)."""
        self.d = fibonacci_dirs(m)                                   # camera -> splat
        B = sh_basis_full(self.d)[:, first:min((degree + 1) ** 2 - 1, 15)]
        self.lo = first
        self.hi = first + B.shape[1]
        self.pinv = np.linalg.pinv(B) if B.shape[1] else None        # (k, M)

    def fit(self, n, f0, rough, base_lin, env, strength):
        """n (N, 3), f0 (N, 3), rough (N,), base_lin (N, M, 3): the surface's own headlight
        response at the sample views (what the reflection adds to). Returns (N, k, 3)."""
        if self.pinv is None:
            return None
        to_cam = -self.d                                             # (M, 3)
        mu = n @ to_cam.T                                            # (N, M)
        R = 2 * mu[..., None] * n[:, None, :] - to_cam[None]         # mirror direction
        R /= np.maximum(np.linalg.norm(R, axis=-1, keepdims=True), 1e-9)
        L = env(R, rough)                                            # (N, M, 3)
        F = f0[:, None, :] + (1 - f0[:, None, :]) * (1 - np.clip(mu, 0, 1))[..., None] ** 5
        add = np.where((mu > 0)[..., None], strength * F * L, 0.0)
        target = srgb(base_lin + add) - srgb(base_lin)
        return np.einsum('km,nmc->nkc', self.pinv, target)


class SurroundFitter:
    """Base colour and SH together for a surface lit by its surroundings (Light from Scan).

    The look from each view is the baked diffuse light plus the surroundings mirrored in
    the surface (roughness-blurred, roughness-aware Schlick Fresnel), in display space.
    Behind the surface the look is mirrored through it, so the function is smooth and a
    plain projection onto DC + bands 1-3 fits it without dipping below the base colour
    face-on (fitting the reflection alone gives a dark pole under a bright Fresnel rim)."""

    def __init__(self, degree, m=192):
        self.d = fibonacci_dirs(m)                                   # camera -> splat
        K = min((degree + 1) ** 2 - 1, 15)
        B = np.concatenate([np.ones((m, 1)), sh_basis_full(self.d)[:, :K]], 1)
        self.K = K
        self.pinv = np.linalg.pinv(B)                                # (1 + K, M)

    def fit(self, n, diffuse, f0, rough, env, strength):
        """n (N, 3), diffuse (N, 3) linear, f0 (N, 3), rough (N,). Returns base (N, 3) display
        value and rest (N, K, 3)."""
        to_cam = -self.d
        mu = n @ to_cam.T                                            # (N, M)
        tc = to_cam[None] - 2 * np.minimum(mu, 0)[..., None] * n[:, None, :]   # mirrored to the front
        mu = np.abs(mu)
        R = 2 * mu[..., None] * n[:, None, :] - tc
        R /= np.maximum(np.linalg.norm(R, axis=-1, keepdims=True), 1e-9)
        L = env(R, rough)                                            # (N, M, 3)
        top = np.maximum(1 - rough[:, None], f0)                     # Fresnel at grazing, damped by roughness
        F = f0[:, None, :] + (top - f0)[:, None, :] * (1 - mu)[..., None] ** 5
        look = srgb(diffuse[:, None, :] + strength * F * L)
        c = np.einsum('km,nmc->nkc', self.pinv, look)
        return c[:, 0], c[:, 1:]


# ------------------------------------------------------------------ geometry
def rasterize_uv(uv_px, res):
    """Texel centres covered by each triangle.

    uv_px: (T, 3, 2) triangle corners in texel units (texel (i, j) has its centre at
    (i + 0.5, j + 0.5)). Returns tri (S,), bary (S, 3), px (S,), py (S,), one row per
    covered texel.

    Rasterisation is conservative by half a texel: a texel whose centre lies just outside
    every triangle (along a UV island's border, where a seam would otherwise leave a
    texel-wide gap between splats) still gets a splat, on the nearest triangle's plane.
    Where several triangles claim a texel, the one it lies deepest inside wins."""
    T = len(uv_px)
    lo = np.clip(np.floor(uv_px.min(1) - 0.5).astype(np.int64), 0, res - 1)
    hi = np.clip(np.ceil(uv_px.max(1) - 0.5).astype(np.int64), 0, res - 1)
    span = hi - lo + 1
    count = span[:, 0] * span[:, 1]
    tris, barys, pxs, pys, depths = [], [], [], [], []
    # Chunks of triangles, so the candidate texels stay at a few million rows at a time.
    start = 0
    csum = np.cumsum(count)
    while start < T:
        limit = (csum[start - 1] if start else 0) + 4_000_000
        end = max(int(np.searchsorted(csum, limit, side='right')), start + 1)
        sl = slice(start, end)
        c = count[sl]
        idx = np.repeat(np.arange(start, end), c)
        first = np.repeat(np.concatenate([[0], np.cumsum(c)[:-1]]), c)
        k = np.arange(len(idx)) - first
        w = span[idx, 0]
        px = lo[idx, 0] + k % w
        py = lo[idx, 1] + k // w
        p = np.stack([px + 0.5, py + 0.5], 1)
        a, b, cc = uv_px[idx, 0], uv_px[idx, 1], uv_px[idx, 2]
        v0, v1, v2 = b - a, cc - a, p - a
        d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1)
        d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
        den = d00 * d11 - d01 * d01
        ok = np.abs(den) > 1e-18
        den = np.where(ok, den, 1.0)
        l1 = (d11 * d20 - d01 * d21) / den
        l2 = (d00 * d21 - d01 * d20) / den
        l0 = 1 - l1 - l2
        # Each barycentric's tolerance: half a texel over that corner's height in texels
        # (height_i = 2 area / opposite edge).
        area2 = np.maximum(np.abs(v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0]), 1e-18)
        tol0 = 0.5 * np.linalg.norm(cc - b, axis=1) / area2
        tol1 = 0.5 * np.linalg.norm(cc - a, axis=1) / area2
        tol2 = 0.5 * np.linalg.norm(v0, axis=1) / area2
        inside = ok & (l0 >= -tol0) & (l1 >= -tol1) & (l2 >= -tol2)
        # how deep inside, in half-texels, for choosing between claimants
        depth = np.minimum(np.minimum(l0 / tol0, l1 / tol1), l2 / tol2)
        tris.append(idx[inside]); pxs.append(px[inside]); pys.append(py[inside])
        barys.append(np.stack([l0, l1, l2], 1)[inside]); depths.append(depth[inside])
        start = end
    tri = np.concatenate(tris) if tris else np.zeros(0, np.int64)
    px = np.concatenate(pxs) if pxs else np.zeros(0, np.int64)
    py = np.concatenate(pys) if pys else np.zeros(0, np.int64)
    bary = np.concatenate(barys) if barys else np.zeros((0, 3))
    depth = np.concatenate(depths) if depths else np.zeros(0)
    key = py * res + px
    order = np.lexsort((-depth, key))                  # per texel, deepest claimant first
    _, first = np.unique(key[order], return_index=True)
    pick = np.sort(order[first])
    return tri[pick], bary[pick], px[pick], py[pick]


def triangle_frames(P, uv_px, sigma=0.7, thickness=0.02):
    """Per triangle: the splat shape for one texel there.

    P: (T, 3, 3) world corners; uv_px: (T, 3, 2) texel-unit corners. A texel step maps
    to the 3-D vectors J = dP/duv; the splat is that footprint (sigma texels wide), flat
    along the triangle's normal. Returns rot (T, 3, 3) columns = axes, scale (T, 3)."""
    E = np.stack([P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]], 2)            # (T, 3, 2)
    Euv = np.stack([uv_px[:, 1] - uv_px[:, 0], uv_px[:, 2] - uv_px[:, 0]], 2)   # (T, 2, 2)
    det = Euv[:, 0, 0] * Euv[:, 1, 1] - Euv[:, 0, 1] * Euv[:, 1, 0]
    det = np.where(np.abs(det) < 1e-12, 1e-12, det)
    inv = np.stack([np.stack([Euv[:, 1, 1], -Euv[:, 0, 1]], 1),
                    np.stack([-Euv[:, 1, 0], Euv[:, 0, 0]], 1)], 1) / det[:, None, None]
    J = E @ inv                                                         # (T, 3, 2)
    U, S, _ = np.linalg.svd(J, full_matrices=False)                     # U (T, 3, 2)
    u1, u2 = U[:, :, 0], U[:, :, 1]
    nrm = np.cross(E[:, :, 0], E[:, :, 1])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-20)
    # Right-handed frame whose third axis is the triangle's normal.
    u2 = np.cross(nrm, u1)
    rot = np.stack([u1, u2, nrm], 2)
    s1 = np.maximum(S[:, 0], 1e-9) * sigma
    s2 = np.maximum(S[:, 1], 1e-9) * sigma
    scale = np.stack([s1, s2, np.maximum(s1, s2) * thickness], 1)
    return rot, scale


def matrix_to_quat(R):
    """Rotation matrices (N, 3, 3) -> quaternions (N, 4) as w, x, y, z."""
    m00, m11, m22 = R[:, 0, 0], R[:, 1, 1], R[:, 2, 2]
    t = m00 + m11 + m22
    q = np.empty((len(R), 4))
    c0 = t > 0
    c1 = ~c0 & (m00 > m11) & (m00 > m22)
    c2 = ~c0 & ~c1 & (m11 > m22)
    c3 = ~c0 & ~c1 & ~c2
    s = np.sqrt(np.maximum(t[c0] + 1, 1e-12)) * 2
    q[c0] = np.stack([0.25 * s, (R[c0, 2, 1] - R[c0, 1, 2]) / s, (R[c0, 0, 2] - R[c0, 2, 0]) / s,
                      (R[c0, 1, 0] - R[c0, 0, 1]) / s], 1)
    s = np.sqrt(np.maximum(1 + m00[c1] - m11[c1] - m22[c1], 1e-12)) * 2
    q[c1] = np.stack([(R[c1, 2, 1] - R[c1, 1, 2]) / s, 0.25 * s, (R[c1, 0, 1] + R[c1, 1, 0]) / s,
                      (R[c1, 0, 2] + R[c1, 2, 0]) / s], 1)
    s = np.sqrt(np.maximum(1 + m11[c2] - m00[c2] - m22[c2], 1e-12)) * 2
    q[c2] = np.stack([(R[c2, 0, 2] - R[c2, 2, 0]) / s, (R[c2, 0, 1] + R[c2, 1, 0]) / s, 0.25 * s,
                      (R[c2, 1, 2] + R[c2, 2, 1]) / s], 1)
    s = np.sqrt(np.maximum(1 + m22[c3] - m00[c3] - m11[c3], 1e-12)) * 2
    q[c3] = np.stack([(R[c3, 1, 0] - R[c3, 0, 1]) / s, (R[c3, 0, 2] + R[c3, 2, 0]) / s,
                      (R[c3, 1, 2] + R[c3, 2, 1]) / s, 0.25 * s], 1)
    return q / np.linalg.norm(q, axis=1, keepdims=True)
