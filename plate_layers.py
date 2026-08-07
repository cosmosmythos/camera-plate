"""Plate data model plus full node-stack rebuild."""

from __future__ import annotations

import traceback

import bpy
from bpy.app.handlers import persistent

from . import nodegroup

BLEND_MODES = [
    ("MIX", "Mix", ""),
    ("ADD", "Add", ""),
    ("SCREEN", "Screen", ""),
    ("MULTIPLY", "Multiply", ""),
    ("OVERLAY", "Overlay", ""),
    ("LIGHTEN", "Lighten", ""),
    ("DARKEN", "Darken", ""),
]

# Node placement mirrors the hand-arranged reference tree: TexCoord ->
# Projector -> Image per row, then a Mix chain feeding the Principled BSDF.
ROW_SPACING = 260.0
TEXCOORD_X = -560.0
GROUP_X = -380.0
IMAGE_X = -200.0
MIX_X = 150.0
BSDF_X = 450.0
PRINCIPLED_Y = -130.0
OUTPUT_X = 700.0

# add_layer sets fields one at a time; each setter triggers a rebuild, so the
# flag pauses them until the layer is fully populated.
_suppress_rebuild = False


def _group_input(group_node, name: str, is_float: bool):
    """Resolution (ints) and Shift (floats) sockets share names; the type picks the right one."""
    return next(
        (
            socket
            for socket in group_node.inputs
            if socket.name == name
            and isinstance(
                socket,
                bpy.types.NodeSocketFloat if is_float else bpy.types.NodeSocketInt,
            )
        ),
        None,
    )


def _hide_sockets(node, keep_inputs: tuple[str, ...] = (), keep_outputs: tuple[str, ...] = ()):
    for sockets, keeps in (
        (node.inputs, keep_inputs),
        (node.outputs, keep_outputs),
    ):
        for socket in sockets:
            if hasattr(socket, "hide"):
                socket.hide = socket.identifier not in keeps


def _socket(sockets, identifier: str, fallback=None) -> bpy.types.NodeSocket | None:
    """Find a socket by its unique identifier (names repeat on Mix nodes)."""
    return next(
        (socket for socket in sockets if socket.identifier == identifier),
        fallback,
    )


def _request_rebuild(material) -> None:
    if not _suppress_rebuild:
        rebuild_tree(material)


def _layer_changed(self, _context):
    material = self.id_data
    if material is not None and hasattr(material, "plate"):
        _request_rebuild(material)


class CAMERAPLATE_Layer(bpy.types.PropertyGroup):
    image: bpy.props.PointerProperty(
        name="Image",
        type=bpy.types.Image,
        update=_layer_changed,
    )
    camera: bpy.props.PointerProperty(
        name="Camera",
        type=bpy.types.Object,
        update=_layer_changed,
    )
    blend_mode: bpy.props.EnumProperty(
        name="Blend Mode",
        description="How this layer composites over the ones below",
        items=BLEND_MODES,
        default="MIX",
        update=_layer_changed,
    )
    mix_factor: bpy.props.FloatProperty(
        name="Opacity",
        description="How much of this layer to keep; scales its alpha",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_layer_changed,
    )
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description="Show this layer",
        default=True,
        update=_layer_changed,
    )
    format: bpy.props.EnumProperty(
        name="Format",
        description="File format for Quick Edit exports",
        default="EXR_16",
        items=[
            ("PNG", "PNG", ""),
            ("JPEG", "JPEG", ""),
            ("TIFF", "TIFF", ""),
            ("EXR_16", "EXR 16-bit Float", ""),
            ("EXR_32", "EXR 32-bit Float", ""),
        ],
    )


class CAMERAPLATE_PlateProps(bpy.types.PropertyGroup):
    layers: bpy.props.CollectionProperty(type=CAMERAPLATE_Layer)
    active_layer_index: bpy.props.IntProperty(
        name="Active Layer Index",
        default=0,
        min=0,
    )


PROPERTY_GROUPS = (CAMERAPLATE_Layer, CAMERAPLATE_PlateProps)


def material_active(context) -> bpy.types.Material | None:
    if context.object is not None:
        return context.object.active_material
    return None


def add_layer(material, image, camera) -> CAMERAPLATE_Layer:
    """Insert a layer at the top of the stack (index 0 composites last, i.e. painted on top)."""
    global _suppress_rebuild
    _suppress_rebuild = True
    try:
        layer = material.plate.layers.add()
        layer.name = f"Plate {len(material.plate.layers)}"
        layer.image = image
        layer.camera = camera
        material.plate.layers.move(len(material.plate.layers) - 1, 0)
    finally:
        _suppress_rebuild = False
    return layer


def _set_projection_values(group_node, camera, image) -> None:
    """Push the camera/image numbers and facing-gate basis rows into the shared group."""
    if camera is None or camera.data is None or camera.type != "CAMERA":
        return
    size = image.size if image is not None else (0, 0)
    for socket_name, value, is_float in (
        ("Focal Length", camera.data.lens, True),
        ("Sensor", camera.data.sensor_width, True),
        ("X", size[0], False),
        ("Y", size[1], False),
        ("X", camera.data.shift_x, True),
        ("Y", camera.data.shift_y, True),
    ):
        input_socket = _group_input(group_node, socket_name, is_float)
        if input_socket is not None:
            input_socket.default_value = value

    # Facing gate: world -> camera-space rotation rows. The group dots its
    # True Normal against each row to get the faced half-space.
    if group_node.node_tree is not None:
        inv = camera.matrix_world.to_3x3().inverted()
        for socket_name, basis_row in zip(("Basis X", "Basis Y", "Basis Z"), inv):
            input_socket = next(
                (socket for socket in group_node.inputs if socket.name == socket_name), None
            )
            if input_socket is not None:
                input_socket.default_value = tuple(basis_row)


def _build_layer_row(
    tree, layer, y, gate_alpha: bool = False
) -> tuple[bpy.types.Node, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """TexCoord -> Projector -> Image row; gate through the group's facing Mask, if present."""
    tex_node = tree.nodes.new("ShaderNodeTexCoord")
    tex_node.location = (TEXCOORD_X, y)
    tex_node.select = False
    # The Object output is measured in the space of the object this property
    # points at; a projector must use the layer's camera space.
    if layer.camera is not None:
        tex_node.object = layer.camera

    group_node = tree.nodes.new("ShaderNodeGroup")
    group_node.location = (GROUP_X, y)
    group_node.select = False
    group_node.node_tree = nodegroup.ensure_nodegroup()

    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.location = (IMAGE_X, y)
    image_node.select = False
    image_node.image = layer.image

    if group_node.node_tree is None:
        # Missing shipped group: fall back to raw object-space UVs so the
        # stack stays readable and buildable.
        tree.links.new(tex_node.outputs["Object"], image_node.inputs["Vector"])
        return image_node, image_node.outputs["Color"], image_node.outputs["Alpha"]

    _set_projection_values(group_node, layer.camera, layer.image)
    _hide_sockets(tex_node, keep_outputs=["Object"])
    _hide_sockets(group_node, keep_inputs=["Vector"], keep_outputs=["Vector", "Mask"])

    tree.links.new(tex_node.outputs["Object"], group_node.inputs["Vector"])
    tree.links.new(group_node.outputs["Vector"], image_node.inputs["Vector"])

    mask_out = group_node.outputs.get("Mask")
    if mask_out is None or layer.camera is None:
        return image_node, image_node.outputs["Color"], image_node.outputs["Alpha"]

    x, y = IMAGE_X + 160, y

    gated_color = tree.nodes.new("ShaderNodeMix")
    gated_color.location = (x, y - 60)
    gated_color.data_type = "RGBA"
    _socket(gated_color.inputs, "A_Color").default_value = (0.0, 0.0, 0.0, 0.0)
    _socket(gated_color.inputs, "Factor_Float").default_value = 1.0
    gated_color.select = False
    tree.links.new(mask_out, _socket(gated_color.inputs, "Factor_Float"))
    tree.links.new(image_node.outputs["Color"], _socket(gated_color.inputs, "B_Color"))

    if gate_alpha:
        gated_alpha = tree.nodes.new("ShaderNodeMath")
        gated_alpha.operation = "MULTIPLY"
        gated_alpha.location = (x + 180, y - 60)
        gated_alpha.select = False
        tree.links.new(mask_out, gated_alpha.inputs[0])
        tree.links.new(image_node.outputs["Alpha"], gated_alpha.inputs[1])
        alpha_out = gated_alpha.outputs[0]
    else:
        alpha_out = image_node.outputs["Alpha"]

    return image_node, _socket(gated_color.outputs, "Result_Color"), alpha_out


def rebuild_tree(material) -> None:
    """Rebuild from the stack, top-first: index 0 composites last, last item is the base."""
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()

    layers = list(material.plate.layers)
    if not layers:
        return

    # Descending list order -> stacking order. Index 0 (topmost) is blended
    # last; the last list item is the base color underneath.
    rows = []
    for index, layer in enumerate(layers):
        y = -(ROW_SPACING * index)
        is_base = index == len(layers) - 1
        rows.append((layer, _build_layer_row(tree, layer, y, not is_base)))
    rows.reverse()

    # Composite bottom-up: the running color starts as the base plate, then
    # each plate above it blends itself over the running result.
    color = rows[0][1][1]
    for index, (layer, (_, layer_color, layer_alpha)) in enumerate(rows[1:], 1):
        stack_index = len(layers) - 1 - index
        factor = tree.nodes.new("ShaderNodeMath")
        factor.location = (MIX_X - 150, -(ROW_SPACING * stack_index))
        factor.operation = "MULTIPLY"
        factor.use_clamp = True
        # Disabled layers fully expose the stack below (factor 0).
        factor.inputs[1].default_value = layer.mix_factor if layer.enabled else 0.0
        tree.links.new(layer_alpha, factor.inputs[0])

        mix = tree.nodes.new("ShaderNodeMix")
        mix.location = (MIX_X, -(ROW_SPACING * stack_index))
        mix.data_type = "RGBA"
        mix.blend_type = layer.blend_mode
        tree.links.new(factor.outputs[0], _socket(mix.inputs, "Factor_Float"))
        tree.links.new(color, _socket(mix.inputs, "A_Color"))
        tree.links.new(layer_color, _socket(mix.inputs, "B_Color"))
        color = _socket(mix.outputs, "Result_Color")

    bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (BSDF_X, PRINCIPLED_Y)
    output_node = tree.nodes.new("ShaderNodeOutputMaterial")
    output_node.location = (OUTPUT_X, PRINCIPLED_Y)

    tree.links.new(color, bsdf.inputs["Base Color"])
    tree.links.new(bsdf.outputs["BSDF"], output_node.inputs["Surface"])

    # Newly added nodes are selected by default; start a fresh tree unselected.
    tree.nodes.active = None
    for node in tree.nodes:
        node.select = False


def _materials_with_layers() -> list[bpy.types.Material]:
    plate_materials = []
    for material in bpy.data.materials:
        plate = getattr(material, "plate", None)
        if plate is not None and len(plate.layers) > 0:
            plate_materials.append(material)
    return plate_materials


def _reload_materials() -> None:
    """Rebuild every plate tree on group update or load; .blend files keep stale wiring."""
    nodegroup.check_update_nodegroup()
    for material in _materials_with_layers():
        rebuild_tree(material)


@persistent
def _load_post(_dummy) -> None:
    try:
        _reload_materials()
    except Exception:
        traceback.print_exc()


def register() -> None:
    for cls in PROPERTY_GROUPS:
        bpy.utils.register_class(cls)
    bpy.types.Material.plate = bpy.props.PointerProperty(type=CAMERAPLATE_PlateProps)

    # A saved .blend carries the old tree from the last session, so enable
    # must not trust it: swap the group and rebuild every stack up front.
    try:
        _reload_materials()
    except Exception:
        traceback.print_exc()

    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)


def unregister() -> None:
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)

    del bpy.types.Material.plate
    for cls in reversed(PROPERTY_GROUPS):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()