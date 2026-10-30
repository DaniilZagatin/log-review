"""Keep issued analyzer selectors out of emitted-record descriptions; do not infer joins."""
import re

PROFILE = 'host-selectors/1'


def _items(value):
    return value if type(value) is list else []


def _add(result, value):
    if type(value) is str and value:
        result.add(value)


def _chain_selectors(result, chain):
    if type(chain) is not dict:
        return
    contract = chain.get('source_contract')
    if type(contract) is dict:
        _add(result, contract.get('candidate_id'))
    for candidate in _items(chain.get('adapter_candidates')):
        if type(candidate) is dict and type(candidate.get('source_contract')) is dict:
            _add(result, candidate['source_contract'].get('candidate_id'))


def packet_selectors(packet):
    result = set()
    context = packet.get('downstream_context')
    if type(context) is dict:
        for name in ('delivered', 'frontier'):
            for entry in _items(context.get(name)):
                if type(entry) is dict:
                    _add(result, entry.get('context_call_id'))
    for site in _items(packet.get('candidates')):
        if type(site) is dict:
            _chain_selectors(result, site.get('logging_chain'))
    return result


def correlation_issue(binding, selectors, site, index):
    value = binding.get('correlation')
    if type(value) is not str:
        return None  # Normal required-text validation remains authoritative.
    found = sorted(key for key in selectors
                   if re.search(r'(?<![\w-])' + re.escape(key) + r'(?![\w-])', value))
    if not found:
        return None
    return {'code':'correlation_host_selector', 'field':f'bindings[{index}].correlation',
        'site_id':binding.get('site_id'), 'selector_values':found,
        'field_hints':{'fields':site.get('fields', []), 'status':site.get('fields_status', 'unknown'), 'complete':False},
        'instruction':'В correlation попала служебная ссылка анализатора; она не является полем записи. '
            'Оставь её в соответствующем typed selector. Назови реально записанные поля, что они идентифицируют '
            'и чего не различают, либо конкретный пробел по прочитанному коду и обёрткам. '
            'field_hints — неполная эвристика, не закрытый список и не доказательство отсутствия. '
            'Порядок вызовов не даёт ключа связи; не придумывай идентификатор попытки.'}


def saved_selectors(data):
    """Use saved host packets and witnesses, never scan model prose for supposed issued IDs."""
    result = set()
    for checkpoint in _items(data.get('checkpoints')):
        if type(checkpoint) is not dict:
            continue
        issued = checkpoint.get('issued_correlation_selectors')
        if (type(issued) is not list or any(type(key) is not str or not key for key in issued)
                or len(set(issued)) != len(issued)):
            from .logging_contracts import LoggingReviewError
            raise LoggingReviewError('Invalid saved issued_correlation_selectors')
        result.update(issued)
        if type(checkpoint.get('initial_packet')) is dict:
            result.update(packet_selectors(checkpoint['initial_packet']))
    for row in _items(data.get('operations')):
        if type(row) is not dict:
            continue
        assessment = row.get('context_assessment')
        if type(assessment) is dict:
            for key in _items(assessment.get('available_context_ids')):
                _add(result, key)
        for binding in _items(row.get('bindings')):
            if type(binding) is dict:
                _add(result, binding.get('context_call_id'))
                _chain_selectors(result, binding.get('chain'))
    return result
