"""Gaussian Splat Patch: erase, clone stamp and heal 3DGS point clouds in Blender 5.3+."""

import bpy

from . import ops, ui

_classes = ui.classes[:1] + ops.classes + ui.classes[1:]
_handles = []


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gsp = bpy.props.PointerProperty(type=ui.GSP_Props)
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(ops.draw_selection, (), 'WINDOW', 'POST_VIEW'))
    _handles.append(bpy.types.SpaceView3D.draw_handler_add(ops.draw_workareas, (), 'WINDOW', 'POST_VIEW'))
    bpy.app.handlers.render_pre.append(ops.sync_workareas)


def unregister():
    ops.STATE["running"] = False
    if ops.sync_workareas in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(ops.sync_workareas)
    while _handles:
        bpy.types.SpaceView3D.draw_handler_remove(_handles.pop(), 'WINDOW')
    del bpy.types.Scene.gsp
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
