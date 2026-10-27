"""Public research obligations and model-authored semantic review.

Code validates provenance, coverage and staleness. It does not certify the
truth of a model's business interpretation or discover every operation.
"""
from __future__ import annotations

import hashlib
import json

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
    def _dependency_implementation(self, task, evidence):
        owner, symbol = task['target'].rsplit('.', 1)
        return [ref for ref in evidence if ref['path'].endswith('/'+owner+'.java') and
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
                "node_ids": [], "gap_ids": [], "automatic": True})

    def _save_research_task(self, args):
        old = next((x for x in self.model['research_tasks'] if x['id'] == args.get('id')), None)
        identifier = self._id('research_tasks', 'task', args.get('id'), 500)
        status = args.get('status', 'open')
        if status not in ('open', 'mapped', 'out_of_scope', 'gap'):
            raise QualityError("status: open, mapped, out_of_scope или gap")
        evidence = self._evidence(args.get('evidence', []))
        if not evidence:
            raise QualityError("Задаче исследования нужны прочитанные основания")
        node_ids = ids(args.get('node_ids', []), 'node_ids', [n['id'] for n in self.model['nodes']])
        gap_ids = ids(args.get('gap_ids', []), 'gap_ids', [g['id'] for g in self.model['gaps']])
        resolution = text(args.get('resolution'), 'resolution') if status != 'open' else ''
        if status == 'mapped' and not node_ids:
            raise QualityError("mapped требует действия карты в node_ids; чтение файла не означает раскрытие исполнения")
        if status == 'gap' and not gap_ids:
            raise QualityError("gap требует зарегистрированный gap_ids")
        if old and old.get('automatic'):
            # Keep the discovery location; callers add implementation evidence.
            evidence = self._evidence(old['evidence'] + [r for r in evidence if r not in old['evidence']])
            if status == 'mapped':
                implementation = self._dependency_implementation(old, evidence)
                if not implementation:
                    raise QualityError('mapped требует прочитанного объявления/реализации зависимости, не только места её регистрации', target=old['target'])
                if not self._nodes_cover_dependency(node_ids, implementation):
                    raise QualityError('mapped: действия карты должны иметь основания в реализации зависимости')
        item = {"id": identifier, "target": text(args.get('target'), 'target', 500),
                "kind": text(args.get('kind'), 'kind', 80), "status": status,
                "resolution": resolution, "evidence": evidence, "node_ids": node_ids,
                "gap_ids": gap_ids, "automatic": bool(old and old.get('automatic'))}
        if old and old.get('automatic') and (item['target'] != old['target'] or item['kind'] != old['kind']):
            raise QualityError("Нельзя менять цель или вид автоматически обнаруженной зависимости")
        return self._put('research_tasks', item)

    def quality_signature(self):
        # Ignore transient run metadata; public interpretation and evidence matter.
        state = {k: self.model[k] for k in ('scenario', 'systems', 'groups', 'nodes', 'edges', 'gaps',
                 'research_tasks', 'research_brief', 'execution_reviews', 'source_reviews')}
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
        ends = {n['id'] for n in self.model['nodes'] if n['kind'] == 'end'}
        if not ends.issubset(normalized['outcomes']['node_ids']):
            raise QualityError('outcomes должен рассмотреть каждый конечный узел', missing_node_ids=sorted(ends-set(normalized['outcomes']['node_ids'])))
        self.model['semantic_review'] = {"checks": normalized, "status": "model_reviewed_not_verified",
            "signature": self.quality_signature(), "brief_revision": (self.model['research_brief'] or {}).get('revision')}
        return {"status": "model_reviewed_not_verified", "semantic_verification": False}

    def quality_errors(self):
        errors = []
        for row in self.model['nodes'] + self.model['edges']:
            if not row.get('basis', '').strip():
                errors.append(f"{row['id']}: заполните публичное обоснование basis")
            if row['evidence_status'] != 'unknown' and not row.get('code_explanations'):
                errors.append(f"{row['id']}: добавьте code_explanations — файл, функция/элемент, смысл")
        for node in self.model['nodes']:
            outgoing = [e for e in self.model['edges'] if e['source']==node['id'] and e['kind']!='data']
            if node['kind'] != 'fork' and len({e['target'] for e in outgoing}) > 1 and any(not e['condition'].strip() for e in outgoing):
                errors.append(f"{node['id']}: несколько направлений требуют условий на всех выходах; общий участок не должен терять категорию маршрута")
            if node['kind']=='end' and node.get('effect_kind','unknown')=='unknown':
                errors.append(f"{node['id']}: укажите effect_kind — decision/recommendation/command/result; не подменяйте решение достигнутым эффектом")
        for task in self.model['research_tasks']:
            if task['status']=='open':
                errors.append(f"{task['id']}: не раскрыт {task['target']}; исследуйте либо сохраните адресный gap")
            if set(task['node_ids']) - {n['id'] for n in self.model['nodes']} or set(task['gap_ids']) - {g['id'] for g in self.model['gaps']}:
                errors.append(f"{task['id']}: ссылки задачи исследования устарели")
            if task.get('automatic') and task['status']=='mapped' and not self._nodes_cover_dependency(
                    task['node_ids'], self._dependency_implementation(task, task['evidence'])):
                errors.append(f"{task['id']}: действия больше не покрывают реализацию зависимости; обновите задачу исследования")
        if self.model['run'].get('research_tasks_overflow'):
            errors.append('Перечень зависимостей превысил лимит; сохранённый результат остаётся частичным')
        brief = self.model.get('research_brief')
        if brief and brief.get('read_signature') != self.read_signature():
            errors.append('research_brief устарел после чтения новых материалов; актуализируйте факты, гипотезу и следующие действия')
        review = self.model.get('semantic_review')
        if not review or review.get('signature') != self.quality_signature():
            errors.append('Выполните актуальный review_semantics после обновления карты, задач исследования и research_brief')
        return errors

    def read_signature(self):
        return hashlib.sha256(json.dumps({p: sorted(rows) for p,rows in sorted(self._reads.items())}, sort_keys=True).encode()).hexdigest()
