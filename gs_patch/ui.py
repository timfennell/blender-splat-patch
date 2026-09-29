import bpy

from .ops import TOOL_ITEMS, STATE
from .splats import is_splat_object


class GSP_Props(bpy.types.PropertyGroup):
    tool: bpy.props.EnumProperty(name="Tool", items=TOOL_ITEMS, default='ERASE')
    radius_px: bpy.props.IntProperty(name="Radius", subtype='PIXEL', default=40, min=3, max=1000,
                                     description="Brush radius in screen pixels ([ and ] while painting)")
    feather: bpy.props.FloatProperty(name="Feather", default=0.35, min=0.0, max=1.0, subtype='FACTOR',
                                     description="Soft outer part of the brush (Shift+[ ] while painting)")
    strength: bpy.props.FloatProperty(name="Strength", default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                                      description="How much the eraser fades splats per stroke")
    spacing: bpy.props.FloatProperty(name="Spacing", default=0.25, min=0.05, max=2.0,
                                     description="Distance between dabs, as a fraction of the radius")
    depth_mode: bpy.props.EnumProperty(name="Depth", items=[
        ('SURFACE', "Surface", "Affect a sphere at the surface under the mouse"),
        ('THROUGH', "Through", "Affect everything under the brush at every depth (pins, X-ray)"),
    ], default='SURFACE')
    use_opacity_feather: bpy.props.BoolProperty(name="Fade Clone Edges", default=True,
                                                description="Fade cloned splats' opacity across the feather")
    replace: bpy.props.BoolProperty(name="Replace Destination", default=True,
                                    description="Remove what is under the clone brush before stamping")
    aligned: bpy.props.BoolProperty(name="Aligned", default=True,
                                    description="Keep the source-to-destination offset between strokes")
    heal_strength: bpy.props.FloatProperty(name="Colour Match", default=1.0, min=0.0, max=1.0,
                                           subtype='FACTOR',
                                           description="How far healed splats shift towards the destination's colour")
    source_set: bpy.props.BoolProperty(default=False)
    source_co: bpy.props.FloatVectorProperty(name="Sample Point", subtype='TRANSLATION')
    border_width: bpy.props.FloatProperty(name="Border", default=0.0, min=0.0, unit='LENGTH',
                                          description="Width of the surrounding ring used to heal (0 = automatic)")
    roughness: bpy.props.FloatProperty(name="Grain", default=1.0, min=0.0, max=2.0, subtype='FACTOR',
                                       description="How much surface roughness the regrown splats keep")
    color_smooth: bpy.props.FloatProperty(name="Colour Smoothing", default=0.4, min=0.0, max=1.0,
                                          subtype='FACTOR',
                                          description="Blend regrown colour towards the local average")
    density: bpy.props.FloatProperty(name="Density", default=1.0, min=0.1, max=4.0,
                                     description="Regrown splat density relative to the surroundings")
    cursor_radius: bpy.props.FloatProperty(name="Radius", default=0.1, min=0.0, unit='LENGTH')
    show_selection: bpy.props.BoolProperty(name="Show Selection", default=True)


class GSP_PT_panel(bpy.types.Panel):
    bl_label = "Splat Patch"
    bl_idname = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    def draw(self, context):
        layout = self.layout
        p = context.scene.gsp
        obj = context.active_object
        if not is_splat_object(obj):
            layout.label(text="Select a Gaussian splat object", icon='INFO')
            layout.operator("wm.ply_import", text="Import PLY", icon='IMPORT')
            layout.operator("wm.spz_import", text="Import SPZ", icon='IMPORT')
            return
        layout.label(text=f"{obj.name}: {len(obj.data.points):,} splats", icon='POINTCLOUD_DATA')

        col = layout.column(align=True)
        grid = col.grid_flow(columns=3, align=True, even_columns=True)
        for ident, name, _, icon, _ in TOOL_ITEMS:
            op = grid.operator("gsp.brush", text=name, icon=icon, depress=(p.tool == ident))
            op.tool = ident
        row = layout.row()
        row.scale_y = 1.4
        if STATE["running"]:
            row.operator("gsp.stop_brush", text="Stop Brush (Esc)", icon='CANCEL')
        else:
            row.operator("gsp.brush", text="Start Brush", icon='BRUSH_DATA').tool = 'KEEP'


class GSP_PT_brush(bpy.types.Panel):
    bl_label = "Brush"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        p = context.scene.gsp
        col = layout.column()
        col.prop(p, "radius_px")
        col.prop(p, "feather", slider=True)
        col.prop(p, "spacing")
        if p.tool in {'ERASE', 'SELECT', 'SPOT'}:
            col.prop(p, "depth_mode", expand=True)
        if p.tool == 'ERASE':
            col.prop(p, "strength", slider=True)
        if p.tool in {'CLONE', 'HEAL'}:
            col.prop(p, "aligned")
            col.prop(p, "replace")
            col.prop(p, "use_opacity_feather")
            if p.tool == 'HEAL':
                col.prop(p, "heal_strength", slider=True)


class GSP_PT_sample(bpy.types.Panel):
    bl_label = "Sample Point"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        layout = self.layout
        p = context.scene.gsp
        if p.source_set:
            layout.prop(p, "source_co", text="")
        else:
            layout.label(text="Ctrl+click with Clone or Heal", icon='INFO')
        layout.operator("gsp.source_from_cursor", icon='CURSOR')


class GSP_PT_selection(bpy.types.Panel):
    bl_label = "Selection & Fill"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        layout = self.layout
        p = context.scene.gsp
        layout.prop(p, "show_selection")
        row = layout.row(align=True)
        row.operator("gsp.select_all", text="All").action = 'SELECT'
        row.operator("gsp.select_all", text="None").action = 'DESELECT'
        row.operator("gsp.select_all", text="Invert").action = 'INVERT'
        row = layout.row(align=True)
        row.operator("gsp.select_cursor_sphere", icon='CURSOR')
        row.prop(p, "cursor_radius", text="")
        layout.operator("gsp.select_faint", icon='GHOST_ENABLED')
        layout.separator()
        layout.operator("gsp.delete_selected", icon='TRASH')
        col = layout.column(align=True)
        col.operator("gsp.fill_selected", text="Fill From Surroundings", icon='SHADERFX').method = 'SURROUND'
        col.operator("gsp.fill_selected", text="Fill From Sample", icon='BRUSH_CLONE').method = 'SOURCE'


class GSP_PT_heal(bpy.types.Panel):
    bl_label = "Heal Settings"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        p = context.scene.gsp
        col = layout.column()
        col.prop(p, "border_width")
        col.prop(p, "roughness", slider=True)
        col.prop(p, "color_smooth", slider=True)
        col.prop(p, "density")
        col.prop(p, "heal_strength", slider=True)


class GSP_PT_export(bpy.types.Panel):
    bl_label = "Export"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        self.layout.operator("gsp.export_ply", icon='EXPORT')


classes = (GSP_Props, GSP_PT_panel, GSP_PT_brush, GSP_PT_sample, GSP_PT_selection,
           GSP_PT_heal, GSP_PT_export)
