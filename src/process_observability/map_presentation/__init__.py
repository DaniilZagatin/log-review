"""Vendored presentation of the first agent's scenario map; no producer runtime is imported.

``scenario_map.py`` is the producer renderer with only its import lines changed (see
PROVENANCE.md); ``theme.py`` and ``compat.py`` hold the minimal helpers it needs.
"""
from .compat import HierarchyError, map_revision, validate_hierarchy
from .scenario_map import EMBED_HEADER_SLOT, EMBED_PANELS_SLOT, render_scenario_map, scenario_map_parts

__all__ = ["EMBED_HEADER_SLOT", "EMBED_PANELS_SLOT", "HierarchyError", "map_revision", "render_scenario_map",
           "scenario_map_parts", "validate_hierarchy"]
