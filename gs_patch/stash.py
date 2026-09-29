"""Erased original splats, kept so the restore brush can bring them back.

The stash is a separate PointCloud datablock referenced from the splat object's data
(custom property "gsp_stash"). It is never linked to an object, so it doesn't render
or export, but it is saved in the .blend and follows undo with the scan.
"""

import bpy
import numpy as np

from .splats import SplatSet, concat_rows, set_from_rows

STASH_KEY = "gsp_stash"


def get(obj, create=False):
    pc = obj.data.get(STASH_KEY)
    if pc is not None and isinstance(pc, bpy.types.PointCloud):
        return pc
    if not create:
        return None
    pc = bpy.data.pointclouds.new(obj.data.name + "_gsp_stash")
    pc.resize(0)
    pc.use_fake_user = True
    obj.data[STASH_KEY] = pc
    return pc


def read(obj):
    """The stash as a SplatSet (empty set if there is none)."""
    pc = get(obj)
    if pc is None or len(pc.points) == 0:
        return None
    return SplatSet.read(pc)


def write(obj, T):
    pc = get(obj, create=T is not None and T.n > 0)
    if pc is None:
        return
    if T is None or T.n == 0:
        pc.resize(0)
        return
    T.write(pc)


def push(obj, S):
    """Move the splats S removed since the last save into the stash."""
    if not S.removed:
        return 0
    added = sum(len(r["position"][1]) for r in S.removed)
    T = read(obj)
    parts = ([] if T is None else [T.arrays]) + S.removed
    S.removed = []
    write(obj, set_from_rows(concat_rows(parts)))
    return added


def clear(obj):
    pc = get(obj)
    if pc is not None:
        pc.resize(0)
