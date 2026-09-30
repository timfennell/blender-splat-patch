import time

import bpy
import numpy as np
from bpy_extras.io_utils import ExportHelper
from mathutils import Vector

from . import core, view, stash
from .splats import SplatSet, is_splat_object, major_axes, SELECT_ATTR, HIDDEN_ATTR, OPACITY_COLUMNS
from .ply_io import write_splat_ply

TOOL_ITEMS = [
    ('ERASE', "Erase", "Paint to delete splats (dust, hairs, pins)", 'TRASH', 0),
    ('SELECT', "Select", "Paint a region to erase or fill later (hold Option/Alt to deselect)", 'RESTRICT_SELECT_OFF', 1),
    ('CLONE', "Clone", "Clone stamp: Ctrl+click sets the sample point, then paint", 'DUPLICATE', 2),
    ('HEAL', "Heal", "Healing brush: clone, then match the colour of the destination", 'MOD_SMOOTH', 3),
    ('SPOT', "Spot Heal", "Paint over a blemish; it is regrown from its surroundings", 'SHADERFX', 4),
    ('BRIDGE', "Bridge", "Paint over an empty hole to continue the surface across it", 'MOD_SOLIDIFY', 5),
    ('RESTORE', "Restore", "Paint to undo edits locally: bring back erased splats and/or remove added ones",
     'RECOVER_LAST', 6),
]

_REV_KEY = "gsp_revision"

# Whether a brush modal is running (module state, so a crash never leaves a stale flag in the file).
STATE = {"running": False}

PAINT_TOOLS = {'ERASE', 'SELECT', 'SPOT'}

# Tool keys while the brush runs: numbers in panel order, plus Photoshop-style letters.
TOOL_KEYS = {'ONE': 'ERASE', 'TWO': 'SELECT', 'THREE': 'CLONE', 'FOUR': 'HEAL', 'FIVE': 'SPOT',
             'SIX': 'BRIDGE', 'SEVEN': 'RESTORE',
             'E': 'ERASE', 'S': 'SELECT', 'C': 'CLONE', 'H': 'HEAL', 'J': 'SPOT', 'B': 'BRIDGE',
             'R': 'RESTORE'}

SHORTCUTS = [
    ("E  S  C  H  J  B  R", "Erase, Select, Clone, Heal, Spot Heal, Bridge, Restore (or 1-7)"),
    ("F", "Resize the brush: move, then click or F to set"),
    ("Shift F", "Set the feather the same way"),
    ("[   ]", "Smaller / larger brush (Shift: feather)"),
    ("Delete", "Erase the selected splats"),
    ("X", "Surface / Through depth"),
    ("Option / Alt", "Select: hold while painting to deselect (Ctrl works too)"),
    ("Ctrl click", "Clone/Heal: set the sample point"),
    ("Esc  Enter", "Stop the brush"),
]
# Tools that can start and continue a stroke with nothing solid under the mouse.
FREE_TOOLS = PAINT_TOOLS | {'BRIDGE', 'RESTORE'}

HELP = {
    'RESTORE': "LMB restore · green = added by edits, red = erased, yellow = faded",
    'BRIDGE': "LMB paint over the hole, a little onto the intact surface around it",
    'ERASE': "LMB paint erase",
    'SELECT': "LMB select · hold Option/Alt (or Ctrl) to deselect",
    'CLONE': "Ctrl+LMB set sample · LMB clone",
    'HEAL': "Ctrl+LMB set sample · LMB heal",
    'SPOT': "LMB paint over the blemish; a matching nearby patch is cloned over it",
}


def props_of(context):
    return context.scene.gsp


def bump_revision(obj):
    rev = int(obj.data.get(_REV_KEY, 0)) + 1
    obj.data[_REV_KEY] = rev
    return rev


def save(obj, S, only=None):
    """Write splats back (only the named columns if given); legacy removals go to the stash."""
    S.write(obj.data, only)
    stash.push(obj, S)


def commit(context, obj, S, message, only=None):
    """Write a brush stroke back. No undo step: the Restore brush is the undo, and one
    step is recorded when the brush ends so Ctrl+Z can't silently drop the session."""
    save(obj, S, only)
    rev = bump_revision(obj)
    obj.data.update_tag()
    for area in context.screen.areas if context.screen else ():
        if area.type == 'VIEW_3D':
            area.tag_redraw()
    return rev


def active_splats(context):
    obj = context.active_object
    return obj if is_splat_object(obj) else None


def _in_ui_region(area, x, y):
    for r in area.regions:
        if r.type in {'UI', 'TOOLS', 'HEADER', 'TOOL_HEADER', 'ASSET_SHELF', 'NAVIGATION_BAR'} \
                and r.x <= x < r.x + r.width and r.y <= y < r.y + r.height and r.width > 1:
            return True
    return False


# =====================================================================
class GSP_OT_brush(bpy.types.Operator):
    """Paint on the splats with the active tool. Esc or Enter to finish"""
    bl_idname = "gsp.brush"
    bl_label = "Splat Brush"
    bl_options = {'REGISTER'}

    tool: bpy.props.EnumProperty(items=TOOL_ITEMS + [('KEEP', "Current", "")], default='KEEP')

    _handle = None

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type == 'VIEW_3D' and active_splats(context)

    # ---------------------------------------------------------- setup
    def invoke(self, context, event):
        p = props_of(context)
        if self.tool != 'KEEP':
            p.tool = self.tool
        if STATE["running"]:
            return {'CANCELLED'}  # the running brush picks up the new tool
        self.obj = context.active_object
        self.area = context.area
        self.region = next(r for r in context.area.regions if r.type == 'WINDOW')
        self.rv3d = context.area.spaces.active.region_3d
        self.load()
        self.proj = view.Projection()
        self.overlay = view.Overlay()
        self.stroke = None
        self.hit = None
        self.hit_normal = None
        # Set by update_hit once the mouse is over a surface; defaults so a stroke that
        # starts over empty space or in Through mode still works.
        self.radius = 0.0
        self.view_dir = np.array([0.0, 0.0, -1.0], np.float32)
        self.last_dab = None
        self.stroke_ctrl = False
        self.mouse = (0, 0)
        self.offset = None          # clone: destination - source, in object space
        self.stroke_src_start = None
        self.last_draw = 0.0
        # The orange object outline would hide the selection overlay; restore it on exit.
        self.overlay_settings = context.space_data.overlay
        self.had_outline = self.overlay_settings.show_outline_selected
        self.overlay_settings.show_outline_selected = False
        # Splats look dark under Solid mode's studio lighting; show their real colours.
        shading = context.space_data.shading
        if shading.type == 'SOLID':
            shading.light = 'FLAT'
        self.resizing = None
        self.shown_tool = None
        self._handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_overlay, (), 'WINDOW', 'POST_VIEW')
        self._handle_px = bpy.types.SpaceView3D.draw_handler_add(self.draw_overlay_px, (), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        STATE["running"] = True
        self.edited = False
        self.status(context)
        return {'RUNNING_MODAL'}

    def load(self):
        self.S = core_S = SplatSet.read(self.obj.data)
        self.axes = major_axes(core_S)
        self.update_reach()
        if core_S.n:
            core_S.near(core_S.pos[0], 0.0)     # build the spatial grid now, not on the first hover
        self.T = stash.read(self.obj)       # erased originals, for the restore brush
        self.rev = int(self.obj.data.get(_REV_KEY, 0))
        self.ptr = self.obj.data.as_pointer()
        return core_S

    def update_reach(self):
        """Very long splats are few; they're always tested, everything else via the grid."""
        reach = np.linalg.norm(self.axes, axis=1)
        self.reach_cap = float(np.percentile(reach, 99)) if len(reach) else 0.0
        self.long_idx = np.nonzero(reach > self.reach_cap)[0]

    def refresh_axes(self):
        """Axes for splats added by the last stroke (rows are only ever appended during a session)."""
        n_old = len(self.axes)
        if self.S.n > n_old:
            tail = SplatSet(self.S.n - n_old, {k: [dt, a[n_old:]] for k, (dt, a) in self.S.arrays.items()})
            self.axes = np.concatenate([self.axes, major_axes(tail)])
            reach = np.linalg.norm(self.axes[n_old:], axis=1)
            self.long_idx = np.concatenate([self.long_idx, n_old + np.nonzero(reach > self.reach_cap)[0]])
        elif self.S.n < n_old:
            self.axes = major_axes(self.S)
            self.update_reach()

    def status(self, context):
        p = props_of(context)
        context.workspace.status_text_set(
            f"Splat {p.tool.title()}:  {HELP[p.tool]} · E S C H J B R tools · F size · Shift+F feather · "
            f"Del erase selection · X surface/through · Esc done")

    def finish(self, context):
        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        if self._handle_px:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle_px, 'WINDOW')
            self._handle_px = None
        STATE["running"] = False
        if getattr(self, "edited", False):
            bpy.ops.ed.undo_push(message="Splat brush")
        try:
            self.overlay_settings.show_outline_selected = self.had_outline
        except ReferenceError:
            pass
        context.workspace.status_text_set(None)
        self.area.tag_redraw()

    # ---------------------------------------------------------- helpers
    def stale(self):
        d = self.obj.data
        return (d.as_pointer() != self.ptr or int(d.get(_REV_KEY, 0)) != self.rev
                or len(d.points) != self.S.n)

    def mouse_local(self, event):
        return event.mouse_x - self.region.x, event.mouse_y - self.region.y

    def update_hit(self, context, mx, my):
        p = props_of(context)
        mw = self.obj.matrix_world
        self.proj.update(self.region, self.rv3d, self.S, mw, self.rev, self.axes)
        hit = self.proj.pick(self.S, mx, my, p.radius_px)
        if hit is None:
            self.hit = None
            return
        self.view_dir = view.view_vector_local(self.region, self.rv3d, mx, my, mw)
        self.radius = view.pixel_radius_to_local(self.region, self.rv3d, mx, my, p.radius_px, hit, mw)
        self.hit = hit
        # Normal estimates are cheap enough to track live on moderate clouds.
        self.hit_anchor, self.hit_normal = core.surface_frame(self.S, hit, self.radius, self.view_dir)

    def source_point(self, context):
        p = props_of(context)
        if not p.source_set:
            return None
        return np.array(self.obj.matrix_world.inverted() @ Vector(p.source_co), np.float32)

    # ---------------------------------------------------------- dabs
    def dab(self, context, mx, my, ctrl):
        p = props_of(context)
        st = self.stroke
        S = self.S
        R = self.radius
        feather = p.feather
        if p.tool in {'BRIDGE', 'SPOT'}:
            self.bridge_centres.append((mx, my))
            if p.tool == 'SPOT' and self.hit is not None:
                self.spot_dabs.append((self.hit_anchor.copy(), self.hit_normal.copy(), self.radius,
                                       self.view_dir.copy()))
        elif p.tool == 'RESTORE':
            self.restore_dab(context, mx, my)
        elif p.tool in PAINT_TOOLS:
            # Through mode, or nothing solid under the mouse (wisps in empty space):
            # take everything under the brush circle at any depth.
            if p.depth_mode == 'THROUGH' or self.hit is None:
                idx, dpx = self.proj.disc(mx, my, p.radius_px, extents=True)
                w = core.feather_weight(dpx, p.radius_px, feather)
            else:
                idx, w = core.sphere_hits_S(S, self.hit, self.radius, feather, self.axes,
                                            self.reach_cap, self.long_idx)
            # Already-erased splats are hidden in place; leave them out, so repainting
            # a cleaned area doesn't mark (and preview) them again.
            live = S.visible[idx]
            idx, w = idx[live], w[live]
            if p.tool == 'ERASE':
                st.erase(idx, w, p.strength)
            elif p.tool == 'SELECT':
                hit = idx[w > 0.5] if feather > 0 else idx
                # Hold Option/Alt (or Ctrl) to deselect; checked every dab, so it can change mid-stroke.
                st.mark_deselect(hit) if ctrl else st.mark_select(hit)
            else:
                st.mark_hole(idx[w > 0.5] if feather > 0 else idx)
        else:
            src = self.hit - self.offset
            cs, ns = core.surface_frame(S, src, R, self.view_dir)
            # Snap onto the surface under the offset source point.
            cd, nd = self.hit_anchor, self.hit_normal
            st.clone(cs, ns, cd, nd, R, feather, replace=p.replace,
                     heal=p.heal_strength if p.tool == 'HEAL' else 0.0)
            self.src_ring = (cs, ns)
        self.last_dab = (mx, my)

    # ---------------------------------------------------------- modal
    def modal(self, context, event):
        p = props_of(context)
        active = context.active_object
        if not STATE["running"] or active is None \
                or active.as_pointer() != self.obj.as_pointer() or not is_splat_object(self.obj):
            self.finish(context)
            return {'CANCELLED'}
        if self.stroke is None and self.stale():
            self.load()
        if p.tool != self.shown_tool:
            self.shown_tool = p.tool
            self.status(context)
        self.area.tag_redraw()

        # Esc/Enter stop the brush wherever the mouse is (e.g. over the sidebar).
        if event.type in {'ESC', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS' and self.resizing is None:
            if self.stroke is not None:
                self.end_stroke(context)
            self.finish(context)
            return {'FINISHED'}

        x, y = event.mouse_x, event.mouse_y
        inside = (self.region.x <= x < self.region.x + self.region.width
                  and self.region.y <= y < self.region.y + self.region.height)
        if self.stroke is None and (not inside or _in_ui_region(self.area, x, y)):
            self.hit = None
            return {'PASS_THROUGH'}

        mx, my = self.mouse_local(event)
        self.mouse = (mx, my)

        if self.resizing is not None:
            return self.modal_resize(context, event, mx, my)

        if event.type == 'Z' and (event.ctrl or event.oskey) and event.value == 'PRESS':
            self.report({'INFO'}, "Undo is off while painting: use the Restore brush (7)")
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and self.stroke is None:
            plain = not (event.ctrl or event.alt or event.oskey)
            if event.type in TOOL_KEYS and plain and not event.shift:
                p.tool = TOOL_KEYS[event.type]
                self.status(context)
                return {'RUNNING_MODAL'}
            if event.type == 'F' and plain:
                self.start_resize(context, mx, my, 'FEATHER' if event.shift else 'RADIUS')
                return {'RUNNING_MODAL'}
            if event.type in {'DEL', 'BACK_SPACE'} and plain:
                self.delete_selection(context)
                return {'RUNNING_MODAL'}
            if event.type in {'LEFT_BRACKET', 'RIGHT_BRACKET'}:
                grow = 1.15 if event.type == 'RIGHT_BRACKET' else 1 / 1.15
                if event.shift:
                    p.feather = min(1.0, max(0.0, p.feather + (0.1 if grow > 1 else -0.1)))
                else:
                    p.radius_px = int(min(1000, max(3, round(p.radius_px * grow))))
                self.update_hit(context, mx, my)
                return {'RUNNING_MODAL'}
            if event.type == 'X':
                p.depth_mode = 'SURFACE' if p.depth_mode == 'THROUGH' else 'THROUGH'
                return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE':
            self.update_hit(context, mx, my)
            if self.stroke is not None and (self.hit is not None or p.tool in FREE_TOOLS):
                spacing = max(p.radius_px * p.spacing, 1.0)
                if self.last_dab is None or np.hypot(mx - self.last_dab[0], my - self.last_dab[1]) >= spacing:
                    self.dab(context, mx, my, event.ctrl or event.alt)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                self.update_hit(context, mx, my)
                if self.hit is None and p.tool not in FREE_TOOLS:
                    return {'RUNNING_MODAL'}
                if p.tool in {'CLONE', 'HEAL'}:
                    if event.ctrl or event.alt:
                        p.source_co = self.obj.matrix_world @ Vector(self.hit_anchor)
                        p.source_set = True
                        self.offset = None
                        return {'RUNNING_MODAL'}
                    src = self.source_point(context)
                    if src is None:
                        self.report({'WARNING'}, "Ctrl+click a clean area first to set the sample point")
                        return {'RUNNING_MODAL'}
                    if self.offset is None or not p.aligned:
                        self.offset = self.hit - src
                self.stroke = core.Stroke(self.S)
                self.bridge_centres = []
                self.spot_dabs = []
                self.rs_remove = np.zeros(self.S.n, bool)
                self.rs_unfade = np.zeros(self.S.n, bool)
                self.rs_restore = np.zeros(0 if self.T is None else self.T.n, bool)
                self.stroke_ctrl = event.ctrl or event.alt
                self.last_dab = None
                self.dab(context, mx, my, event.ctrl or event.alt)
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and self.stroke is not None:
                self.end_stroke(context)
                return {'RUNNING_MODAL'}

        if self.stroke is not None:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def end_stroke(self, context):
        p = props_of(context)
        st, S = self.stroke, self.S
        self.stroke = None
        try:
            if p.tool == 'SELECT':
                S.selected[st.select] = True
                S.selected[st.deselect] = False
                S.selected[S.hidden] = False
                self.rev = commit(context, self.obj, S, "Splat select", only=(SELECT_ATTR,))
            elif p.tool == 'BRIDGE':
                self.end_bridge(context)
            elif p.tool == 'RESTORE':
                self.end_restore(context)
            elif p.tool == 'SPOT':
                self.end_spot(context)
            else:
                faded_before = int(S.faded.sum())
                removed, added = st.commit_clones_and_erase(p.use_opacity_feather)
                if removed or added or int(S.faded.sum()) != faded_before:
                    self.rev = commit(context, self.obj, S, f"Splat {p.tool.lower()}",
                                      only=None if added else OPACITY_COLUMNS)
        except Exception as ex:  # keep the modal alive and the data consistent
            self.report({'ERROR'}, f"Stroke failed: {ex}")
            self.load()
            raise
        self.ptr = self.obj.data.as_pointer()
        self.refresh_axes()
        self.T = stash.read(self.obj)
        self.proj.key = None
        self.edited = True

    # ---------------------------------------------------------- shortcuts
    def start_resize(self, context, mx, my, kind):
        """Blender-style F resize: the circle stays put and follows the mouse's distance."""
        p = props_of(context)
        r = float(p.radius_px)
        if kind == 'RADIUS':
            centre = (mx - r, my)
        else:
            centre = (mx - r * (1.0 - p.feather), my)
        self.resizing = dict(kind=kind, centre=centre, radius=p.radius_px, feather=p.feather)
        context.workspace.status_text_set(
            f"Move to set the brush {'size' if kind == 'RADIUS' else 'feather'} · click or F to confirm · "
            f"Esc or right-click to cancel")

    def modal_resize(self, context, event, mx, my):
        p = props_of(context)
        rs = self.resizing
        d = float(np.hypot(mx - rs["centre"][0], my - rs["centre"][1]))
        if event.type == 'MOUSEMOVE':
            if rs["kind"] == 'RADIUS':
                p.radius_px = int(min(1000, max(3, round(d))))
            else:
                p.feather = float(min(1.0, max(0.0, 1.0 - d / max(p.radius_px, 1))))
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and event.type in {'LEFTMOUSE', 'F', 'RET', 'NUMPAD_ENTER', 'SPACE'}:
            self.resizing = None
            self.status(context)
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and event.type in {'RIGHTMOUSE', 'ESC'}:
            p.radius_px, p.feather = rs["radius"], rs["feather"]
            self.resizing = None
            self.status(context)
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}

    def delete_selection(self, context):
        S = self.S
        n = S.hide(S.selected.copy())
        if n == 0:
            self.report({'INFO'}, "Nothing selected to erase")
            return
        self.report({'INFO'}, f"Erased {n} selected splats")
        self.rev = commit(context, self.obj, S, "Splat delete", only=OPACITY_COLUMNS + (SELECT_ATTR,))
        self.proj.key = None
        self.edited = True

    def restore_dab(self, context, mx, my):
        """Mark added splats to remove and erased/faded splats to bring back, under the brush."""
        p = props_of(context)
        S, T, r = self.S, self.T, float(p.radius_px)
        surface = p.depth_mode == 'SURFACE' and self.hit is not None
        if surface:
            idx, w = core.sphere_hits_S(S, self.hit, self.radius, p.feather)
        else:
            idx, d = self.proj.disc(mx, my, r)
            w = core.feather_weight(d, r, p.feather)
        idx = idx[w > 0.5]
        if p.remove_added:
            self.rs_remove[idx[S.added[idx] & S.visible[idx]]] = True
        if not p.restore_erased:
            return
        self.rs_unfade[idx[S.faded[idx] & ~S.added[idx]]] = True
        # Erased splats have no surface to hit, so pick them on screen, but not ones
        # hidden behind the surface the brush is on.
        V = np.array(self.rv3d.view_matrix @ self.obj.matrix_world, np.float32)
        hdepth = -(self.hit @ V[2, :3] + V[2, 3]) if surface else np.inf
        hidx, d = self.proj.disc(mx, my, r)
        hidx = hidx[(core.feather_weight(d, r, p.feather) > 0.5)]
        hidx = hidx[S.hidden[hidx] & ~S.added[hidx] & (self.proj.depth[hidx] <= hdepth + self.radius)]
        self.rs_unfade[hidx] = True
        if T is not None and T.n:     # splats a 0.1.3 session moved to the stash
            sx, sy, ok = self.proj.project(T.pos)
            m = ok & (core.feather_weight(np.hypot(sx - mx, sy - my), r, p.feather) > 0.5)
            if surface:
                m &= -(T.pos @ V[2, :3] + V[2, 3]) <= hdepth + self.radius
            self.rs_restore |= m

    def end_restore(self, context):
        back, unfaded, removed = core.apply_restore(self.S, self.T, self.rs_remove, self.rs_unfade,
                                                    self.rs_restore)
        if not (back or unfaded or removed):
            return
        if self.T is not None and self.rs_restore.any():
            stash.write(self.obj, self.T)
        self.report({'INFO'}, f"Restored {back} erased, {unfaded} faded; removed {removed} added")
        self.rev = commit(context, self.obj, self.S, "Splat restore", only=OPACITY_COLUMNS)

    def end_bridge(self, context):
        """Fit the intact surface around the painted hole and grow splats across it."""
        p = props_of(context)
        S, proj, r = self.S, self.proj, float(p.radius_px)
        if not self.bridge_centres:
            return
        mw = self.obj.matrix_world
        proj.update(self.region, self.rv3d, S, mw, self.rev, self.axes)
        fp = view.Footprint(self.bridge_centres, r, self.region.width, self.region.height)
        inner = fp.mask(radius_px=r)
        outer = fp.mask(radius_px=r, extra_px=r * p.bridge_rim)
        in_inner = fp.lookup(inner, proj.sx, proj.sy, proj.ok)
        in_outer = fp.lookup(outer, proj.sx, proj.sy, proj.ok)

        # Rim = the front layer of splats in the band around the painted area. Splats
        # further back (the far wall of the hole, the other side of the body) are not rim.
        band = np.nonzero(in_outer & ~in_inner & (S.opacity > 0.1))[0]
        if len(band) < 12:
            self.report({'WARNING'}, "No intact surface around the painted area; paint onto its edge")
            return
        ix = np.floor(proj.sx[band] / fp.cell).astype(int)
        iy = np.floor(proj.sy[band] / fp.cell).astype(int)
        cid = ix * fp.gh + iy
        depth = proj.depth[band]
        solid = S.opacity[band] > 0.3
        if solid.sum() < 12:
            solid = np.ones(len(band), bool)
        # Nearest opaque splat per screen cell; cells with only faint splats (hair) use
        # their nearest splat of any opacity, so nothing behind them counts as rim.
        front = np.full(fp.gw * fp.gh, np.inf)
        np.minimum.at(front, cid[solid], depth[solid])
        any_front = np.full(fp.gw * fp.gh, np.inf)
        np.minimum.at(any_front, cid, depth)
        front = np.where(np.isfinite(front), front, any_front)
        first = band[depth <= front[cid] * 1.0001]
        cx, cy = np.mean(self.bridge_centres, axis=0)
        anchor = np.median(S.pos[first], axis=0) if len(first) else S.pos[band].mean(0)
        R = view.pixel_radius_to_local(self.region, self.rv3d, cx, cy, r, anchor, mw)
        in_front = depth <= front[cid] + 0.5 * R
        ring = band[in_front]
        # Screen distance of each rim splat outside the painted area; the closest ones
        # are the hole's edge.
        centres = np.asarray(self.bridge_centres, np.float32)
        gap = np.full(len(ring), np.inf, np.float32)
        for x0, y0 in centres:
            gap = np.minimum(gap, np.hypot(proj.sx[ring] - x0, proj.sy[ring] - y0) - r)
        at_edge = gap < 0.35 * r * p.bridge_rim
        # Keep one layer: model the edge's view depth as a plane over the screen, fitted
        # robustly (starting from the median, then MAD rejection), and drop rim splats off
        # it. Near an outline the band can see things far behind; those go here.
        rsx, rsy, rdep = proj.sx[ring], proj.sy[ring], proj.depth[ring]
        X = np.stack([rsx - cx, rsy - cy, np.ones(len(ring))], 1)
        m = at_edge.copy()
        if m.sum() >= 12:
            res = rdep - np.median(rdep[m])
            for _ in range(5):
                mad = np.median(np.abs(res[m])) * 1.4826 + 1e-9
                m = at_edge & (np.abs(res) < 3.5 * mad)
                if m.sum() < 12:
                    break
                coef, *_ = np.linalg.lstsq(X[m], rdep[m], rcond=None)
                res = rdep - X @ coef
            mad = np.median(np.abs(res[m])) * 1.4826 + 1e-9 if m.sum() else np.inf
            layer = np.abs(res) < max(4.0 * mad, 0.05 * R)
            if layer.sum() >= 12:
                ring, at_edge = ring[layer], at_edge[layer]
        edge = ring[at_edge]
        near = np.nonzero(in_outer & S.visible)[0]
        toward = -view.view_vector_local(self.region, self.rv3d, cx, cy, mw)

        # What counts as the hole is what the user sees: parts of the painted area that are
        # empty or clearly deeper than the rim's surface (fitted across the screen).
        hole = self.visible_hole(fp, inner, band, front, cid, cx, cy, r)
        if not hole.any():
            self.report({'INFO'}, "The surface under the painted area is intact; nothing to fill")
            return

        def in_footprint(pts):
            return fp.lookup(hole, *proj.project(pts))

        if p.bridge_source != 'SAMPLE':
            added = self.view_fill(context, fp, hole, band, cx, cy)
            if added == 0:
                self.report({'WARNING'}, "Couldn't read the surface around the hole; paint a little onto its edge")
                return
            self.last_bridge = (len(ring), 0, added, "Bridged")
            self.report({'INFO'}, f"Bridged from surroundings: +{added} splats")
            self.rev = commit(context, self.obj, S, "Splat bridge")
            return

        source = None
        if p.bridge_source == 'SAMPLE':
            if not p.source_set:
                self.report({'ERROR'}, "Set a sample point first (Ctrl+click with Clone/Heal, or from the 3D cursor)")
                return
            source = self.source_point(context)
        removed, added, msg = core.bridge_fill(
            S, ring, near, in_footprint, toward, edge=edge, roughness=p.roughness, color_smooth=p.color_smooth,
            density=p.density, source=source, heal=p.heal_strength, feather=p.feather,
            seed=int(time.time()), footprint_is_hole=True)
        self.last_bridge = (len(ring), removed, added, msg)
        if added == 0 and removed == 0:
            self.report({'WARNING'}, msg)
            return
        self.report({'INFO'}, f"{msg}: +{added} splats")
        self.rev = commit(context, self.obj, S, "Splat bridge")

    def visible_hole(self, fp, inner, band, front, cid, cx, cy, r):
        """Screen cells of the painted area where the surface is missing, as seen from here."""
        S, proj = self.S, self.proj
        scale = max(r * 2.0, 1.0)

        def design(x, y):
            u, v = (x - cx) / scale, (y - cy) / scale
            return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], 1)

        cells, first = np.unique(cid, return_index=True)
        fx, fy = proj.sx[band][first], proj.sy[band][first]
        fd = front[cells]
        A = design(fx, fy)
        coef = np.array([np.median(fd), 0, 0, 0, 0, 0], np.float64)
        m = np.ones(len(fd), bool)
        for _ in range(6):
            res = fd - A @ coef
            mad = np.median(np.abs(res[m])) * 1.4826 + 1e-9
            m = np.abs(res) < 3.0 * mad
            if m.sum() < 12:
                break
            coef, *_ = np.linalg.lstsq(A[m], fd[m], rcond=None)
        res = fd - A @ coef
        mad = np.median(np.abs(res[m])) * 1.4826 + 1e-9
        # Front-most splat of each painted cell (opaque ones first, any if a cell has none).
        idx = np.nonzero(fp.lookup(inner, proj.sx, proj.sy, proj.ok) & S.visible & (S.opacity > 0.1))[0]
        ix = np.floor(proj.sx[idx] / fp.cell).astype(int)
        iy = np.floor(proj.sy[idx] / fp.cell).astype(int)
        key = ix * fp.gh + iy
        d = proj.depth[idx]
        top = np.full(fp.gw * fp.gh, np.inf)
        solid = S.opacity[idx] > 0.3
        np.minimum.at(top, key[solid], d[solid])
        anyt = np.full(fp.gw * fp.gh, np.inf)
        np.minimum.at(anyt, key, d)
        top = np.where(np.isfinite(top), top, anyt).reshape(fp.gw, fp.gh)
        X, Y = fp.X, fp.Y
        expect = (design(X.ravel(), Y.ravel()) @ coef).reshape(X.shape)
        deep = top - expect > max(4.0 * mad, 1e-4)          # includes empty cells (inf)
        hole = inner & deep
        # grow by a cell so the patch overlaps the edge of the hole slightly
        grown = hole.copy()
        grown[1:, :] |= hole[:-1, :]; grown[:-1, :] |= hole[1:, :]
        grown[:, 1:] |= hole[:, :-1]; grown[:, :-1] |= hole[:, 1:]
        self._hole_fit = (design, coef, mad)
        return grown & inner

    def view_fill(self, context, fp, hole, band, cx, cy):
        """Grow new splats over the hole cells, at the rim's depth as seen from this view.

        Each new splat copies a nearby splat of the rim's surface layer (colour blended with
        its neighbours) and keeps that donor's offset within the layer, so grain and
        thickness carry across. Density matches the rim's surface layer per screen area.
        """
        from mathutils.kdtree import KDTree
        p = props_of(context)
        S, proj, mw = self.S, self.proj, self.obj.matrix_world
        design, coef, mad = self._hole_fit
        rres = proj.depth[band] - design(proj.sx[band], proj.sy[band]) @ coef
        # How deep the rim's surface goes before it's opaque: per screen cell, walk front to
        # back accumulating opacity; the patch copies everything down to that depth, so it
        # gets the hair and the skin under it, not just the translucent top.
        cid = (np.floor(proj.sx[band] / fp.cell).astype(np.int64) * fp.gh
               + np.floor(proj.sy[band] / fp.cell).astype(np.int64))
        order = np.lexsort((rres, cid))
        c_s, r_s, o_s = cid[order], rres[order], S.opacity[band][order]
        starts = np.r_[0, np.nonzero(c_s[1:] != c_s[:-1])[0] + 1]
        cum = np.zeros(len(order))
        logt = np.log(np.clip(1 - o_s, 1e-6, 1))
        for s0, s1 in zip(starts, np.r_[starts[1:], len(order)]):
            cum[s0:s1] = 1 - np.exp(np.cumsum(logt[s0:s1]))
        opaque_at = [r_s[s0:s1][np.argmax(cum[s0:s1] >= 0.95)] for s0, s1 in zip(starts, np.r_[starts[1:], len(order)])
                     if cum[s1 - 1] >= 0.95]
        thick = float(np.median(opaque_at)) if opaque_at else 4 * mad
        # never reach far past the surface: behind a thin edge the band can see the far side
        layer = (rres > -6 * mad) & (rres <= min(max(thick, 4 * mad), 10 * mad))
        ring, rres = band[layer], rres[layer]
        if len(ring) < 12:
            return 0
        rc = np.unique(np.floor(proj.sx[ring] / fp.cell).astype(int) * fp.gh
                       + np.floor(proj.sy[ring] / fp.cell).astype(int))
        per_cell = len(ring) / max(len(rc), 1) * p.density
        xs, ys = np.nonzero(hole)
        rng = np.random.default_rng(int(time.time()))
        counts = rng.poisson(per_cell, len(xs))
        m = int(counts.sum())
        if m == 0:
            return 0
        sx = (np.repeat(xs, counts) + rng.random(m)) * fp.cell
        sy = (np.repeat(ys, counts) + rng.random(m)) * fp.cell
        kd = KDTree(len(ring))
        for i, (a, b) in enumerate(zip(proj.sx[ring], proj.sy[ring])):
            kd.insert((a, b, 0.0), i)
        kd.balance()
        k = min(12, len(ring))
        donor = np.empty(m, np.int64)
        mean_dc = np.zeros((m, 3), np.float32)
        base = S.base
        for j in range(m):
            nb = kd.find_n((sx[j], sy[j], 0.0), k)
            ids = np.array([t[1] for t in nb])
            w = 1.0 / (np.array([t[2] for t in nb]) + 1.0)
            w /= w.sum()
            donor[j] = ids[rng.choice(len(ids), p=w)]
            if base is not None:
                mean_dc[j] = (base[ring[ids], :3] * w[:, None]).sum(0)
        depth = design(sx, sy) @ coef + p.roughness * rres[donor]
        # Unproject each screen point to its view ray at that view depth (object space).
        Pinv = np.linalg.inv(np.array(self.rv3d.perspective_matrix @ mw, np.float64))
        V = np.array(self.rv3d.view_matrix @ mw, np.float64)
        ndc = np.stack([sx / self.region.width * 2 - 1, sy / self.region.height * 2 - 1], 1)

        def unproj(z):
            hpt = np.concatenate([ndc, np.full((m, 1), z), np.ones((m, 1))], 1) @ Pinv.T
            return hpt[:, :3] / hpt[:, 3:4]
        a0, b0 = unproj(-1.0), unproj(1.0)
        da = -(a0 @ V[2, :3] + V[2, 3])
        db = -(b0 @ V[2, :3] + V[2, 3])
        t = (depth - da) / np.where(np.abs(db - da) > 1e-12, db - da, 1e-12)
        src = ring[donor]
        overrides = {"position": (a0 + (b0 - a0) * t[:, None]).astype(np.float32),
                     "gsp_selected": np.zeros((m, 1), bool)}
        if base is not None:
            nb_ = base[src].copy()
            nb_[:, :3] = (1 - p.color_smooth) * nb_[:, :3] + p.color_smooth * mean_dc
            overrides[core.BASE_ATTR] = nb_
        S.append_copies(src, overrides)
        return m

    def end_spot(self, context):
        """Spot Heal: clone a nearby patch over the spot and match its colour to the spot's surroundings.

        Like Photoshop's spot healing brush, the source is chosen automatically: eight
        candidate patches around the painted area are tried, and the one whose surface
        faces the same way and whose surroundings best match the spot's is cloned in,
        replacing what was under the brush with a feathered, colour-matched patch.
        """
        p = props_of(context)
        S = self.S
        dabs = self.spot_dabs
        if not dabs:
            self.report({'WARNING'}, "Paint over the surface to heal it")
            return
        anchors = np.array([d[0] for d in dabs], np.float32)
        normals = np.array([d[1] for d in dabs], np.float32)
        R = float(np.median([d[2] for d in dabs]))
        cd0 = anchors.mean(0)
        nd0 = normals.mean(0)
        nd0 /= max(np.linalg.norm(nd0), 1e-9)
        extent = float(np.max(np.linalg.norm(anchors - cd0, axis=1))) if len(anchors) > 1 else 0.0
        reach = extent + R
        t1 = np.cross(nd0, [0.0, 0.0, 1.0] if abs(nd0[2]) < 0.9 else [1.0, 0.0, 0.0])
        t1 /= np.linalg.norm(t1)
        t2 = np.cross(nd0, t1)
        target = core.ring_mean_dc(S, cd0, reach)
        # How much surface the spot itself has: a source must have a comparable amount.
        own = S.near(cd0, reach)
        own = own[S.visible[own] & (S.opacity[own] > 0.2) & (np.linalg.norm(S.pos[own] - cd0, axis=1) < reach)]
        need = max(3, int(0.4 * len(own)))
        dens_d = None
        best = None
        for dist in (reach + 1.5 * R, reach + 3.0 * R):
            for k in range(8):
                ang = 2 * np.pi * k / 8
                probe = cd0 + (np.cos(ang) * t1 + np.sin(ang) * t2) * dist
                cs, ns = core.surface_frame(S, probe, R, -nd0)
                facing = float(np.dot(ns, nd0))
                if facing < 0.6:
                    continue
                cand = S.near(cs, reach)
                live = cand[S.visible[cand] & (S.opacity[cand] > 0.2)]
                live = live[np.linalg.norm(S.pos[live] - cs, axis=1) < reach]
                if len(live) < need:
                    continue
                if dens_d is None:
                    cand_d = S.near(cd0, reach * 1.6)
                    dd = np.linalg.norm(S.pos[cand_d] - cd0, axis=1)
                    ring_d = cand_d[(dd > reach) & (dd < reach * 1.6) & S.visible[cand_d] & (S.opacity[cand_d] > 0.2)]
                    area = np.pi * ((reach * 1.6) ** 2 - reach ** 2)
                    dens_d = max(len(ring_d) / area, 1e-9)
                dens_s = len(live) / (np.pi * reach ** 2)
                colour = core.ring_mean_dc(S, cs, reach)
                cdiff = 0.0 if (target is None or colour is None) else float(np.linalg.norm(colour - target))
                score = cdiff + 0.5 * abs(np.log(dens_s / dens_d)) + 0.5 * (1 - facing)
                if best is None or score < best[0]:
                    best = (score, cs, dist)
        if best is None:
            self.report({'WARNING'}, "No similar surface nearby to heal from; try Bridge or Clone")
            return
        offset = best[1] - cd0
        st = core.Stroke(S)
        for anc, nrm, r_, vd in dabs:
            cs, ns = core.surface_frame(S, anc + offset, r_, vd)
            st.clone(cs, ns, anc, nrm, r_, max(p.feather, 0.25), replace=True, heal=p.heal_strength)
        removed, added = st.commit_clones_and_erase(True)
        self.spot_source = best[1]
        self.report({'INFO'}, f"Healed from a nearby patch: replaced {removed}, added {added}")
        self.rev = commit(context, self.obj, S, "Splat spot heal")

    # ---------------------------------------------------------- drawing
    def draw_overlay(self):
        try:
            self._draw()
        except ReferenceError:
            pass

    def _draw(self):
        area = bpy.context.area
        if area is None or area.as_pointer() != self.area.as_pointer():
            return
        p = bpy.context.scene.gsp
        ov = self.overlay
        ov.clear()
        ov.matrix = self.obj.matrix_world
        S = self.S
        st = self.stroke
        tool = p.tool

        if tool == 'RESTORE':
            self.draw_restore(ov, p, st)
        if st is not None and tool == 'SELECT':
            for mask, col in ((st.select, (1.0, 0.55, 0.1, 0.9)), (st.deselect, (0.35, 0.35, 0.4, 0.9))):
                idx = np.nonzero(mask)[0]
                if len(idx):
                    pos, rgba = view.cap(S.pos[idx], view.solid_rgba(len(idx), col))
                    ov.points.append((pos, rgba, 2.0))

        if st is not None:
            if tool == 'ERASE':
                idx = np.nonzero((st.factor < 0.999) & S.visible)[0]
                if len(idx):
                    a = 1.0 - st.factor[idx]
                    rgba = np.column_stack([np.ones_like(a), 0.15 * np.ones_like(a),
                                            0.15 * np.ones_like(a), 0.35 + 0.6 * a]).astype(np.float32)
                    pos, rgba = view.cap(S.pos[idx], rgba)
                    ov.points.append((pos, rgba, 2.0))
            elif tool in {'CLONE', 'HEAL'}:
                pos, rgb = st.clone_preview()
                if pos is not None:
                    rgba = np.column_stack([rgb, np.full(len(rgb), 0.95)]).astype(np.float32)
                    pos, rgba = view.cap(pos, rgba)
                    ov.points.append((pos, rgba, 2.5))

        if self.hit is not None and tool not in {'BRIDGE', 'SPOT'}:
            R = self.radius
            n = self.hit_normal
            col = {'ERASE': (1.0, 0.3, 0.3), 'SELECT': (1.0, 0.6, 0.1), 'CLONE': (0.3, 0.9, 0.4),
                   'HEAL': (0.3, 0.9, 0.8), 'SPOT': (1.0, 0.3, 0.9), 'RESTORE': (1.0, 1.0, 1.0)}[tool]
            if p.depth_mode == 'THROUGH' and tool in {'ERASE', 'SELECT', 'SPOT', 'RESTORE'}:
                n = -self.view_dir
            ov.rings.append((self.hit, n, R, (*col, 1.0), 2.0))
            if p.feather > 0:
                ov.rings.append((self.hit, n, R * (1 - p.feather), (*col, 0.45), 1.5))
            if tool in {'CLONE', 'HEAL'} and p.source_set:
                if self.offset is not None and p.aligned:
                    src = self.hit - self.offset
                else:
                    src = self.source_point(bpy.context)
                ov.rings.append((src, -self.view_dir, R, (0.3, 0.6, 1.0, 0.9), 2.0))
                ov.rings.append((src, -self.view_dir, R * 0.08, (0.3, 0.6, 1.0, 0.9), 2.0))
        elif tool in {'CLONE', 'HEAL'} and p.source_set:
            src = self.source_point(bpy.context)
            ov.rings.append((src, (0, 0, 1), 0.02, (0.3, 0.6, 1.0, 0.9), 2.0))
        ov.draw()

    def draw_restore(self, ov, p, st):
        S, T = self.S, self.T
        stroking = st is not None
        none = np.zeros(S.n, bool)
        layers = []
        if p.restore_erased:
            mark = self.rs_unfade if stroking else none
            erased = S.hidden & ~S.added
            faded = S.faded & ~S.hidden
            layers += [(S.pos[erased & ~mark], (1.0, 0.25, 0.25, 0.85)),
                       (S.pos[faded & ~mark], (1.0, 0.8, 0.2, 0.85)),
                       (S.pos[(erased | faded) & mark], (1.0, 1.0, 1.0, 0.95))]
            if T is not None and T.n:
                tmark = self.rs_restore if stroking else np.zeros(T.n, bool)
                layers += [(T.pos[~tmark], (1.0, 0.25, 0.25, 0.85)), (T.pos[tmark], (1.0, 1.0, 1.0, 0.95))]
        if p.remove_added:
            mark = self.rs_remove if stroking else none
            added = S.added & ~S.hidden
            layers += [(S.pos[added & ~mark], (0.2, 1.0, 0.4, 0.85)),
                       (S.pos[added & mark], (0.3, 0.3, 0.3, 0.9))]
        for pos, col in layers:
            if len(pos):
                pos, rgba = view.cap(pos, view.solid_rgba(len(pos), col))
                ov.points.append((pos, rgba, 2.0))

    def draw_overlay_px(self):
        # Screen circle when nothing solid is under the mouse, so the brush is
        # still visible (and usable) over wisps in empty space.
        try:
            area = bpy.context.area
            if area is None or area.as_pointer() != self.area.as_pointer():
                return
            p = bpy.context.scene.gsp
            if self.resizing is not None:
                cx, cy = self.resizing["centre"]
                view.draw_screen_ring(cx, cy, p.radius_px, (1.0, 1.0, 1.0, 0.95), 2.0)
                if p.feather > 0:
                    view.draw_screen_ring(cx, cy, p.radius_px * (1 - p.feather), (1.0, 1.0, 1.0, 0.45))
                return
            if p.tool in {'BRIDGE', 'SPOT'}:
                col = (0.3, 0.8, 1.0) if p.tool == 'BRIDGE' else (1.0, 0.35, 0.9)
                if self.stroke is not None:
                    view.draw_screen_discs(self.bridge_centres, p.radius_px, (*col, 0.25))
                view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px, (*col, 0.9))
                view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px * (1 + p.bridge_rim),
                                      (*col, 0.35))
                return
            if self.hit is not None or p.tool not in PAINT_TOOLS | {'RESTORE'}:
                return
            view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px, (1.0, 1.0, 1.0, 0.7))
            if p.feather > 0:
                view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px * (1 - p.feather),
                                      (1.0, 1.0, 1.0, 0.3))
        except ReferenceError:
            pass


# =====================================================================
_SEL_CACHE = {"key": None, "pos": None}


def draw_selection():
    """Draw selected splats of the active splat object (registered for every 3D view)."""
    ctx = bpy.context
    p = getattr(ctx.scene, "gsp", None)
    obj = ctx.active_object
    if p is None or not p.show_selection or not is_splat_object(obj):
        return
    pc = obj.data
    attr = pc.attributes.get(SELECT_ATTR)
    n = len(pc.points)
    if attr is None or n == 0:
        return
    key = (pc.as_pointer(), int(pc.get(_REV_KEY, 0)), n)
    if key != _SEL_CACHE["key"]:
        sel = np.empty(n, bool)
        attr.data.foreach_get("value", sel)
        hid = pc.attributes.get(HIDDEN_ATTR)
        if hid is not None:
            hidden = np.empty(n, bool)
            hid.data.foreach_get("value", hidden)
            sel &= ~hidden
        pos = None
        if sel.any():
            allpos = np.empty(n * 3, np.float32)
            pc.attributes["position"].data.foreach_get("vector", allpos)
            pos, _ = view.cap(allpos.reshape(n, 3)[sel], None)
        _SEL_CACHE.update(key=key, pos=pos)
    pos = _SEL_CACHE["pos"]
    if pos is None:
        return
    ov = view.Overlay()
    ov.matrix = obj.matrix_world
    ov.points.append((pos, view.solid_rgba(len(pos), (1.0, 0.55, 0.1, 0.9)), 2.0))
    ov.draw()


class _SplatOp:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_splats(context) is not None


class GSP_OT_select_all(_SplatOp, bpy.types.Operator):
    """Select, deselect or invert the splat selection"""
    bl_idname = "gsp.select_all"
    bl_label = "Select All Splats"
    action: bpy.props.EnumProperty(items=[('SELECT', "Select", ""), ('DESELECT', "Deselect", ""),
                                          ('INVERT', "Invert", "")], default='DESELECT')

    def execute(self, context):
        obj = active_splats(context)
        S = SplatSet.read(obj.data)
        sel = S.selected
        if self.action == 'SELECT':
            sel[:] = S.visible
        elif self.action == 'DESELECT':
            sel[:] = False
        else:
            sel[:] = ~sel & S.visible
        save(obj, S)
        bump_revision(obj)
        return {'FINISHED'}


class GSP_OT_select_faint(_SplatOp, bpy.types.Operator):
    """Add nearly transparent splats (haze, floaters) to the selection"""
    bl_idname = "gsp.select_faint"
    bl_label = "Select Faint Splats"
    threshold: bpy.props.FloatProperty(name="Max Opacity", default=0.05, min=0.0, max=1.0)

    def execute(self, context):
        obj = active_splats(context)
        S = SplatSet.read(obj.data)
        m = (S.opacity < self.threshold) & S.visible
        S.selected[m] = True
        save(obj, S)
        bump_revision(obj)
        self.report({'INFO'}, f"Selected {int(m.sum())} faint splats")
        return {'FINISHED'}


class GSP_OT_select_floaters(_SplatOp, bpy.types.Operator):
    """Add splats that aren't connected to the main specimen (stray wisps, leftovers) to the selection"""
    bl_idname = "gsp.select_floaters"
    bl_label = "Select Floaters"

    def execute(self, context):
        obj = active_splats(context)
        p = props_of(context)
        S = SplatSet.read(obj.data)
        scale = sum(abs(s) for s in obj.matrix_world.to_scale()) / 3.0
        m, cell = core.floater_mask(S, p.floater_gap / max(scale, 1e-12), p.floater_keep)
        S.selected[m] = True
        save(obj, S)
        bump_revision(obj)
        self.report({'INFO'}, f"Selected {int(m.sum())} floating splats (gap {cell * scale:.4g})")
        return {'FINISHED'}


class GSP_OT_select_cursor_sphere(_SplatOp, bpy.types.Operator):
    """Select splats inside a sphere around the 3D cursor"""
    bl_idname = "gsp.select_cursor_sphere"
    bl_label = "Select Around Cursor"

    def execute(self, context):
        obj = active_splats(context)
        p = props_of(context)
        S = SplatSet.read(obj.data)
        c = np.array(obj.matrix_world.inverted() @ context.scene.cursor.location, np.float32)
        scale = sum(abs(s) for s in obj.matrix_world.to_scale()) / 3.0
        m = (np.linalg.norm(S.pos - c, axis=1) < p.cursor_radius / max(scale, 1e-12)) & S.visible
        S.selected[m] = True
        save(obj, S)
        bump_revision(obj)
        self.report({'INFO'}, f"Selected {int(m.sum())} splats")
        return {'FINISHED'}


class GSP_OT_delete_selected(_SplatOp, bpy.types.Operator):
    """Delete the selected splats"""
    bl_idname = "gsp.delete_selected"
    bl_label = "Delete Selected Splats"

    def execute(self, context):
        obj = active_splats(context)
        S = SplatSet.read(obj.data)
        m = S.selected.copy()
        if not m.any():
            self.report({'WARNING'}, "No splats selected")
            return {'CANCELLED'}
        S.hide(m)
        save(obj, S)
        bump_revision(obj)
        self.report({'INFO'}, f"Deleted {int(m.sum())} splats")
        return {'FINISHED'}


class GSP_OT_fill_selected(_SplatOp, bpy.types.Operator):
    """Delete the selected splats and regrow the surface across the gap"""
    bl_idname = "gsp.fill_selected"
    bl_label = "Fill Selection"
    method: bpy.props.EnumProperty(items=[
        ('SURROUND', "From Surroundings", "Grow new splats from the border around the gap"),
        ('SOURCE', "From Sample", "Clone the sample area into the gap and match its colour"),
    ], default='SURROUND')

    def execute(self, context):
        obj = active_splats(context)
        p = props_of(context)
        S = SplatSet.read(obj.data)
        hole = S.selected.copy()
        source = None
        if self.method == 'SOURCE':
            if not p.source_set:
                self.report({'ERROR'}, "Set a sample point first (Ctrl+click with Clone, or from the 3D cursor)")
                return {'CANCELLED'}
            source = np.array(obj.matrix_world.inverted() @ Vector(p.source_co), np.float32)
        removed, added, msg = core.heal_fill(
            S, hole, border=p.border_width, roughness=p.roughness, color_smooth=p.color_smooth,
            density=p.density, source=source, heal=p.heal_strength, feather=p.feather,
            seed=int(time.time()), footprint_is_hole=True)
        if removed == 0 and added == 0:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}
        save(obj, S)
        bump_revision(obj)
        self.report({'INFO'}, f"{msg}: removed {removed}, added {added}")
        return {'FINISHED'}


class GSP_OT_source_from_cursor(bpy.types.Operator):
    """Use the 3D cursor as the clone/heal sample point"""
    bl_idname = "gsp.source_from_cursor"
    bl_label = "Sample From 3D Cursor"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        p = props_of(context)
        p.source_co = context.scene.cursor.location
        p.source_set = True
        return {'FINISHED'}


class GSP_OT_export_ply(bpy.types.Operator, ExportHelper):
    """Save the active splat object as a standard 3D Gaussian splat PLY"""
    bl_idname = "gsp.export_ply"
    bl_label = "Export Splat PLY"
    filename_ext = ".ply"
    filter_glob: bpy.props.StringProperty(default="*.ply", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return active_splats(context) is not None

    def invoke(self, context, event):
        obj = active_splats(context)
        if not self.filepath:
            self.filepath = bpy.path.clean_name(obj.name) + "_patched.ply"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        obj = active_splats(context)
        n = write_splat_ply(self.filepath, SplatSet.read(obj.data).visible_subset())
        self.report({'INFO'}, f"Wrote {n} splats to {self.filepath}")
        return {'FINISHED'}


class GSP_OT_stop_brush(bpy.types.Operator):
    """Leave the splat brush"""
    bl_idname = "gsp.stop_brush"
    bl_label = "Stop Brush"

    def execute(self, context):
        STATE["running"] = False
        return {'FINISHED'}


classes = (
    GSP_OT_brush,
    GSP_OT_select_all,
    GSP_OT_select_faint,
    GSP_OT_select_floaters,
    GSP_OT_select_cursor_sphere,
    GSP_OT_delete_selected,
    GSP_OT_fill_selected,
    GSP_OT_source_from_cursor,
    GSP_OT_export_ply,
    GSP_OT_stop_brush,
)
