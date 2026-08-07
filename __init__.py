from __future__ import annotations

import bpy

from .plate_operators import OPERATORS
from .ui import PANELS


def register():
    for operator in OPERATORS:
        bpy.utils.register_class(operator)
    for panel in PANELS:
        bpy.utils.register_class(panel)


def unregister():
    for panel in reversed(PANELS):
        bpy.utils.unregister_class(panel)
    for operator in reversed(OPERATORS):
        bpy.utils.unregister_class(operator)


if __name__ == "__main__":
    register()