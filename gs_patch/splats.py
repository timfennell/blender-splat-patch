"""Bulk access to a Blender 5.3 Gaussian splat point cloud as NumPy arrays.

Blender's PLY importer stores a splat as a POINTCLOUD with these point attributes:
  position        FLOAT_VECTOR  centre (object space)
  scale           FLOAT_VECTOR  linear scale (exp of the PLY log scale)
  rotation        QUATERNION    w, x, y, z
  radiance:base   FLOAT4        f_dc_0..2 (SH DC coefficients), opacity (0..1, sigmoid applied)
  radiance:sh_k   FLOAT_VECTOR  higher SH coefficient k for r, g, b

Every point attribute is carried through edits, so anything else on the object survives too.
"""

import numpy as np

# data_type -> (components, foreach key, numpy dtype)
_LAYOUT = {
    'FLOAT': (1, 'value', np.float32),
    'INT': (1, 'value', np.int32),
    'INT8': (1, 'value', np.int8),
    'BOOLEAN': (1, 'value', bool),
    'FLOAT2': (2, 'vector', np.float32),
    'INT32_2D': (2, 'value', np.int32),
    'FLOAT_VECTOR': (3, 'vector', np.float32),
    'FLOAT4': (4, 'vector', np.float32),
    'QUATERNION': (4, 'value', np.float32),
    'FLOAT_COLOR': (4, 'color', np.float32),
    'BYTE_COLOR': (4, 'color', np.float32),
    'FLOAT4X4': (16, 'value', np.float32),
}

SELECT_ATTR = "gsp_selected"
ADDED_ATTR = "gsp_added"            # splat was created by an edit (clone, heal, fill, bridge)
FADED_ATTR = "gsp_faded"            # splat's opacity was reduced by a feathered edit
ORIG_OPACITY_ATTR = "gsp_orig_opacity"
BASE_ATTR = "radiance:base"

# Bookkeeping attributes every splat set carries, created on first read.
_TRACKING = {SELECT_ATTR: 'BOOLEAN', ADDED_ATTR: 'BOOLEAN', FADED_ATTR: 'BOOLEAN',
             ORIG_OPACITY_ATTR: 'FLOAT'}
SH_C0 = 0.28209479177387814


def is_splat_object(obj):
    return (obj is not None and obj.type == 'POINTCLOUD'
            and obj.data.attributes.get("position") is not None)


class SplatSet:
    """All point attributes of one point cloud, as (N, components) arrays."""

    def __init__(self, n, arrays):
        self.n = n
        self.arrays = arrays  # name -> [data_type, ndarray]
        # Original splats removed since the last save; they go to the stash so the
        # restore brush can bring them back. Each entry is {name: [data_type, rows]}.
        self.removed = []

    # ------------------------------------------------------------------ io
    @classmethod
    def read(cls, pc):
        n = len(pc.points)
        arrays = {}
        for attr in pc.attributes:
            if attr.domain != 'POINT' or attr.name.startswith('.'):
                continue
            layout = _LAYOUT.get(attr.data_type)
            if layout is None:
                continue
            width, key, dtype = layout
            buf = np.empty(n * width, dtype)
            if n:
                attr.data.foreach_get(key, buf)
            arrays[attr.name] = [attr.data_type, buf.reshape(n, width)]
        for name, data_type in _TRACKING.items():
            if name not in arrays:
                width, _, dtype = _LAYOUT[data_type]
                arrays[name] = [data_type, np.zeros((n, width), dtype)]
        return cls(n, arrays)

    def write(self, pc):
        if len(pc.points) != self.n:
            pc.resize(self.n)
        for name, (data_type, arr) in self.arrays.items():
            attr = pc.attributes.get(name)
            if attr is None:
                attr = pc.attributes.new(name, data_type, 'POINT')
            width, key, dtype = _LAYOUT[data_type]
            if self.n:
                attr.data.foreach_set(key, np.ascontiguousarray(arr, dtype).ravel())
        pc.update_tag()

    # ------------------------------------------------------------ accessors
    def get(self, name):
        entry = self.arrays.get(name)
        return None if entry is None else entry[1]

    @property
    def pos(self):
        return self.arrays["position"][1]

    @property
    def base(self):
        return self.get(BASE_ATTR)

    @property
    def opacity(self):
        base = self.base
        if base is None:
            return np.ones(self.n, np.float32)
        return base[:, 3]

    @property
    def dc(self):
        base = self.base
        return None if base is None else base[:, :3]

    @property
    def selected(self):
        return self.arrays[SELECT_ATTR][1][:, 0]

    @property
    def added(self):
        return self.arrays[ADDED_ATTR][1][:, 0]

    @property
    def faded(self):
        return self.arrays[FADED_ATTR][1][:, 0]

    @property
    def orig_opacity(self):
        return self.arrays[ORIG_OPACITY_ATTR][1][:, 0]

    def sh_names(self):
        names = [k for k in self.arrays if k.startswith("radiance:sh_")]
        return sorted(names, key=lambda k: int(k.rsplit('_', 1)[1]))

    def rgb(self, idx=None):
        """Approximate display colour (view-independent DC term)."""
        dc = self.dc
        if dc is None:
            n = self.n if idx is None else len(idx)
            return np.full((n, 3), 0.8, np.float32)
        if idx is not None:
            dc = dc[idx]
        return np.clip(0.5 + SH_C0 * dc, 0.0, 1.0)

    # ------------------------------------------------------------- editing
    def keep(self, mask, stash=True):
        """Keep only mask. Removed original splats are remembered for the stash unless stash=False."""
        mask = np.asarray(mask, bool)
        if stash:
            gone = ~mask & ~self.added
            if gone.any():
                rows = {name: [dt, arr[gone].copy()] for name, (dt, arr) in self.arrays.items()}
                # Stash them as they were before any fade, so restoring is exact.
                was_faded = rows[FADED_ATTR][1][:, 0]
                if BASE_ATTR in rows and was_faded.any():
                    rows[BASE_ATTR][1][was_faded, 3] = rows[ORIG_OPACITY_ATTR][1][was_faded, 0]
                rows[FADED_ATTR][1][:] = False
                rows[SELECT_ATTR][1][:] = False
                self.removed.append(rows)
        for entry in self.arrays.values():
            entry[1] = entry[1][mask]
        self.n = int(np.count_nonzero(mask))

    def fade(self, factor):
        """Multiply opacity by factor (N,), remembering each splat's opacity before its first fade."""
        base = self.base
        if base is None:
            return
        changed = factor < 1.0
        first = changed & ~self.faded & ~self.added
        self.orig_opacity[first] = base[first, 3]
        self.faded[first] = True
        base[:, 3] *= factor

    def append_copies(self, src_idx, overrides):
        """Append copies of points src_idx; overrides maps attr name -> (M, w) array.

        New splats are flagged as added by an edit, so the restore brush can remove them.
        """
        if len(src_idx) == 0:
            return
        m = len(src_idx)
        defaults = {ADDED_ATTR: np.ones((m, 1), bool), FADED_ATTR: np.zeros((m, 1), bool),
                    ORIG_OPACITY_ATTR: np.zeros((m, 1), np.float32), SELECT_ATTR: np.zeros((m, 1), bool)}
        for name, entry in self.arrays.items():
            new = overrides.get(name)
            if new is None:
                new = defaults.get(name)
            if new is None:
                new = entry[1][src_idx]
            entry[1] = np.concatenate([entry[1], np.asarray(new, entry[1].dtype)], axis=0)
        self.n += m

    def append_rows(self, rows):
        """Append whole rows from another splat set ({name: [data_type, array]}), as originals."""
        m = len(rows["position"][1]) if "position" in rows else 0
        if m == 0:
            return
        for name, entry in self.arrays.items():
            if name in rows:
                new = rows[name][1]
            else:
                new = np.zeros((m,) + entry[1].shape[1:], entry[1].dtype)
            if name in (ADDED_ATTR, FADED_ATTR, SELECT_ATTR):
                new = np.zeros((m, 1), bool)
            entry[1] = np.concatenate([entry[1], np.asarray(new, entry[1].dtype)], axis=0)
        self.n += m

    def rows(self, mask):
        return {name: [dt, arr[mask].copy()] for name, (dt, arr) in self.arrays.items()}


def concat_rows(parts):
    """Stack a list of {name: [data_type, array]} into one, filling missing attributes with zeros."""
    parts = [p for p in parts if p and len(p["position"][1])]
    if not parts:
        return None
    names = {}
    for p in parts:
        for name, (dt, arr) in p.items():
            names.setdefault(name, (dt, arr.shape[1:], arr.dtype))
    out = {}
    for name, (dt, shape, dtype) in names.items():
        chunks = []
        for p in parts:
            m = len(p["position"][1])
            chunks.append(p[name][1] if name in p else np.zeros((m,) + shape, dtype))
        out[name] = [dt, np.concatenate(chunks, axis=0)]
    return out


def set_from_rows(rows):
    n = len(rows["position"][1]) if rows else 0
    return SplatSet(n, {k: [dt, arr] for k, (dt, arr) in (rows or {}).items()})


def major_axes(S, sigmas=2.0):
    """Half-length vector of each splat's longest axis (object space), sigmas deep.

    Needle-shaped splats draw far from their centre, so hit tests use the segment
    centre +/- this vector rather than the centre alone.
    """
    scale = S.get("scale")
    rot = S.get("rotation")
    if scale is None:
        return np.zeros((S.n, 3), np.float32)
    k = np.argmax(scale, axis=1)
    length = scale[np.arange(S.n), k] * sigmas
    if rot is None:
        axes = np.zeros((S.n, 3), np.float32)
        axes[np.arange(S.n), k] = 1.0
        return axes * length[:, None]
    q = rot / np.maximum(np.linalg.norm(rot, axis=1, keepdims=True), 1e-12)
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    cols = (
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)], 1),
        np.stack([2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)], 1),
        np.stack([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)], 1),
    )
    axes = np.where((k == 0)[:, None], cols[0], np.where((k == 1)[:, None], cols[1], cols[2]))
    return (axes * length[:, None]).astype(np.float32)
