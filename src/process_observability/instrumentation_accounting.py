"""Deterministic source-design accounting, not proof of adequate or delivered business telemetry."""

SCHEMA = 'instrumentation-fact-accounting/1'


def account(facts, cards):
    """Every candidate fact has proposed changes, a static candidate or remains visibly unaddressed.

    A static candidate does not certify delivery/quality or that no change is needed.
    No missing event is interpreted as absence/failure of the underlying operation.
    """
    by_fact = {fact['id']: [] for fact in facts}
    for card in cards:
        if card['fact_id'] not in by_fact:
            raise ValueError('Instrumentation card references an unknown fact')
        by_fact[card['fact_id']].append(card['id'])
    rows = []
    for fact in facts:
        proposed = by_fact[fact['id']]
        state = ('card_proposed' if proposed else 'static_candidate' if fact['instrumentation'] == 'found'
                 else 'unaddressed')
        rows.append({'fact_id': fact['id'], 'state': state, 'card_ids': proposed})
    return {'schema': SCHEMA, 'facts': rows,
            'unaddressed_fact_ids': [row['fact_id'] for row in rows if row['state'] == 'unaddressed'],
            'basis': 'source design accounting only; static candidates and proposed cards do not establish '
                     'executed operations, delivered records, quality or completeness'}
