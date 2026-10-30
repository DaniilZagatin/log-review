"""Nearby calls are source navigation, never automatic business bindings."""
from pathlib import Path
import tempfile
import unittest

from process_observability.map_input import MapView, RepoContext
from process_observability.source_tools import snapshot_repository
from process_observability.log_discovery import discover_logging
from process_observability.logging_review import _Navigator, ReviewOptions

PYTHON = '''import logging
journal=logging.getLogger(__name__)
def appointment_details(key):
    journal.info("appointment key %s",key)
    return gateway.details(key)
def collect():
    item=gateway.next()
    if item is None:
        journal.info("none available")
        return
    return appointment_details(item.key)
'''
JAVA = '''import org.slf4j.Logger;
class Library {
    Logger journal;
    void describe(String key) {
        journal.info("book key {}",key);
        catalog.details(key);
    }
    void collect() {
        var book=catalog.next();
        if (book==null) {
            journal.info("empty shelf");
            return;
        }
        describe(book.key);
    }
}
'''


class ContextReuseControls(unittest.TestCase):
    def packet(self,path,code,first,last,options=None):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/path).write_text(code)
            view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
                'nodes':[{'id':'receive','kind':'operation','name':'Получить объект','evidence':[
                    {'path':'source-1/'+path,'line_start':first,'line_end':last,
                     'quote':'\n'.join(code.splitlines()[first-1:last])}]}],
                'edges':[],'run':{'id':'synthetic','status':'completed'}})
            snap=snapshot_repository(root,max_files=None,max_chars=None,logging_calls=['journal.info'])
            context=RepoContext(snap,view);context.logging_contracts=[]
            inventory=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,
                                       max_hints=None,include_columns=True,exclude_declarations=True)
            return _Navigator(context,inventory,options or ReviewOptions()).initial(view.operations[0])

    def test_existing_log_in_next_called_method_is_delivered_but_not_bound(self):
        for path,code,first,last,emit in [('Clinic.py',PYTHON,7,10,4),('Library.java',JAVA,9,13,5)]:
            with self.subTest(path=path):
                packet=self.packet(path,code,first,last)
                site=next(s for s in packet['candidates'] if s['start_line']==emit)
                self.assertEqual(site['candidate_scope'],'navigation_context')
                self.assertNotIn(site['id'],packet['required_candidate_ids'])
                self.assertNotIn('bindings',packet)
                self.assertTrue(packet['downstream_context']['delivered'])

    def test_uninvoked_function_is_not_added_by_name_or_proximity(self):
        code=PYTHON.replace('return appointment_details(item.key)','return item')
        packet=self.packet('Clinic.py',code,7,10)
        self.assertFalse(any(s['start_line']==4 for s in packet['candidates']))

    def test_budget_omission_is_explicit_and_never_complete(self):
        packet=self.packet('Clinic.py',PYTHON,7,10,ReviewOptions(max_context_lines=6))
        frontier=packet['downstream_context']['frontier']
        self.assertTrue(any(x['reason']=='context_line_budget' for x in frontier))
        self.assertFalse(packet['search']['complete'])

    def test_four_log_bearing_callees_keep_an_explicit_frontier(self):
        lines=['import logging','journal=logging.getLogger(__name__)']
        for i in range(4):lines.extend([f'def stage{i}(item):',f'    journal.info("stage {i} %s", item)'])
        lines.extend(['def collect(item):','    item.apply()'])
        lines.extend(f'    stage{i}(item)' for i in range(4))
        packet=self.packet('Media.py','\n'.join(lines)+'\n',12,12)
        self.assertEqual(len(packet['downstream_context']['delivered']),3)
        self.assertEqual(len(packet['downstream_context']['frontier']),1)
        self.assertEqual(packet['downstream_context']['frontier'][0]['reason'],'context_call_limit')
        self.assertFalse(packet['search']['complete'])

    def test_known_deeper_record_is_not_reported_as_absent(self):
        code=('import logging\njournal=logging.getLogger(__name__)\n'
              'def leaf(key):\n    journal.info("key %s",key)\n'
              'def wrapper(key):\n    leaf(key)\n'
              'def collect(key):\n    key.apply()\n    wrapper(key)\n')
        packet=self.packet('Archive.py',code,8,8)
        self.assertTrue(any(e['reason']=='context_depth_limit' for e in packet['downstream_context']['frontier']))
        self.assertFalse(packet['search']['complete'])


if __name__=='__main__':unittest.main()
