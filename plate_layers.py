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

ROW_SPACING = 260.0
ROW_ORIGIN_Y = 120.0
TEXCOORD_X = -1500.0
GROUP_X = -1320.0
IMAGE_X = -1140.0
ALPHA_MATH_X = -1040.0
MIX_X = -840.0
STACK_MIX_X = -600.0
MATH_Y_OFFSET = 40.0
PRJ_OUT_X = -320.0
PRJ_OUT_Y = 60.0
PRJ_BASE_X = -900.0
PRJ_BASE_Y = 300.0
BSDF_X = -200.0
SHADER_Y = 100.0
OUTPUT_X = 200.0
OUTPUT_Y = 100.0

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
                socket.hide = socket.name not in keeps


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


class PROJECTIONCAM_Layer(bpy.types.PropertyGroup):
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
        description="Blend mode",
        items=BLEND_MODES,
        default="MIX",
        update=_layer_changed,
    )
    mix_factor: bpy.props.FloatProperty(
        name="Opacity",
        description="Opacity",
        default=1.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_layer_changed,
    )
    enabled: bpy.props.BoolProperty(
        name="Enabled",
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
            ("EXR_16", "EXR 16-bit", ""),
            ("EXR_32", "EXR 32-bit", ""),
        ],
    )


class PROJECTIONCAM_PlateProps(bpy.types.PropertyGroup):
    layers: bpy.props.CollectionProperty(type=PROJECTIONCAM_Layer)
    active_layer_index: bpy.props.IntProperty(
        name="Active Layer Index",
        default=0,
        min=0,
    )


PROPERTY_GROUPS = (PROJECTIONCAM_Layer, PROJECTIONCAM_PlateProps)


def material_active(context) -> bpy.types.Material | None:
    if context.object is not None:
        return context.object.active_material
    return None


def add_layer(material, image, camera) -> PROJECTIONCAM_Layer:
    """Add a new top layer (index 0 composites last)."""
    global _suppress_rebuild
    _suppress_rebuild = True
    try:
        material.plate.layers.add()
        layer = material.plate.layers[-1]
        layer.name = f"Plate {len(material.plate.layers)}"
        layer.image = image
        layer.camera = camera
        material.plate.layers.move(len(material.plate.layers) - 1, 0)
        return material.plate.layers[0]
    finally:
        _suppress_rebuild = False


def _set_projection_values(group_node, camera, image) -> None:
    """Push the camera/image numbers and facing-gate basis rows into the shared group."""
    if camera is None or camera.data is None or camera.type != "CAMERA":
        return

    size = image.size if image is not None else (0, 0)
    if (size[0] <= 0 or size[1] <= 0) and image is not None:
        # Degenerate plate (e.g. a file Blender cannot decode): refresh once,
        # then fall back to the on-disk header size so the aspect stays true.
        try:
            image.reload()
        except Exception:
            pass
        size = image.size
        if size[0] <= 0 or size[1] <= 0:
            from .plate_files import file_dimensions  # local: avoids a module cycle

            dims = file_dimensions(bpy.path.abspath(image.filepath))
            if dims is not None:
                size = dims

    pushes = [
        ("Focal Length", camera.data.lens, True),
        ("Sensor", camera.data.sensor_width, True),
        ("X", camera.data.shift_x, True),
        ("Y", camera.data.shift_y, True),
    ]
    if size[0] > 0 and size[1] > 0:
        pushes += [("X", size[0], False), ("Y", size[1], False)]
    for socket_name, value, is_float in pushes:
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


def _prj_tag(node) -> None:
    node["PRJ_own"] = True


def _remove_prj_nodes(tree) -> None:
    """Delete addon-owned nodes; PRJ_OUT and user nodes are never removed."""
    for node in list(tree.nodes):
        if node.get("PRJ_own") and not node.get("PRJ_out"):
            tree.nodes.remove(node)


def _output_bsdf(tree):
    """The BSDF feeding the Material Output's Surface; None if none."""
    output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if output is None:
        return None
    for link in output.inputs["Surface"].links:
        return link.from_node
    return None


def _first_connect(tree, reroute, material) -> None:
    """One-time PRJ_OUT -> shader color link; the user owns it from then on."""
    bsdf = _output_bsdf(tree)
    if bsdf is None:
        bsdf = next(
            (n for n in tree.nodes if n.bl_idname.startswith("ShaderNodeBsdf")), None
        )
    if bsdf is None:
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (BSDF_X, SHADER_Y)
        bsdf.select = False
        output = next((n for n in tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if output is None:
            output = tree.nodes.new("ShaderNodeOutputMaterial")
            output.location = (OUTPUT_X, OUTPUT_Y)
            output.select = False
        tree.links.new(bsdf.outputs[0], output.inputs["Surface"])
    tree.links.new(reroute.outputs[0], bsdf.inputs[0])


def _build_layer_row(
    tree, layer, y, gate_alpha: bool = False, base_socket=None
) -> tuple[bpy.types.Node, bpy.types.NodeSocket, bpy.types.NodeSocket]:
    """TexCoord -> Projector -> Image row; gate through the group's facing Mask, if present."""
    tex_node = tree.nodes.new("ShaderNodeTexCoord")
    tex_node.location = (TEXCOORD_X, y)
    tex_node.select = False
    _prj_tag(tex_node)
    # The Object output is measured in the space of the object this property
    # points at; a projector must use the layer's camera space.
    if layer.camera is not None:
        tex_node.object = layer.camera

    group_node = tree.nodes.new("ShaderNodeGroup")
    group_node.location = (GROUP_X, y)
    group_node.select = False
    group_node.node_tree = nodegroup.ensure_nodegroup()
    _prj_tag(group_node)

    image_node = tree.nodes.new("ShaderNodeTexImage")
    image_node.location = (IMAGE_X, y)
    image_node.select = False
    image_node.image = layer.image
    image_node.extension = "CLIP"
    _prj_tag(image_node)

    if group_node.node_tree is None:
        # Missing shipped group: fall back to raw object-space UVs so the
        # stack stays readable and buildable.
        tree.links.new(tex_node.outputs["Object"], image_node.inputs["Vector"])
        return image_node, image_node.outputs["Color"], image_node.outputs["Alpha"]

    _set_projection_values(group_node, layer.camera, layer.image)
    _hide_sockets(tex_node, keep_outputs=["Object"])
    _hide_sockets(
        group_node,
        keep_inputs=["Vector", "Resolution", "Shift"],
        keep_outputs=["Vector", "Mask"],
    )

    tree.links.new(tex_node.outputs["Object"], group_node.inputs["Vector"])
    tree.links.new(group_node.outputs["Vector"], image_node.inputs["Vector"])

    mask_out = group_node.outputs.get("Mask")
    if mask_out is None or layer.camera is None:
        return image_node, image_node.outputs["Color"], image_node.outputs["Alpha"]

    # Plate alpha rides along with the facing mask, else unpainted pixels paint black.
    gated_alpha = tree.nodes.new("ShaderNodeMath")
    gated_alpha.operation = "MULTIPLY"
    gated_alpha.location = (ALPHA_MATH_X, y + MATH_Y_OFFSET)
    gated_alpha.hide = True
    gated_alpha.select = False
    _prj_tag(gated_alpha)
    tree.links.new(mask_out, gated_alpha.inputs[0])
    tree.links.new(image_node.outputs["Alpha"], gated_alpha.inputs[1])

    gated_color = tree.nodes.new("ShaderNodeMix")
    gated_color.location = (MIX_X, y)
    gated_color.data_type = "RGBA"
    _prj_tag(gated_color)
    # The bottom row blends the plate over PRJ_BASE; upper rows gate over black
    # (alpha reaches their stack factor instead, avoiding double-gating).
    color_a = _socket(gated_color.inputs, "A_Color")
    if base_socket is not None:
        tree.links.new(base_socket, color_a)
        tree.links.new(gated_alpha.outputs[0], _socket(gated_color.inputs, "Factor_Float"))
    else:
        color_a.default_value = (0.0, 0.0, 0.0, 0.0)
        tree.links.new(mask_out, _socket(gated_color.inputs, "Factor_Float"))
    gated_color.select = False
    tree.links.new(image_node.outputs["Color"], _socket(gated_color.inputs, "B_Color"))

    return (
        image_node,
        _socket(gated_color.outputs, "Result_Color"),
        gated_alpha.outputs[0] if gate_alpha else image_node.outputs["Alpha"],
    )


def rebuild_tree(material) -> None:
    """Rebuild the addon-owned chain only; PRJ_OUT/PRJ_BASE outgoing wiring is user territory."""
    material.use_nodes = True
    tree = material.node_tree
    _remove_prj_nodes(tree)

    layers = list(material.plate.layers)

    base_reroute = next((n for n in tree.nodes if n.get("PRJ_base")), None)
    if base_reroute is None:
        base_reroute = tree.nodes.new("NodeReroute")
        base_reroute.name = "PRJ_BASE"
        base_reroute.location = (PRJ_BASE_X, PRJ_BASE_Y)
        base_reroute["PRJ_base"] = True
    if not base_reroute.label:
        base_reroute.label = base_reroute.name

    if layers:
        # Descending list order -> stacking order. Index 0 (topmost) is blended
        # last; the last list item is the base color underneath.
        rows = []
        for index, layer in enumerate(layers):
            y = ROW_ORIGIN_Y - ROW_SPACING * index
            is_base = index == len(layers) - 1
            base_socket = base_reroute.outputs[0] if is_base else None
            rows.append(
                (layer, _build_layer_row(tree, layer, y, not is_base, base_socket))
            )
        rows.reverse()

        # Composite bottom-up: the running color starts as the base plate, then
        # each plate above it blends itself over the running result.
        color = rows[0][1][1]
        for index, (layer, (_, layer_color, layer_alpha)) in enumerate(rows[1:], 1):
            stack_index = len(layers) - 1 - index
            row_y = ROW_ORIGIN_Y - ROW_SPACING * stack_index
            factor = tree.nodes.new("ShaderNodeMath")
            factor.location = (MIX_X, row_y + MATH_Y_OFFSET)
            factor.operation = "MULTIPLY"
            factor.use_clamp = True
            factor.hide = True
            _prj_tag(factor)
            # Disabled layers fully expose the stack below (factor 0).
            factor.inputs[1].default_value = layer.mix_factor if layer.enabled else 0.0
            tree.links.new(layer_alpha, factor.inputs[0])

            mix = tree.nodes.new("ShaderNodeMix")
            mix.location = (STACK_MIX_X, row_y)
            mix.data_type = "RGBA"
            mix.blend_type = layer.blend_mode
            _prj_tag(mix)
            tree.links.new(factor.outputs[0], _socket(mix.inputs, "Factor_Float"))
            tree.links.new(color, _socket(mix.inputs, "A_Color"))
            tree.links.new(layer_color, _socket(mix.inputs, "B_Color"))
            color = _socket(mix.outputs, "Result_Color")
        output_source = color
    else:
        output_source = base_reroute.outputs[0]

    reroute = next((n for n in tree.nodes if n.get("PRJ_out")), None)
    if reroute is None:
        reroute = tree.nodes.new("NodeReroute")
        reroute.name = "PRJ_OUT"
        reroute.location = (PRJ_OUT_X, PRJ_OUT_Y)
        reroute["PRJ_out"] = True
        _first_connect(tree, reroute, material)
    if not reroute.label:
        reroute.label = reroute.name

    # Only the final stack output may feed PRJ_OUT; nothing else is allowed.
    for link in list(tree.links):
        if link.to_node is reroute:
            tree.links.remove(link)
    tree.links.new(output_source, reroute.inputs[0])

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


def _rebuild_later() -> None:
    """Deferred rebuild: startup registration runs before bpy.data is fully ready."""
    try:
        _reload_materials()
    except Exception:
        traceback.print_exc()
    return None


@persistent
def _load_post(_dummy) -> None:
    try:
        _reload_materials()
    except Exception:
        traceback.print_exc()


def register() -> None:
    for cls in PROPERTY_GROUPS:
        bpy.utils.register_class(cls)
    bpy.types.Material.plate = bpy.props.PointerProperty(type=PROJECTIONCAM_PlateProps)
    bpy.types.Scene.prj_export_baselayer = bpy.props.BoolProperty(
        name="Export Base Layer",
        description="Bake a rendered frame from the active layer's camera when Quick Edit runs",
        default=True,
    )
    bpy.types.Scene.prj_bake_engine = bpy.props.EnumProperty(
        name="Bake Engine",
        description="Render engine",
        items=[
            ("EEVEE", "EEVEE", ""),
            ("CYCLES", "CYCLES", ""),
        ],
        default="EEVEE",
    )
    bpy.types.Scene.prj_bake_samples = bpy.props.IntProperty(
        name="Samples",
        description="Render samples",
        default=4,
        min=1,
        max=1024,
    )
    bpy.types.Scene.prj_cameras_hidden = bpy.props.BoolProperty(
        name="Projection Cameras Hidden",
        default=False,
    )

    # A saved .blend carries the old tree from the last session, so enable
    # must not trust it: swap the group and rebuild every stack up front.
    if hasattr(bpy.data, "node_groups"):
        _rebuild_later()
    else:
        bpy.app.timers.register(_rebuild_later, first_interval=1.0)

    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)


def unregister() -> None:
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    try:
        bpy.app.timers.unregister(_rebuild_later)
    except Exception:
        pass

    del bpy.types.Material.plate
    del bpy.types.Scene.prj_export_baselayer
    del bpy.types.Scene.prj_bake_engine
    del bpy.types.Scene.prj_bake_samples
    del bpy.types.Scene.prj_cameras_hidden
    for cls in reversed(PROPERTY_GROUPS):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()