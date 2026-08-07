"""Viewport-drag and resolution-dialog operators."""

import os
import subprocess

import bpy
import blf
import gpu
from gpu_extras.batch import batch_for_shader

from .plate_mapping import (
    RegionSelection,
    PlateCamera,
    compute_plate_camera,
    apply_to_camera,
)
from .plate_layers import add_layer, rebuild_tree, material_active
from .plate_files import IMAGE_FILE_FORMATS, ensure_image_file, image_target_path

MIN_DRAW_SIZE = 8  # pixels; smaller rectangles are discarded
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MIN_PLATE_RESOLUTION = 64

HELP_FONT_SIZE = 14
HELP_COLOR = (1.0, 1.0, 0.1, 0.95)
PLATE_OUTLINE_COLOR = (1.0, 1.0, 0.1, 1.0)
PLATE_FILL_COLOR = (1.0, 1.0, 0.1, 0.25)
PLATE_COLLECTION_NAME = "_CP_CAM"
PLATE_OBJECT_PREFIX = "CP"
PLATE_CAMERA_NAME = "CP_camera"
DEFAULT_MATERIAL_NAME = "_CP_MAT"
HELP_MARGIN_X = 12
HELP_MARGIN_Y = 10
HELP_LINE_SPACING = 1.35


def _is_near_black(color) -> bool:
    """True when text shadowing would be invisible and is skipped."""
    def to_linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (to_linear(channel) for channel in color[:3])
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return luminance <= 0.05


def _draw_label(
    text: str, x: float, y: float, color=HELP_COLOR, font_scale: float = 1.0
) -> tuple[float, float]:
    font_id = 0
    blf.size(font_id, int(HELP_FONT_SIZE * font_scale))
    width, height = blf.dimensions(font_id, text)

    shadowed = not _is_near_black(color)
    if shadowed:
        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 1.0)
        blf.shadow_offset(font_id, 1, -1)
    blf.color(font_id, *color)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)
    if shadowed:
        blf.disable(font_id, blf.SHADOW)
    return width, height


# Computed while the mouse is in the viewport; the dialog (no viewport region) consumes it.
_pending_plate_camera: PlateCamera | None = None


class CameraPlateDrawOperator(bpy.types.Operator):
    bl_idname = "cameraplate.draw"
    bl_label = "Draw Camera Frame"
    bl_description = "Draw a camera frame in the viewport"
    bl_options = {"REGISTER", "UNDO"}

    _start_x: float
    _start_y: float
    _end_x: float
    _end_y: float
    _drawing: bool
    _handle: object = None

    @property
    def width(self) -> float:
        return abs(self._end_x - self._start_x)

    @property
    def height(self) -> float:
        return abs(self._end_y - self._start_y)

    def invoke(self, context, event):
        if context.region_data is None:
            self.report({"ERROR"}, "Please run this from a 3D viewport.")
            return {"CANCELLED"}
        return self._enter_modal(context, event.mouse_region_x, event.mouse_region_y)

    def modal(self, context, event):
        # Wait for the first left-mouse press inside the viewport.
        if not self._drawing:
            if event.type == "MOUSEMOVE":
                # Follow the cursor with the hint only; no rectangle is drawn
                # until the viewport press.
                self._start_x = event.mouse_region_x
                self._start_y = event.mouse_region_y
                self._end_x = event.mouse_region_x
                self._end_y = event.mouse_region_y
                context.region.tag_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                self._drawing = True
                self._start_x = event.mouse_region_x
                self._start_y = event.mouse_region_y
                self._end_x = event.mouse_region_x
                self._end_y = event.mouse_region_y
                return {"RUNNING_MODAL"}
            if event.type in {"RIGHTMOUSE", "ESC"}:
                self._cleanup()
                return {"CANCELLED"}
            return {"RUNNING_MODAL"}

        # Dragging: only the end follows, so the rectangle grows.
        if event.type == "MOUSEMOVE":
            self._end_x = event.mouse_region_x
            self._end_y = event.mouse_region_y
            context.region.tag_redraw()
            return {"RUNNING_MODAL"}

        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            self._cleanup()
            if self.width < MIN_DRAW_SIZE or self.height < MIN_DRAW_SIZE:
                return {"CANCELLED"}
            self._open_dialog(context)
            return {"FINISHED"}

        if event.type in {"RIGHTMOUSE", "ESC"}:
            self._cleanup()
            return {"CANCELLED"}

        return {"RUNNING_MODAL"}

    def execute(self, context):
        # draw() without a drag event starts from the viewport centre.
        if context.region_data is None:
            self.report({"ERROR"}, "Please run this from a 3D viewport.")
            return {"CANCELLED"}
        region = context.region
        return self._enter_modal(context, region.width / 2.0, region.height / 2.0)

    def _enter_modal(self, context, start_x, start_y):
        self._drawing = False
        self._start_x = start_x
        self._start_y = start_y
        self._end_x = start_x
        self._end_y = start_y
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw, (context,), "WINDOW", "POST_PIXEL"
        )
        context.window_manager.modal_handler_add(self)
        context.region.tag_redraw()
        return {"RUNNING_MODAL"}

    def _open_dialog(self, context):
        global _pending_plate_camera

        selection = RegionSelection(
            self._start_x, self._start_y, self._end_x, self._end_y
        )
        plate_camera = compute_plate_camera(context.region, context.region_data, selection)
        if plate_camera is None:
            self.report({"ERROR"}, "Could not derive camera parameters.")
            return
        _pending_plate_camera = plate_camera

        # Popups open at the cursor, so move it to the rectangle's centre.
        midpoint_x = (self._start_x + self._end_x) / 2.0
        midpoint_y = (self._start_y + self._end_y) / 2.0
        context.window.cursor_warp(
            int(context.region.x + midpoint_x),
            int(context.region.y + midpoint_y),
        )

        # Default plate resolution keeps the rectangle's aspect ratio.
        aspect = self.width / max(self.height, 1)
        if aspect >= 1.0:
            default_width = DEFAULT_WIDTH
            default_height = max(round(DEFAULT_WIDTH / aspect), MIN_PLATE_RESOLUTION)
        else:
            default_height = DEFAULT_HEIGHT
            default_width = max(round(DEFAULT_HEIGHT * aspect), MIN_PLATE_RESOLUTION)

        bpy.ops.cameraplate.plate_dialog(
            "INVOKE_DEFAULT", plate_width=default_width, plate_height=default_height
        )

    def _draw_help(self, context):
        dpi_scale = getattr(context.preferences.system, "dpi", 72) / 72.0
        font_scale = getattr(context.preferences.view, "ui_scale", 1.0) * dpi_scale

        if self._drawing:
            lines = ["Release LMB to confirm · Esc to cancel"]
            if self.width >= 2 and self.height >= 2:
                lines.append(
                    f"{int(self.width)}x{int(self.height)}px · "
                    f"{self.width / max(self.height, 1):.2f} aspect"
                )
        else:
            lines = ["Click and drag to draw · Esc to cancel"]
        x = self._end_x + HELP_MARGIN_X
        y = self._end_y + HELP_MARGIN_Y
        for line in lines:
            _, height = _draw_label(line, x, y, font_scale=font_scale)
            y -= height * HELP_LINE_SPACING

    def _draw(self, context):
        self._draw_help(context)

        # Preview while dragging may be smaller than MIN_DRAW_SIZE; only the
        # release decision honours that limit.
        if self.width < 2 or self.height < 2:
            return

        left = min(self._start_x, self._end_x)
        bottom = min(self._start_y, self._end_y)
        right = left + self.width
        top = bottom + self.height

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        shader.bind()
        gpu.state.blend_set("ALPHA")

        # Thick bright-yellow outline.
        gpu.state.line_width_set(3.0)
        outline = [(left, bottom), (left, top), (right, top), (right, bottom), (left, bottom)]
        shader.uniform_float("color", PLATE_OUTLINE_COLOR)
        batch_for_shader(shader, "LINE_STRIP", {"pos": outline}).draw(shader)
        gpu.state.line_width_set(1.0)

        # Fill the whole rectangle: two triangles, a proper quad split.
        fill = [
            (left, bottom), (right, bottom), (right, top),
            (left, bottom), (right, top), (left, top),
        ]
        shader.uniform_float("color", PLATE_FILL_COLOR)
        batch_for_shader(shader, "TRIS", {"pos": fill}).draw(shader)
        gpu.state.blend_set("NONE")

    def _cleanup(self):
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, "WINDOW")
            self._handle = None


class CameraPlateDialogOperator(bpy.types.Operator):
    bl_idname = "cameraplate.plate_dialog"
    bl_label = "Settings"
    bl_options = {"REGISTER", "UNDO"}

    plate_width: bpy.props.IntProperty(
        name="Width",
        description="Image width",
        default=DEFAULT_WIDTH, min=32, max=16384,
    )
    plate_height: bpy.props.IntProperty(
        name="Height",
        description="Image height",
        default=DEFAULT_HEIGHT, min=32, max=16384,
    )
    create_image: bpy.props.BoolProperty(
        name="Create Image",
        description="Create a blank image",
        default=True,
    )
    image_name: bpy.props.StringProperty(
        name="Name",
        description="Image name",
        default="",
    )
    image_alpha: bpy.props.BoolProperty(
        name="Transparent",
        description="Transparent image",
        default=True,
    )
    image_format: bpy.props.EnumProperty(
        name="Format",
        description="File format for the generated image and Quick Edit exports",
        default="EXR_16",
        items=[
            ("PNG", "PNG", ""),
            ("JPEG", "JPEG", ""),
            ("TIFF", "TIFF", ""),
            ("EXR_16", "EXR 16-bit Float", ""),
            ("EXR_32", "EXR 32-bit Float", ""),
        ],
    )
    material_mode: bpy.props.EnumProperty(
        name="Material",
        description="Where to add the projection nodes",
        items=[
            ("CREATE", "New", ""),
            ("EXISTING", "Existing", ""),
        ],
        default="CREATE",
    )
    material_name: bpy.props.StringProperty(
        name="Name",
        description="Material name; empty = _CP_MAT",
        default="",
    )
    material_choice: bpy.props.StringProperty(
        name="Material",
        description="Existing material to add the projection to",
        default="",
    )

    def invoke(self, context, event):
        if not self.image_name:
            self.image_name = f"{PLATE_OBJECT_PREFIX}_{self.plate_width}x{self.plate_height}"
        if not self.material_name:
            self.material_name = DEFAULT_MATERIAL_NAME
        return context.window_manager.invoke_props_dialog(self, width=220)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "plate_width")
        layout.prop(self, "plate_height")
        layout.prop(self, "create_image")
        box = layout.box()
        box.enabled = self.create_image
        box.prop(self, "image_name")
        box.prop(self, "image_format")
        alpha_row = box.row()
        alpha_row.enabled = self.image_format in {"PNG", "TIFF"}
        alpha_row.prop(self, "image_alpha")
        box.prop(self, "material_mode")
        if self.material_mode == "EXISTING":
            box.prop_search(self, "material_choice", bpy.data, "materials", text="")
        else:
            box.prop(self, "material_name")

    def execute(self, context):
        global _pending_plate_camera
        plate_camera = _pending_plate_camera
        _pending_plate_camera = None
        if plate_camera is None:
            self.report({"ERROR"}, "Draw camera frame first.")
            return {"CANCELLED"}
        camera_object = self._create_camera(context, plate_camera)
        if self.create_image:
            image = self._create_image(context)
            camera_object["CP_image"] = image
            self._create_material(camera_object, image)
        return {"FINISHED"}

    def _create_image(self, context):
        exr = self.image_format.startswith("EXR")
        alpha = self.image_alpha and self.image_format != "JPEG"
        float_buffer = exr
        image = bpy.data.images.new(
            name=self.image_name or PLATE_OBJECT_PREFIX,
            width=self.plate_width,
            height=self.plate_height,
            alpha=alpha or exr,
            float_buffer=float_buffer,
        )
        if float_buffer:
            image.use_half_precision = self.image_format == "EXR_16"
        image.file_format = IMAGE_FILE_FORMATS[self.image_format]
        image.colorspace_settings.name = "Non-Color" if exr else "sRGB"
        fill_alpha = 0.0 if alpha or exr else 1.0
        image.generated_type = "BLANK"
        image.generated_color = (0.0, 0.0, 0.0, fill_alpha)
        image.update()
        # Give the datablock its future disk home right away, so the image
        # carries the correct extension and connects to the Quick Edit file.
        image.filepath_raw = image_target_path(image, self.image_format)
        return image

    def _resolve_material(self, image):
        if self.material_mode == "EXISTING":
            material = bpy.data.materials.get(self.material_choice)
            if material is not None:
                return material
            return self._new_material(DEFAULT_MATERIAL_NAME)

        default_name = DEFAULT_MATERIAL_NAME
        name = self.material_name or default_name
        material = bpy.data.materials.get(name)
        return material if material is not None else self._new_material(name)

    def _create_material(self, camera_object, image):
        material = self._resolve_material(image)
        add_layer(material, image=image, camera=camera_object)
        rebuild_tree(material)
        return material

    def _new_material(self, name):
        return bpy.data.materials.new(name)

    def cancel(self, context):
        global _pending_plate_camera
        _pending_plate_camera = None

    def _create_camera(self, context, plate_camera: PlateCamera) -> bpy.types.Object:
        camera_data = bpy.data.cameras.new(PLATE_CAMERA_NAME)
        camera_object = bpy.data.objects.new(PLATE_CAMERA_NAME, camera_data)
        self._link_to_plate_collection(context, camera_object)
        apply_to_camera(camera_object, plate_camera)
        context.scene.camera = camera_object

        # The render aspect follows the plate resolution; matching the rect
        # aspect keeps the frame exact on both axes.
        context.scene.render.resolution_x = self.plate_width
        context.scene.render.resolution_y = self.plate_height
        return camera_object

    def _link_to_plate_collection(self, context, camera_object):
        collection = bpy.data.collections.get(PLATE_COLLECTION_NAME)
        if collection is None:
            collection = bpy.data.collections.new(PLATE_COLLECTION_NAME)
            context.scene.collection.children.link(collection)
        collection.objects.link(camera_object)


class CameraPlateQuickEditOperator(bpy.types.Operator):
    bl_idname = "cameraplate.quick_edit"
    bl_label = "Quick Edit"
    bl_description = "Open the active layer's image in the external editor set in Blender preferences"
    bl_options = {"REGISTER"}

    def execute(self, context):
        material = material_active(context)
        if material is None:
            self.report({"ERROR"}, "Select an object with a material.")
            return {"CANCELLED"}
        plate = material.plate
        if len(plate.layers) == 0:
            self.report({"ERROR"}, "No plate layers to edit.")
            return {"CANCELLED"}
        index = min(plate.active_layer_index, len(plate.layers) - 1)
        layer = plate.layers[index]
        if layer.image is None:
            self.report({"ERROR"}, "The active layer has no image.")
            return {"CANCELLED"}

        editor = context.preferences.filepaths.image_editor
        if not editor:
            self.report(
                {"ERROR"},
                "Set an external image editor in Preferences > File Paths.",
            )
            return {"CANCELLED"}

        try:
            path = ensure_image_file(layer)
        except Exception as exc:
            self.report({"ERROR"}, f"Could not write the image to disk: {exc}")
            return {"CANCELLED"}

        try:
            subprocess.Popen([editor, path])
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to launch editor: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class CameraPlateReloadImageOperator(bpy.types.Operator):
    bl_idname = "cameraplate.reload_image"
    bl_label = "Reload"
    bl_description = "Reload the active layer's image from disk, picking up external edits"
    bl_options = {"REGISTER"}

    def execute(self, context):
        material = material_active(context)
        if material is None:
            self.report({"ERROR"}, "Select an object with a material.")
            return {"CANCELLED"}
        plate = material.plate
        if len(plate.layers) == 0:
            self.report({"ERROR"}, "No plate layers to edit.")
            return {"CANCELLED"}
        index = min(plate.active_layer_index, len(plate.layers) - 1)
        layer = plate.layers[index]
        if layer.image is None:
            self.report({"ERROR"}, "The active layer has no image.")
            return {"CANCELLED"}
        absolute = bpy.path.abspath(layer.image.filepath)
        if not layer.image.filepath or not os.path.exists(absolute):
            self.report({"ERROR"}, "The image has no file on disk to reload.")
            return {"CANCELLED"}
        layer.image.reload()
        return {"FINISHED"}


class CameraPlateLayerRemoveOperator(bpy.types.Operator):
    bl_idname = "cameraplate.layer_remove"
    bl_label = "Remove Layer"
    bl_description = "Remove the active layer from the plate stack"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        material = material_active(context)
        if material is None:
            self.report({"ERROR"}, "Select an object with a material.")
            return {"CANCELLED"}
        plate = material.plate
        if len(plate.layers) == 0:
            return {"CANCELLED"}
        index = min(plate.active_layer_index, len(plate.layers) - 1)
        plate.layers.remove(index)
        plate.active_layer_index = min(plate.active_layer_index, len(plate.layers) - 1)
        rebuild_tree(material)
        return {"FINISHED"}


class CameraPlateLayerMoveOperator(bpy.types.Operator):
    bl_idname = "cameraplate.layer_move"
    bl_label = "Move Layer"
    bl_description = "Move the active layer up or down the stack"
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=[
            ("UP", "Up", ""),
            ("DOWN", "Down", ""),
        ],
    )

    def execute(self, context):
        material = material_active(context)
        if material is None:
            self.report({"ERROR"}, "Select an object with a material.")
            return {"CANCELLED"}
        plate = material.plate
        if len(plate.layers) < 2:
            return {"CANCELLED"}
        index = min(plate.active_layer_index, len(plate.layers) - 1)
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= target < len(plate.layers)):
            return {"CANCELLED"}
        plate.layers.move(index, target)
        plate.active_layer_index = target
        rebuild_tree(material)
        return {"FINISHED"}


OPERATORS = (
    CameraPlateDrawOperator,
    CameraPlateDialogOperator,
    CameraPlateQuickEditOperator,
    CameraPlateReloadImageOperator,
    CameraPlateLayerRemoveOperator,
    CameraPlateLayerMoveOperator,
)