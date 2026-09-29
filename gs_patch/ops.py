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
    ('SELECT', "Select", "Paint a region to erase or fill later (Ctrl: deselect)", 'RESTRICT_SELECT_OFF', 1),
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
# Tools that can start and continue a stroke with nothing solid under the mouse.
FREE_TOOLS = PAINT_TOOLS | {'BRIDGE', 'RESTORE'}

HELP = {
    'RESTORE': "LMB restore · green = added by edits, red = erased, yellow = faded",
    'BRIDGE': "LMB paint over the hole, a little onto the intact surface around it",
    'ERASE': "LMB paint erase",
    'SELECT': "LMB select · Ctrl+LMB deselect",
    'CLONE': "Ctrl+LMB set sample · LMB clone",
    'HEAL': "Ctrl+LMB set sample · LMB heal",
    'SPOT': "LMB paint over blemish",
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
            f"Splat {p.tool.title()}:  {HELP[p.tool]} · [ ] radius · Shift+[ ] feather · "
            f"X surface/through · 1-7 tools · Esc/Enter done")

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
        if p.tool == 'BRIDGE':
            self.bridge_centres.append((mx, my))
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
            if p.tool == 'ERASE':
                st.erase(idx, w, p.strength)
            elif p.tool == 'SELECT':
                st.mark_select(idx[w > 0.5] if feather > 0 else idx)
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

        x, y = event.mouse_x, event.mouse_y
        inside = (self.region.x <= x < self.region.x + self.region.width
                  and self.region.y <= y < self.region.y + self.region.height)
        if self.stroke is None and (not inside or _in_ui_region(self.area, x, y)):
            self.hit = None
            return {'PASS_THROUGH'}

        mx, my = self.mouse_local(event)
        self.mouse = (mx, my)

        if event.type in {'ESC', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if self.stroke is not None:
                self.end_stroke(context)
            self.finish(context)
            return {'FINISHED'}

        if event.type == 'Z' and (event.ctrl or event.oskey) and event.value == 'PRESS':
            self.report({'INFO'}, "Undo is off while painting: use the Restore brush (7)")
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and self.stroke is None:
            tools = {'ONE': 'ERASE', 'TWO': 'SELECT', 'THREE': 'CLONE', 'FOUR': 'HEAL', 'FIVE': 'SPOT',
                     'SIX': 'BRIDGE', 'SEVEN': 'RESTORE'}
            if event.type in tools:
                p.tool = tools[event.type]
                self.status(context)
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
                    self.dab(context, mx, my, event.ctrl)
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
                self.rs_remove = np.zeros(self.S.n, bool)
                self.rs_unfade = np.zeros(self.S.n, bool)
                self.rs_restore = np.zeros(0 if self.T is None else self.T.n, bool)
                self.stroke_ctrl = event.ctrl
                self.last_dab = None
                self.dab(context, mx, my, event.ctrl)
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
                if self.stroke_ctrl:
                    S.selected[st.select] = False
                else:
                    S.selected[st.select] = True
                S.selected[S.hidden] = False
                self.rev = commit(context, self.obj, S, "Splat select", only=(SELECT_ATTR,))
            elif p.tool == 'BRIDGE':
                self.end_bridge(context)
            elif p.tool == 'RESTORE':
                self.end_restore(context)
            elif p.tool == 'SPOT':
                if not st.hole.any():
                    return
                removed, added, msg = core.heal_fill(
                    S, st.hole, border=p.border_width, roughness=p.roughness,
                    color_smooth=p.color_smooth, density=p.density, seed=int(time.time()))
                self.report({'INFO'}, f"{msg}: -{removed} +{added}")
                self.rev = commit(context, self.obj, S, "Splat spot heal")
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

        def in_footprint(pts):
            return fp.lookup(inner, *proj.project(pts))

        source = None
        if p.bridge_source == 'SAMPLE':
            if not p.source_set:
                self.report({'ERROR'}, "Set a sample point first (Ctrl+click with Clone/Heal, or from the 3D cursor)")
                return
            source = self.source_point(context)
        removed, added, msg = core.bridge_fill(
            S, ring, near, in_footprint, toward, edge=edge, roughness=p.roughness, color_smooth=p.color_smooth,
            density=p.density, source=source, heal=p.heal_strength, feather=p.feather,
            seed=int(time.time()))
        self.last_bridge = (len(ring), added, msg)
        if added == 0:
            self.report({'WARNING'}, msg)
            return
        self.report({'INFO'}, f"{msg}: +{added} splats")
        self.rev = commit(context, self.obj, S, "Splat bridge")

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
            idx = np.nonzero(st.select)[0]
            if len(idx):
                col = (0.35, 0.35, 0.4, 0.9) if self.stroke_ctrl else (1.0, 0.55, 0.1, 0.9)
                pos, rgba = view.cap(S.pos[idx], view.solid_rgba(len(idx), col))
                ov.points.append((pos, rgba, 2.0))

        if st is not None:
            if tool == 'ERASE':
                idx = np.nonzero(st.factor < 0.999)[0]
                if len(idx):
                    a = 1.0 - st.factor[idx]
                    rgba = np.column_stack([np.ones_like(a), 0.15 * np.ones_like(a),
                                            0.15 * np.ones_like(a), 0.35 + 0.6 * a]).astype(np.float32)
                    pos, rgba = view.cap(S.pos[idx], rgba)
                    ov.points.append((pos, rgba, 2.0))
            elif tool == 'SPOT':
                idx = np.nonzero(st.hole)[0]
                if len(idx):
                    pos, rgba = view.cap(S.pos[idx], view.solid_rgba(len(idx), (1.0, 0.2, 0.8, 0.8)))
                    ov.points.append((pos, rgba, 2.0))
            elif tool in {'CLONE', 'HEAL'}:
                pos, rgb = st.clone_preview()
                if pos is not None:
                    rgba = np.column_stack([rgb, np.full(len(rgb), 0.95)]).astype(np.float32)
                    pos, rgba = view.cap(pos, rgba)
                    ov.points.append((pos, rgba, 2.5))

        if self.hit is not None and tool != 'BRIDGE':
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
            if p.tool == 'BRIDGE':
                if self.stroke is not None:
                    view.draw_screen_discs(self.bridge_centres, p.radius_px, (0.3, 0.8, 1.0, 0.25))
                view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px, (0.3, 0.8, 1.0, 0.9))
                view.draw_screen_ring(self.mouse[0], self.mouse[1], p.radius_px * (1 + p.bridge_rim),
                                      (0.3, 0.8, 1.0, 0.35))
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
            seed=int(time.time()))
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
