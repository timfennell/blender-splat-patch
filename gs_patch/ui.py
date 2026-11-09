import bpy

from .ops import TOOL_ITEMS, STATE, SHORTCUTS
from .splats import is_splat_object
from .volumes import SHAPES as VOLUME_SHAPES


class GSP_Props(bpy.types.PropertyGroup):
    tool: bpy.props.EnumProperty(name="Tool", items=TOOL_ITEMS, default='ERASE')
    lod_fraction: bpy.props.FloatProperty(
        name="Detail", default=0.1, min=0.005, max=0.9, subtype='FACTOR',
        description="Share of each model's splats a low-detail stand-in keeps")
    work_radius: bpy.props.FloatProperty(name="Work Radius", default=0.1, min=1e-6, unit='LENGTH',
                                         description="Radius of the full-detail work area around the 3D cursor")
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
    volume_shape: bpy.props.EnumProperty(name="Shape", items=VOLUME_SHAPES, default='BOX')
    volume: bpy.props.PointerProperty(name="Volume", type=bpy.types.Object,
                                      poll=lambda self, ob: ob.type == 'EMPTY' and "gsp_volume" in ob,
                                      description="The selection volume the buttons use")
    restore_erased: bpy.props.BoolProperty(
        name="Restore Erased", default=True,
        description="Bring back splats that edits erased or faded (red and yellow)")
    remove_added: bpy.props.BoolProperty(
        name="Remove Added", default=True,
        description="Remove splats that edits added (green)")
    bridge_source: bpy.props.EnumProperty(name="Fill With", items=[
        ('SURROUND', "Surroundings", "Grow the bridge from the rim around the hole"),
        ('SAMPLE', "Sample", "Clone the area around the sample point onto the bridge, colour-matched to the rim"),
    ], default='SURROUND')
    bridge_rim: bpy.props.FloatProperty(
        name="Rim Width", default=1.0, min=0.2, max=4.0,
        description="How far around the painted area to read the intact surface, as a multiple of the brush radius")
    floater_gap: bpy.props.FloatProperty(
        name="Gap", default=0.0, min=0.0, unit='LENGTH',
        description="Splats separated from the specimen by more than this count as floaters (0 = automatic)")
    floater_keep: bpy.props.FloatProperty(
        name="Keep Clumps Above", default=0.02, min=0.0, max=1.0, subtype='FACTOR',
        description="Clumps at least this fraction of the largest one are kept (e.g. detached legs)")
    # ------------------------------------------------------- mesh to splats
    conv_spacing: bpy.props.FloatProperty(
        name="Splat Spacing", default=0.0, min=0.0, unit='LENGTH', precision=4,
        description="Distance between splats on the surface (0 = 1/400 of the object's size). "
                    "Halving it makes four times as many splats")
    conv_degree: bpy.props.EnumProperty(name="SH Degree", items=[
        ('AUTO', "Match Scene", "The highest degree among the scene's scans (3 if there are none): "
                                "every splat in one PLY must use the same degree"),
        ('0', "0", "Colour only: no view-dependent shading, and nothing for relighting"),
        ('1', "1", "Diffuse shading only"),
        ('2', "2", "Diffuse and broad shine"),
        ('3', "3", "Diffuse and the sharpest shine splats can hold"),
    ], default='AUTO')
    conv_lighting: bpy.props.EnumProperty(name="Lighting", items=[
        ('HEADLIGHT', "Light on Camera",
         "Colour as a scan lit from the camera would learn it, so the splats relight like your scans"),
        ('SCAN', "Light from Scan",
         "Lit by the scan around it: Spirula Render renders the surroundings from the object, Cycles lights the "
         "mesh with that, the scan's reflections go into the view-dependent colour, and a contact shadow layer "
         "darkens the scan beneath. For scans of real places, lit by their own light"),
        ('BAKED', "Bake Scene Lighting",
         "Cycles bakes the scene's lights and shadows into a flat colour: looks right as it is, "
         "but the shadows stay put when relit"),
    ], default='HEADLIGHT')
    conv_gain: bpy.props.FloatProperty(
        name="Brightness", default=1.0, min=0.0, soft_max=4.0,
        description="Exposure of the converted colour (1: a white surface facing the light is display white). "
                    "Match it to your scans")
    conv_shine: bpy.props.FloatProperty(
        name="Shine", default=1.0, min=0.0, soft_max=3.0,
        description="How bright highlights narrower than the splats can draw are kept (0: their true, "
                    "faint spread-out energy)")
    conv_reflections: bpy.props.BoolProperty(
        name="Reflect Surroundings", default=False,
        description="Bake a blurred reflection of the surroundings (rendered from the object's centre with the "
                    "scene's engine) into the shine. Looks right under the scan's own light; game lamps read "
                    "it as highlights")
    conv_reflect_strength: bpy.props.FloatProperty(
        name="Reflection", default=1.0, min=0.0, soft_max=2.0, subtype='FACTOR',
        description="Strength of the baked reflection")
    conv_probe_res: bpy.props.IntProperty(
        name="Probe Size", default=64, min=16, max=512, subtype='PIXEL',
        description="Size of each of the six renders of the surroundings")
    conv_bake_samples: bpy.props.IntProperty(
        name="Bake Samples", default=64, min=1, max=4096,
        description="Cycles samples for baking the scene's lighting")
    conv_contact_shadows: bpy.props.BoolProperty(
        name="Contact Shadows", default=True,
        description="Darken the scan where the object blocks its light, with a separate layer of black splats "
                    "(<mesh>_shadow); the scan itself isn't changed")
    conv_shadow_strength: bpy.props.FloatProperty(
        name="Shadow Strength", default=1.0, min=0.0, soft_max=2.0, subtype='FACTOR',
        description="How dark the contact shadow is (1: the share of the scan's light the object blocks)")
    conv_shadow_reach: bpy.props.FloatProperty(
        name="Shadow Reach", default=0.0, min=0.0, unit='LENGTH',
        description="How far from the object the scan is shadowed (0 = half the object's size)")
    conv_two_sided: bpy.props.BoolProperty(
        name="Two-Sided", default=False,
        description="Splats on both faces, for open surfaces seen from behind (leaves, cards, planes)")
    conv_hide_source: bpy.props.BoolProperty(
        name="Hide Mesh", default=True,
        description="Hide the mesh after converting (it is kept, to edit and convert again)")


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
        if p.tool in {'ERASE', 'SELECT', 'RESTORE'}:
            col.prop(p, "depth_mode", expand=True)
        if p.tool == 'SPOT':
            col.prop(p, "heal_strength", slider=True)
        if p.tool == 'RESTORE':
            col.prop(p, "restore_erased", toggle=True, icon='RECOVER_LAST')
            col.prop(p, "remove_added", toggle=True, icon='TRASH')
        if p.tool == 'BRIDGE':
            col.prop(p, "bridge_source", expand=True)
            col.prop(p, "bridge_rim")
            if p.bridge_source == 'SAMPLE':
                col.prop(p, "heal_strength", slider=True)
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
        ob = context.active_object
        return is_splat_object(ob) or (ob is not None and ob.type == 'EMPTY' and "gsp_volume" in ob)

    def draw(self, context):
        layout = self.layout
        p = context.scene.gsp
        layout.prop(p, "show_selection")
        box = layout.box()
        box.label(text="Volume", icon='MOD_WIREFRAME')
        row = box.row(align=True)
        row.prop(p, "volume_shape", text="")
        row.operator("gsp.add_volume", text="Add", icon='ADD').shape = p.volume_shape
        box.prop(p, "volume", text="")
        row = box.row(align=True)
        row.operator("gsp.volume_select", text="Select", icon='ADD').action = 'SELECT'
        row.operator("gsp.volume_select", text="Deselect", icon='REMOVE').action = 'DESELECT'
        row.operator("gsp.volume_select", text="Only", icon='RESTRICT_SELECT_OFF').action = 'ONLY'
        box.operator("gsp.remove_volume", icon='X')
        if not is_splat_object(context.active_object):
            return
        row = layout.row(align=True)
        row.operator("gsp.select_all", text="All").action = 'SELECT'
        row.operator("gsp.select_all", text="None").action = 'DESELECT'
        row.operator("gsp.select_all", text="Invert").action = 'INVERT'
        row = layout.row(align=True)
        row.operator("gsp.select_cursor_sphere", icon='CURSOR')
        row.prop(p, "cursor_radius", text="")
        layout.operator("gsp.select_faint", icon='GHOST_ENABLED')
        col = layout.column(align=True)
        col.operator("gsp.select_floaters", icon='OUTLINER_DATA_POINTCLOUD')
        row = col.row(align=True)
        row.prop(p, "floater_gap")
        row.prop(p, "floater_keep", text="Keep")
        layout.separator()
        layout.operator("gsp.delete_selected", icon='TRASH')
        col = layout.column(align=True)
        col.operator("gsp.fill_selected", text="Fill From Surroundings", icon='SHADERFX').method = 'SURROUND'
        col.operator("gsp.fill_selected", text="Fill From Sample", icon='DUPLICATE').method = 'SOURCE'


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


class GSP_PT_shortcuts(bpy.types.Panel):
    bl_label = "Shortcuts"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="While the brush is running:")
        for keys, what in SHORTCUTS:
            row = col.row()
            split = row.split(factor=0.34)
            split.label(text=keys)
            split.label(text=what)


class GSP_PT_export(bpy.types.Panel):
    bl_label = "Bake, Merge & Export"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return is_splat_object(context.active_object)

    def draw(self, context):
        self.layout.operator("gsp.bake_edits", icon='CHECKMARK')
        self.layout.operator("gsp.merge_splats", icon='AUTOMERGE_ON')
        self.layout.operator("gsp.export_ply", icon='EXPORT')


class GSP_PT_lod(bpy.types.Panel):
    bl_label = "Level of Detail"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"

    @classmethod
    def poll(cls, context):
        return any(is_splat_object(o) for o in context.selected_objects) or is_splat_object(context.active_object)

    def draw(self, context):
        from . import lod
        layout = self.layout
        p = context.scene.gsp
        obj = context.active_object
        if is_splat_object(obj):
            full = lod.full_data(obj)
            if lod.is_proxy(obj):
                layout.label(text=f"Low detail: {len(obj.data.points):,} of {len(full.points):,} splats",
                             icon='MOD_DECIM')
            elif lod.is_workarea(obj):
                layout.label(text=f"Work area: {len(obj.data.points):,} showing of {len(full.points):,}",
                             icon='PIVOT_CURSOR')
            else:
                layout.label(text=f"Full detail: {len(full.points):,} splats", icon='MESH_UVSPHERE')
        layout.prop(p, "lod_fraction")
        row = layout.row(align=True)
        row.operator("gsp.low_detail", icon='MOD_DECIM').fraction = p.lod_fraction
        row.operator("gsp.full_detail", icon='MESH_UVSPHERE')
        layout.label(text="Applies to the selected models", icon='INFO')
        box = layout.box()
        box.label(text="Work Area (active model)")
        box.prop(p, "work_radius")
        row = box.row(align=True)
        op = row.operator("gsp.focus_workarea", icon='PIVOT_CURSOR')
        op.radius, op.fraction = p.work_radius, p.lod_fraction
        row.operator("gsp.leave_workarea", icon='LOOP_BACK')


class GSP_PT_convert(bpy.types.Panel):
    bl_label = "Mesh to Splats"
    bl_parent_id = "GSP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Splat Patch"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from .mesh_convert import SOURCE_KEY
        layout = self.layout
        p = context.scene.gsp
        layout.prop(p, "conv_spacing")
        layout.prop(p, "conv_degree")
        layout.prop(p, "conv_lighting")
        layout.prop(p, "conv_gain")
        if p.conv_lighting == 'HEADLIGHT':
            layout.prop(p, "conv_shine")
            col = layout.column()
            col.enabled = p.conv_degree != '0' and p.conv_degree != '1'
            col.prop(p, "conv_reflections")
            sub = col.column(align=True)
            sub.enabled = p.conv_reflections
            sub.prop(p, "conv_reflect_strength")
            sub.prop(p, "conv_probe_res")
        elif p.conv_lighting == 'SCAN':
            col = layout.column(align=True)
            col.prop(p, "conv_bake_samples")
            col.prop(p, "conv_probe_res")
            col.prop(p, "conv_reflect_strength")
            box = layout.box()
            box.prop(p, "conv_contact_shadows")
            sub = box.column(align=True)
            sub.enabled = p.conv_contact_shadows
            sub.prop(p, "conv_shadow_strength")
            sub.prop(p, "conv_shadow_reach")
        else:
            layout.prop(p, "conv_bake_samples")
        row = layout.row()
        row.prop(p, "conv_two_sided")
        row.prop(p, "conv_hide_source")
        row = layout.row()
        row.scale_y = 1.3
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        obj = context.active_object
        if not meshes and obj is not None and obj.get(SOURCE_KEY):
            row.operator("gsp.convert_mesh", text="Convert Again", icon='FILE_REFRESH')
        else:
            row.operator("gsp.convert_mesh", icon='POINTCLOUD_DATA')


classes = (GSP_Props, GSP_PT_panel, GSP_PT_brush, GSP_PT_sample, GSP_PT_selection,
           GSP_PT_heal, GSP_PT_shortcuts, GSP_PT_lod, GSP_PT_export, GSP_PT_convert)
