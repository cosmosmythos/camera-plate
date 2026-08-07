"""Viewport-drag and resolution-dialog operators."""

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

MIN_DRAW_SIZE = 32  # pixels; smaller rectangles are discarded
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MIN_PLATE_RESOLUTION = 64

HELP_FONT_SIZE = 13
HELP_COLOR = (1.0, 1.0, 0.1, 0.95)
PLATE_OUTLINE_COLOR = (1.0, 1.0, 0.1, 1.0)
PLATE_FILL_COLOR = (1.0, 1.0, 0.1, 0.25)
PLATE_COLLECTION_NAME = "_CP_"
PLATE_OBJECT_PREFIX = "CP"
HELP_MARGIN_X = 12
HELP_MARGIN_Y = 10
HELP_LINE_SPACING = 1.35


def _is_near_black(color) -> bool:
    """True when a color's perceived luminance is near-black, so the text
    shadow would be invisible and is skipped."""
    def to_linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (to_linear(channel) for channel in color[:3])
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return luminance <= 0.05


def _draw_label(
    text: str, x: float, y: float, color=HELP_COLOR, font_scale: float = 1.0
) -> tuple[float, float]:
    """Draw shadowed text, auto-skip the shadow on dark colors."""
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


# Computed while the mouse is in the viewport; the dialog (no viewport
# region) consumes it.
_pending_plate_camera: PlateCamera | None = None


class CameraPlateDrawOperator(bpy.types.Operator):
    """Add a camera that frames the rectangle."""

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
                # Follow the cursor with the hint only: keep start == end
                # so no rectangle is drawn until the viewport press.
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
        # Starting via the UI goes through invoke(); execute() keeps
        # `bpy.ops.cameraplate.draw()` usable when no event is available.
        if context.region_data is None:
            self.report({"ERROR"}, "Please run this from a 3D viewport.")
            return {"CANCELLED"}
        region = context.region
        return self._enter_modal(context, region.width / 2.0, region.height / 2.0)

    def _enter_modal(self, context, start_x, start_y):
        """Set the initial cursor spot, add the modal + draw handlers."""
        # The rectangle only starts on the first click; the hint overlay
        # is already up so the user knows what to do.
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

        # Popups open at the cursor, so move it to the rect centre.
        midpoint_x = (self._start_x + self._end_x) / 2.0
        midpoint_y = (self._start_y + self._end_y) / 2.0
        context.window.cursor_warp(
            int(context.region.x + midpoint_x),
            int(context.region.y + midpoint_y),
        )

        # The default plate resolution keeps the rectangle's aspect ratio.
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
        """Overlay hints under the cursor: what to do next, current size."""
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
    """Confirm the plate resolution, then create the camera."""

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
    image_float: bpy.props.BoolProperty(
        name="32 bit",
        description="32 bit float",
        default=True,
    )

    def invoke(self, context, event):
        if not self.image_name:
            self.image_name = f"{PLATE_OBJECT_PREFIX}_{self.plate_width}x{self.plate_height}"
        return context.window_manager.invoke_props_dialog(self, width=200)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "plate_width")
        layout.prop(self, "plate_height")
        layout.prop(self, "create_image")
        box = layout.box()
        box.enabled = self.create_image
        box.prop(self, "image_name")
        box.prop(self, "image_alpha")
        box.prop(self, "image_float")

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
        return {"FINISHED"}

    def _create_image(self, context):
        image = bpy.data.images.new(
            name=self.image_name or PLATE_OBJECT_PREFIX,
            width=self.plate_width,
            height=self.plate_height,
            alpha=self.image_alpha,
            float_buffer=self.image_float,
        )
        fill_alpha = 0.0 if self.image_alpha else 1.0
        image.generated_type = "BLANK"
        image.generated_color = (0.0, 0.0, 0.0, fill_alpha)
        image.update()
        return image

    def cancel(self, context):
        global _pending_plate_camera
        _pending_plate_camera = None

    def _create_camera(self, context, plate_camera: PlateCamera) -> bpy.types.Object:
        camera_data = bpy.data.cameras.new(PLATE_OBJECT_PREFIX)
        camera_object = bpy.data.objects.new(PLATE_OBJECT_PREFIX, camera_data)
        self._link_to_plate_collection(context, camera_object)
        apply_to_camera(camera_object, plate_camera)
        context.scene.camera = camera_object

        # The render aspect follows the plate resolution; matching the rect
        # aspect keeps the frame exact on both axes.
        context.scene.render.resolution_x = self.plate_width
        context.scene.render.resolution_y = self.plate_height
        return camera_object

    def _link_to_plate_collection(self, context, camera_object):
        """Ensure the '_CP_' collection exists and link the camera into it."""
        collection = bpy.data.collections.get(PLATE_COLLECTION_NAME)
        if collection is None:
            collection = bpy.data.collections.new(PLATE_COLLECTION_NAME)
            context.scene.collection.children.link(collection)
        collection.objects.link(camera_object)


OPERATORS = (CameraPlateDrawOperator, CameraPlateDialogOperator)