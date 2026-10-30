"""Public research obligations and model-authored semantic review.

Code validates provenance, coverage and staleness. It does not certify the
truth of a model's business interpretation or discover every operation.
"""
from __future__ import annotations

import hashlib
import json
import re

from pko.audit import sanitize
from pko.scenario_map.code_references import dependencies, symbol_valid

SEMANTIC_CHECKS = ("outcomes", "order", "routing", "atomicity", "dependencies")
EFFECT_KINDS = ("decision", "recommendation", "command", "result", "unknown")


class QualityError(ValueError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def text(value, field, limit=3000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise QualityError(f"{field}: требуется непустая строка до {limit} символов")
    return str(sanitize(value.strip()))


def ids(value, field, existing):
    if not isinstance(value, list) or len(value) > 1500 or any(not isinstance(x, str) for x in value):
        raise QualityError(f"{field}: требуется список ID")
    if len(set(value)) != len(value) or set(value) - set(existing):
        raise QualityError(f"{field}: повторяющиеся или неизвестные ID")
    return list(value)


class ResearchQuality:
    @staticmethod
    def business_text(value, field, limit=3000):
        value = text(value, field, limit)
        # Only unmistakable code syntax is rejected; natural business acronyms
        # and product names remain legitimate. This is not semantic validation.
        if re.search(r"`|\b\w+\.\w+\(|\b\w+\(\)|\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b[a-z]+[A-Z][A-Za-z]*\b|[<>]|[=!]=|&&|\|\||\b[A-Za-z_]\w*\s*[<>]=?\s*\d|\.(?:java|py|ts|js)(?:\b|:)", value):
            raise QualityError(f"{field}: напишите смысл бизнес-языком; код, имена функций и технические идентификаторы оставьте в основаниях")
        return value

    @staticmethod
    def _overlap(a, b):
        return a['path'] == b['path'] and a['line_start'] <= b['line_end'] and b['line_start'] <= a['line_end']

    def external_interactions(self, rows, operation_evidence):
        if not isinstance(rows, list) or len(rows) > 12:
            raise QualityError("external_interactions: ожидается до 12 взаимодействий")
        result = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'name','purpose','direction','endpoint','contract','evidence'}:
                raise QualityError("external_interactions: неверная структура")
            refs = self._evidence(row.get('evidence', []))
            if not refs or not any(self._overlap(a, b) for a in refs for b in operation_evidence):
                raise QualityError("Внешнее взаимодействие требует прочитанных оснований вызова, связанных с операцией")
            if row.get('direction') not in ('request','send','receive'):
                raise QualityError("direction: request, send или receive")
            endpoint, contract = row.get('endpoint', ''), row.get('contract', '')
            if not isinstance(endpoint, str) or not isinstance(contract, str) or not (endpoint.strip() or contract.strip()):
                raise QualityError("Внешнее взаимодействие требует endpoint или contract; имя класса не доказывает вызов")
            result.append({'name': text(row.get('name'), 'external.name', 300),
                           'purpose': self.business_text(row.get('purpose'), 'external.purpose', 1500),
                           'direction': row['direction'], 'endpoint': str(sanitize(endpoint.strip())),
                           'contract': str(sanitize(contract.strip())), 'evidence': refs})
        return result

    def _dependency_files(self, task):
        if task.get('implementation'):
            return sorted({ref['path'] for ref in task['implementation']})
        owner = task['target'].rsplit('.', 1)[0] if '.' in task['target'] else task['target']
        basename = owner.rsplit('.', 1)[-1]
        candidates = [p for p in self._files if p.endswith('/'+basename+'.java') or p.endswith('/'+basename+'.py')]
        origin = (task.get('evidence') or [{}])[0].get('path', '')
        if origin.endswith('.java'):
            caller = '\n'.join(self._reads.get(origin, {}).get(n, '') for n in sorted(self._reads.get(origin, {})))
            imports = re.findall(r'(?m)^\s*import\s+([\w.]+\.'+re.escape(basename)+r')\s*;', caller)
            if imports:
                return [p for p in candidates if any(p.endswith('/'+name.replace('.', '/')+'.java') for name in imports)]
            same_package = [p for p in candidates if p.rsplit('/',1)[0] == origin.rsplit('/',1)[0]]
            if same_package:
                return same_package
        return candidates

    def _dependency_implementation(self, task, evidence):
        if task.get('implementation'):
            # A locally defined executor: its definition was delivered with the
            # call site, so it is read once every line of the range was read.
            return [self._range(ref) for ref in task['implementation']
                    if all(line in self._reads.get(ref['path'], {}) for line in range(ref['line_start'], ref['line_end'] + 1))]
        symbol = task['target'].rsplit('.', 1)[-1]
        files = self._dependency_files(task)
        # Identical class/function names in other packages are not proof that a
        # call resolves there. Read imports or the declaration to disambiguate.
        if len(files) != 1:
            return []
        return [ref for ref in evidence if ref['path'] == files[0] and
                symbol_valid(symbol, 'field' if task['kind']=='prompt' else 'function', ref['quote'])]

    def _nodes_cover_dependency(self, node_ids, implementation):
        return any(any(a['path']==b['path'] and a['line_start']<=b['line_end'] and b['line_start']<=a['line_end']
                       for a in node['evidence'] for b in implementation)
                   for node in self.model['nodes'] if node['id'] in node_ids)

    def require_coverage(self, node_id, field, citations, scope):
        missing = []
        for ref in citations:
            numbers = [line for line in range(ref['line_start'], ref['line_end']+1)
                       if not any(a['path']==ref['path'] and a['line_start']<=line<=a['line_end'] for a in scope)]
            missing.extend({"path": ref['path'], "line_start": a, "line_end": b} for a,b in self._ranges(numbers))
        if missing:
            locations = ', '.join(f"{r['path']}:{r['line_start']}-{r['line_end']}" for r in missing)
            raise QualityError(f"{node_id}: обзор не покрывает основания {field}; добавьте в область обзора: {locations}",
                               node_id=node_id, field=field, missing_evidence=missing,
                               recovery="inspect_map(section='nodes'); затем дополните evidence обзора всеми missing_evidence")

    def code_explanations(self, value, evidence):
        if not isinstance(value, list) or len(value) > 12:
            raise QualityError("code_explanations: ожидается до 12 пояснений")
        result = []
        for row in value:
            if not isinstance(row, dict) or set(row) != {"kind", "symbol", "explanation", "evidence"}:
                raise QualityError("code_explanations: нужны kind, symbol, explanation, evidence")
            refs = self._evidence(row["evidence"])
            if not refs or len({r['path'] for r in refs}) != 1:
                raise QualityError("Пояснение относится к одному прочитанному файлу")
            if not any(a['path'] == b['path'] and a['line_start'] <= b['line_end'] and b['line_start'] <= a['line_end']
                       for a in refs for b in evidence):
                raise QualityError("Пояснение должно пересекаться с основаниями объекта")
            name = row['symbol']
            if not isinstance(name, str) or len(name) > 200 or not symbol_valid(name, row['kind'], '\n'.join(r['quote'] for r in refs)):
                raise QualityError("Имя функции/поля не найдено в объявлении прочитанного фрагмента; прочитайте объявление или используйте fragment/lambda с пустым symbol")
            result.append({"kind": row['kind'], "symbol": name, "explanation": text(row['explanation'], 'explanation', 1000), "evidence": refs})
        return result

    def discover_work(self, path):
        for item in dependencies(path, self._reads.get(path, {})):
            key = (item['target'], item['evidence'][0]['path'], item['evidence'][0]['line_start'])
            automatic = [t for t in self.model['research_tasks'] if t.get('automatic')]
            if any((t['target'], t['evidence'][0]['path'], t['evidence'][0]['line_start']) == key or
                   any(self._range(ref) == item['evidence'][0] for ref in t['evidence']) and t['target'] == item['target']
                   for t in automatic):
                continue
            if not item.get('implementation'):
                # The same implementation called from several places is one unit
                # of research. Merge only when both resolve to exactly the same
                # file: same-named classes in other packages stay separate. Each
                # call site is kept, and repeated calls remain separate operations.
                files = self._dependency_files(item)
                same = next((t for t in automatic if t['target'] == item['target'] and t['kind'] == item['kind'] and
                             not t.get('implementation') and len(files) == 1 and self._dependency_files(t) == files and
                             len(t['evidence']) < 12), None)
                if same is not None:
                    same['evidence'] = same['evidence'] + self._evidence(item['evidence'][:1])
                    continue
            if len(self.model['research_tasks']) >= 500:
                self.model['run']['research_tasks_overflow'] = True
                break
            self._counters['task'] += 1
            self.model['research_tasks'].append({"id": f"task-{self._counters['task']}", **item,
                "evidence": self._evidence(item['evidence']), "status": "open", "resolution": "",
                "node_ids": [], "gap_ids": [], "child_task_ids": [], "automatic": True})

    def _task_mapping_error(self, task, seen=None):
        """Validate the local assertion; dependency completeness is computed separately."""
        implementation = self._dependency_implementation(task, task['evidence'])
        if task.get('automatic') and not implementation:
            files = self._dependency_files(task)
            where = (f"реализация ожидается в {', '.join(files[:3])}; прочитайте её объявление" if len(files) == 1 else
                     f"возможные реализации: {', '.join(files[:3])}; по импортам определите нужную и прочитайте её" if files else
                     'реализации нет в переданных источниках: зафиксируйте пробел missing_source либо обоснуйте out_of_scope')
            return f'mapped требует прочитанной реализации зависимости, а не места её вызова; {where}'
        children = task.get('child_task_ids', [])
        index = {t['id']: t for t in self.model['research_tasks']}
        if children:
            for identifier in children:
                child = index.get(identifier)
                if not child:
                    return f'неизвестная дочерняя задача {identifier}'
                if task.get('automatic') and not any(self._overlap(ref, scope) for ref in child['evidence'] for scope in implementation):
                    return 'дочерняя задача не обнаружена в прочитанной реализации родителя'
        elif task.get('automatic') and not self._nodes_cover_dependency(task['node_ids'], implementation):
            return 'действия карты должны иметь основания в реализации зависимости'
        if not task['node_ids'] and not children:
            return 'mapped требует действия карты в node_ids или дочерние задачи; чтение файла не означает раскрытие исполнения'

    def research_coverage(self):
        """Finite reachability over task dependencies; no copied parent status or gaps.

        A local mapped assertion alone is not proof of coverage. Its independently
        scoped inventory must be current and every discovered action represented.
        Cycles share findings through reachability, never manufacture completeness.
        """
        index = {t['id']: t for t in self.model['research_tasks']}
        nodes = {n['id']: n for n in self.model['nodes']}
        edges = {e['id']: e for e in self.model['edges']}
        adjacency, local, represented = {}, {}, {}
        for identifier, task in index.items():
            implementation = self._dependency_implementation(task, task['evidence'])
            scope = implementation or task['evidence']
            children = set(task.get('child_task_ids', []))
            children.update(t['id'] for t in index.values() if t.get('automatic') and t['id'] != identifier
                            and any(self._overlap(ref, area) for ref in t['evidence'] for area in implementation))
            adjacency[identifier] = children.intersection(index)
            issues, actions = [], []
            status = task['status']
            if status == 'out_of_scope':
                issue = self._task_exclusion_error(task)
                if issue: issues.append(issue)
            elif status == 'mapped':
                issue = self._task_mapping_error(task)
                if issue: issues.append(issue)
                relevant = [r for r in self.model['execution_reviews'] if self._within(scope, r['evidence'])]
                if not relevant:
                    issues.append('нет актуального перечня действий обследованного участка')
                for review in relevant:
                    stale = any(n not in nodes or self._fingerprint(nodes[n]) != fp for n, fp in review['node_fingerprints'].items())
                    incident = {e['id'] for e in edges.values() if e['source'] in review['node_fingerprints'] or e['target'] in review['node_fingerprints']}
                    stale = stale or incident != set(review['edge_fingerprints']) or any(
                        e not in edges or self._fingerprint(edges[e]) != fp for e, fp in review['edge_fingerprints'].items())
                    if stale: issues.append(f"{review['id']}: обзор участка устарел")
                    actions.extend(op for op in review['operations'] if any(self._overlap(a, b) for a in op['evidence'] for b in scope))
                    pending_routes = [r for r in review.get('route_expectations', [])
                                      if not r['edge_ids'] and not r['completion_node_id']
                                      and any(self._overlap(a, b) for a in r['evidence'] for b in scope)]
                    if pending_routes:
                        issues.append('не все найденные условия отображены: ' + ', '.join(r['id'] for r in pending_routes))
                if any(not op.get('node_id') or op['node_id'] not in nodes for op in actions):
                    issues.append('не все найденные действия отображены')
                if not actions and not children:
                    issues.append('нет перечня самостоятельных действий')
            elif status == 'gap':
                issue = self._task_gap_error(task)
                if issue: issues.append(issue)
                issues.append('участок содержит адресный пробел')
            else:
                issues.append(f'локальная работа не завершена: {status}')
            local[identifier] = {'issues': list(dict.fromkeys(issues)), 'action_ids': sorted({op['id'] for op in actions})}
            represented[identifier] = any(op.get('node_id') in nodes for op in actions)
        reached_by = {}
        for identifier in index:
            reached, todo = set(), [identifier]
            while todo:
                current = todo.pop()
                if current in reached: continue
                reached.add(current)
                if index[current]['status'] != 'out_of_scope':
                    todo.extend(adjacency[current] - reached)
            reached_by[identifier] = reached
        for identifier, task in index.items():
            cyclic = task['status'] != 'out_of_scope' and any(
                identifier in reached_by[child] for child in adjacency[identifier])
            if cyclic and not any(represented[child] for child in reached_by[identifier]):
                local[identifier]['issues'].append('круговая делегация без достижимого отображённого действия')
        rows = []
        for identifier, task in index.items():
            reached = reached_by[identifier]
            incomplete = sorted(n for n in reached if local[n]['issues'])
            gap_ids = sorted({g for n in reached for g in index[n].get('gap_ids', [])})
            rows.append({'task_id': identifier, 'local_status': task['status'],
                         'status': 'partial' if incomplete else 'excluded' if task['status']=='out_of_scope' else 'covered',
                         'dependency_ids': sorted(adjacency[identifier]), 'incomplete_task_ids': incomplete,
                         'gap_ids': gap_ids, 'local_issues': local[identifier]['issues'],
                         'action_ids': local[identifier]['action_ids']})
        return {'status': 'unknown' if not rows else 'partial' if any(r['status']=='partial' for r in rows) else 'covered',
                'tasks': rows, 'semantic_completeness_verified': False}

    def _task_exclusion_error(self, task):
        """An exclusion is a visible scope decision, not a way to close mapped work."""
        exclusions = [value for value in self.model['scenario'].get('exclusions', [])]
        if not exclusions:
            return ('out_of_scope требует исключения границы: сначала зафиксируйте в set_scope(exclusions), '
                    'какой соседний вариант или часть процесса не исследуется и почему')
        if task.get('exclusion') not in exclusions:
            return 'exclusion — одно из исключений set_scope: ' + ' | '.join(exclusions[:12])
        if task['node_ids']:
            return f"задача отражена операциями {', '.join(task['node_ids'])}: закройте её как mapped"
        implementation = self._dependency_implementation(task, task['evidence']) if task.get('automatic') else []
        covering = sorted({node['id'] for node in self.model['nodes'] for ref in node['evidence']
                           for scope in implementation if self._overlap(ref, scope)})
        if covering:
            return f"реализация зависимости уже отражена операциями {', '.join(covering[:12])}: закройте задачу как mapped"
        return None

    def _task_gap_error(self, task):
        gaps = {g['id']: g for g in self.model['gaps']}
        if not task['gap_ids']:
            return 'gap требует зарегистрированный gap_ids'
        for identifier in task['gap_ids']:
            gap = gaps.get(identifier)
            if not gap or task['id'] not in gap.get('task_ids', []):
                return 'gap должен явно адресовать эту задачу через task_ids'
            if not any(self._overlap(a, b) for a in gap.get('evidence', []) for b in task['evidence']):
                return 'gap требует основания, относящиеся к конкретной зависимости'
            if self._dependency_files(task):
                if gap['category'] == 'missing_source':
                    return 'реализация зависимости есть в переданных файлах; нельзя закрыть её отсутствием внешнего кода'
                # An unexplored dependency is honest unfinished work inside the variant, read or not.
                if gap['category'] != 'unexplored' and not self._dependency_implementation(task, task['evidence']):
                    return 'локальная зависимость ещё не прочитана; прочитайте объявление перед фиксацией неизвестного поведения'
        return None

    def _save_research_task(self, args):
        old = next((x for x in self.model['research_tasks'] if x['id'] == args.get('id')), None)
        identifier = self._id('research_tasks', 'task', args.get('id'), 500)
        status = args.get('status', 'open')
        if status not in ('open', 'read', 'analyzed', 'mapped', 'out_of_scope', 'gap'):
            raise QualityError("status: open, read, analyzed, mapped, out_of_scope или gap")
        evidence = self._evidence(args.get('evidence', []))
        if not evidence:
            raise QualityError("Задаче исследования нужны прочитанные основания")
        if old and old.get('automatic'):
            evidence = self._evidence(old['evidence'] + [r for r in evidence if r not in old['evidence']])
        node_ids = ids(args.get('node_ids', []), 'node_ids', [n['id'] for n in self.model['nodes']])
        gap_ids = ids(args.get('gap_ids', []), 'gap_ids', [g['id'] for g in self.model['gaps']])
        child_ids = ids(args.get('child_task_ids', []), 'child_task_ids', [t['id'] for t in self.model['research_tasks']])
        if identifier in child_ids:
            raise QualityError('Задача не может быть своей дочерней зависимостью')
        item = {"id": identifier, "target": text(args.get('target'), 'target', 500),
                "kind": text(args.get('kind'), 'kind', 80), "status": status,
                "resolution": text(args.get('resolution'), 'resolution') if status != 'open' else '',
                "evidence": evidence, "node_ids": node_ids, "gap_ids": gap_ids,
                "child_task_ids": child_ids, "automatic": bool(old and old.get('automatic'))}
        if status == 'out_of_scope':
            item['exclusion'] = args.get('exclusion') if isinstance(args.get('exclusion'), str) else None
        if old and old.get('implementation'):
            # A discovered local definition keeps its location across updates.
            item['implementation'] = old['implementation']
        if old and old.get('automatic') and (item['target'] != old['target'] or item['kind'] != old['kind']):
            raise QualityError("Нельзя менять цель или вид автоматически обнаруженной зависимости")
        if status in ('read', 'analyzed') and item['automatic'] and not self._dependency_implementation(item, evidence):
            raise QualityError('Для read/analyzed прочитайте объявление зависимости', target=item['target'])
        reason = (self._task_mapping_error(item) if status == 'mapped' else self._task_gap_error(item) if status == 'gap'
                  else self._task_exclusion_error(item) if status == 'out_of_scope' else None)
        if reason:
            raise QualityError(reason, target=item['target'])
        return self._put('research_tasks', item)

    def quality_signature(self):
        # Ignore transient run metadata; public interpretation and evidence matter.
        # research_brief is excluded: its freshness against new reads is checked
        # separately (read_signature), and it does not change routes or outcomes.
        state = {k: self.model[k] for k in ('scenario', 'nodes', 'edges', 'gaps',
                 'research_tasks', 'execution_reviews', 'source_reviews')}
        state['nodes'] = [self._fingerprint(row) for row in self.model['nodes']]
        state['edges'] = [self._fingerprint(row) for row in self.model['edges']]
        return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _review_semantics(self, args):
        checks = args.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(SEMANTIC_CHECKS):
            raise QualityError('checks: нужны outcomes, order, routing, atomicity, dependencies')
        normalized, problems = {}, []

        def problem(field, message, **details):
            problems.append({'field': field, 'problem': message, **details})

        for name, row in checks.items():
            try:
                if not isinstance(row, dict) or set(row) - {'status','explanation','evidence','node_ids','edge_ids','gap_ids'}:
                    raise QualityError(f'{name}: неверная структура смысловой проверки')
                if row.get('status') not in ('reviewed', 'uncertain'):
                    raise QualityError(f'{name}: status reviewed или uncertain')
                refs = self._evidence(row.get('evidence', []))
                if not refs:
                    raise QualityError(f'{name}: нужны прочитанные основания проверки')
                value = {"status": row['status'], "explanation": text(row.get('explanation'), name), "evidence": refs}
                for key, collection in (('node_ids','nodes'),('edge_ids','edges'),('gap_ids','gaps')):
                    value[key] = ids(row.get(key, []), key, [x['id'] for x in self.model[collection]])
                if row['status'] == 'uncertain' and not value['gap_ids']:
                    raise QualityError(f'{name}: неустановленное поведение требует gap_ids')
                normalized[name] = value
            except ValueError as exc:
                problem('checks.'+name, str(exc), **getattr(exc, 'details', {}))
        ends = set(self.model['scenario'].get('terminal_operation_ids', []))
        if 'outcomes' in normalized and not ends.issubset(normalized['outcomes']['node_ids']):
            problem('checks.outcomes', 'outcomes должен рассмотреть каждый конечный узел', missing_node_ids=sorted(ends-set(normalized['outcomes']['node_ids'])))
        required_nodes = self._reviewable_operations()
        if 'atomicity' in normalized and required_nodes - set(normalized['atomicity']['node_ids']):
            problem('checks.atomicity', 'atomicity должен рассмотреть каждую операцию', missing_node_ids=sorted(required_nodes-set(normalized['atomicity']['node_ids'])))
        dependency_gaps = {g for task in self.model['research_tasks'] if task['status']=='gap' for g in task['gap_ids']}
        if 'dependencies' in normalized and dependency_gaps and (normalized['dependencies']['status'] != 'uncertain' or dependency_gaps-set(normalized['dependencies']['gap_ids'])):
            problem('checks.dependencies', 'dependencies: нераскрытые зависимости требуют uncertain и всех адресных gap_ids')
        edges = {e['id']: e for e in self.model['edges'] if e['kind'] != 'data'}
        routing = normalized.get('routing')
        addressed = set()
        if routing is not None:
            missing = set(edges) - set(routing['edge_ids'])
            addressed = {e for g in self.model['gaps'] if g['id'] in routing['gap_ids'] for e in g.get('edge_ids', [])}
            if missing - addressed:
                problem('checks.routing', 'routing должен рассмотреть каждый переход либо адресовать его gap', missing_edge_ids=sorted(missing-addressed))
        cases = None
        try:
            cases = self._route_cases(args.get('route_cases', []))
        except ValueError as exc:
            problem('route_cases', str(exc), **getattr(exc, 'details', {}))
        # Invalid cases or routing references cause one primary diagnostic, not
        # a cascade of uncovered-path errors built from an incomplete parse.
        if cases is not None and routing is not None:
            covered_edges = {identifier for case in cases for identifier in case['edge_ids']}
            fanout = {e['source'] for e in edges.values() if sum(x['source']==e['source'] for x in edges.values()) > 1}
            uncovered = [e['id'] for e in edges.values() if e['source'] in fanout and e['id'] not in covered_edges and e['id'] not in addressed]
            if uncovered:
                problem('route_cases', 'route_cases: разберите конкретные случаи для всех исходящих ветвей', missing_edge_ids=uncovered)
            for node in self.model['nodes']:
                if node['id'] not in ends or not node.get('completion_condition'):
                    continue
                addressed_node = any(g['id'] in routing['gap_ids'] and node['id'] in g['node_ids'] for g in self.model['gaps'])
                if addressed_node:
                    if routing['status'] != 'uncertain':
                        problem('checks.routing', 'routing: неизвестное продолжение условного завершения требует uncertain', node_id=node['id'])
                    continue
                stops = any(case['path'][-1] == node['id'] for case in cases)
                continues = any(node['id'] in case['path'][:-1] for case in cases)
                has_outgoing = any(e['source']==node['id'] for e in edges.values())
                if not has_outgoing or not stops or not continues:
                    problem('route_cases', 'route_cases: условное завершение требует отдельного случая остановки и случая продолжения либо адресного gap', node_id=node['id'])
        if problems:
            # Keep the established detail keys for consumers of a single issue;
            # callers can now fix all independent issues in one resubmission.
            details = {}
            for row in problems:
                for key, value in row.items():
                    if key not in ('field', 'problem') and key not in details:
                        details[key] = value
            details.update(issues=problems[:30], problem_count=len(problems))
            raise QualityError('; '.join(p['problem'] for p in problems[:20]), **details)
        self.model['semantic_review'] = {"checks": normalized, "route_cases": cases, "status": "model_reviewed_not_verified",
            "signature": self.quality_signature(), "brief_revision": (self.model['research_brief'] or {}).get('revision')}
        return {"status": "model_reviewed_not_verified", "semantic_verification": False}

    @staticmethod
    def _range(ref):
        return {'path': ref['path'], 'line_start': ref['line_start'], 'line_end': ref['line_end']}

    def _route_cases(self, rows):
        if not isinstance(rows, list) or len(rows) > 500:
            raise QualityError('route_cases: ожидается до 500 конкретных случаев')
        result, problems = [], []
        node_index = {n['id']: n for n in self.model['nodes']}
        edges = [e for e in self.model['edges'] if e['kind'] != 'data']
        pairs = {(e['source'], e['target']) for e in edges}
        for number, row in enumerate(rows, 1):
            # Validate every case and report all problems at once: one wrong
            # route must not cost a full resubmission per discovered error.
            name = row.get('name', '') if isinstance(row, dict) else ''

            def problem(field, message, **extra):
                problems.append({'case_index': number, 'case': str(name)[:200], 'field': field, 'problem': message, **extra})
            if not isinstance(row, dict) or set(row) - {'name','input','path','expected_result','evidence','edge_ids'} or not {'name','input','path','expected_result','evidence'}.issubset(row):
                problem('route_cases', 'нужны name, input, path, expected_result, evidence')
                continue
            path = row['path']
            if not isinstance(path, list) or not 1 <= len(path) <= 1000 or any(not isinstance(n,str) or n not in node_index for n in path):
                problem('path', 'нужны существующие операции в порядке выполнения',
                        unknown_ids=[n for n in path if not isinstance(n, str) or n not in node_index][:10] if isinstance(path, list) else [])
                continue
            missing_pairs = [[a, b] for a, b in zip(path, path[1:]) if (a, b) not in pairs]
            if missing_pairs:
                problem('path', 'между этими операциями нет перехода', missing_transitions=missing_pairs[:10])
                continue
            try:
                refs = self._evidence(row['evidence'])
            except (QualityError, ValueError) as exc:
                problem('evidence', str(exc))
                continue
            if not refs:
                problem('evidence', 'нужны прочитанные основания входной ситуации и ожидаемого результата')
                continue
            selected = row.get('edge_ids')
            if selected is not None and (not isinstance(selected, list) or len(selected) != len(path)-1):
                problem('edge_ids', 'нужен один переход для каждой пары соседних операций пути')
                continue
            traversed, ambiguous = [], []
            for i, (a, b) in enumerate(zip(path, path[1:])):
                candidates = [e for e in edges if e['source']==a and e['target']==b and (selected is None or e['id']==selected[i])]
                if len(candidates) != 1:
                    ambiguous.append({'from': a, 'to': b, 'edge_ids': [e['id'] for e in edges if e['source']==a and e['target']==b]})
                    continue
                traversed.append(candidates[0])
            if ambiguous:
                problem('edge_ids', 'между операциями несколько переходов; укажи edge_ids по порядку пути', transitions=ambiguous[:10])
                continue
            # The traversed transitions already carry their own checked evidence;
            # the harness collects it. The case cites what supports the situation
            # and the expected result, which must belong to this route.
            route = [r for n in path for r in node_index[n].get('evidence', [])] + \
                    [r for e in traversed for r in e.get('evidence', []) + e.get('sender_evidence', []) + e.get('receiver_evidence', [])]
            if len(path) == 1:
                stop = node_index[path[0]].get('evidence', [])
                if stop and not any(self._overlap(a, b) for a in refs for b in stop):
                    problem('evidence', 'основания остановки должны относиться к конечной операции',
                            node_id=path[0], expected_ranges=[self._range(r) for r in stop[:3]])
                    continue
            elif route and not any(self._overlap(a, b) for a in refs for b in route):
                problem('evidence', 'основания случая не относятся ни к одной операции или переходу этого маршрута',
                        expected_ranges=[self._range(r) for r in route[:4]])
                continue
            try:
                texts = {key: self.business_text(row[key], 'route_cases.'+key) for key in ('name','input','expected_result')}
            except QualityError as exc:
                problem('name/input/expected_result', str(exc))
                continue
            transition_evidence = []
            for edge in traversed:
                for ref in edge.get('evidence', []):
                    if self._range(ref) not in transition_evidence:
                        transition_evidence.append(self._range(ref))
            result.append(texts | {'path': list(path), 'edge_ids': [e['id'] for e in traversed], 'evidence': refs,
                                   'transition_evidence': transition_evidence[:50]})
        if problems:
            raise QualityError(f'route_cases: требуют исправления случаи {", ".join(str(n) for n in sorted({p["case_index"] for p in problems})[:20])}; '
                               'остальные случаи корректны и могут быть отправлены без изменений',
                               cases=problems[:20], problem_count=len(problems))
        return result

    def quality_errors(self):
        errors = []
        for row in self.model['nodes'] + self.model['edges']:
            if not row.get('basis', '').strip():
                errors.append(f"{row['id']}: заполните публичное обоснование basis")
            if row['evidence_status'] != 'unknown' and not row.get('code_explanations'):
                errors.append(f"{row['id']}: добавьте code_explanations — файл, функция/элемент, смысл")
        for node in self.model['nodes']:
            outgoing = [e for e in self.model['edges'] if e['source']==node['id'] and e['kind']!='data']
            if node.get('completion_condition') and not outgoing and not any(node['id'] in g['node_ids'] for g in self.model['gaps']):
                errors.append(f"{node['id']}: условное завершение требует альтернативного продолжения либо адресного gap")
            if len(outgoing) > 1 and node.get('continuation_mode', 'sequence') == 'sequence':
                errors.append(f"{node['id']}: несколько связей требуют continuation_mode=choice или all")
            if node.get('continuation_mode') in ('choice', 'all') and len(outgoing) + bool(node.get('completion_condition') and node.get('continuation_mode')=='choice') < 2 and not any(node['id'] in g['node_ids'] for g in self.model['gaps']):
                errors.append(f"{node['id']}: заявленный выбор/параллельность требует двух продолжений либо адресного gap")
            if (len(outgoing) > 1 or node.get('completion_condition')) and any(not e['condition'].strip() for e in outgoing):
                errors.append(f"{node['id']}: несколько направлений требуют условий на всех выходах; общий участок не должен терять категорию маршрута")
            if node['id'] in self.model['scenario'].get('terminal_operation_ids', []) and node.get('effect_kind','unknown')=='unknown':
                errors.append(f"{node['id']}: укажите effect_kind — decision/recommendation/command/result; не подменяйте решение достигнутым эффектом")
            if (node['id'] in self.model['scenario'].get('terminal_operation_ids', [])
                    and node.get('outcome', 'unknown') == 'unknown'
                    and node.get('outcome_scope', 'branch') != 'shared'
                    and not any(node['id'] in gap['node_ids'] for gap in self.model['gaps'])):
                errors.append(f"{node['id']}: бизнес-исход конечной операции не установлен; уточните outcome по основаниям либо добавьте адресный gap")
        errors.extend(self._reviewed_kind_errors(
            review for review in self.model['execution_reviews'] if self._review_is_current(review)))
        for task in self.model['research_tasks']:
            if task['status'] in ('open','read','analyzed'):
                errors.append(f"{task['id']}: не раскрыт {task['target']}; исследуйте либо сохраните адресный gap")
            if set(task['node_ids']) - {n['id'] for n in self.model['nodes']} or set(task['gap_ids']) - {g['id'] for g in self.model['gaps']}:
                errors.append(f"{task['id']}: ссылки задачи исследования устарели")
            reason = self._task_mapping_error(task) if task['status']=='mapped' else self._task_gap_error(task) if task['status']=='gap' else None
            if reason:
                errors.append(f"{task['id']}: действия больше не покрывают реализацию или пробел не соответствует задаче: {reason}")
            reason = self._task_exclusion_error(task) if task['status'] == 'out_of_scope' else None
            if reason:
                errors.append(f"{task['id']}: исключение задачи не соответствует границе или карте: {reason}")
        if self.model['run'].get('research_tasks_overflow'):
            errors.append('Перечень зависимостей превысил лимит; сохранённый результат остаётся частичным')
        brief = self.model.get('research_brief')
        if brief and brief.get('read_signature') != self.read_signature():
            errors.append('research_brief устарел после чтения новых материалов; актуализируйте факты, гипотезу и следующие действия')
        review = self.model.get('semantic_review')
        if not review or review.get('signature') != self.quality_signature():
            errors.append('Выполните актуальный review_semantics: после прошлой проверки изменились операции, переходы, задачи, обзоры исполнения или источники')
        return errors

    def _reviewed_kind_errors(self, reviews=None):
        """What the execution review says an operation is must be visible on the graph.

        A check has more than one continuation (or ends the scenario conditionally,
        or its other outcome is an addressed gap); a call to another system names
        that system in external_interactions.
        """
        errors, nodes = [], {n['id']: n for n in self.model['nodes']}
        gap_nodes = {n for gap in self.model['gaps'] for n in gap['node_ids']}
        outgoing = {}
        for edge in self.model['edges']:
            if edge['kind'] != 'data':
                outgoing.setdefault(edge['source'], set()).add(edge['target'])
        for review in self.model['execution_reviews'] if reviews is None else reviews:
            for operation in review['operations']:
                node = nodes.get(operation['node_id'])
                if node is None:
                    continue
                if (operation['kind'] == 'validation' and len(outgoing.get(node['id'], ())) < 2
                        and not node.get('completion_condition') and node['id'] not in gap_nodes):
                    errors.append(f"{node['id']}: обзор называет операцию проверкой, но у неё одно продолжение; покажите переход для "
                                  "другого исхода, условное завершение completion_condition или адресный gap")
                if operation['kind'] in ('integration', 'publish') and not node.get('external_interactions'):
                    errors.append(f"{node['id']}: обзор называет операцию вызовом системы; заполните external_interactions — "
                                  "адресат, назначение, направление, endpoint или contract и строки вызова")
        return errors

    def read_signature(self):
        return hashlib.sha256(json.dumps({p: sorted(rows) for p,rows in sorted(self._reads.items())}, sort_keys=True).encode()).hexdigest()
