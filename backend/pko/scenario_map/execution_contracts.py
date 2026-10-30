"""Preserve discovered execution across graph edits, without proving semantics.

Relations are model discoveries backed by delivered source lines. Validation
checks their projection, never infers execution order by sorting source lines.
"""
from copy import deepcopy

from pko.scenario_map.quality import QualityError


class ExecutionContractError(QualityError):
    def __init__(self, message, code='execution_projection', **details):
        super().__init__(message, **details)
        self.code = code


class ExecutionContracts:
    def _projection_issues_for_model(self, model, review):
        active = self.model
        try:
            self.model = model
            return self._projection_issues(review)
        finally:
            self.model = active

    @staticmethod
    def _fact_value(row):
        ignored = {'node_id', 'edge_ids', 'completion_node_id', 'mapping_status', 'name'}
        value = {k: v for k, v in row.items() if k not in ignored}
        # Missing optional properties in earlier state mean no discovery.
        if 'action' in row:
            value.setdefault('external_interactions', [])
        else:
            value.setdefault('from_operation_id', '')
            value.setdefault('to_operation_id', '')
        return value

    def _execution_revisions(self, old, item, amendment):
        changed = {}
        for key in ('operations', 'route_expectations'):
            before = {r['id']: r for r in old.get(key, [])}
            changed[key] = [r['id'] for r in item[key] if r['id'] in before
                            and self._fact_value(before[r['id']]) != self._fact_value(r)]
        if any(changed.values()) and not amendment:
            raise ExecutionContractError(
                'Обнаруженное исполнение изменилось: укажите amendment с причиной и прочитанными основаниями; '
                'для привязки сохраняйте действие, условие и порядок.', 'fact_amendment_required',
                review_id=item['id'], operation_ids=changed['operations'], route_ids=changed['route_expectations'])
        reason, refs = '', []
        if amendment is not None:
            reason, refs = self._grounded_amendment(amendment)
            self.require_coverage(item['id'], 'amendment.evidence', refs, item['evidence'])
        facts = lambda review: {key: {r['id']: self._fact_value(r) for r in review.get(key, [])}
                                for key in ('operations', 'route_expectations')}
        fact_changed = facts(old) != facts(item)
        item['fact_revision'] = old.get('fact_revision', 1) + int(bool(old) and fact_changed)
        item['mapping_revision'] = old.get('mapping_revision', 1) + int(bool(old) and self._execution_mapping(old) != self._execution_mapping(item))
        history = deepcopy(old.get('fact_changes', []))
        if any(changed.values()):
            history.append({'revision': item['fact_revision'], 'reason': reason, 'evidence': refs,
                            'operation_ids': changed['operations'], 'route_ids': changed['route_expectations']})
        item['fact_changes'] = history[-30:]

    @staticmethod
    def _execution_mapping(review):
        return {r['id']: (r.get('node_id'), r.get('edge_ids'), r.get('completion_node_id'))
                for key in ('operations', 'route_expectations') for r in review.get(key, [])}

    def _grounded_amendment(self, row):
        if not isinstance(row, dict) or set(row) != {'reason', 'evidence'}:
            raise ExecutionContractError('amendment: нужны reason и evidence', 'invalid_arguments')
        reason = row['reason']
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 3000:
            raise ExecutionContractError('amendment.reason: нужна причина до 3000 символов', 'invalid_arguments')
        refs = self._evidence(row['evidence'], 'amendment.evidence')
        if not refs:
            raise ExecutionContractError('amendment.evidence: нужны прочитанные основания', 'invalid_evidence')
        return reason.strip(), refs

    def _projection_issues(self, review):
        """Check recorded endpoints/path and explicit call sites, not prose equivalence."""
        nodes = {n['id']: n for n in self.model['nodes']}
        edges = {e['id']: e for e in self.model['edges']}
        operations = {op['id']: op for op in review['operations']}
        issues = []
        for op in operations.values():
            node = nodes.get(op['node_id'])
            if not node:
                continue
            for interaction in op.get('external_interactions', []):
                matches = [candidate for candidate in node.get('external_interactions', [])
                           if all(candidate.get(k, '') == interaction.get(k, '') for k in ('direction', 'endpoint', 'contract'))
                           and self._within(interaction['evidence'], candidate.get('evidence', []))]
                if not matches:
                    issues.append({'operation_id': op['id'], 'node_id': node['id'], 'field': 'external_interactions',
                                   'problem': 'Найденное внешнее обращение не отображено: сохраните контракт, направление и строки вызова'})
        for route in review.get('route_expectations', []):
            origin, target = route.get('from_operation_id'), route.get('to_operation_id')
            if not origin and not target:
                continue
            problem = ''
            if origin not in operations or target not in operations:
                problem = 'Отношение исполнения ссылается на отсутствующее найденное действие'
            elif route['completion_node_id']:
                problem = 'Отношение двух действий отображается путём edge_ids, а не завершением одного действия'
            elif route['edge_ids']:
                start, end = operations[origin]['node_id'], operations[target]['node_id']
                path = [edges.get(e) for e in route['edge_ids']]
                if not start or not end:
                    problem = 'Сначала сопоставьте оба найденных действия с узлами'
                elif (any(e is None or e['kind'] == 'data' for e in path)
                      or path[0]['source'] != start or path[-1]['target'] != end
                      or any(a['target'] != b['source'] for a, b in zip(path, path[1:]))):
                    problem = 'Путь нарушает найденный порядок: edge_ids должны вести от from_operation_id к to_operation_id'
                elif path[0]['kind'] == 'branch' or sum(e['source'] == start and e['kind'] != 'data' for e in edges.values()) > 1:
                    normalize = lambda value: ' '.join(value.split()).rstrip('.').casefold()
                    if normalize(path[0].get('condition', '')) != normalize(route['condition']):
                        problem = 'Условие первого перехода отличается от найденной альтернативы: перенесите condition ожидания без переформулировки'
            if problem:
                issues.append({'route_id': route['id'], 'field': 'route_expectations', 'problem': problem})
        return issues

    def _apply_execution_mappings(self, args, aliases, alias_kinds):
        """Change only projection; discovered meanings and revisions stay intact."""
        op_rows = args.get('operation_mappings', [])
        route_rows = args.get('route_mappings', [])
        if not isinstance(op_rows, list) or not isinstance(route_rows, list):
            raise ExecutionContractError('operation_mappings и route_mappings: ожидаются списки', 'invalid_arguments')
        operations = {op['id']: (review, op) for review in self.model['execution_reviews'] for op in review['operations']}
        routes = {r['id']: (review, r) for review in self.model['execution_reviews'] for r in review.get('route_expectations', [])}
        nodes = {n['id']: n for n in self.model['nodes']}
        edges = {e['id']: e for e in self.model['edges']}
        changed, seen = set(), set()

        def resolve(value, kind):
            if not isinstance(value, str):
                raise ExecutionContractError('Привязка требует строковый ID', 'invalid_arguments')
            if value.startswith('$'):
                key = value[1:]
                if key not in aliases or alias_kinds[key] != kind:
                    raise ExecutionContractError('Неизвестный local_ref в привязке: '+value, 'unknown_id')
                value = aliases[key]
            if value and value not in (nodes if kind == 'steps' else edges):
                raise ExecutionContractError('Неизвестный ID в привязке: '+value, 'unknown_id')
            return value

        for row in op_rows:
            if not isinstance(row, dict) or set(row) != {'operation_id', 'node_id'}:
                raise ExecutionContractError('operation_mappings: нужны operation_id и node_id', 'invalid_arguments')
            identifier = row['operation_id']
            if not isinstance(identifier, str) or identifier not in operations or identifier in seen:
                raise ExecutionContractError('Нужен уникальный существующий operation_id', 'unknown_id')
            seen.add(identifier)
            review, op = operations[identifier]
            node_id = resolve(row['node_id'], 'steps')
            if node_id:
                if not any(self._overlap(a, b) for a in op['evidence'] for b in nodes[node_id]['evidence']):
                    raise ExecutionContractError('Основания действия и узла не пересекаются', operation_id=identifier)
                self.require_coverage(identifier, 'node.evidence', nodes[node_id]['evidence'], review['evidence'])
            op.update(node_id=node_id, mapping_status='mapped' if node_id else 'unmapped')
            changed.add(review['id'])
        for row in route_rows:
            if not isinstance(row, dict) or set(row) - {'route_id', 'edge_ids', 'completion_node_id'} or 'route_id' not in row:
                raise ExecutionContractError('route_mappings: нужны route_id и привязка пути/завершения', 'invalid_arguments')
            identifier = row['route_id']
            if not isinstance(identifier, str) or identifier not in routes or identifier in seen:
                raise ExecutionContractError('Нужен уникальный существующий route_id', 'unknown_id')
            seen.add(identifier)
            review, route = routes[identifier]
            values = row.get('edge_ids', [])
            if not isinstance(values, list) or len(values) > 1500:
                raise ExecutionContractError('route_mappings.edge_ids: нужен список до 1500 ID', 'invalid_arguments')
            selected = [resolve(e, 'transitions') for e in values]
            if len(set(selected)) != len(selected) or any(not e for e in selected):
                raise ExecutionContractError('Повтор или пустой edge_id в привязке', 'invalid_arguments')
            terminal = resolve(row.get('completion_node_id', ''), 'steps')
            for obj in [edges[e] for e in selected] + ([nodes[terminal]] if terminal else []):
                if not any(self._overlap(a, b) for a in route['evidence'] for b in obj['evidence']):
                    raise ExecutionContractError('Основания ожидания и его привязки не пересекаются', route_id=identifier)
            route.update(edge_ids=selected, completion_node_id=terminal, mapping_status='mapped' if selected or terminal else 'unmapped')
            changed.add(review['id'])
        for review in self.model['execution_reviews']:
            if review['id'] in changed:
                mapped = [op['node_id'] for op in review['operations'] if op['node_id']]
                if len(mapped) != len(set(mapped)):
                    raise ExecutionContractError('Самостоятельные найденные действия требуют разных узлов', review_id=review['id'])
        return sorted(changed)
