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
BASE_ATTR = "radiance:base"
SH_C0 = 0.28209479177387814


def is_splat_object(obj):
    return (obj is not None and obj.type == 'POINTCLOUD'
            and obj.data.attributes.get("position") is not None)


class SplatSet:
    """All point attributes of one point cloud, as (N, components) arrays."""

    def __init__(self, n, arrays):
        self.n = n
        self.arrays = arrays  # name -> [data_type, ndarray]

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
        if SELECT_ATTR not in arrays:
            arrays[SELECT_ATTR] = ['BOOLEAN', np.zeros((n, 1), bool)]
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
    def keep(self, mask):
        for entry in self.arrays.values():
            entry[1] = entry[1][mask]
        self.n = int(np.count_nonzero(mask))

    def append_copies(self, src_idx, overrides):
        """Append copies of points src_idx; overrides maps attr name -> (M, w) array."""
        if len(src_idx) == 0:
            return
        for name, entry in self.arrays.items():
            new = overrides.get(name)
            if new is None:
                new = entry[1][src_idx]
            entry[1] = np.concatenate([entry[1], np.asarray(new, entry[1].dtype)], axis=0)
        self.n += len(src_idx)
