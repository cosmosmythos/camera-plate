"""Image file paths and raw-pixel disk writes for Quick Edit."""

import os

import bpy

EXTENSIONS = {
    "PNG": "png",
    "JPEG": "jpg",
    "TIFF": "tiff",
    "EXR_16": "exr",
    "EXR_32": "exr",
}
IMAGE_FILE_FORMATS = {
    "PNG": "PNG",
    "JPEG": "JPEG",
    "TIFF": "TIFF",
    "EXR_16": "OPEN_EXR",
    "EXR_32": "OPEN_EXR",
}


def output_dir() -> str:
    """Render output directory (Blender resolves /tmp/ per-OS); app temp dir as fallback."""
    scene = bpy.context.scene
    resolved = bpy.path.abspath(scene.render.filepath) if scene is not None else ""
    if resolved and not resolved.endswith((os.sep, "/")):
        resolved = os.path.dirname(resolved)
    return resolved if resolved else bpy.app.tempdir


def image_target_path(base_name, format_key) -> str:
    """Predictable path in the render output directory, named after the base name."""
    directory = output_dir()
    os.makedirs(directory, exist_ok=True)
    base = "".join(c for c in base_name if c.isalnum() or c in "_-.") or "layer"
    return os.path.join(directory, f"{base}.{EXTENSIONS.get(format_key, 'png')}")


def file_dimensions(path: str) -> tuple[int, int] | None:
    """Header-only size read; works even when Blender cannot decode the file."""
    try:
        import OpenImageIO as oiio
        inp = oiio.ImageInput.open(path)
        if inp is None:
            return None
        try:
            spec = inp.spec()
        finally:
            inp.close()
        if spec.width > 0 and spec.height > 0:
            return (spec.width, spec.height)
    except Exception:
        pass
    return None


def write_buffer_file(buffer, width, height, format_key: str, base_name: str) -> str:
    """Write a bottom-up RGBA float32 buffer raw; Blender's own save() would color-manage it."""
    fmt = format_key
    path = image_target_path(base_name, fmt)

    try:
        import numpy as np
        import OpenImageIO as oiio

        pixels = np.ascontiguousarray(buffer, dtype=np.float32).reshape(height, width, 4)

        if fmt.startswith("EXR"):
            # EXR is bottom-up like the buffer: no flip.
            data = pixels
            bit_depth = "half" if fmt == "EXR_16" else "float"
            spec = oiio.ImageSpec(width, height, 4, bit_depth)
            spec.attribute("compression", "zip")
            spec.channelnames = ("R", "G", "B", "A")
        else:
            # PNG/JPEG/TIFF are top-down: flip rows.
            rgba = np.ascontiguousarray(pixels[::-1, :, :])
            channels = 3 if fmt == "JPEG" else 4  # JPEG has no alpha
            data = np.ascontiguousarray((np.clip(rgba[..., :channels], 0.0, 1.0) * 255.0).astype(np.uint8))
            spec = oiio.ImageSpec(width, height, channels, "uint8")

        out = oiio.ImageOutput.create(path)
        if out is None or not out.open(path, spec):
            raise RuntimeError("OpenImageIO could not open the target file.")
        ok = out.write_image(data)
        out.close()
        if not ok:
            raise RuntimeError("OpenImageIO write failure.")
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    return path


def write_image_file(image, format_key: str, force: bool = False) -> str:
    """Write the image's pixels; an existing on-disk file is kept unless force. Raises on failure."""
    raw = image.filepath
    if not force and raw:
        absolute = bpy.path.abspath(raw)
        if os.path.exists(absolute):
            return absolute

    import numpy as np
    width, height = image.size
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"image '{image.name}' has no pixel data (0x0); "
            "the plate file is unreadable - restore it or rebuild the plate."
        )
    pixels = np.array(image.pixels, dtype=np.float32).reshape(height, width, 4)
    path = write_buffer_file(pixels, width, height, format_key, image.name)

    # Make Blender aware of the hand-written file so Reload can find it.
    image.filepath_raw = path
    image.source = "FILE"
    image.file_format = IMAGE_FILE_FORMATS.get(format_key, "PNG")
    return path


def ensure_image_file(layer) -> str:
    """Write the layer's plate image; the on-disk paint is never overwritten."""
    return write_image_file(layer.image, layer.format)
