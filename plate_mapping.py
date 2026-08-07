"""Viewport-selection math to camera parameters."""

from __future__ import annotations

import math
from dataclasses import dataclass

import bpy
from bpy_extras import view3d_utils
from mathutils import Vector, Matrix
from mathutils.geometry import intersect_line_plane


@dataclass
class RegionSelection:
    start_x: float
    start_y: float
    end_x: float
    end_y: float

    @property
    def width(self) -> float:
        return abs(self.end_x - self.start_x)

    @property
    def height(self) -> float:
        return abs(self.end_y - self.start_y)

    @property
    def origin_x(self) -> float:
        return min(self.start_x, self.end_x)

    @property
    def origin_y(self) -> float:
        return min(self.start_y, self.end_y)

    @property
    def center_x(self) -> float:
        return self.origin_x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.origin_y + self.height / 2.0


@dataclass
class PlateCamera:
    world_matrix: Matrix
    is_perspective: bool
    lens_mm: float
    ortho_scale: float
    shift_x: float
    shift_y: float
    sensor_mm: float


def _view_geometry(region_3d) -> tuple[Vector, Vector, Vector, Vector]:
    """(eye, screen_right, screen_up, forward) of the 3D viewport; the lens field is not a real lens."""
    eye = region_3d.view_matrix.inverted().translation
    orientation = region_3d.view_rotation
    return (
        eye,
        orientation @ Vector((1.0, 0.0, 0.0)),
        orientation @ Vector((0.0, 1.0, 0.0)),
        orientation @ Vector((0.0, 0.0, -1.0)),
    )


def _unproject_to_plane(
    region, region_3d, screen_x: float, screen_y: float, plane_point: Vector, plane_normal: Vector
) -> Vector | None:
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, Vector((screen_x, screen_y)))
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, Vector((screen_x, screen_y)))
    if origin is None or direction is None:
        return None
    far = origin + direction * 1000.0
    return intersect_line_plane(origin, far, plane_point, plane_normal)


def compute_plate_camera(
    region, region_3d, selection: RegionSelection, sensor_mm: float = 36.0
) -> PlateCamera | None:
    """Pose matches the viewport; FOV derives from the rect's world size (Space.lens is ignored)."""
    eye, screen_right, screen_up, forward = _view_geometry(region_3d)
    plane_point = eye + forward  # the sensor plane sits one world unit ahead

    def unproject(screen_x: float, screen_y: float):
        return _unproject_to_plane(region, region_3d, screen_x, screen_y, plane_point, forward)

    left = selection.origin_x
    bottom = selection.origin_y
    right = left + selection.width
    top = bottom + selection.height

    # Clamp just inside the window so rays never degenerate at the viewport edge.
    corners_world = []
    for corner_x, corner_y in ((left, bottom), (right, bottom), (left, top), (right, top)):
        clamped_x = min(max(corner_x, 1.0), region.width - 1.0)
        clamped_y = min(max(corner_y, 1.0), region.height - 1.0)
        corners_world.append(unproject(clamped_x, clamped_y))
    if any(point is None for point in corners_world):
        return None
    corners_world = [Vector(point) for point in corners_world]

    def extent(axis: Vector) -> float:
        projected = [(point - corners_world[0]) @ axis for point in corners_world]
        return max(projected) - min(projected)

    world_width = max(extent(screen_right), 1e-9)
    world_height = max(extent(screen_up), 1e-9)

    rect_center = unproject(selection.center_x, selection.center_y) or plane_point
    center_offset_right = (rect_center - plane_point) @ screen_right
    center_offset_up = (rect_center - plane_point) @ screen_up

    is_perspective = bool(region_3d.is_perspective)

    if is_perspective:
        focal_distance = 1.0  # the sensor plane sits one unit in front of the eye
        half_angle = math.atan(world_width / 2.0 / focal_distance)
        lens_mm = (sensor_mm / 2.0) / math.tan(half_angle)

        # Shift is a fraction of the sensor width; dividing by the rect's own
        # world width re-centres a rect drawn off the viewport axis.
        shift_x = center_offset_right / world_width
        shift_y = center_offset_up / world_width
        ortho_scale = 0.0
    else:
        lens_mm = 0.0
        ortho_scale = world_height  # ortho_scale is a linear world extent
        shift_x = center_offset_right / world_width * 2.0
        shift_y = center_offset_up / world_height * 2.0

    return PlateCamera(
        world_matrix=region_3d.view_matrix.inverted(),
        is_perspective=is_perspective,
        lens_mm=float(lens_mm),
        ortho_scale=float(ortho_scale),
        shift_x=float(shift_x),
        shift_y=float(shift_y),
        sensor_mm=float(sensor_mm),
    )


def apply_to_camera(camera_object: bpy.types.Object, plate_camera: PlateCamera) -> None:
    camera_object.matrix_world = plate_camera.world_matrix
    camera_data = camera_object.data
    camera_data.type = "PERSP" if plate_camera.is_perspective else "ORTHO"
    camera_data.sensor_fit = "HORIZONTAL" if plate_camera.is_perspective else "AUTO"
    camera_data.sensor_width = plate_camera.sensor_mm
    if plate_camera.is_perspective:
        camera_data.lens = plate_camera.lens_mm
    else:
        camera_data.ortho_scale = plate_camera.ortho_scale
    camera_data.shift_x = plate_camera.shift_x
    camera_data.shift_y = plate_camera.shift_y
    camera_data.dof.use_dof = False