"""Minimal N-panel: one button to start drawing a camera-plate rectangle."""

from __future__ import annotations

import bpy


class CAMERAPLATE_PT_main(bpy.types.Panel):
    bl_idname = "CAMERAPLATE_PT_main"
    bl_label = "Camera Plate"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Plate"

    def draw(self, context):
        self.layout.operator("cameraplate.draw", text="Add Camera Plate", icon="VIEW_CAMERA")


PANELS = (CAMERAPLATE_PT_main,)