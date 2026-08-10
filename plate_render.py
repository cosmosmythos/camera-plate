"""Render the scene from a layer's camera."""

import os
import traceback

import numpy as np

import bpy

from .plate_files import output_dir, write_buffer_file

CAPTURE_NODE_NAME = "PRJ_Plate_Capture"
CAPTURE_PREFIX = "prj_plate_capture"
BASELAYER_SUFFIX = "_baselayer"


def _linear_to_srgb(buffer) -> None:
    """In place; alpha passes through untouched."""
    rgb = buffer[..., :3]
    low = rgb <= 0.0031308
    rgb[low] *= 12.92
    rgb[~low] = 1.055 * np.power(rgb[~low], 1.0 / 2.4) - 0.055


def _install_capture_node(scene, directory):
    """Wire a File Output node straight to Render Layers; returns a cleanup callable."""
    tree = getattr(scene, "compositing_node_group", None)
    tree_created = tree is None
    if tree_created:
        tree = bpy.data.node_groups.new(name="Compositor", type="CompositorNodeTree")
        scene.compositing_node_group = tree

    nodes = tree.nodes
    links = tree.links
    rl = next((node for node in nodes if node.type == "R_LAYERS"), None)
    if rl is None:
        rl = nodes.new("CompositorNodeRLayers")
        rl.location = (-400, 300)

    out_node = nodes.get(CAPTURE_NODE_NAME)
    if out_node is None:
        out_node = nodes.new("CompositorNodeOutputFile")
        out_node.name = CAPTURE_NODE_NAME
        out_node.location = (600, -200)

    if hasattr(out_node.format, "media_type"):
        out_node.format.media_type = "IMAGE"
    out_node.format.file_format = "OPEN_EXR"
    out_node.format.color_depth = "16"
    out_node.format.exr_codec = "ZIPS"

    out_node.directory = directory
    out_node.file_name = CAPTURE_PREFIX
    if hasattr(out_node, "use_file_extension"):
        out_node.use_file_extension = True
    if hasattr(out_node, "file_output_items") and len(out_node.file_output_items) == 0:
        out_node.file_output_items.new("RGBA", "")

    if rl.outputs and out_node.inputs:
        src = rl.outputs[0]
        dst = out_node.inputs[0]
        if not any(link.from_socket is src and link.to_socket is dst for link in links):
            links.new(src, dst)

    def cleanup():
        tree = getattr(scene, "compositing_node_group", None)
        if tree is None:
            return
        node = tree.nodes.get(CAPTURE_NODE_NAME)
        if node is not None:
            tree.nodes.remove(node)
        if tree_created and not tree.nodes:
            scene.compositing_node_group = None
            bpy.data.node_groups.remove(tree)

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
        import numpy as np
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
        scene.use_nodes,
    )
    cleanup = None
    try:
        scene.camera = camera_object
        scene.render.engine = "BLENDER_EEVEE"
        scene.render.film_transparent = True
        width, height = layer.image.size
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        scene.render.resolution_percentage = 100
        scene.use_nodes = True

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
                                 f"{layer.name}{BASELAYER_SUFFIX}")
    except Exception:
        traceback.print_exc()
        return None
    finally:
        if cleanup is not None:
            cleanup()
        (scene.camera, scene.render.engine, scene.render.film_transparent,
         scene.render.resolution_x, scene.render.resolution_y,
         scene.render.resolution_percentage, scene.use_nodes) = saved
