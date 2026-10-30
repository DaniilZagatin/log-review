"""No candidate fact silently disappears when the model proposes only a short change list."""
import copy
import unittest
from process_observability.instrumentation_accounting import account


class FactAccountingTests(unittest.TestCase):
    def test_cards_static_candidates_and_unaddressed_facts_are_distinct_and_exhaustive(self):
        facts = [{'id': 'a', 'instrumentation': 'unknown'}, {'id': 'b', 'instrumentation': 'found'},
                 {'id': 'c', 'instrumentation': 'unknown'}]
        cards = [{'id': 'change-a', 'fact_id': 'a'}]
        result = account(facts, cards)
        self.assertEqual(result['facts'], [
            {'fact_id': 'a', 'state': 'card_proposed', 'card_ids': ['change-a']},
            {'fact_id': 'b', 'state': 'static_candidate', 'card_ids': []},
            {'fact_id': 'c', 'state': 'unaddressed', 'card_ids': []}])
        self.assertEqual(result['unaddressed_fact_ids'], ['c'])
        self.assertIn('do not establish', result['basis'])
        self.assertNotIn('failure', str(result).casefold())

    def test_multiple_proposals_do_not_turn_into_multiple_facts_or_suppress_other_gaps(self):
        facts = [{'id': 'a', 'instrumentation': 'unknown'}, {'id': 'b', 'instrumentation': 'unknown'}]
        before = copy.deepcopy(facts)
        result = account(facts, [{'id': 'x', 'fact_id': 'a'}, {'id': 'y', 'fact_id': 'a'}])
        self.assertEqual(len(result['facts']), 2)
        self.assertEqual(result['facts'][0]['card_ids'], ['x', 'y'])
        self.assertEqual(result['unaddressed_fact_ids'], ['b'])
        self.assertEqual(facts, before)

    def test_unknown_fact_reference_is_refused(self):
        with self.assertRaises(ValueError):
            account([{'id': 'a', 'instrumentation': 'unknown'}], [{'id': 'bad', 'fact_id': 'foreign'}])
