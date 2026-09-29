"""Gaussian Splat Patch: erase, clone stamp and heal 3DGS point clouds in Blender 5.3+."""

import bpy

from . import ops, ui

_classes = ui.classes[:1] + ops.classes + ui.classes[1:]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gsp = bpy.props.PointerProperty(type=ui.GSP_Props)


def unregister():
    ops.STATE["running"] = False
    del bpy.types.Scene.gsp
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
