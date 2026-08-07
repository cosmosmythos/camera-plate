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


def image_target_path(image, format_key) -> str:
    """Predictable path under Blender's temp dir, named after the image."""
    directory = bpy.app.tempdir
    os.makedirs(directory, exist_ok=True)
    base = "".join(c for c in image.name if c.isalnum() or c in "_-.") or "layer"
    return os.path.join(directory, f"{base}.{EXTENSIONS.get(format_key, 'png')}")


def ensure_image_file(layer) -> str:
    """Write raw pixels untouched; Blender's own save() would color-manage them. Raises on failure."""
    image = layer.image
    raw = image.filepath
    if raw:
        absolute = bpy.path.abspath(raw)
        if os.path.exists(absolute):
            return absolute

    fmt = layer.format
    path = image_target_path(image, fmt)

    try:
        import numpy as np
        import OpenImageIO as oiio

        width, height = image.size
        pixels = np.array(image.pixels, dtype=np.float32).reshape(height, width, 4)

        if fmt.startswith("EXR"):
            # EXR is bottom-up like Blender's pixel buffer: no flip.
            data = np.ascontiguousarray(pixels)
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
            raise RuntimeError("OpenImageIO reported a write failure.")
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    # Make Blender aware of the hand-written file so Reload can find it.
    image.filepath_raw = path
    image.source = "FILE"
    image.file_format = IMAGE_FILE_FORMATS.get(fmt, "PNG")
    return path
