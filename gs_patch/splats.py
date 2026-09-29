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
HIDDEN_ATTR = "gsp_hidden"          # splat was erased: kept (opacity 0) so it can be restored
ORIG_OPACITY_ATTR = "gsp_orig_opacity"
BASE_ATTR = "radiance:base"

# Bookkeeping attributes every splat set carries, created on first read.
_TRACKING = {SELECT_ATTR: 'BOOLEAN', ADDED_ATTR: 'BOOLEAN', FADED_ATTR: 'BOOLEAN',
             HIDDEN_ATTR: 'BOOLEAN', ORIG_OPACITY_ATTR: 'FLOAT'}
# Columns an opacity-only edit (erase, fade, restore) changes.
OPACITY_COLUMNS = (BASE_ATTR, HIDDEN_ATTR, FADED_ATTR, ORIG_OPACITY_ATTR)
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
        self._grid = None

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

    def write(self, pc, only=None):
        """Write back to the point cloud. `only` limits it to those columns when the count is unchanged."""
        if len(pc.points) != self.n:
            pc.resize(self.n)
            only = None
        for name, (data_type, arr) in self.arrays.items():
            if only is not None and name not in only and pc.attributes.get(name) is not None:
                continue
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

    @property
    def hidden(self):
        return self.arrays[HIDDEN_ATTR][1][:, 0]

    @property
    def visible(self):
        return ~self.hidden

    # --------------------------------------------------------- spatial grid
    def near(self, center, radius):
        """Indices of splats that may lie within radius of center (a superset; filter by distance)."""
        g = self._grid
        if g is None or g.n > self.n or self.n - g.n > max(100000, self.n // 10):
            g = self._grid = _Grid(self.pos, self)
        cand = g.query(center, radius)
        if self.n > g.n:   # splats added since the grid was built
            extra = np.arange(g.n, self.n)
            cand = np.concatenate([cand, extra[np.linalg.norm(self.pos[extra] - center, axis=1) < radius]])
        return cand

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
    def hide(self, mask):
        """Erase splats without removing them: opacity 0, flagged, original opacity kept."""
        mask = np.asarray(mask, bool) & ~self.hidden
        if not mask.any():
            return 0
        base = self.base
        remember = mask & ~self.faded
        if base is not None:
            self.orig_opacity[remember] = base[remember, 3]
            base[mask, 3] = 0.0
        self.hidden[mask] = True
        self.faded[mask] = False
        self.selected[mask] = False
        return int(mask.sum())

    def unhide(self, mask):
        """Bring back erased or faded splats at their original opacity."""
        mask = np.asarray(mask, bool) & (self.hidden | self.faded)
        base = self.base
        if base is not None:
            base[mask, 3] = self.orig_opacity[mask]
        self.hidden[mask] = False
        self.faded[mask] = False
        return int(mask.sum())

    def visible_subset(self):
        """A copy with the erased splats dropped: what export writes."""
        vis = self.visible
        return SplatSet(int(vis.sum()), {k: [dt, a[vis]] for k, (dt, a) in self.arrays.items()})

    def keep(self, mask, stash=True):
        """Keep only mask. Removed original splats are remembered for the stash unless stash=False."""
        mask = np.asarray(mask, bool)
        self._grid = None
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
        factor = np.where(self.hidden, 1.0, factor)
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
                    HIDDEN_ATTR: np.zeros((m, 1), bool),
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
            if name in (ADDED_ATTR, FADED_ATTR, SELECT_ATTR, HIDDEN_ATTR):
                new = np.zeros((m, 1), bool)
            entry[1] = np.concatenate([entry[1], np.asarray(new, entry[1].dtype)], axis=0)
        self.n += m

    def rows(self, mask):
        return {name: [dt, arr[mask].copy()] for name, (dt, arr) in self.arrays.items()}


class _Grid:
    """Uniform voxel grid over splat centres, for fast local queries on large scans."""

    def __init__(self, pos, S):
        self.n = len(pos)
        scale = S.get("scale")
        cell = 2.0 * float(np.median(scale.max(1))) if scale is not None and len(pos) else 0.0
        extent = float(np.max(pos.max(0) - pos.min(0))) if len(pos) else 1.0
        self.cell = max(cell, extent / 1024.0, 1e-9)
        self.lo = pos.min(0) if len(pos) else np.zeros(3, np.float32)
        ijk = np.floor((pos - self.lo) / self.cell).astype(np.int64)
        self.dims = ijk.max(0) + 1 if len(pos) else np.ones(3, np.int64)
        key = (ijk[:, 0] * self.dims[1] + ijk[:, 1]) * self.dims[2] + ijk[:, 2]
        self.order = np.argsort(key, kind='stable')
        sk = key[self.order]
        self.keys, self.starts = np.unique(sk, return_index=True)
        self.ends = np.append(self.starts[1:], len(sk))

    def query(self, center, radius):
        if self.n == 0:
            return np.zeros(0, np.int64)
        a = np.clip(np.floor((center - radius - self.lo) / self.cell).astype(np.int64), 0, self.dims - 1)
        b = np.clip(np.floor((center + radius - self.lo) / self.cell).astype(np.int64), 0, self.dims - 1)
        span = b - a + 1
        if np.prod(span) > 3_000_000:        # huge brush: scanning everything is cheaper
            return np.arange(self.n)
        ii, jj, kk = np.meshgrid(np.arange(a[0], b[0] + 1), np.arange(a[1], b[1] + 1),
                                 np.arange(a[2], b[2] + 1), indexing='ij')
        want = ((ii * self.dims[1] + jj) * self.dims[2] + kk).ravel()
        at = np.clip(np.searchsorted(self.keys, want), 0, len(self.keys) - 1)
        found = at[self.keys[at] == want]
        s, e = self.starts[found], self.ends[found]
        lengths = e - s
        if lengths.sum() == 0:
            return np.zeros(0, np.int64)
        offs = np.repeat(s - np.concatenate([[0], np.cumsum(lengths)[:-1]]), lengths)
        return self.order[np.arange(lengths.sum()) + offs]


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
