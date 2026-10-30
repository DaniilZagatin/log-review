"""Source delivery, not a semantic oracle: existing fields must be visible to the card author."""
from test_direct_atomic import AtomicCase, Business, atomic_handlers
from process_observability import workflow
from types import SimpleNamespace


class InstrumentationSourceContextTests(AtomicCase):
    def test_fact_source_precedes_unrelated_hashes_under_a_tight_request_budget(self):
        run = object.__new__(workflow._Run)
        run.review = SimpleNamespace(map_mode='atomic', business_request_chars=14000)
        run.code_model = object()
        run.full_system = lambda system: system
        run.token_fits = lambda *args, **kwargs: True
        run.context_omissions = {}
        # The unrelated row sorts first by ID and cannot fit. The small fact source can fit.
        own = {'id': 'zzz-fact', 'safe_quote': 'logger.info("selected", item_id, status)'}
        unrelated = {'id': 'aaa-other', 'safe_quote': 'x' * 14000}
        run.evidence = {item['id']: SimpleNamespace(to_dict=lambda item=item: dict(item))
                        for item in (unrelated, own)}
        coverage = SimpleNamespace(facts=[SimpleNamespace(evidence_ids=['zzz-fact'])])
        context = run.fit('TASK: instrumentation', {},
                          [('evidence', run.instrumentation_evidence(coverage), 'bundle.json')])
        self.assertEqual(context['evidence'], [own])
        self.assertEqual(context['context_omissions']['evidence']['omitted'], 1)

    def test_atomic_instrumentation_receives_verified_redacted_quotes(self):
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business)
        [call] = [call for call in business.calls if call['task'] == 'TASK: instrumentation']
        supplied = call['brief']['evidence']
        by_id = {item['id']: item for item in bundle['evidence']}
        self.assertTrue(supplied)
        self.assertTrue(any(item.get('safe_quote') for item in supplied),
                        'Paths and heuristic fields alone cannot show what an existing log contains')
        for item in supplied:
            self.assertEqual(item, by_id[item['id']])
            self.assertEqual(item['text_validation'], 'verified')
            self.assertEqual(item['semantic_validation'], 'unverified')
        self.assertEqual(call['brief']['context_omissions']['evidence']['supplied'], len(supplied))
        self.assertNotIn('get_token(', str(supplied))  # the frozen source is already redacted
        self.assertTrue(all(value['status'] == 'completed' for value in bundle['manifest']['stages'].values()))

    def test_atomic_tail_instructions_distinguish_identity_grain_and_observed_boundaries(self):
        business = Business(**atomic_handlers())
        self.run_case(business)
        systems = {call['task']: call['system'] for call in business.calls}
        self.assertIn('heuristic field', systems['TASK: instrumentation'])
        self.assertIn('mutually exclusive', systems['TASK: instrumentation'])
        self.assertIn('one counting grain', systems['TASK: metrics'])
        self.assertIn('human work start', systems['TASK: metrics'])
        # Legacy strict-demo prompts retain the historical request identity.
        self.assertNotIn('one counting grain', workflow.METRICS_SYSTEM)
