import copy
import tempfile
import unittest
from pathlib import Path

from process_observability.report_sources import collect_report_sources
from process_observability.source_tools import snapshot_repository
from process_observability.report import build_html


class ReportSourceTests(unittest.TestCase):
    def test_collects_redacted_frozen_ranges_without_changing_semantic_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'app.py').write_text('password = "do-not-display-this"\nlogger.info("order_created", order_id=order.id)\n')
            snapshot=snapshot_repository(root)
            site={'path':'app.py','start_line':1,'end_line':2,'symbol':'<module>', 'event_key':'order_created'}
            bundle={'logging_inventory':{'sites':[site,copy.deepcopy(site)]}, 'instrumentation_plan':{'cards':[]}}
            before=copy.deepcopy(bundle)
            result=collect_report_sources(snapshot,bundle)
            self.assertEqual(bundle,before)
            self.assertEqual(len(result['excerpts']),1)
            self.assertEqual(result['unavailable'],[])
            evidence=result['excerpts'][0]
            self.assertNotIn('do-not-display-this',evidence['safe_quote'])
            self.assertIn('order_created',evidence['safe_quote'])
            self.assertEqual(evidence['source_anchor']['snapshot_id'],snapshot.snapshot_id)
            (root/'app.py').write_text('raise RuntimeError("must not be imported or re-read")\n')
            self.assertEqual(collect_report_sources(snapshot,bundle),result)

    def test_invalid_and_oversized_ranges_remain_unavailable_without_clipping(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'app.py').write_text('x = 1\n'*90)
            snapshot=snapshot_repository(root)
            sites=[{'path':'../secret.py','start_line':1,'end_line':1},
                   {'path':'app.py','start_line':1,'end_line':90}]
            result=collect_report_sources(snapshot,{'logging_inventory':{'sites':sites}})
            self.assertEqual(result['excerpts'],[])
            self.assertEqual(len(result['unavailable']),2)

    def test_direct_code_links_show_exact_snippet_and_ignore_nonexistent_proposed_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'app.py').write_text('logger.info("created", order_id=order.id)\n')
            snapshot=snapshot_repository(root)
            site={'path':'app.py','start_line':1,'end_line':1,'symbol':'<module>', 'event_key':'created','fields':['order_id']}
            bundle={'logging_inventory':{'sites':[site]},'process_map':{'event_mappings':[
                {'event_key':'created','business_action':'Заказ создан','lifecycle_role':'result'}]},
                'log_coverage':{'facts':[]},'instrumentation_plan':{'cards':[]}}
            bundle['report_sources']=collect_report_sources(snapshot,bundle)
            page=build_html(bundle)
            self.assertIn('href="#code-1"',page)
            self.assertIn('id="code-1"',page)
            self.assertIn('logger.info(&quot;created&quot;, order_id=order.id)',page)
            self.assertIn('Заказ создан',page)
            # No filesystem or executable links supplied by analyzed source.
            self.assertNotIn('href="file:',page)
            self.assertNotIn('href="javascript:',page)
