"""Location candidates between static logging sites and map operations.

A site inside the cited code of exactly one operation becomes a candidate
binding by location. Operation evidence is checked first; the wider function
ranges of code explanations are used only when no operation cites the site,
because one function usually holds several atomic operations. A candidate says
where the emit is, not what business fact it records: event role, lifecycle and
correlation stay unestablished until a reviewed binding exists.
"""
from __future__ import annotations


def _valid(refs) -> list[dict]:
    return [ref for ref in refs or [] if isinstance(ref, dict) and isinstance(ref.get("path"), str)
            and isinstance(ref.get("line_start"), int) and isinstance(ref.get("line_end"), int)]


def _explanation_ranges(operation: dict) -> list[dict]:
    return _valid([ref for row in operation.get("code_explanations") or [] for ref in row.get("evidence") or []])


def _overlaps(site: dict, ref: dict) -> bool:
    return (site["path"] == ref["path"] and site["line_start"] <= ref["line_end"]
            and ref["line_start"] <= site["line_end"])


def _read(site: dict, coverage: dict[str, list]) -> bool:
    return any(start <= site["line_start"] and site["line_end"] <= end
               for start, end in coverage.get(site["path"], []))


def researched_paths(model: dict) -> list[str]:
    """Files the researcher read, falling back to files cited by operations."""
    paths = {row["path"] for row in model.get("read_coverage") or [] if isinstance(row.get("path"), str)}
    if not paths:
        paths = {ref["path"] for node in model.get("nodes") or []
                 for ref in _valid(node.get("evidence")) + _explanation_ranges(node)}
    return sorted(paths)


def link_sites(model: dict, inventory: dict, map_revision: str) -> dict:
    """Annotate sites with read and link status; return candidate bindings.

    Operations are referenced as ``(map_revision, operation_id)``; names are
    never matched. A site cited by several operations stays ambiguous.
    """
    operations = [node for node in model.get("nodes") or [] if node.get("kind") == "operation"]
    coverage = {row["path"]: [tuple(pair) for pair in row.get("ranges") or []]
                for row in model.get("read_coverage") or [] if isinstance(row.get("path"), str)}
    tiers = (("operation_evidence", lambda node: _valid(node.get("evidence"))),
             ("code_explanation", _explanation_ranges))
    sites, bindings = [], []
    for site in inventory.get("sites") or []:
        matches, basis = [], None
        for basis, ranges in tiers:
            matches = [node["id"] for node in operations if any(_overlaps(site, ref) for ref in ranges(node))]
            if matches:
                break
        status = "linked" if len(matches) == 1 else "ambiguous" if matches else "unlinked"
        sites.append({**site, "read_by_researcher": _read(site, coverage), "link_status": status,
                      "link_basis": basis if matches else None, "operation_ids": matches})
        if status == "linked":
            bindings.append({"id": "bind-" + site["id"].removeprefix("log-"), "site_id": site["id"],
                             "map_revision": map_revision, "operation_id": matches[0],
                             "provenance": "static_location", "link_basis": basis,
                             "authority_status": "proposed", "event_role": "unknown"})
    return {"sites": sites, "bindings": bindings,
            "counts": {"sites": len(sites), "linked": len(bindings),
                       "ambiguous": sum(site["link_status"] == "ambiguous" for site in sites),
                       "unlinked": sum(site["link_status"] == "unlinked" for site in sites),
                       "operations_with_sites": len({b["operation_id"] for b in bindings}),
                       "operations": len(operations)}}
