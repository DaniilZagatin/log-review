"""Map-first report presentation: trusted renderer reuse, logging overlay, CSP and offline assets.

All offline. The page is inspected as text; no browser runs here, so visual layout, real focus
and pan/zoom gestures remain a separate browser check.
"""
import base64
from copy import deepcopy
import hashlib
from html import unescape
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from process_observability import map_presentation
from process_observability.logging_contracts import LEGACY_POLICY, LoggingReviewError, canonical
from process_observability.logging_report import (SHELL_JS, build_html, export_logging, logging_overlay, validate_report,
                                                  verify_export, write_report)
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.map_presentation import scenario_map_parts
from process_observability.map_presentation.scenario_map import _PRESENTATION_SCRIPT, _SCRIPT
from process_observability.operation_coverage import summarize
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE, Scripted, model_reply, operation

PACKAGE = Path(map_presentation.__file__).resolve().parent
SVG_NAMESPACE = 'http://www.w3.org/2000/svg'


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements, self.scripts, self.current = [], [], None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == 'script':
            self.current = {'attributes': dict(attrs), 'text': ''}
            self.scripts.append(self.current)

    def handle_endtag(self, tag):
        if tag == 'script': self.current = None

    def handle_data(self, value):
        if self.current is not None: self.current['text'] += value

    def data(self, identifier):
        return json.loads(next(s['text'] for s in self.scripts if s['attributes'].get('id') == identifier))

    def executable(self):
        return [s['text'] for s in self.scripts if 'type' not in s['attributes']]


def unassessed(row, missing, *, legacy=False):
    """An X row of either policy; its missing items are model hypotheses."""
    task = {'operation_id': row['operation_id'], 'kind': 'assessment', 'description': 'Синтетическая незавершённая проверка'}
    value = {'operation_id': row['operation_id'], 'state': 'X', 'expected_signal': row['expected_signal'], 'bindings': [],
             'missing': list(missing), 'changes': [], 'review_tasks': [task],
             'limitations': ['Синтетическая незавершённая проверка'], 'search': {}, 'warnings': []}
    if not legacy:
        task.update(blocking=True, scope='source'); value['missing_status'] = 'hypothesis'
    return value


class MapFirstUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); repo = self.root/'repo'; repo.mkdir(); (repo/'app.py').write_text(CODE)
        model = {'schema_version': '2.0', 'scenario': {'title': 'Обработка обращения', 'entry_operation_ids': ['save']},
                 'sources': [{'id': 'source-1'}],
                 'nodes': [operation(), operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown'),
                           operation(identifier='third', first=8, last=9)],
                 'edges': [{'id': 'e1', 'source': 'save', 'target': 'unlogged'}, {'id': 'e2', 'source': 'unlogged', 'target': 'third'}],
                 'run': {'status': 'completed', 'id': 'fixture'}}
        self.view = MapView.from_dict(model)
        data = review_logging(self.view, snapshot_repository(repo, max_files=None, max_chars=None),
                              Scripted(lambda p, n: model_reply(p, key='absent' if p['operation']['id'] == 'unlogged' else 'done')))
        rows = data['operations']
        index = next(i for i, row in enumerate(rows) if row['operation_id'] == 'third')
        rows[index] = unassessed(rows[index], ['Факт результата операции'])
        data['summary'] = summarize(self.view.operation_ids, rows)
        data['changes'] = [change for row in rows for change in row.get('changes') or []]
        data['hierarchy'] = {'map_revision': self.view.revision, 'membership': {'save': 'stage-1', 'unlogged': 'stage-1'},
                             'stages': [{'id': 'stage-1', 'name': 'Приём обращения', 'goal': 'Принять обращение',
                                         'result': 'Обращение сохранено', 'collapsible': True, 'entry_operation_id': 'save',
                                         'operation_ids': ['save', 'unlogged']}],
                             'unassigned': [{'operation_id': 'third', 'reason': 'Не отнесена к этапу'}],
                             'status': {'result': 'complete', 'reasons': []}}
        self.data = data
        self.assertTrue(validate_report(data))

    def test_report_embeds_the_trusted_renderer_instead_of_a_second_layout(self):
        page = build_html(self.data)
        scripts = Page(page).executable()
        self.assertEqual(scripts, [_PRESENTATION_SCRIPT, SHELL_JS])
        self.assertTrue(_PRESENTATION_SCRIPT.startswith(_SCRIPT[:200]), 'the extended script starts as the trusted renderer')
        for name in ('function layoutScenarioGraph(', 'function routeScenarioEdge(', 'function projectScenarioStages(',
                     'function scenarioNodeScope(', 'function applyZoom(', 'function expandStage(', 'id="viewport"',
                     'id="view-mode"', 'id="notation-toggle"', 'id="fit"', 'id="reset"', 'id="inspector"'):
            self.assertIn(name, page)
        for former in ('Math.min(i,3)', 'function projection(', 'function groupState(', 'style.zoom=scale'):
            self.assertNotIn(former, page)

    def test_every_inline_script_hash_is_in_the_csp_and_nothing_loads_from_outside(self):
        page = build_html(self.data)
        parsed = Page(page)
        policy = unescape(next(attrs['content'] for tag, attrs in parsed.elements
                               if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy'))
        scripts = parsed.executable()
        self.assertEqual(len(scripts), 2)
        for script in scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii')
            self.assertIn("'sha256-" + digest + "'", policy)
        script_src = next(part for part in policy.split(';') if part.strip().startswith('script-src'))
        self.assertNotIn('unsafe', script_src)
        for directive in ("default-src 'none'", "style-src 'unsafe-inline'", 'font-src data:', 'img-src data:',
                          "base-uri 'none'", "form-action 'none'"):
            self.assertIn(directive, policy)
        self.assertNotIn('unsafe-eval', page)
        self.assertEqual(page.count('data:font/woff2;base64,'), 3)
        for weight in (400, 600, 700):
            self.assertEqual((PACKAGE/'assets'/'dpss'/f'SBSansDisplay-{weight}.woff2').read_bytes()[:4], b'wOF2')
        stripped = page.replace(SVG_NAMESPACE, '')
        self.assertNotIn('http://', stripped)
        self.assertNotIn('https://', stripped)
        self.assertNotIn('@import', page)
        for tag, attrs in parsed.elements:
            if tag in ('script', 'img', 'link', 'iframe', 'object', 'embed', 'audio', 'video', 'source'):
                self.assertFalse(attrs.get('src') or attrs.get('href') or attrs.get('data'), tag)
        for script in scripts:
            for forbidden in ('fetch(', 'XMLHttpRequest', 'innerHTML', 'insertAdjacentHTML', 'eval(', 'new Function'):
                self.assertNotIn(forbidden, script)

    def test_overlay_is_bound_to_the_map_revision_and_a_foreign_hierarchy_is_shown_as_disabled(self):
        overlay = logging_overlay(self.data, self.view)
        self.assertEqual(overlay['map_revision'], self.view.revision)
        self.assertEqual(set(overlay['operations']), set(self.view.operation_ids))
        with self.assertRaises(ValueError):
            scenario_map_parts(self.data['map'], overlay={**overlay, 'map_revision': 'sha256:' + '1' * 64}, embedded=True)
        foreign = deepcopy(self.data); foreign['hierarchy']['map_revision'] = 'sha256:foreign'
        page = build_html(foreign)
        self.assertNotIn('id="map-hierarchy"', page)
        self.assertIn('Бизнес-этапы не показаны на карте', page)
        counts = Page(page).data('map-presentation')['overlay']
        self.assertEqual(counts['group_counts'], {})
        self.assertEqual(counts['unassigned_counts'], self.data['summary']['counts'], 'operations stay on the map')

    def test_stage_counts_partition_the_operations_once(self):
        presentation = Page(build_html(self.data)).data('map-presentation')
        overlay = presentation['overlay']
        state = {row['operation_id']: row['state'] for row in self.data['operations']}
        self.assertTrue(presentation['embedded'])
        self.assertEqual(overlay['total'], 3)
        self.assertEqual(overlay['counts'], self.data['summary']['counts'])
        self.assertEqual(overlay['group_counts']['stage-1'],
                         {key: [state['save'], state['unlogged']].count(key) for key in 'CPUX'})
        self.assertEqual(overlay['unassigned_counts'], {key: int(state['third'] == key) for key in 'CPUX'})
        self.assertEqual(sum(sum(c.values()) for c in overlay['group_counts'].values())
                         + sum(overlay['unassigned_counts'].values()), self.data['summary']['denominator'])

    def test_unassessed_missing_items_are_labelled_hypotheses_everywhere(self):
        out = self.root/'export'
        write_report(self.data, out)
        rows = (out/'operations.csv').read_text(encoding='utf-8-sig').splitlines()
        third = next(line for line in rows if line.startswith('third,'))
        self.assertIn('Не проверено; предположение модели: Факт результата операции', third)
        self.assertFalse(any('предположение модели' in line for line in rows if not line.startswith('third,')))
        overlay = logging_overlay(self.data, self.view)
        self.assertEqual(overlay['operations']['third']['missing_status'], 'hypothesis')
        self.assertEqual(overlay['operations']['third']['missing'], ['Факт результата операции'])
        page = build_html(self.data)
        self.assertIn('Предположения модели — не проверено', page)
        self.assertIn('Это гипотезы о недостающих сведениях, а не установленное отсутствие записи.', page)
        # Legacy rows without missing_status: an X row is still a hypothesis.
        legacy = deepcopy(self.data)
        next(row for row in legacy['operations'] if row['operation_id'] == 'third').pop('missing_status')
        self.assertEqual(logging_overlay(legacy, self.view)['operations']['third']['missing_status'], 'hypothesis')

    def test_legacy_policy_one_all_unassessed_bundle_still_exports_with_the_new_map(self):
        legacy = deepcopy(self.data)
        legacy['policy'] = LEGACY_POLICY
        legacy['operations'] = [unassessed(row, ['Факт возникновения операции'], legacy=True) for row in legacy['operations']]
        legacy['summary'] = summarize(self.view.operation_ids, legacy['operations'])
        legacy['changes'] = []
        run = self.root/'legacy-run'; run.mkdir()
        (run/'logging_review.json').write_text(canonical(legacy) + '\n', encoding='utf-8')
        out = self.root/'legacy-export'
        result = export_logging(run, out)
        self.assertTrue(verify_export(out))
        self.assertEqual(result['summary']['counts'], {'C': 0, 'P': 0, 'U': 0, 'X': 3})
        page = (out/'report.html').read_text(encoding='utf-8')
        self.assertIn('function layoutScenarioGraph(', page)
        self.assertIn('Не проверена: 3', page)
        self.assertIn('Подтверждённых привязок бизнес-записей пока нет', page)
        csv_rows = (out/'operations.csv').read_text(encoding='utf-8-sig').splitlines()[1:]
        self.assertEqual(len(csv_rows), 3)
        self.assertTrue(all('Не проверено; предположение модели: Факт возникновения операции' in line for line in csv_rows))

    def test_three_sections_full_registry_and_table_selection_wiring(self):
        page = build_html(self.data)
        parsed = Page(page)
        tabs = [attrs for tag, attrs in parsed.elements if attrs.get('role') == 'tab']
        self.assertEqual([tab['data-view'] for tab in tabs], ['map', 'logging', 'improvements'])
        self.assertEqual([tab['aria-controls'] for tab in tabs], ['panel-map', 'panel-logging', 'panel-improvements'])
        panels = {attrs['id']: attrs for tag, attrs in parsed.elements if attrs.get('role') == 'tabpanel'}
        self.assertEqual(set(panels), {'panel-logging', 'panel-improvements'})
        for attrs in panels.values():
            self.assertIn('hidden', attrs); self.assertIn('inert', attrs)
        sites = self.data['inventory']['sites']
        unbound = sum(site['review_status'] != 'mapped' for site in sites)
        self.assertIn(f'Полный реестр: {len(sites)} мест, {unbound} без принятой привязки', page)
        for site in sites:
            self.assertIn(f"{site['path']}:{site['start_line']}", page)
        self.assertIn('data-report-operation="save"', page)
        self.assertIn('Что ещё проверить', page)
        self.assertIn('Область и ограничения', page)
        for call in ('map.selectNode(', 'map.hidePanel()', 'map.showPanel()', 'map.onRequestMap('):
            self.assertIn(call, SHELL_JS)
        for name in ('window.scenarioMapView=Object.freeze(', 'function selectNodeById(', 'presentationRevealMap();selectNode(node,true)'):
            self.assertIn(name, _PRESENTATION_SCRIPT)
        for label in ('Цвет: бизнес-исход', 'Цвет: покрытие логами', 'Залогирована', 'Частично', 'Не залогирована', 'Не проверена'):
            self.assertIn(label, page)

    def test_saved_row_semantics_are_delegated_to_operation_coverage(self):
        with patch('process_observability.logging_report.validate_saved_rows',
                   side_effect=LoggingReviewError('delegated rule')) as delegated:
            with self.assertRaisesRegex(LoggingReviewError, 'delegated rule'):
                validate_report(self.data)
        delegated.assert_called_once()
        view, data = delegated.call_args.args
        self.assertEqual(view.revision, self.view.revision)
        self.assertIs(data, self.data)

    def test_product_packages_work_from_a_plain_copy_without_developer_checkout(self):
        import subprocess
        copy = self.root/'portable-src'
        source = Path(__file__).resolve().parents[1]/'src'
        for package in ('process_observability', 'pko'):
            shutil.copytree(source/package, copy/package, ignore=shutil.ignore_patterns('__pycache__'))
        payload = self.root/'review.json'
        payload.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
        script = """import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from process_observability.product_report import build_product_html
from process_observability.metrics import build_metrics
import pko, process_observability
root=Path(sys.argv[1]).resolve()
assert Path(pko.__file__).resolve().is_relative_to(root)
assert Path(process_observability.__file__).resolve().is_relative_to(root)
data=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
page=build_product_html(data,build_metrics(data))
assert page.count('data:font/woff2;base64,')==3
assert 'panel-metrics' in page
assert 'Цвет: покрытие логами' in page
"""
        result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-B', '-c', script, str(copy), str(payload)],
                                cwd=self.root, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_package_data_ships_the_renderer_assets(self):
        pyproject = tomllib.loads((Path(__file__).resolve().parents[1]/'pyproject.toml').read_text(encoding='utf-8'))
        patterns = pyproject['tool']['setuptools']['package-data']['process_observability.map_presentation']
        for required in ('assets/dpss/*.woff2', 'assets/horizon/tokens.css'):
            self.assertIn(required, patterns)
        for pattern in patterns:
            self.assertTrue(list(PACKAGE.glob(pattern)), pattern)
        self.assertEqual(len(list(PACKAGE.glob('assets/dpss/*.woff2'))), 3)


if __name__ == '__main__': unittest.main()
