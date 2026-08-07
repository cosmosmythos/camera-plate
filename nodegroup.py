"""Append-and-version the shipped ``PRJ_Projector`` node group."""

from __future__ import annotations

import os
import re
import traceback

import bpy

GROUP_NAME = "PRJ_Projector"
BLEND_FILENAME = "PRJ_Projector.blend"

# Matches Blender's auto-suffixed duplicates: "PRJ_Projector.001".
_ORPHAN_SUFFIX = re.compile(r"^" + re.escape(GROUP_NAME) + r"\.\d+$")


def _blend_path() -> str:
    return os.path.join(os.path.dirname(__file__), BLEND_FILENAME)


def _append_nodegroup(nodegroup_name: str) -> bpy.types.NodeTree | None:
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
        traceback.print_exc()
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
            group = bpy.data.node_groups[name]
            version = group.description.strip()
            bpy.data.node_groups.remove(group)
            return version or None
        return None
    except Exception:
        traceback.print_exc()
        return None


def _version_tuple(description: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", description or "")
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _is_outdated(current: str, shipped: str) -> bool:
    """True when the live group lags behind the shipped version; newer hand-edited groups survive."""
    current_version = _version_tuple(current)
    shipped_version = _version_tuple(shipped)
    if current_version is None or shipped_version is None:
        return current != shipped
    return shipped_version > current_version


def _find_group_references(
    existing: bpy.types.NodeTree,
) -> list[tuple[bpy.types.NodeTree, bpy.types.Node]]:
    references = []
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in tree.nodes:
            if getattr(node, "node_tree", None) is existing:
                references.append((tree, node))
    return references


def cleanup_orphan_copies() -> bool:
    """Remove unused ``PRJ_Projector.001``-style leftovers (safe: only removed when nothing uses them)."""
    removed_any = False
    for group in list(bpy.data.node_groups):
        if not _ORPHAN_SUFFIX.match(group.name):
            continue
        if group.users > 0:
            continue
        bpy.data.node_groups.remove(group)
        removed_any = True
    return removed_any


def check_update_nodegroup(nodegroup_name: str = GROUP_NAME) -> bool:
    """Bring the live group up to date; re-points references first, then rebuilds every plate stack."""
    from .plate_layers import _materials_with_layers, rebuild_tree  # late import: avoids a cycle

    existing = bpy.data.node_groups.get(nodegroup_name)
    if existing is None:
        return False

    shipped = _shipped_version(nodegroup_name)
    if shipped is None:
        return False

    current = existing.description.strip() if existing.description else ""
    if not _is_outdated(current, shipped):
        return False

    # Capture references before the old group is removed.
    references = _find_group_references(existing)
    plate_materials = _materials_with_layers()

    bpy.data.node_groups.remove(existing)
    replacement = _append_nodegroup(nodegroup_name)
    if replacement is None:
        return True
    replacement.description = shipped

    # Re-point surviving references; plate stacks are wiped and rebuilt below anyway.
    for parent_tree, group_node in references:
        if group_node.name not in parent_tree.nodes:
            continue
        if group_node.node_tree is None:
            group_node.node_tree = replacement

    for material in plate_materials:
        try:
            rebuild_tree(material)
        except Exception:
            traceback.print_exc()

    cleanup_orphan_copies()
    return True


def ensure_nodegroup(nodegroup_name: str = GROUP_NAME) -> bpy.types.NodeTree | None:
    return _append_nodegroup(nodegroup_name)