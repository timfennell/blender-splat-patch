"""Drives the brush in a real Blender window with simulated input.

Run:  blender --enable-event-simulate --python tests/test_ui.py -- <scan.ply>
"""
import os, sys, tempfile, traceback
import bpy
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import addon_utils
addon_utils.disable("bl_ext.user_default.gs_patch")
import gs_patch
gs_patch.register()
from gs_patch.splats import SplatSet
from gs_patch.ops import STATE

SCAN = sys.argv[-1]
FAILS = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}", flush=True)
    if not ok:
        FAILS.append(label)


def steps():
    win = bpy.context.window_manager.windows[0]
    win.event_simulate(type='LEFTMOUSE', value='PRESS', x=40, y=300)
    win.event_simulate(type='LEFTMOUSE', value='RELEASE', x=40, y=300)
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.wm.ply_import(filepath=SCAN)
    obj = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = obj
    area = max((a for a in win.screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
    region = [r for r in area.regions if r.type == 'WINDOW'][0]
    sp = area.spaces.active
    sp.shading.type = 'SOLID'; sp.shading.light = 'STUDIO'
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.view3d.view_selected()
        sp.region_3d.view_distance *= 0.8
    yield 0.8
    cx, cy = region.x + region.width // 2, region.y + region.height // 2
    g = bpy.context.scene.gsp

    def ev(t, v, x, y, **k):
        win.event_simulate(type=t, value=v, x=int(x), y=int(y), **k)

    def key(k, **kw):
        ev(k, 'PRESS', cx, cy, **kw); ev(k, 'RELEASE', cx, cy, **kw)

    def start(tool):
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.gsp.brush('INVOKE_DEFAULT', tool=tool)

    def stroke(x0, y0, x1, y1, n=20):
        ev('MOUSEMOVE', 'NOTHING', x0, y0); yield 0.03
        ev('LEFTMOUSE', 'PRESS', x0, y0)
        for i in range(1, n + 1):
            ev('MOUSEMOVE', 'NOTHING', x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n); yield 0.02
        ev('LEFTMOUSE', 'RELEASE', x1, y1); yield 0.4

    # Brush start: flat lighting, letter keys, F resize
    g.radius_px = 40; g.feather = 0.35; g.depth_mode = 'THROUGH'
    start('SELECT'); yield 0.3
    check("brush start sets flat lighting", sp.shading.light == 'FLAT')
    seen = []
    for k in ('C', 'J', 'B', 'R', 'H', 'E', 'S'):
        key(k); yield 0.03; seen.append(g.tool)
    check("tool keys", seen == ['CLONE', 'SPOT', 'BRIDGE', 'RESTORE', 'HEAL', 'ERASE', 'SELECT'], str(seen))
    ev('MOUSEMOVE', 'NOTHING', cx, cy); yield 0.05
    key('F'); yield 0.03
    ev('MOUSEMOVE', 'NOTHING', cx + 30, cy); yield 0.05
    ev('LEFTMOUSE', 'PRESS', cx + 30, cy); ev('LEFTMOUSE', 'RELEASE', cx + 30, cy); yield 0.1
    check("F resize", g.radius_px == 70, f"{g.radius_px}px")

    # A stroke that starts over empty viewport (crashed before 0.1.12)
    yield from stroke(cx + 250, region.y + 60, cx, cy)
    sel = int(SplatSet.read(obj.data).selected.sum())
    check("select stroke starting over empty space", sel > 0 and STATE["running"], f"{sel} selected")
    key('DEL'); yield 0.4
    S = SplatSet.read(obj.data)
    check("Delete erases the selection", int(S.hidden.sum()) == sel and not S.selected.any(),
          f"{int(S.hidden.sum())} erased")

    # Restore brings them back
    key('R'); yield 0.05
    g.radius_px = 120
    yield from stroke(cx + 250, region.y + 60, cx, cy)
    S = SplatSet.read(obj.data)
    check("Restore brings erased splats back", int(S.hidden.sum()) < sel, f"{int(S.hidden.sum())} still erased")

    # Export while the brush is running: stops the brush, writes the visible splats
    path = os.path.join(tempfile.mkdtemp(), "out.ply")
    with bpy.context.temp_override(window=win, area=area, region=region):
        bpy.ops.gsp.export_ply(filepath=path)
    yield 0.5
    S = SplatSet.read(obj.data)
    head = open(path, "rb").read(200).decode("ascii", "replace")
    n = int(head.split("element vertex ")[1].split()[0])
    check("export from inside the brush", n == int(S.visible.sum()) and not STATE["running"],
          f"{n} splats, brush running={STATE['running']}")
    print("ALL PASSED" if not FAILS else f"FAILED: {FAILS}", flush=True)


gen = None


def tick():
    global gen
    try:
        if gen is None:
            gen = steps()
        return next(gen)
    except StopIteration:
        bpy.ops.wm.quit_blender(); return None
    except Exception:
        print("FAIL  exception", traceback.format_exc(), flush=True)
        bpy.ops.wm.quit_blender(); return None


bpy.app.timers.register(tick, first_interval=2.0)
