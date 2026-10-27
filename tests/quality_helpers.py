"""Explicit public review fixtures for tests of unrelated graph invariants."""
from copy import deepcopy
from pko.scenario_map.quality import SEMANTIC_CHECKS


def review_payload(state):
    evidence = next((n['evidence'] for n in state.model['nodes'] if n['evidence']), [])
    return {'checks': {key: {'status': 'reviewed',
        'explanation': 'Синтетическая проверка: условия, порядок и эффекты заданы тестовой фикстурой; это не оценка реальной модели.',
        'evidence': deepcopy(evidence), 'node_ids': [n['id'] for n in state.model['nodes']],
        'edge_ids': [e['id'] for e in state.model['edges']], 'gap_ids': []} for key in SEMANTIC_CHECKS}}


def explain_args(args):
    args = deepcopy(args)
    args.setdefault('basis', args.get('description') or 'Действие показано в прочитанной тестовой реализации.')
    args.setdefault('code_explanations', [{'kind':'fragment','symbol':'',
        'explanation': args['basis'], 'evidence': args['evidence'][:1]}] if args.get('evidence') else [])
    if args.get('kind') == 'end':
        args.setdefault('effect_kind','result')
    return args
