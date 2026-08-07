"""Extension entry point; wires registration together."""

from __future__ import annotations

import bpy

from .plate_layers import register as register_plate_layers
from .plate_layers import unregister as unregister_plate_layers
from .plate_operators import OPERATORS
from .ui import PANELS


def register():
    register_plate_layers()
    for operator in OPERATORS:
        bpy.utils.register_class(operator)
    for panel in PANELS:
        bpy.utils.register_class(panel)


def unregister():
    for panel in reversed(PANELS):
        bpy.utils.unregister_class(panel)
    for operator in reversed(OPERATORS):
        bpy.utils.unregister_class(operator)
    unregister_plate_layers()


if __name__ == "__main__":
    register()