"""N-panel with the layer stack UI."""

from __future__ import annotations

import bpy

from .plate_layers import material_active


class PROJECTIONCAM_UL_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.prop(
                item,
                "enabled",
                text="",
                icon="HIDE_OFF" if item.enabled else "HIDE_ON",
                emboss=False,
            )
            layout.prop(item, "name", text="", emboss=False)
            if item.image:
                layout.label(text=item.image.name, icon="IMAGE_DATA", translate=False)


class PROJECTIONCAM_PT_main(bpy.types.Panel):
    bl_idname = "PROJECTIONCAM_PT_main"
    bl_label = "Projection Camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Projection"

    def draw(self, context):
        layout = self.layout
        layout.operator("projectioncam.draw", text="Draw Camera Frame", icon="VIEW_CAMERA")

        hidden = context.scene.prj_cameras_hidden
        layout.operator(
            "projectioncam.toggle_camera_visibility",
            text="Show Cameras" if hidden else "Hide Cameras",
        )

        material = material_active(context)
        if material is None:
            layout.label(text="No material on the active object.", icon="INFO")
            return

        plate = material.plate
        layout.label(text="Layer Stack", icon="TEXTURE")

        row = layout.row(align=True)
        row.template_list(
            "PROJECTIONCAM_UL_layers",
            "",
            plate,
            "layers",
            plate,
            "active_layer_index",
        )
        actions = row.column(align=True)
        actions.operator("projectioncam.layer_remove", text="", icon="REMOVE")
        actions.separator()
        actions.operator("projectioncam.layer_move", text="", icon="TRIA_UP").direction = "UP"
        actions.operator("projectioncam.layer_move", text="", icon="TRIA_DOWN").direction = "DOWN"

        if len(plate.layers) == 0:
            layout.label(text="Draw a frame to create the first layer.", icon="INFO")
            return

        scene = context.scene
        header = layout.row(align=True)
        header.prop(
            scene,
            "prj_layer_settings_expanded",
            icon="TRIA_DOWN" if scene.prj_layer_settings_expanded else "TRIA_RIGHT",
            icon_only=True,
            emboss=False,
        )
        header.label(text="Layer Settings")
        if scene.prj_layer_settings_expanded:
            layer = plate.layers[plate.active_layer_index]
            settings = layout.column(align=True)
            settings.prop(layer, "blend_mode", text="")
            settings.prop(layer, "format", text="")
            settings.prop(layer, "mix_factor", slider=True)

        box = layout.box()
        export = box.column(align=True)
        export.prop(scene, "prj_export_baselayer", text="Export Base Image")
        if scene.prj_export_baselayer:
            export.row(align=True).prop(scene, "prj_bake_engine", expand=True)
            export.prop(scene, "prj_bake_samples")
        actions = box.row(align=True)
        actions.operator("projectioncam.quick_edit", text="Quick Edit", icon="IMAGE_DATA")
        actions.operator("projectioncam.reload_image", text="", icon="FILE_REFRESH")


PANELS = (PROJECTIONCAM_UL_layers, PROJECTIONCAM_PT_main)