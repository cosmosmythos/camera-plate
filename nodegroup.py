"""Append-and-version node groups shipped in the addon's .blend.

Pattern mirrors the lensynth addon: the shipped blend loads on demand,
and each group's `description` field holds its version string. The live
group is swapped out automatically whenever the shipped version differs.
"""

import os

import bpy

GROUP_NAME = "CP_Projector"
BLEND_FILENAME = "CP_projector.blend"


def _blend_path() -> str:
    return os.path.join(os.path.dirname(__file__), BLEND_FILENAME)


def _append_nodegroup(nodegroup_name: str):
    """Return the group from the shipped blend, appending it if needed."""
    existing = bpy.data.node_groups.get(nodegroup_name)
    if existing is not None:
        return existing

    blend_path = _blend_path()
    if not os.path.exists(blend_path):
        return None
    try:
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            if nodegroup_name not in data_from.node_groups:
                return None
            data_to.node_groups = [nodegroup_name]
        return bpy.data.node_groups.get(nodegroup_name)
    except Exception:
        return None


def _shipped_version(nodegroup_name: str) -> str | None:
    """Read the shipped group's version without leaving copies behind."""
    blend_path = _blend_path()
    if not os.path.exists(blend_path):
        return None
    try:
        before = set(bpy.data.node_groups.keys())
        with bpy.data.libraries.load(blend_path, link=False) as (data_from, data_to):
            if nodegroup_name not in data_from.node_groups:
                return None
            data_to.node_groups = [nodegroup_name]

        for name in set(bpy.data.node_groups.keys()) - before:
            ng = bpy.data.node_groups[name]
            ver = ng.description.strip()
            bpy.data.node_groups.remove(ng)
            return ver or None
        return None
    except Exception:
        return None


def check_update_nodegroup(nodegroup_name: str = GROUP_NAME) -> bool:
    """Refresh the live group if it lags behind the shipped version."""
    existing = bpy.data.node_groups.get(nodegroup_name)
    if existing is None:
        return False

    shipped = _shipped_version(nodegroup_name)
    if shipped is None:
        return False

    current = existing.description.strip()
    if current == shipped:
        return False

    # Wipe any material nodes referencing the stale group before removal.
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in list(tree.nodes):
            if getattr(node, "node_tree", None) is existing:
                tree.nodes.remove(node)

    bpy.data.node_groups.remove(existing)
    new_group = _append_nodegroup(nodegroup_name)
    if new_group is not None:
        new_group.description = shipped
    return True


def ensure_nodegroup(nodegroup_name: str = GROUP_NAME):
    """Append the group and refresh if the shipped version is newer."""
    check_update_nodegroup(nodegroup_name)
    return _append_nodegroup(nodegroup_name)