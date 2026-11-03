"""Low-detail stand-ins for arranging several big scans.

A proxy is a small point cloud with a fraction of the model's splats (the most visible ones
more likely: opaque and large), each grown a little so the model still reads as solid, and
without the view-dependent colour. While it's on, the object's data is the proxy; the full
model is kept (fake user) and pointed to from the proxy ("gsp_full"), so the viewport and
Blender's depsgraph only ever touch the small cloud. Full Detail swaps it back.

Everything that changes or writes the model (export, merge, bake, the stash) reads the full
data through full_data(); the brushes don't run on a proxy.
"""

import bpy
import numpy as np

from .splats import _LAYOUT, BASE_ATTR, HIDDEN_ATTR

FULL_KEY = "gsp_full"          # on the proxy: the full point cloud
PROXY_KEY = "gsp_proxy"        # on the full cloud: its proxy (shared by linked duplicates)
FRACTION_KEY = "gsp_lod_fraction"
_KEEP = ("position", "scale", "rotation", BASE_ATTR)
GROW = 0.12                    # kept splats grow by (1 / fraction) ** GROW


def is_proxy(obj):
    return obj is not None and obj.type == 'POINTCLOUD' and \
        isinstance(obj.data.get(FULL_KEY), bpy.types.PointCloud)


def full_data(obj):
    """The object's full point cloud, whether a proxy is showing or not."""
    if is_proxy(obj):
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


def make_proxy(obj, fraction=0.1):
    """Show a low-detail stand-in in place of the object's model. Returns its splat count."""
    full = full_data(obj)
    if is_proxy(obj):
        if abs(float(obj.data.get(FRACTION_KEY, 0)) - fraction) < 1e-6:
            return len(obj.data.points)
        restore(obj)
    old = full.get(PROXY_KEY)
    if isinstance(old, bpy.types.PointCloud) and abs(float(old.get(FRACTION_KEY, 0)) - fraction) < 1e-6:
        proxy = old                                    # a linked duplicate made it already
    else:
        cols = {name: _read(full, name) for name in _KEEP}
        _, base = cols[BASE_ATTR]
        _, scale = cols["scale"]
        n = len(full.points)
        visible = np.ones(n, bool)
        _, hidden = _read(full, HIDDEN_ATTR)
        if hidden is not None:
            visible &= ~hidden[:, 0].astype(bool)
        vis_idx = np.nonzero(visible)[0]
        op = base[vis_idx, 3] if base is not None else np.ones(len(vis_idx), np.float32)
        chosen, p = choose(op, None if scale is None else scale[vis_idx], fraction)
        pick = vis_idx[chosen]
        proxy = bpy.data.pointclouds.new(full.name + "_lod")
        proxy.resize(len(pick))
        # Fewer splats each cover more: grow them so a surface stays closed (between the
        # area a surface needs, 1/f, and the volume fur needs, 1/f per axis cubed).
        grow = np.float32((len(vis_idx) / max(len(pick), 1)) ** GROW)
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
            attr = proxy.attributes.get(name) or proxy.attributes.new(name, dt, 'POINT')
            attr.data.foreach_set(_LAYOUT[dt][1], np.ascontiguousarray(a).ravel())
        for m in full.materials:
            proxy.materials.append(m)
        proxy[FULL_KEY] = full
        proxy[FRACTION_KEY] = float(fraction)
        full[PROXY_KEY] = proxy
    full.use_fake_user = True                          # kept while nothing shows it
    obj.data = proxy
    return len(proxy.points)


def restore(obj):
    """Bring back the full model. Returns its splat count."""
    if not is_proxy(obj):
        return len(obj.data.points)
    proxy = obj.data
    full = proxy[FULL_KEY]
    obj.data = full
    full.use_fake_user = False
    # Remove it unless a linked duplicate still shows it (the full cloud's pointer to it
    # counts as a user too, so look at the objects themselves).
    if not any(o.data == proxy for o in bpy.data.objects):
        if full.get(PROXY_KEY) == proxy:
            del full[PROXY_KEY]
        bpy.data.pointclouds.remove(proxy)
    else:
        full.use_fake_user = True                      # still behind another object's proxy
    return len(full.points)
