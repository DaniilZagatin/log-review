"""Independent source-view wording regressions; fixtures synthetic, no target execution."""
import unittest
import re

from process_observability.report import build_html
from test_operation_inspector import InspectorCase, card_of, number_of, operation_ids, section, text_of


class LogLabelHonestyTests(InspectorCase):
    def test_no_linked_logs_does_not_claim_an_exact_emit_site(self):
        bundle=self.inspected()
        bundle['process_map']['event_mappings']=[]
        bundle['atomic_map']['event_mappings']=[]
        words=text_of(section(card_of(build_html(bundle),1),'logs'))
        self.assertIn('Связанных записей',words)
        self.assertNotIn('Связь по точному месту',words)
        self.assertNotIn('Есть связи по ключу',words)

    def test_fact_links_do_not_assert_that_a_configured_call_observes_them(self):
        bundle=self.inspected()
        for key in ('process_map','atomic_map'):
            for mapping in bundle[key]['event_mappings']:
                mapping['role_support']='no_static_emit'
        page=build_html(bundle)
        self.assertNotIn('Наблюдает факты',page)
        self.assertIn('Связанные факты',page)
        ids=operation_ids(bundle)
        card=card_of(page,number_of(bundle,ids['refund']))
        self.assertIn('refund.event',text_of(section(card,'logs')))
        self.assertIn('href="#fact-',section(card,'logs'))

    def test_logging_table_result_role_without_emit_support_stays_unconfirmed(self):
        bundle=self.inspected()
        for mapping in bundle['atomic_map']['event_mappings']:
            mapping['role_support']='no_static_emit'
        rows=re.findall(r'<tr(?:\s[^>]*)?>.*?</tr>',build_html(bundle),re.S)
        result_rows=[row for row in rows if 'refund.event' in row and 'Связанные факты' in row
                     and ':17–17' in text_of(row)]
        self.assertTrue(result_rows)
        for row in result_rows:
            self.assertIn('прямая запись в журнал здесь не подтверждена', text_of(row))
            self.assertNotIn('не подтверждает новый результат', text_of(row))


if __name__=='__main__':
    unittest.main()
