"""N-panel with the layer stack UI."""

from __future__ import annotations

import bpy

from .plate_layers import material_active


class CAMERAPLATE_UL_layers(bpy.types.UIList):
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


class CAMERAPLATE_PT_main(bpy.types.Panel):
    bl_idname = "CAMERAPLATE_PT_main"
    bl_label = "Camera Plate"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Plate"

    def draw(self, context):
        layout = self.layout
        layout.operator("cameraplate.draw", text="Draw Camera Frame", icon="VIEW_CAMERA")

        material = material_active(context)
        if material is None:
            layout.label(text="No material on the active object.", icon="INFO")
            return

        plate = material.plate
        layout.label(text="Layer Stack", icon="TEXTURE")

        row = layout.row(align=True)
        row.template_list(
            "CAMERAPLATE_UL_layers",
            "",
            plate,
            "layers",
            plate,
            "active_layer_index",
        )
        actions = row.column(align=True)
        actions.operator("cameraplate.layer_remove", text="", icon="REMOVE")
        actions.separator()
        actions.operator("cameraplate.layer_move", text="", icon="TRIA_UP").direction = "UP"
        actions.operator("cameraplate.layer_move", text="", icon="TRIA_DOWN").direction = "DOWN"

        if len(plate.layers) == 0:
            layout.label(text="Draw a frame to create the first layer.", icon="INFO")
            return

        layer = plate.layers[plate.active_layer_index]
        box = layout.box()
        box.label(text="Active Layer", icon="TEXTURE")
        box.prop(layer, "blend_mode")
        box.prop(layer, "format")
        box.prop(layer, "mix_factor", slider=True)
        row = box.row(align=True)
        row.operator("cameraplate.quick_edit", text="Quick Edit", icon="IMAGE_DATA")
        row.operator("cameraplate.reload_image", text="", icon="FILE_REFRESH")


PANELS = (CAMERAPLATE_UL_layers, CAMERAPLATE_PT_main)