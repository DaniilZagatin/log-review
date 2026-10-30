"""Small contracts for reviewing logging over somebody else's immutable map.

No dependency on the former map builder, business facts or metric catalog.
"""
import hashlib
import json
from pathlib import PurePosixPath

from .redaction import redact_text

SCHEMA = 'logging-review/1'
# Policy 2: typed lifecycle moments, blocking vs runtime-only review questions, isolated optional proposals,
# source-bound Java wrapper chains and operator-declared external logging contracts. Saved policy-1 reports keep
# their original acceptance rules (operation_coverage.binding_is_sufficient(policy=...)).
POLICY_2 = 'operation-logging-policy/2'
POLICY = 'operation-logging-policy/3'
LEGACY_POLICY = 'operation-logging-policy/1'
POLICIES = (LEGACY_POLICY, POLICY_2, POLICY)
STATES = ('C', 'P', 'U', 'X')
LABELS = {'C': 'Залогирована', 'P': 'Частично', 'U': 'Не залогирована', 'X': 'Не проверена'}
ROLES = ('request', 'start', 'result', 'receiver_response', 'error', 'repeat', 'no_op')
# When the record is written relative to the operation's own action. A record before the action is intent only.
MOMENTS = ('before_action', 'after_action', 'after_confirmation', 'on_failure', 'unknown')
CHANGE_KINDS = ('map_existing', 'join', 'enrich', 'add_field', 'add_event', 'move_emit')
# Preference order of proposals (requirements §7): reuse before duplicate instrumentation.
EXISTING_RECORD_KINDS = ('map_existing', 'join', 'enrich', 'add_field', 'move_emit')
# source/map questions block an assessment; runtime questions (level enablement, execution, delivery) are limits of
# static review and never mean that the source search is unfinished.
TASK_SCOPES = ('source', 'map', 'runtime')
DISPOSITIONS = ('not_log', 'technical_log', 'unresolved', 'other_operation_log')
CONTRACTS_SCHEMA = 'logging-contracts/1'


class LoggingReviewError(ValueError):
    pass


class ChangeValidationError(LoggingReviewError):
    """An optional proposal is invalid; the mandatory assessment may still be sound."""
    def __init__(self, message, index=None, local_id=None):
        super().__init__(message)
        self.index, self.local_id = index, local_id


def validate_binding_moment(binding):
    """An explicit pre-action statement cannot simultaneously claim an outcome; no semantic promotion."""
    if binding.get('moment') == 'before_action':
        established = binding.get('establishes', [])
        if (type(established) is list and 'result' in established) or binding.get('covers_outcomes'):
            raise LoggingReviewError('before_action фиксирует только намерение: establishes=["occurrence"], '
                'covers_outcomes=[]. Если запись действительно подтверждает результат, обоснуй другой момент по коду; '
                'не меняй moment только ради прохождения проверки.')


def task_is_blocking(task):
    """Typed tasks state it explicitly; legacy untyped tasks stay conservative (assessment blocks)."""
    if type(task) is not dict: return True
    if type(task.get('blocking')) is bool: return task['blocking']
    return task.get('kind', 'assessment') == 'assessment'


def review_task(operation_id, kind, description, *, blocking=True, scope='source'):
    return {'operation_id': operation_id, 'kind': kind, 'description': description, 'blocking': bool(blocking), 'scope': scope}


def validate_logging_contracts(value):
    """Operator-declared external logging APIs; a declaration is auditable input, never inferred from a name."""
    import re
    if value is None: return []
    if type(value) is dict:
        if value.get('schema_version') != CONTRACTS_SCHEMA or set(value) - {'schema_version', 'contracts'}:
            raise LoggingReviewError('Expected ' + CONTRACTS_SCHEMA + ' with only schema_version/contracts')
        value = value.get('contracts')
    if type(value) not in (list, tuple) or len(value) > 100:
        raise LoggingReviewError('logging contracts: expected at most 100 declarations')
    result, seen = [], set()
    for item in value:
        if type(item) is not dict or set(item) - {'callee', 'basis'} or not {'callee', 'basis'} <= set(item):
            raise LoggingReviewError('logging contract: expected exactly {callee, basis}')
        callee = item['callee']
        if type(callee) is not str or re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+', callee) is None:
            raise LoggingReviewError('logging contract callee must be an exact qualified call such as Vendor.Sdk.info')
        if callee in seen: raise LoggingReviewError('Duplicate logging contract callee')
        seen.add(callee)
        result.append({'callee': callee, 'basis': text(item['basis'], 'logging contract basis', maximum=2000)})
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_path(value):
    from .source_tools import blocked_path
    if (type(value) is not str or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))
            or blocked_path(value)):
        raise LoggingReviewError('Expected an admitted repository-relative path')
    return value


def text(value, field, *, required=True, maximum=20000):
    if type(value) is not str or len(value) > maximum or (required and not value.strip()):
        raise LoggingReviewError(field + ': expected nonblank bounded text')
    import re
    if re.search(r'</?(?:think|thinking|reasoning|analysis)\b', value, re.I):
        raise LoggingReviewError(field + ': use a public description and source references, without private reasoning tags')
    return redact_text(value)


def strings(value, field, *, maximum=100):
    if type(value) is not list or len(value) > maximum:
        raise LoggingReviewError(field + ': expected a bounded list')
    return [text(item, field) for item in value]


def fields(value, allowed, required, label):
    if type(value) is not dict:
        raise LoggingReviewError(label + ': expected an object')
    missing, extra = set(required) - value.keys(), value.keys() - set(allowed)
    if missing or extra:
        raise LoggingReviewError(label + ': missing ' + ','.join(sorted(missing)) + '; unexpected ' +
                                 ','.join(safe_public(str(key)[:160]) + ('…' if len(str(key)) > 160 else '') for key in sorted(extra)))


def safe_public(value, *, source_data=False, validated_quotes=frozenset()):
    """Filter answers; preserve source text only for trusted reads or validated quotes."""
    import re
    private = re.compile(r'^(?:reasoning(?:_content)?|thinking|analysis|chain_of_thought|api_?key|authorization|password|secret|access_token)$', re.I)
    if isinstance(value, dict):
        address = ('path', 'start_line', 'end_line', 'quote')
        verified = (all(type(value.get(k)) is t for k, t in zip(address, (str, int, int, str)))
                    and tuple(value[k] for k in address) in validated_quotes)
        return {str(k): safe_public(v, source_data=source_data or (verified and k == 'quote'),
                                    validated_quotes=validated_quotes)
                for k, v in value.items() if not private.fullmatch(str(k))
                and not re.search(r'</?(?:think|thinking|reasoning|analysis)\b', str(k), re.I)}
    if isinstance(value, list):
        return [safe_public(v, source_data=source_data, validated_quotes=validated_quotes) for v in value]
    if isinstance(value, str):
        if not source_data and re.search(r'</?(?:think|thinking|reasoning|analysis)\b', value, re.I):
            return '[omitted: private reasoning markers]'
        return redact_text(value)
    return value


def unknown(operation_id, reason, expected=None, search=None):
    shortened = len(reason) > 2000
    reason = safe_public(reason[:2000])  # Bound diagnostics before redaction; source data is never cut.
    if shortened: reason += ' [diagnostic shortened; source text is unchanged]'
    return {'operation_id': operation_id, 'state': 'X', 'expected_signal': expected or {},
            'bindings': [], 'missing': [], 'missing_status': 'hypothesis', 'changes': [], 'change_diagnostics': [],
            'review_tasks': [review_task(operation_id, 'assessment', text(reason, 'reason'))],
            'limitations': [text(reason, 'reason')], 'search': search or {}, 'warnings': []}
