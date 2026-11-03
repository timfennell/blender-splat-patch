"""Low-detail stand-ins for arranging several big scans.

A proxy is a small point cloud with a fraction of the model's splats (the most visible ones
more likely: opaque and large), each grown a little so the model still reads as solid, and
without the view-dependent colour. While it's on, the object's data is the proxy; the full
model is kept (fake user) and pointed to from the proxy ("gsp_full"), so the viewport and
Blender's depsgraph only ever touch the small cloud. Full Detail swaps it back.

Everything that changes or writes the model (export, merge, bake, the stash) reads the full
data through full_data(); the brushes don't run on a proxy.

A work area is a mix of both: every splat of the full model inside a sphere, with all its
attributes and its row in the full model (gsp_full_index), plus the stand-in's splats outside
it, locked (gsp_locked) so no tool edits, samples or selects them. The brushes run on it as on
any model; sync() writes the region's edits back into the full model, which final renders,
export, merge and bake always use. The full model marks the region's rows (gsp_in_work), so
splats a tool removed can be removed there too.
"""

import bpy
import numpy as np

from .splats import _LAYOUT, BASE_ATTR, HIDDEN_ATTR, LOCKED_ATTR, FULL_INDEX_ATTR, SplatSet

FULL_KEY = "gsp_full"          # on the proxy or work area: the full point cloud
PROXY_KEY = "gsp_proxy"        # on the full cloud: its proxy (shared by linked duplicates)
FRACTION_KEY = "gsp_lod_fraction"
WORK_KEY = "gsp_workarea"      # on a work area: centre x, y, z and radius (object space)
SYNCED_KEY = "gsp_synced_revision"
IN_WORK_ATTR = "gsp_in_work"   # on the full cloud: rows copied into the work area
REV_KEY = "gsp_revision"
_KEEP = ("position", "scale", "rotation", BASE_ATTR)
GROW = 0.12                    # kept splats grow by (1 / fraction) ** GROW


def is_standin(obj):
    return obj is not None and obj.type == 'POINTCLOUD' and \
        isinstance(obj.data.get(FULL_KEY), bpy.types.PointCloud)


def is_workarea(obj):
    return is_standin(obj) and WORK_KEY in obj.data


def is_proxy(obj):
    """A plain low-detail stand-in (not a work area, which the brushes can edit)."""
    return is_standin(obj) and WORK_KEY not in obj.data


def full_data(obj):
    """The object's full point cloud, whether a proxy or work area is showing or not."""
    if is_standin(obj):
        return obj.data[FULL_KEY]
    return obj.data


def _read(pc, name):
    attr = pc.attributes.get(name)
    if attr is None or attr.domain != 'POINT':
        return None, None
    width, key, dtype = _LAYOUT[attr.data_type]
    n = len(pc.points)
    buf = np.empty(n * width, dtype)
    if n:
        attr.data.foreach_get(key, buf)
    return attr.data_type, buf.reshape(n, width)


def choose(opacity, scale, fraction, seed=1):
    """Indices of about `fraction` of the splats: a weighted sample without replacement
    (Efraimidis-Spirakis), weight = opacity x the splat's face-on area, so the splats that
    make the picture are kept and faint specks mostly go."""
    n = len(opacity)
    k = max(1, min(n, int(round(n * fraction))))
    if scale is not None:
        s = np.sort(scale, axis=1)
        area = s[:, -1] * s[:, -2]
    else:
        area = np.ones(n)
    w = np.maximum(opacity.astype(np.float64) * np.sqrt(area.astype(np.float64)), 1e-12)
    u = np.random.default_rng(seed).random(n)
    key = np.log(np.maximum(u, 1e-300)) / w
    idx = np.sort(np.argpartition(-key, k - 1)[:k])
    # each kept splat's chance of being kept (about k w / sum w, at most 1)
    p = np.minimum(1.0, k * w[idx] / w.sum())
    return idx, p


def _proxy_columns(full, fraction):
    """The stand-in's columns ({name: (data_type, array)}) for a full point cloud."""
    cols = {name: _read(full, name) for name in _KEEP}
    _, base = cols[BASE_ATTR]
    _, scale = cols["scale"]
    n = len(full.points)
    visible = np.ones(n, bool)
    _, hidden = _read(full, HIDDEN_ATTR)
    if hidden is not None:
        visible &= ~hidden[:, 0].astype(bool)
    vis_idx = np.nonzero(visible)[0]
    if len(vis_idx) == 0:
        return {name: (dt, arr[:0]) for name, (dt, arr) in cols.items() if arr is not None}
    op = base[vis_idx, 3] if base is not None else np.ones(len(vis_idx), np.float32)
    chosen, p = choose(op, None if scale is None else scale[vis_idx], fraction)
    pick = vis_idx[chosen]
    # Fewer splats each cover more: grow them so a surface stays closed (between the
    # area a surface needs, 1/f, and the volume fur needs, 1/f per axis cubed).
    grow = np.float32((len(vis_idx) / max(len(pick), 1)) ** GROW)
    out = {}
    for name, (dt, arr) in cols.items():
        if arr is None:
            continue
        a = arr[pick]
        if name == "scale":
            a = a * grow
        elif name == BASE_ATTR:
            # A kept splat stands for the ones dropped around it: it blocks their light
            # too (optical depth / chance of being kept), so faint layers like a wing's
            # membrane keep their density instead of turning see-through.
            a = a.copy()
            tau = -np.log1p(-np.clip(a[:, 3], 0.0, 0.999)) / p
            a[:, 3] = np.minimum(1.0 - np.exp(-tau / grow ** 2), 0.995)
        out[name] = (dt, a)
    return out


def _set(pc, name, dt, arr):
    attr = pc.attributes.get(name) or pc.attributes.new(name, dt, 'POINT')
    if len(arr):
        attr.data.foreach_set(_LAYOUT[dt][1], np.ascontiguousarray(arr, _LAYOUT[dt][2]).ravel())


def _drop(old, keep_for=None):
    """Remove a stand-in point cloud nothing shows any more."""
    if old is None or old == keep_for or any(o.data == old for o in bpy.data.objects):
        return
    full = old.get(FULL_KEY)
    if isinstance(full, bpy.types.PointCloud) and full.get(PROXY_KEY) == old:
        del full[PROXY_KEY]
    bpy.data.pointclouds.remove(old)


def make_proxy(obj, fraction=0.1):
    """Show a low-detail stand-in in place of the object's model. Returns its splat count."""
    full = full_data(obj)
    if is_proxy(obj):
        if abs(float(obj.data.get(FRACTION_KEY, 0)) - fraction) < 1e-6:
            return len(obj.data.points)
        restore(obj)
    elif is_workarea(obj):
        restore(obj)
    old = full.get(PROXY_KEY)
    if isinstance(old, bpy.types.PointCloud) and abs(float(old.get(FRACTION_KEY, 0)) - fraction) < 1e-6:
        proxy = old                                    # a linked duplicate made it already
    else:
        cols = _proxy_columns(full, fraction)
        proxy = bpy.data.pointclouds.new(full.name + "_lod")
        proxy.resize(len(cols["position"][1]))
        for name, (dt, arr) in cols.items():
            _set(proxy, name, dt, arr)
        for m in full.materials:
            proxy.materials.append(m)
        proxy[FULL_KEY] = full
        proxy[FRACTION_KEY] = float(fraction)
        full[PROXY_KEY] = proxy
    full.use_fake_user = True                          # kept while nothing shows it
    obj.data = proxy
    return len(proxy.points)


def restore(obj):
    """Bring back the full model (a work area's edits go into it first). Returns its splat count."""
    if not is_standin(obj):
        return len(obj.data.points)
    if is_workarea(obj):
        sync(obj)
    shown = obj.data
    full = shown[FULL_KEY]
    obj.data = full
    full.use_fake_user = False
    _clear_region(full)
    # Remove it unless a linked duplicate still shows it (the full cloud's pointer to it
    # counts as a user too, so look at the objects themselves).
    if any(o.data == shown for o in bpy.data.objects):
        full.use_fake_user = True                      # still behind another object's stand-in
    else:
        _drop(shown)
    return len(full.points)


# ------------------------------------------------------------------ work area
def _clear_region(full):
    attr = full.attributes.get(IN_WORK_ATTR)
    if attr is not None:
        full.attributes.remove(attr)


def workarea(obj):
    """(centre, radius) of the object's work area in object space, or None."""
    if not is_workarea(obj):
        return None
    c = obj.data[WORK_KEY]
    return np.array(c[:3], np.float64), float(c[3])


def open_workarea(obj, center, radius, fraction=0.1):
    """Show full detail inside a sphere (object space) and the stand-in outside it.
    Moving an open work area writes its edits back first. Returns (inside, outside) counts."""
    full = full_data(obj)
    if is_workarea(obj):
        sync(obj)
    old = obj.data if is_standin(obj) else None
    center = np.asarray(center, np.float64)
    n = len(full.points)
    _, pos = _read(full, "position")
    inside = np.nonzero(np.linalg.norm(pos - center, axis=1) < radius)[0] if n else np.zeros(0, np.int64)
    del pos
    proxy = _proxy_columns(full, fraction)
    ppos = proxy["position"][1]
    outside = np.linalg.norm(ppos - center, axis=1) >= radius
    m_in, m_out = len(inside), int(outside.sum())

    hyb = bpy.data.pointclouds.new(full.name + "_work")
    hyb.resize(m_in + m_out)
    for attr in list(full.attributes):
        name = attr.name
        if attr.domain != 'POINT' or name.startswith('.') or name == IN_WORK_ATTR \
                or attr.data_type not in _LAYOUT:
            continue
        dt, col = _read(full, name)
        a = col[inside]
        del col
        if name in proxy:
            b = proxy[name][1][outside]
        else:                                          # SH etc.: the stand-in has none
            b = np.zeros((m_out,) + a.shape[1:], a.dtype)
        _set(hyb, name, dt, np.concatenate([a, b.astype(a.dtype)], axis=0))
    _set(hyb, FULL_INDEX_ATTR, 'INT',
         np.concatenate([inside.astype(np.int32), np.full(m_out, -1, np.int32)])[:, None])
    _set(hyb, LOCKED_ATTR, 'BOOLEAN', np.concatenate([np.zeros(m_in, bool), np.ones(m_out, bool)])[:, None])
    region = np.zeros(n, bool)
    region[inside] = True
    _set(full, IN_WORK_ATTR, 'BOOLEAN', region[:, None])
    for mat in full.materials:
        hyb.materials.append(mat)
    hyb[FULL_KEY] = full
    hyb[FRACTION_KEY] = float(fraction)
    hyb[WORK_KEY] = [float(center[0]), float(center[1]), float(center[2]), float(radius)]
    rev = int(full.get(REV_KEY, 0)) + 1
    full[REV_KEY] = rev
    hyb[REV_KEY] = rev
    hyb[SYNCED_KEY] = rev
    full.use_fake_user = True
    obj.data = hyb
    _drop(old)
    return m_in, m_out


def sync(obj):
    """Write a work area's edits into the full model. Returns True if anything was written."""
    if not is_workarea(obj):
        return False
    hyb = obj.data
    full = hyb[FULL_KEY]
    rev = int(hyb.get(REV_KEY, 0))
    if int(hyb.get(SYNCED_KEY, -1)) == rev:
        return False
    H = SplatSet.read(hyb)
    fi = H.get(FULL_INDEX_ATTR)[:, 0].astype(np.int64)
    locked = H.locked
    there = fi >= 0
    new = ~there & ~locked
    F = SplatSet.read(full)
    in_work = F.get(IN_WORK_ATTR)
    region = np.nonzero(in_work[:, 0])[0] if in_work is not None else np.zeros(0, np.int64)
    gone = np.setdiff1d(region, fi[there])             # removed by a tool (e.g. Restore)
    for name, (dt, arr) in H.arrays.items():
        if name in (FULL_INDEX_ATTR, LOCKED_ATTR):
            continue
        if name not in F.arrays:
            F.arrays[name] = [dt, np.zeros((F.n,) + arr.shape[1:], arr.dtype)]
        F.arrays[name][1][fi[there]] = arr[there]
    if IN_WORK_ATTR not in F.arrays:
        F.arrays[IN_WORK_ATTR] = ['BOOLEAN', np.zeros((F.n, 1), bool)]
    if len(gone):
        keep = np.ones(F.n, bool)
        keep[gone] = False
        F.keep(keep, stash=False)
        fi[there] -= np.searchsorted(gone, fi[there])
    if new.any():
        start, m = F.n, int(new.sum())
        for name, entry in F.arrays.items():
            if name in H.arrays:
                add = H.arrays[name][1][new]
            elif name == IN_WORK_ATTR:
                add = np.ones((m, 1), bool)
            else:
                add = np.zeros((m,) + entry[1].shape[1:], entry[1].dtype)
            entry[1] = np.concatenate([entry[1], np.asarray(add, entry[1].dtype)], axis=0)
        F.n += m
        fi[new] = np.arange(start, start + m)
    F.write(full)
    del F
    _set(hyb, FULL_INDEX_ATTR, 'INT', fi.astype(np.int32)[:, None])
    full[REV_KEY] = int(full.get(REV_KEY, 0)) + 1
    full.update_tag()
    hyb[SYNCED_KEY] = rev
    return True
