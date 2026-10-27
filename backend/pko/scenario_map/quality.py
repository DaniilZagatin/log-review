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
        owner = task['target'].rsplit('.', 1)[0] if '.' in task['target'] else task['target']
        basename = owner.rsplit('.', 1)[-1]
        candidates = [p for p in self._files if p.endswith('/'+basename+'.java') or p.endswith('/'+basename+'.py')]
        origin = task.get('evidence', [{}])[0].get('path', '')
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
            if any((t['target'], t['evidence'][0]['path'], t['evidence'][0]['line_start']) == key
                   for t in self.model['research_tasks'] if t.get('automatic')):
                continue
            if len(self.model['research_tasks']) >= 500:
                self.model['run']['research_tasks_overflow'] = True
                break
            self._counters['task'] += 1
            self.model['research_tasks'].append({"id": f"task-{self._counters['task']}", **item,
                "evidence": self._evidence(item['evidence']), "status": "open", "resolution": "",
                "node_ids": [], "gap_ids": [], "child_task_ids": [], "automatic": True})

    def _task_mapping_error(self, task, seen=None):
        seen = set(seen or ())
        if task['id'] in seen:
            return 'цикл зависимостей задач'
        seen.add(task['id'])
        implementation = self._dependency_implementation(task, task['evidence'])
        if task.get('automatic') and not implementation:
            return 'mapped требует прочитанного объявления/реализации зависимости, не только места её регистрации'
        children = task.get('child_task_ids', [])
        index = {t['id']: t for t in self.model['research_tasks']}
        discovered = {t['id'] for t in index.values() if t.get('automatic') and t['id'] != task['id'] and
                      any(self._overlap(ref, scope) for ref in t['evidence'][:1] for scope in implementation)}
        if any(index[x]['status'] != 'out_of_scope' for x in discovered - set(children)):
            return 'добавьте все найденные дочерние зависимости либо обоснованно исключите их'
        if children:
            index = {t['id']: t for t in self.model['research_tasks']}
            for identifier in children:
                child = index.get(identifier)
                if not child or child['status'] != 'mapped':
                    return 'дочерняя задача ещё не отображена в карте'
                if not any(self._overlap(ref, scope) for ref in child['evidence'][:1] for scope in implementation):
                    return 'дочерняя задача не обнаружена в прочитанной реализации родителя'
                if self._task_mapping_error(child, seen):
                    return 'дочерняя задача больше не покрывает свою реализацию'
                if not set(child['node_ids']).issubset(task['node_ids']):
                    return 'mapped должен включать операции всех дочерних задач'
            discovered = {t['id'] for t in index.values() if t.get('automatic') and t['id'] != task['id'] and
                          any(self._overlap(ref, scope) for ref in t['evidence'][:1] for scope in implementation)}
            if any(index[x]['status'] != 'out_of_scope' for x in discovered - set(children)):
                return 'добавьте все найденные дочерние зависимости либо обоснованно исключите их'
        elif task.get('automatic') and not self._nodes_cover_dependency(task['node_ids'], implementation):
            return 'действия карты должны иметь основания в реализации зависимости'
        if not task['node_ids']:
            return 'mapped требует действия карты в node_ids; чтение файла не означает раскрытие исполнения'
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
                if not self._dependency_implementation(task, task['evidence']):
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
        if child_ids:
            for child in self.model['research_tasks']:
                if child['id'] in child_ids:
                    node_ids = list(dict.fromkeys(node_ids + child['node_ids']))
        item = {"id": identifier, "target": text(args.get('target'), 'target', 500),
                "kind": text(args.get('kind'), 'kind', 80), "status": status,
                "resolution": text(args.get('resolution'), 'resolution') if status != 'open' else '',
                "evidence": evidence, "node_ids": node_ids, "gap_ids": gap_ids,
                "child_task_ids": child_ids, "automatic": bool(old and old.get('automatic'))}
        if old and old.get('automatic') and (item['target'] != old['target'] or item['kind'] != old['kind']):
            raise QualityError("Нельзя менять цель или вид автоматически обнаруженной зависимости")
        if status in ('read', 'analyzed') and item['automatic'] and not self._dependency_implementation(item, evidence):
            raise QualityError('Для read/analyzed прочитайте объявление зависимости', target=item['target'])
        reason = self._task_mapping_error(item) if status == 'mapped' else self._task_gap_error(item) if status == 'gap' else None
        if reason:
            raise QualityError(reason, target=item['target'])
        return self._put('research_tasks', item)

    def quality_signature(self):
        # Ignore transient run metadata; public interpretation and evidence matter.
        # research_brief is excluded: its freshness against new reads is checked
        # separately (read_signature), and it does not change routes or outcomes.
        state = {k: self.model[k] for k in ('scenario', 'groups', 'nodes', 'edges', 'gaps',
                 'research_tasks', 'execution_reviews', 'source_reviews')}
        state['nodes'] = [self._fingerprint(row) for row in self.model['nodes']]
        state['edges'] = [self._fingerprint(row) for row in self.model['edges']]
        state.pop('groups', None)
        return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _review_semantics(self, args):
        checks = args.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(SEMANTIC_CHECKS):
            raise QualityError('checks: нужны outcomes, order, routing, atomicity, dependencies')
        normalized = {}
        for name, row in checks.items():
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
        ends = set(self.model['scenario'].get('terminal_operation_ids', []))
        if not ends.issubset(normalized['outcomes']['node_ids']):
            raise QualityError('outcomes должен рассмотреть каждый конечный узел', missing_node_ids=sorted(ends-set(normalized['outcomes']['node_ids'])))
        required_nodes = self._reviewable_operations()
        if required_nodes - set(normalized['atomicity']['node_ids']):
            raise QualityError('atomicity должен рассмотреть каждую операцию', missing_node_ids=sorted(required_nodes-set(normalized['atomicity']['node_ids'])))
        dependency_gaps = {g for task in self.model['research_tasks'] if task['status']=='gap' for g in task['gap_ids']}
        if dependency_gaps and (normalized['dependencies']['status'] != 'uncertain' or dependency_gaps-set(normalized['dependencies']['gap_ids'])):
            raise QualityError('dependencies: нераскрытые зависимости требуют uncertain и всех адресных gap_ids')
        edges = {e['id']: e for e in self.model['edges'] if e['kind'] != 'data'}
        routing = normalized['routing']
        missing = set(edges) - set(routing['edge_ids'])
        addressed = {e for g in self.model['gaps'] if g['id'] in routing['gap_ids'] for e in g.get('edge_ids', [])}
        if missing - addressed:
            raise QualityError('routing должен рассмотреть каждый переход либо адресовать его gap', missing_edge_ids=sorted(missing-addressed))
        cases = self._route_cases(args.get('route_cases', []))
        covered_edges = {identifier for case in cases for identifier in case['edge_ids']}
        fanout = {e['source'] for e in edges.values() if sum(x['source']==e['source'] for x in edges.values()) > 1}
        uncovered = [e['id'] for e in edges.values() if e['source'] in fanout and
                     e['id'] not in covered_edges and e['id'] not in addressed]
        if uncovered:
            raise QualityError('route_cases: разберите конкретные случаи для всех исходящих ветвей', missing_edge_ids=uncovered)
        for node in self.model['nodes']:
            if node['id'] not in ends or not node.get('completion_condition'):
                continue
            addressed_node = any(g['id'] in routing['gap_ids'] and node['id'] in g['node_ids'] for g in self.model['gaps'])
            if addressed_node:
                if routing['status'] != 'uncertain':
                    raise QualityError('routing: неизвестное продолжение условного завершения требует uncertain')
                continue
            stops = any(case['path'][-1] == node['id'] for case in cases)
            continues = any(node['id'] in case['path'][:-1] for case in cases)
            has_outgoing = any(e['source']==node['id'] for e in edges.values())
            if not has_outgoing or not stops or not continues:
                raise QualityError('route_cases: условное завершение требует отдельного случая остановки и случая продолжения либо адресного gap', node_id=node['id'])
        self.model['semantic_review'] = {"checks": normalized, "route_cases": cases, "status": "model_reviewed_not_verified",
            "signature": self.quality_signature(), "brief_revision": (self.model['research_brief'] or {}).get('revision')}
        return {"status": "model_reviewed_not_verified", "semantic_verification": False}

    def _route_cases(self, rows):
        if not isinstance(rows, list) or len(rows) > 500:
            raise QualityError('route_cases: ожидается до 500 конкретных случаев')
        result = []
        nodes = {n['id'] for n in self.model['nodes']}
        edges = [e for e in self.model['edges'] if e['kind'] != 'data']
        pairs = {(e['source'], e['target']) for e in edges}
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'name','input','path','expected_result','evidence','edge_ids'} or not {'name','input','path','expected_result','evidence'}.issubset(row):
                raise QualityError('route_cases: нужны name, input, path, expected_result, evidence')
            path = row['path']
            if not isinstance(path, list) or not 1 <= len(path) <= 1000 or any(not isinstance(n,str) or n not in nodes for n in path):
                raise QualityError('route_cases.path: нужны существующие операции в порядке выполнения')
            if any((a,b) not in pairs for a,b in zip(path,path[1:])):
                raise QualityError('route_cases.path: путь содержит отсутствующий переход')
            refs = self._evidence(row['evidence'])
            if not refs:
                raise QualityError('route_cases: нужны прочитанные основания случая')
            if len(path)==1:
                node = next(n for n in self.model['nodes'] if n['id']==path[0])
                if node.get('evidence') and not any(self._overlap(a,b) for a in refs for b in node['evidence']):
                    raise QualityError('route_cases: основания остановки должны относиться к конечной операции')
            selected = row.get('edge_ids')
            if selected is not None and (not isinstance(selected, list) or len(selected) != len(path)-1):
                raise QualityError('route_cases.edge_ids: один переход для каждой пары операций')
            traversed = []
            for i, (a,b) in enumerate(zip(path,path[1:])):
                candidates = [e for e in edges if e['source']==a and e['target']==b and (selected is None or e['id']==selected[i])]
                if len(candidates) != 1:
                    raise QualityError('route_cases: неоднозначный переход; укажите edge_ids по порядку пути')
                edge = candidates[0]
                support = edge.get('evidence', []) + edge.get('sender_evidence', []) + edge.get('receiver_evidence', [])
                if support and not any(self._overlap(a,b) for a in refs for b in support):
                    raise QualityError('route_cases: основания случая должны относиться к пройденным переходам')
                traversed.append(edge['id'])
            result.append({key: self.business_text(row[key], 'route_cases.'+key) for key in ('name','input','expected_result')} |
                          {'path': list(path), 'edge_ids': traversed, 'evidence': refs})
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
        for task in self.model['research_tasks']:
            if task['status'] in ('open','read','analyzed'):
                errors.append(f"{task['id']}: не раскрыт {task['target']}; исследуйте либо сохраните адресный gap")
            if set(task['node_ids']) - {n['id'] for n in self.model['nodes']} or set(task['gap_ids']) - {g['id'] for g in self.model['gaps']}:
                errors.append(f"{task['id']}: ссылки задачи исследования устарели")
            reason = self._task_mapping_error(task) if task['status']=='mapped' else self._task_gap_error(task) if task['status']=='gap' else None
            if reason:
                errors.append(f"{task['id']}: действия больше не покрывают реализацию или пробел не соответствует задаче: {reason}")
        if self.model['run'].get('research_tasks_overflow'):
            errors.append('Перечень зависимостей превысил лимит; сохранённый результат остаётся частичным')
        brief = self.model.get('research_brief')
        if brief and brief.get('read_signature') != self.read_signature():
            errors.append('research_brief устарел после чтения новых материалов; актуализируйте факты, гипотезу и следующие действия')
        review = self.model.get('semantic_review')
        if not review or review.get('signature') != self.quality_signature():
            errors.append('Выполните актуальный review_semantics: после прошлой проверки изменились операции, переходы, задачи, обзоры исполнения или источники')
        return errors

    def read_signature(self):
        return hashlib.sha256(json.dumps({p: sorted(rows) for p,rows in sorted(self._reads.items())}, sort_keys=True).encode()).hexdigest()
