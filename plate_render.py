"""Render the scene from a layer's camera."""

import os
import traceback
import numpy as np
import bpy
from .plate_files import output_dir, write_buffer_file

CAPTURE_NODE_NAME = "PRJ_Plate_Capture"
CAPTURE_PREFIX = "prj_plate_capture"
BASELAYER_NAME = "PRJ_baselayer"
RENDER_SAMPLES = 4


def _eevee_engine_id() -> str:
    """The EEVEE engine id for this Blender: 4.2-4.4 shipped EEVEE Next under
    BLENDER_EEVEE_NEXT, 5.0 restored BLENDER_EEVEE; ask the enum instead of guessing."""
    engine_items = bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
    return "BLENDER_EEVEE" if "BLENDER_EEVEE" in engine_items else "BLENDER_EEVEE_NEXT"


def _linear_to_srgb(buffer) -> None:
    """In place; alpha passes through untouched."""
    rgb = buffer[..., :3]
    low = rgb <= 0.0031308
    rgb[low] *= 12.92
    rgb[~low] = 1.055 * np.power(rgb[~low], 1.0 / 2.4) - 0.055


def _install_capture_node(scene, directory):
    """Wire a File Output node straight to Render Layers; returns a cleanup callable."""
    use_nodes_changed = False
    if hasattr(scene, "compositing_node_group"):
        # Blender 5.x: the compositing tree is an explicit scene property.
        tree = scene.compositing_node_group
        tree_created = tree is None
        if tree_created:
            tree = bpy.data.node_groups.new(name="Compositor", type="CompositorNodeTree")
            scene.compositing_node_group = tree
    else:
        # Blender 4.x: the tree is embedded in the scene and its pointer is
        # read-only; the use_nodes toggle decides whether the compositor runs.
        use_nodes_changed = not scene.use_nodes
        if use_nodes_changed:
            scene.use_nodes = True
        tree = scene.node_tree
        tree_created = False

    nodes = tree.nodes
    links = tree.links
    created_nodes = []
    render_layers_node = next((node for node in nodes if node.type == "R_LAYERS"), None)
    if render_layers_node is None:
        render_layers_node = nodes.new("CompositorNodeRLayers")
        render_layers_node.location = (-400, 0)
        created_nodes.append(render_layers_node)

    out_node = nodes.get(CAPTURE_NODE_NAME)
    if out_node is None:
        out_node = nodes.new("CompositorNodeOutputFile")
        out_node.name = CAPTURE_NODE_NAME
        out_node.location = (200, 0)
        created_nodes.append(out_node)

    if hasattr(out_node.format, "media_type"):
        out_node.format.media_type = "IMAGE"
    out_node.format.file_format = "OPEN_EXR"
    out_node.format.color_depth = "16"
    out_node.format.exr_codec = "ZIPS"

    if hasattr(scene, "compositing_node_group"):
        # Blender 5.x names the path/filename fields directly on the node.
        out_node.directory = directory
        out_node.file_name = CAPTURE_PREFIX
        if hasattr(out_node, "use_file_extension"):
            out_node.use_file_extension = True
        if hasattr(out_node, "file_output_items") and len(out_node.file_output_items) == 0:
            out_node.file_output_items.new("RGBA", "")
    else:
        # Blender 4.x keeps them on base_path and the per-slot path property.
        out_node.base_path = directory
        out_node.file_slots[0].path = CAPTURE_PREFIX

    if render_layers_node.outputs and out_node.inputs:
        src = render_layers_node.outputs[0]
        dst = out_node.inputs[0]
        if not any(link.from_socket is src and link.to_socket is dst for link in links):
            links.new(src, dst)

    def cleanup():
        for node in created_nodes:
            if node.name in tree.nodes:
                tree.nodes.remove(node)
        stale = tree.nodes.get(CAPTURE_NODE_NAME)
        if stale is not None:
            tree.nodes.remove(stale)
        if tree_created:
            if hasattr(scene, "compositing_node_group"):
                scene.compositing_node_group = None
                if tree.name in bpy.data.node_groups:
                    bpy.data.node_groups.remove(tree)
        elif use_nodes_changed:
            scene.use_nodes = False

    return cleanup


def _resolve_capture_path(directory, frame) -> str | None:
    """Locate the compositor-written EXR: frame-padded, bare, or any prefix match."""
    candidates = [
        os.path.join(directory, f"{CAPTURE_PREFIX}{frame:04d}.exr"),
        os.path.join(directory, f"{CAPTURE_PREFIX}.exr"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    try:
        for name in os.listdir(directory):
            if name.startswith(CAPTURE_PREFIX) and name.endswith(".exr"):
                return os.path.join(directory, name)
    except OSError:
        pass
    return None


def _read_capture(path):
    """Read the capture EXR into a bottom-up (H, W, 4) float32 buffer; None on failure."""
    try:
        import OpenImageIO as oiio

        inp = oiio.ImageInput.open(path)
        if inp is None:
            return None
        spec = inp.spec()
        width, height = spec.width, spec.height
        arr = inp.read_image(0, 0, 0, 4, "float")
        inp.close()
        if arr is None:
            return None
        return np.ascontiguousarray(arr, dtype=np.float32).reshape(height, width, 4)
    except Exception:
        return None


def _remove_capture_files(directory, frame) -> None:
    for name in (f"{CAPTURE_PREFIX}{frame:04d}.exr", f"{CAPTURE_PREFIX}.exr"):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def bake_baselayer(context, layer) -> str | None:
    """Render the scene from the layer's camera as a reference image file; None on failure."""
    scene = context.scene
    camera_object = layer.camera
    if camera_object is None or layer.image is None:
        return None

    directory = output_dir()
    frame = scene.frame_current
    saved = (
        scene.camera,
        scene.render.engine,
        scene.render.film_transparent,
        scene.render.resolution_x,
        scene.render.resolution_y,
        scene.render.resolution_percentage,
        scene.eevee.taa_render_samples,
        scene.use_nodes,
    )
    cleanup = None
    try:
        scene.camera = camera_object
        engine_choice = getattr(scene, "prj_bake_engine", "EEVEE")
        samples = getattr(scene, "prj_bake_samples", RENDER_SAMPLES)
        scene.render.engine = "CYCLES" if engine_choice == "CYCLES" else _eevee_engine_id()
        scene.render.film_transparent = True
        # This bake is a projection reference, not a beauty pass: the user's
        # sample count stays bounded so Quick Edit does not stall.
        if engine_choice == "CYCLES":
            scene.cycles.samples = samples
        else:
            scene.eevee.taa_render_samples = samples
        width, height = layer.image.size
        if width <= 0 or height <= 0:
            # Degenerate plate (e.g. a file Blender cannot decode): refresh once,
            # then fall back to the on-disk header size so the bake stays true.
            try:
                layer.image.reload()
            except Exception:
                pass
            size = layer.image.size
            if size[0] <= 0 or size[1] <= 0:
                from .plate_files import file_dimensions  # local: avoids a module cycle

                dims = file_dimensions(bpy.path.abspath(layer.image.filepath))
                if dims is not None:
                    width, height = dims
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100

        os.makedirs(directory, exist_ok=True)
        # A stale capture must never be read as fresh if the render fails to write.
        _remove_capture_files(directory, frame)
        cleanup = _install_capture_node(scene, directory)

        bpy.ops.render.render(write_still=False)

        capture_path = _resolve_capture_path(directory, frame)
        if capture_path is None:
            return None

        buffer = _read_capture(capture_path)
        _remove_capture_files(directory, frame)
        if buffer is None:
            return None

        if not layer.format.startswith("EXR"):
            _linear_to_srgb(buffer)
        return write_buffer_file(buffer, buffer.shape[1], buffer.shape[0], layer.format,
                                 BASELAYER_NAME)
    except Exception:
        traceback.print_exc()
        return None
    finally:
        if cleanup is not None:
            cleanup()
        (scene.camera, scene.render.engine, scene.render.film_transparent,
         scene.render.resolution_x, scene.render.resolution_y,
         scene.render.resolution_percentage, scene.eevee.taa_render_samples,
         scene.use_nodes) = saved
