"""Attach frozen, redacted source excerpts after all semantic/model work is complete.

Presentation data only: no new source access, semantic approval or checkpoint identity change.
"""
from .source_tools import SourceError


def collect_report_sources(snapshot, bundle):
    inventory = bundle.get('logging_inventory') or {}
    cards = (bundle.get('instrumentation_plan') or {}).get('cards') or []
    refs = [{k: item.get(k) for k in ('path', 'start_line', 'end_line', 'symbol')}
            for item in inventory.get('sites', []) if isinstance(item, dict)]
    refs += [{k: card['source_anchor'].get(k) for k in ('path', 'start_line', 'end_line', 'symbol')}
             for card in cards if isinstance(card, dict) and isinstance(card.get('source_anchor'), dict)]
    excerpts, unavailable, seen = [], [], set()
    for ref in refs:
        if ref.get('symbol') is None:
            ref['symbol'] = '<module>'
        identity = tuple(ref.get(k) for k in ('path', 'start_line', 'end_line', 'symbol'))
        try:
            if identity in seen:
                continue
            seen.add(identity)
            excerpts.append(snapshot.evidence(ref).to_dict())
        except (SourceError, TypeError) as exc:
            unavailable.append({'ref': ref, 'reason': str(exc)})
    return {'schema': 'report-sources/1', 'snapshot_id': snapshot.snapshot_id,
            'excerpts': excerpts, 'unavailable': unavailable}
