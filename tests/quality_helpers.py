"""Explicit public review fixtures for tests of unrelated graph invariants."""
from copy import deepcopy
from pko.scenario_map.quality import SEMANTIC_CHECKS


def review_payload(state):
    evidence = next((n['evidence'] for n in state.model['nodes'] if n['evidence']), [])
    payload = {'checks': {key: {'status': 'reviewed',
        'explanation': 'Синтетическая проверка: условия, порядок и эффекты заданы тестовой фикстурой; это не оценка реальной модели.',
        'evidence': deepcopy(evidence), 'node_ids': [n['id'] for n in state.model['nodes']],
        'edge_ids': [e['id'] for e in state.model['edges']], 'gap_ids': []} for key in SEMANTIC_CHECKS}}
    payload['route_cases'] = [
        {'name': 'Проверка конкретного продолжения',
         'input': edge.get('condition') or 'Операция успешно завершена; выполняется следующий шаг.',
         'path': [edge['source'], edge['target']], 'edge_ids': [edge['id']],
         'expected_result': 'Выполняется указанная следующая операция.',
         'evidence': deepcopy(edge.get('evidence') or evidence)}
        for edge in state.model['edges'] if edge['kind'] != 'data']
    payload['route_cases'].extend({'name':'Условное завершение', 'input':node['completion_condition'], 'path':[node['id']],
        'edge_ids':[], 'expected_result':'Сценарий завершён на проверяемой операции.', 'evidence':deepcopy(node['evidence'])}
        for node in state.model['nodes'] if node.get('completion_condition') and node['id'] in state.model['scenario'].get('terminal_operation_ids', []))
    return payload


def explain_args(args):
    args = deepcopy(args)
    args.setdefault('basis', args.get('description') or 'Действие показано в прочитанной тестовой реализации.')
    args.setdefault('code_explanations', [{'kind':'fragment','symbol':'',
        'explanation': args['basis'], 'evidence': args['evidence'][:1]}] if args.get('evidence') else [])
    if 'source' not in args:
        args.setdefault('kind', 'operation')
        args.setdefault('effect_kind','result')
    return args
