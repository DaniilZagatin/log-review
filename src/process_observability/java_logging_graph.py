"""Source-bound Java call navigation for the map-first logging review only (LoggingNavigation).

The admitted original text is lexed with the lexer and declaration parser of java_declarations; nothing is
compiled, imported or executed. The legacy declaration index (declaration_index/java_declarations.extract) and its
cache identities are not changed or used as an output of this module.

Per file the index keeps the package, imports (single-type, on-demand and static), top-level and nested types (kind,
extends/implements), fields with declared types, methods/constructors/initializers (name, parameter names and
types, arity with explicit varargs, declared result type, body span) and the call sites inside member bodies: line,
column, end line, callee text as written, receiver form and argument count.

A call is resolved only from declared static types: implicit or explicit this, fields (inherited through in-repo
supertypes), parameters, typed locals, ``var x = new T(...)``, ``new T(...).m()``, casts, static ``Type.m()`` and
getter chains through declared result types. Type names resolve through same-file and nested types, single-type
imports, the same package, on-demand imports and fully qualified names within the admitted snapshot; anything
else is external. Statuses:

- exact: the receiver type declares exactly one method with that name and a compatible arity and no in-repo
  subtype overrides it; inherited: the same, found in an in-repo supertype; super: ``super.m(...)``;
- dispatch: a virtual call with in-repo overrides/implementations, all listed in targets; ``incomplete`` when
  anonymous classes of the receiver family exist (they are not indexed as types);
- implicit: a compiler-generated constructor or record accessor without a source body (no edge);
- ambiguous: several same-name methods or constructors accept the argument count (argument types are not resolved);
- external: the receiver type is outside the admitted snapshot (JDK, library, unknown import);
- unresolved: the receiver or the member is unknown. Same-name methods elsewhere are hints, never edges.

Not resolved: lambdas and method references as targets, reflection, generic type arguments, field initializers,
members of local/anonymous classes (their calls belong to the enclosing member) and argument types of overloads.
"""

import bisect
from collections import deque
import re

from .java_declarations import (TYPE_KINDS, _Failure, _OPEN, _Parser, _TEXT_BLOCK_OPENING, _TOKEN, _UNTERMINATED,
                                _balance, _line_starts, _unicode_guard)
from .logging_contracts import LoggingReviewError

TOOL = 'java_logging_graph/1'
EDGE_STATUSES = ('exact', 'inherited', 'super')
STATUSES = ('exact', 'inherited', 'super', 'dispatch', 'implicit', 'ambiguous', 'external', 'unresolved')
PLAN_STATUSES = ('confirmed', 'no_emission', 'unconfirmed_external', 'ambiguous', 'unresolved', 'depth_limit',
                 'unsupported')
MAX_ROUTE_HOPS = 6        # invocation_route and logging_chain
TRANSITIVE_DEPTH = 4      # related(): depth of focus-following
TRANSITIVE_SPANS = 16     # related(): additional method spans delivered by focus-following
HINT_CAP = 20             # same cap as code_graph not_edges
EXPLORE_CAP = 256         # members visited by one bounded closure

# Generic standard logging APIs: a receiver must resolve to one of these by import or fully qualified name.
STANDARD_LOGGERS = {
    'org.slf4j.Logger': frozenset({'trace', 'debug', 'info', 'warn', 'error'}),
    'java.util.logging.Logger': frozenset({'severe', 'warning', 'info', 'config', 'fine', 'finer', 'finest', 'log',
                                           'logp', 'logrb', 'entering', 'exiting', 'throwing'}),
    'org.apache.logging.log4j.Logger': frozenset({'trace', 'debug', 'info', 'warn', 'error', 'fatal', 'log', 'printf',
                                                  'catching', 'throwing', 'traceEntry', 'traceExit', 'entry', 'exit'}),
    'org.apache.commons.logging.Log': frozenset({'trace', 'debug', 'info', 'warn', 'error', 'fatal'}),
    'java.lang.System.Logger': frozenset({'log'}),
}

NOT_SUPPORTED = 'Навигация вызовов в этом участке не поддержана; отсутствие логов не установлено'
HINTS_PARTIAL = 'Часть подсказок возможных реализаций не выдана статическим навигатором'
HINTS_LIMITED = 'Ограничен список возможных реализаций получателя'
HINT_WITH_LOG = 'Не разрешена связь с возможной реализацией, содержащей запись: '
UNRESOLVED_TARGET = 'Не разрешена локальная цель вызова '
CALLERS_LIMITED = 'Ограничен точный список вызывающих мест для '
ROUTE_CUT = ('Маршрут вызовов прослежен не до конца из-за ограничения навигации: {route}; '
             'в непрослеженной части есть кандидат записи журнала')
LIMIT_STATIC = ('Статический разбор не доказывает включённость уровня журналирования, фактическое выполнение '
                'и доставку записи')
LIMIT_CONTRACT = ('Реализация внешнего вызова {callee} отсутствует в репозитории; признание его записью журнала '
                  'основано на объявленном оператором контракте: {basis}')
LIMIT_DETECTOR = ('Конечная запись найдена детектором стандартных вызовов журналирования по тексту вызова; '
                  'тип получателя навигатором не проверялся')
LIMIT_STANDARD = 'Конечная запись — вызов стандартного API журналирования {fqn}; тип получателя установлен по импорту'
LIMIT_SINGLE_DISPATCH = ('Выбор реализации во время выполнения не доказан: в репозитории найдена единственная '
                         'реализация {method}; анонимные классы, лямбды и внешние реализации не исключены')
LIMIT_DISPATCH = 'Выбор реализации во время выполнения среди {count} реализаций в репозитории не доказан: {callee}'
LIMIT_ANONYMOUS = 'Анонимные классы семейства получателя не проиндексированы как реализации: {callee}'
LIMIT_NOT_FORWARDING = ('Переход {step} не передаёт параметры или локальные значения обёртки; связь содержимого '
                        'записи с данными вызова не установлена')
LIMIT_OTHER_CALLS = ('В просмотренных телах есть неразрешённые или внешние вызовы ({count}); условия выполнения '
                     'ветки с записью не доказаны')
LIMIT_EXTERNAL = 'Внешний вызов {callee} ({path}:{line}): контракт журналирования не объявлен; запись не подтверждена'
LIMIT_NO_EMISSION = ('Отсутствие записи установлено только для статически разрешённых вызовов цепочки; лямбды как '
                     'цели, ссылки на методы, отражение и инициализаторы полей не прослеживаются')

_PRIMITIVES = frozenset({'boolean', 'byte', 'char', 'short', 'int', 'long', 'float', 'double', 'void'})
_JAVA_LANG = frozenset({
    'Object', 'String', 'System', 'Math', 'StrictMath', 'Integer', 'Long', 'Double', 'Float', 'Short', 'Byte',
    'Boolean', 'Character', 'Number', 'Void', 'Class', 'ClassLoader', 'Thread', 'ThreadLocal', 'Runnable',
    'Runtime', 'Process', 'ProcessBuilder', 'StringBuilder', 'StringBuffer', 'CharSequence', 'Iterable',
    'Comparable', 'AutoCloseable', 'Cloneable', 'Enum', 'Record', 'Throwable', 'Exception', 'Error',
    'RuntimeException', 'IllegalArgumentException', 'IllegalStateException', 'NullPointerException',
    'UnsupportedOperationException', 'IndexOutOfBoundsException', 'ArithmeticException', 'ClassCastException',
    'InterruptedException', 'SecurityException', 'StackTraceElement', 'Override', 'Deprecated',
    'SuppressWarnings', 'FunctionalInterface', 'SafeVarargs'})
_OBJECT_METHODS = frozenset({'toString', 'equals', 'hashCode', 'getClass', 'notify', 'notifyAll', 'wait', 'clone',
                             'finalize'})
_ENUM_METHODS = frozenset({'name', 'ordinal', 'compareTo', 'values', 'valueOf', 'getDeclaringClass',
                           'describeConstable'})
# Unqualified words that are followed by "(" without being a method invocation.
_NOT_CALLS = frozenset({'if', 'for', 'while', 'switch', 'catch', 'synchronized', 'try', 'return', 'throw', 'assert',
                        'new', 'else', 'case', 'do', 'yield', 'instanceof', 'finally', 'default'})
# Words that may directly precede an unqualified invocation; any other identifier marks a declaration header.
_CALL_PRECEDERS = frozenset({'return', 'throw', 'else', 'case', 'yield', 'assert', 'do'})
# Words that cannot end the type of a local variable declaration.
_NOT_TYPES = frozenset({'return', 'throw', 'new', 'else', 'case', 'yield', 'assert', 'do', 'instanceof', 'break',
                        'continue', 'goto', 'package', 'import', 'default', 'this', 'super', 'extends', 'implements',
                        'throws', 'static', 'final', 'abstract', 'public', 'private', 'protected', 'synchronized',
                        'volatile', 'transient', 'native', 'strictfp', 'class', 'interface', 'enum', 'permits',
                        'sealed', 'try', 'catch', 'finally', 'if', 'for', 'while', 'switch'})
_DECLARATOR_NEXT = frozenset({'=', ';', ',', ':', ')', '&', '|'})
_DOTTED = re.compile(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*')
_MEMBER_KINDS = {'method': 'java_method', 'constructor': 'java_constructor', 'initializer': 'java_initializer'}


def _lex(text, starts):
    """Tokens (kind, value, line, column) with the java_declarations token rules; columns are line offsets."""
    tokens, position, size = [], 0, len(text)
    while position < size:
        match = _TOKEN.match(text, position)
        kind, line = match.lastgroup, bisect.bisect_right(starts, position)
        if kind in _UNTERMINATED:
            raise _Failure(_UNTERMINATED[kind], line)
        if kind == 'open_text_block':
            raise _Failure('UnterminatedTextBlock' if _TEXT_BLOCK_OPENING.match(text, position) else 'InvalidTextBlock',
                           line)
        column = position - starts[line - 1]
        if kind in ('name', 'punct'):
            tokens.append((kind, match.group(), line, column))
        elif kind in ('string', 'char', 'text_block', 'number'):
            tokens.append(('literal', None, line, column))
        elif kind == 'other':
            if match.group() == '\\':
                raise _Failure('UnicodeEscapeOutsideLiteral', line)
            tokens.append(('other', None, line, column))
        position = match.end()
    return tokens


class _Declarations(_Parser):
    """The declaration-index parser, additionally keeping token positions of every declaration."""

    def __init__(self, tokens, match):
        super().__init__(tokens, match)
        self.spans = []

    def add(self, kind, name, start, name_at, close, owner, modifiers, annotations, body, count, signature):
        position = super().add(kind, name, start, name_at, close, owner, modifiers, annotations, body, count,
                               signature)
        self.spans.append((start, name_at, close))
        return position


def _type_text(ref):
    return '?' if ref is None else ref[0].rsplit('.', 1)[-1] + '[]' * ref[1]


class _File:
    """Tokens, header and declarations of one admitted Java text."""

    def __init__(self, path, text):
        self.path, self.status, self.error = path, 'indexed', None
        self.package, self.imports, self.types, self.members, self.calls = '', [], [], [], []
        self.kinds, self.values, self.lines, self.columns, self.match, self.parser, self.size = [], [], [], [], {}, None, 0
        if type(text) is not str:
            self.status, self.error = 'parse_failed', 'NotText'
            return
        try:
            starts = _line_starts(text)
            _unicode_guard(text, starts)
            tokens = _lex(text, starts)
            plain = [token[:3] for token in tokens]
            self.match = _balance(plain)
            self.parser = _Declarations(plain, self.match)
            self.parser.unit()
        except _Failure as failure:
            self.status, self.error = 'parse_failed', failure.kind
            return
        except (RecursionError, IndexError, KeyError):
            self.status, self.error = 'parse_failed', 'ExtractorError'
            return
        self.kinds = [token[0] for token in tokens]
        self.values = [token[1] for token in tokens]
        self.lines = [token[2] for token in tokens]
        self.columns = [token[3] for token in tokens]
        self.size = len(tokens)
        if self.parser.notes:
            self.status = 'partial'

    def value(self, index):
        return self.values[index] if 0 <= index < self.size else None

    def kind(self, index):
        return self.kinds[index] if 0 <= index < self.size else None

    def header(self):
        """Package and imports before the first type declaration."""
        values, kinds, index = self.values, self.kinds, 0
        while index < self.size:
            if values[index] == ';':
                index += 1
                continue
            index, _, _ = self.parser.modifiers(index, self.size)
            word = values[index] if index < self.size and kinds[index] == 'name' else None
            if word not in ('package', 'import'):
                return
            close = self.parser.semicolon(index, self.size)
            if close is None:
                return
            parts = [values[i] for i in range(index + 1, close) if kinds[i] == 'name']
            if word == 'package':
                self.package = '.'.join(parts)
            else:
                static = bool(parts) and parts[0] == 'static'
                self.imports.append({'name': '.'.join(parts[1:] if static else parts), 'static': static,
                                     'wildcard': any(values[i] == '*' for i in range(index + 1, close))})
            index = close + 1

    def angle_close(self, index, end):
        """Index of the ">" closing type arguments opened at index, or None when the tokens are not a type list."""
        depth = 0
        while index < end:
            value = self.values[index]
            if value == '<':
                depth += 1
            elif value == '>':
                depth -= 1
                if depth == 0:
                    return index
            elif value == '[':
                index = self.match[index] + 1
                continue
            elif not (self.kinds[index] == 'name' or value in ('.', ',', '?', '&', '@')):
                return None
            index += 1
        return None

    def type_ref(self, index, end):
        """((dotted base, dimensions, varargs), next index) of a type written at index, or (None, index)."""
        values, kinds = self.values, self.kinds
        while (index + 1 < end and values[index] == '@' and kinds[index + 1] == 'name'
               and values[index + 1] != 'interface'):
            index += 2
            while index + 1 < end and values[index] == '.' and kinds[index + 1] == 'name':
                index += 2
            if index < end and values[index] == '(':
                index = self.match[index] + 1
        if index >= end or kinds[index] != 'name':
            return None, index
        parts = [values[index]]
        index += 1
        while index < end:
            if values[index] == '<':
                close = self.angle_close(index, end)
                if close is None:
                    break
                index = close + 1
                continue
            if values[index] == '.' and index + 1 < end and kinds[index + 1] == 'name':
                parts.append(values[index + 1])
                index += 2
                continue
            break
        dims, varargs = 0, False
        while index < end:
            if values[index] == '[' and self.match.get(index) == index + 1:
                dims += 1
                index += 2
                continue
            if values[index] == '...':
                varargs, dims = True, dims + 1
                index += 1
                continue
            break
        return ('.'.join(parts), dims, varargs), index

    def split(self, index, end):
        """Top-level comma-separated parts [start, stop) of index..end."""
        parts, start = [], index
        while index < end:
            value = self.values[index]
            if value in _OPEN:
                index = self.match[index] + 1
                continue
            if value == '<' and index > 0 and (self.values[index - 1] == '.' or (
                    self.kinds[index - 1] == 'name' and self.values[index - 1][:1].isupper())):
                close = self.angle_close(index, end)
                if close is not None:
                    index = close + 1
                    continue
            if value == ',':
                parts.append((start, index))
                start = index + 1
            index += 1
        if start < end:
            parts.append((start, end))
        return parts

    def parameters(self, opener, close):
        """Formal parameters: name, type (base, dims) and varargs; unparsed ones keep their position in the arity."""
        values, kinds, result = self.values, self.kinds, []
        for start, stop in self.split(opener + 1, close):
            if 'this' in values[start:stop]:
                continue  # a receiver parameter is not a formal parameter
            index = start
            while index < stop and (values[index] == 'final' or (values[index] == '@' and kinds[index + 1] == 'name')):
                index = index + 1 if values[index] == 'final' else self.parser.annotation(index, stop)[1]
            ref, index = self.type_ref(index, stop)
            if ref is not None and index < stop and kinds[index] == 'name':
                name, dims = values[index], ref[1]
                index += 1
                while index < stop and values[index] == '[':
                    dims += 1
                    index = self.match[index] + 1
                result.append({'name': name, 'type': (ref[0], dims), 'varargs': ref[2]})
            else:
                result.append({'name': None, 'type': None, 'varargs': False})
        return result

    def type_header(self, index, end):
        """Type parameters, record components and extends/implements names between a type name and its body."""
        values, kinds = self.values, self.kinds
        header = {'type_params': set(), 'components': [], 'extends': [], 'implements': []}
        if index < end and values[index] == '<':
            close = self.angle_close(index, end)
            if close is not None:
                depth = 0
                for position in range(index, close + 1):
                    if values[position] == '<':
                        depth += 1
                    elif values[position] == '>':
                        depth -= 1
                    elif depth == 1 and kinds[position] == 'name' and values[position - 1] in ('<', ','):
                        header['type_params'].add(values[position])
                index = close + 1
        if index < end and values[index] == '(':
            header['components'] = self.parameters(index, self.match[index])
            index = self.match[index] + 1
        clause = None
        while index < end:
            if kinds[index] == 'name' and values[index] in ('extends', 'implements', 'permits'):
                clause = values[index]
                index += 1
                continue
            if clause in ('extends', 'implements') and kinds[index] == 'name':
                ref, after = self.type_ref(index, end)
                if ref is not None:
                    header[clause].append(ref[0])
                    index = max(after, index + 1)
                    continue
            index += 1
        return header

    def expression_end(self, index, end):
        """Index of the "," or ";" ending a declarator initializer at this level."""
        while index < end:
            value = self.values[index]
            if value in (',', ';'):
                return index
            if value in _OPEN:
                index = self.match[index] + 1
                continue
            if value == '<' and self.kinds[index - 1] == 'name' and self.values[index - 1][:1].isupper():
                close = self.angle_close(index, end)
                if close is not None:
                    index = close + 1
                    continue
            index += 1
        return end


class JavaIndex:
    """Lazy-resolving Java declaration/call index over admitted original texts {path: text}."""

    def __init__(self, sources):
        self.files, self.types, self.members, self.calls = {}, {}, {}, {}
        self.by_fqn, self.duplicate_fqns, self.packages, self.by_name = {}, set(), set(), {}
        self.anonymous = []  # (simple type name, path, first line, last line) of anonymous class bodies
        for path in sorted(sources):
            self._index_file(path, sources[path])
        self.package_roots = {package.split('.')[0] for package in self.packages if package}
        self.anonymous_names = {item[0] for item in self.anonymous}
        self._super_cache, self._children, self._resolved, self._incoming = {}, None, {}, None
        self._subtype_cache = {}

    # Indexing.

    def _index_file(self, path, text):
        f = _File(path, text)
        self.files[path] = f
        if f.status == 'parse_failed':
            return
        try:
            types, members, calls, anonymous = self._declare(f)
        except (RecursionError, IndexError, KeyError, TypeError, AttributeError, ValueError):
            f.status, f.error, f.types, f.members, f.calls = 'parse_failed', 'ExtractorError', [], [], []
            return
        self.types.update(types)
        self.members.update(members)
        self.calls.update(calls)
        self.anonymous.extend(anonymous)
        self.packages.add(f.package)
        f.types, f.members = list(types), list(members)
        f.calls = sorted(calls, key=lambda identifier: calls[identifier]['index'])
        for tid, info in types.items():
            if info['fqn'] in self.by_fqn:
                self.duplicate_fqns.add(info['fqn'])
            else:
                self.by_fqn[info['fqn']] = tid
        for mid, member in members.items():
            if member['kind'] == 'java_method':
                self.by_name.setdefault(member['name'], []).append(mid)

    def _declare(self, f):
        f.header()
        declarations, spans = f.parser.declarations, f.parser.spans
        types, members, calls, anonymous, type_ids = {}, {}, {}, [], {}
        for index, decl in enumerate(declarations):
            start, name_at, close = spans[index]
            if decl['kind'] in TYPE_KINDS:
                outer = type_ids.get(decl['parent'])
                opener = f.match[close]
                header = f.type_header(name_at + 1, opener)
                tid = 'jt:%s:%s' % (f.path, decl['qualname'])
                if tid in types:
                    tid += '#%d' % index
                types[tid] = {'id': tid, 'path': f.path, 'name': decl['name'], 'qualname': decl['qualname'],
                              'fqn': (f.package + '.' if f.package else '') + decl['qualname'], 'kind': decl['kind'],
                              'outer': outer, 'modifiers': list(decl['modifiers']), 'start_line': decl['start_line'],
                              'end_line': decl['end_line'], 'extends': header['extends'],
                              'implements': header['implements'], 'type_params': header['type_params'],
                              'components': header['components'], 'fields': {}, 'methods': [], 'constructors': [],
                              'initializers': [], 'nested': {}, 'init_code': False, 'body': (opener, close),
                              'decl': index}
                if outer is not None:
                    types[outer]['nested'].setdefault(decl['name'], tid)
                type_ids[index] = tid
                continue
            owner = types[type_ids[decl['parent']]]
            member = self._member(f, decl, start, name_at, close, owner)
            if member['id'] in members:
                member['id'] += '#%d' % index
            members[member['id']] = member
            owner[{'java_method': 'methods', 'java_constructor': 'constructors',
                   'java_initializer': 'initializers'}[member['kind']]].append(member['id'])
            if member['kind'] == 'java_initializer' and not member['static']:
                owner['init_code'] = True
        for tid, info in types.items():
            children = {spans[i][0]: spans[i][2] for i, decl in enumerate(declarations) if decl['parent'] == info['decl']}
            self._fields(f, info, children)
        for member in members.values():
            self._scan(f, member, calls, anonymous)
        return types, members, calls, anonymous

    def _member(self, f, decl, start, name_at, close, owner):
        kind, values = decl['kind'], f.values
        params, result, type_params, body = [], None, set(), None
        if kind == 'initializer':
            body = (name_at + 1, close)
        elif decl['signature'] == 'compact':
            params = [dict(item) for item in owner['components']]
            body = (name_at + 2, close)  # Name { ... }
        else:
            paren = name_at + 1
            params = f.parameters(paren, f.match[paren])
            position, _, _ = f.parser.modifiers(start, name_at)
            if values[position] == '<':
                after = f.parser.angles(position, name_at)
                if after is not None:
                    type_params = {values[i] for i in range(position + 1, after - 1)
                                   if f.kinds[i] == 'name' and values[i - 1] in ('<', ',')}
                    position = after
            if kind == 'method' and position < name_at:
                ref, _ = f.type_ref(position, name_at)
                result = None if ref is None else (ref[0], ref[1])
            if decl['body'] == 'block':
                body = (f.match[close] + 1, close)
        arity = len(params)
        return {'id': 'jm:%s:%s/%d@%d' % (f.path, decl['qualname'], arity, decl['start_line']), 'path': f.path,
                'kind': _MEMBER_KINDS[kind], 'name': decl['name'], 'qualname': decl['qualname'], 'type': owner['id'],
                'start_line': decl['start_line'], 'definition_line': decl['definition_line'],
                'end_line': decl['end_line'], 'params': params, 'arity': arity,
                'varargs': bool(params) and params[-1]['varargs'], 'result': result, 'type_params': type_params,
                'modifiers': list(decl['modifiers']), 'static': 'static' in decl['modifiers'], 'body': body,
                'signature': decl['name'] + '(' + ','.join(_type_text(p['type']) for p in params) + ')',
                'calls': [], 'locals': [], 'nested': [], 'nested_methods': set(), 'method_refs': []}

    def _fields(self, f, info, children):
        values, kinds, parser = f.values, f.kinds, f.parser
        opener, close = info['body']
        for item in info['components']:
            if item['name']:
                info['fields'][item['name']] = {'name': item['name'], 'type': item['type'], 'static': False}
        index = opener + 1
        if info['kind'] == 'enum':
            index = self._enum_constants(f, info, index, close)
        while index < close:
            if index in children:
                index = children[index] + 1
                continue
            if values[index] == ';':
                index += 1
                continue
            position, modifiers, _ = parser.modifiers(index, close)
            ref, position = f.type_ref(position, close)
            if ref is None or position >= close or kinds[position] != 'name' or f.value(position + 1) == '(':
                index = parser.recover(index, close)
                continue
            static = 'static' in modifiers or info['kind'] in ('interface', 'annotation_type')
            while position < close and kinds[position] == 'name':
                name, dims = values[position], ref[1]
                position += 1
                while position < close and values[position] == '[':
                    dims += 1
                    position = f.match[position] + 1
                if position < close and values[position] == '=':
                    end = f.expression_end(position + 1, close)
                    if not static and any(values[i] in ('(', 'new') for i in range(position + 1, end)):
                        info['init_code'] = True
                    position = end
                info['fields'].setdefault(name, {'name': name, 'type': (ref[0], dims), 'static': static})
                if position < close and values[position] == ',':
                    position += 1
                    continue
                break
            index = position + 1 if position < close and values[position] == ';' else parser.recover(index, close)

    def _enum_constants(self, f, info, index, close):
        values, kinds, expect = f.values, f.kinds, True
        while index < close:
            value = values[index]
            if value == ';':
                return index + 1
            if value == '@' and kinds[index + 1] == 'name':
                index = f.parser.annotation(index, close)[1]
                continue
            if value in _OPEN:
                index = f.match[index] + 1
                expect = False
                continue
            if expect and kinds[index] == 'name':
                info['fields'].setdefault(value, {'name': value, 'type': (info['name'], 0), 'static': True})
                expect = False
            elif value == ',':
                expect = True
            index += 1
        return close

    # Member bodies: locals and call sites.

    def _scan(self, f, member, calls, anonymous):
        if member['body'] is None:
            return
        lo, hi = member['body']
        values, kinds, match = f.values, f.kinds, f.match
        nested = member['nested']
        for index in range(lo, hi):
            if kinds[index] != 'name':
                continue
            word, previous = values[index], values[index - 1]
            if word == 'new' and previous != '::':
                ref, after = f.type_ref(index + 1, hi)
                if ref is not None and after < hi and values[after] == '(':
                    body = match[after] + 1
                    if body < hi and values[body] == '{':
                        nested.append((body, match[body]))
                        anonymous.append((ref[0].rsplit('.', 1)[-1], f.path, f.lines[body], f.lines[match[body]]))
            elif previous not in ('.', '::') and ((word in ('class', 'interface', 'enum') and f.kind(index + 1) == 'name')
                                                  or (word == 'record' and f.kind(index + 1) == 'name'
                                                      and f.value(index + 2) in ('(', '<'))):
                position = index + 2
                while position < hi and values[position] not in ('{', ';'):
                    position = match[position] + 1 if values[position] in ('(', '[') else position + 1
                if position < hi and values[position] == '{':
                    nested.append((position, match[position]))
        by_paren, creation_tokens, stack = {}, set(), []
        for index in range(lo, hi):
            kind, word = kinds[index], values[index]
            if kind == 'punct':
                if word in _OPEN:
                    stack.append(index)
                elif word in (')', ']', '}'):
                    if stack:
                        stack.pop()
                elif word == '->':
                    self._lambda_parameters(f, member, index, hi)
                elif word == '::':
                    member['method_refs'].append(f.lines[index])
                continue
            if kind != 'name':
                continue
            inside = any(first < index < last for first, last in nested)
            following = f.value(index + 1)
            if word == 'new' and values[index - 1] != '::':
                call = self._creation(f, member, index, hi, inside)
                if call is not None:
                    calls[call['id']] = call
                    member['calls'].append(call['id'])
                    by_paren[call['paren']] = call
                    creation_tokens.update(range(index + 1, call['paren']))
                continue
            if following == '(':
                if index in creation_tokens:
                    continue
                call = self._call(f, member, index, lo, by_paren, inside)
                if call is not None:
                    calls[call['id']] = call
                    member['calls'].append(call['id'])
                    by_paren[call['paren']] = call
                continue
            if (values[index - 1] not in ('.', '::') and following in _DECLARATOR_NEXT
                    and not (following == '=' and f.value(index + 2) == '=')):
                ref = self._type_before(f, index - 1, lo)
                if ref is not None:
                    if ref[0] == 'var':
                        ref = self._initializer_type(f, index + 2, hi) if following == '=' else None
                    member['locals'].append((word, ref, index, self._scope_end(f, stack, hi)))

    def _creation(self, f, member, index, hi, inside):
        ref, paren = f.type_ref(index + 1, hi)
        if ref is None or paren >= hi or f.values[paren] != '(':
            return None
        close = f.match[paren]
        return {'id': 'jc:%s:%d' % (f.path, index), 'path': f.path, 'member': member['id'], 'form': 'creation',
                'name': ref[0].rsplit('.', 1)[-1], 'type_ref': (ref[0], 0), 'receiver': None,
                'callee': 'new ' + ref[0], 'start_line': f.lines[index], 'column': f.columns[index],
                'end_line': f.lines[close], 'name_line': f.lines[index + 1], 'name_column': f.columns[index + 1],
                'argc': self._argc(f, paren, close), 'arg_names': self._arg_names(f, paren, close), 'nested': inside,
                'index': index, 'paren': paren, 'start_index': index, 'anonymous': f.value(close + 1) == '{'}

    def _call(self, f, member, index, lo, by_paren, inside):
        values, kinds = f.values, f.kinds
        word, previous = values[index], values[index - 1]
        receiver = None
        if word in ('this', 'super') and previous != '.':
            if inside or previous not in ('{', ';', '}'):
                return None
            form = 'this_ctor' if word == 'this' else 'super_ctor'
        elif previous == '.':
            form, receiver = 'qualified', self._expr(f, index - 2, lo, by_paren)
        elif previous == '>':
            opening = self._angle_open(f, index - 1, lo)
            if opening is None:
                if word in _NOT_CALLS:
                    return None
                form = 'implicit'  # a comparison operator precedes the invocation
            elif opening - 1 >= lo and values[opening - 1] == '.':
                form, receiver = 'qualified', self._expr(f, opening - 2, lo, by_paren)
            else:
                if inside:
                    member['nested_methods'].add(word)
                return None  # a generic result type of a local/anonymous class method declaration
        elif previous in ('@', '::'):
            return None
        else:
            if word in _NOT_CALLS:
                return None
            if (kinds[index - 1] == 'name' and previous not in _CALL_PRECEDERS) or previous == ']':
                if inside:
                    member['nested_methods'].add(word)
                return None  # a declaration header of a local or anonymous class member
            form = 'implicit'
        start = index if receiver is None else self._start(receiver, index)
        if receiver is not None and start - 1 >= 0 and values[start - 1] == '@':
            return None  # a qualified annotation
        paren = index + 1
        close = f.match[paren]
        callee = word if receiver is None else self._render(receiver) + '.' + word
        return {'id': 'jc:%s:%d' % (f.path, index), 'path': f.path, 'member': member['id'], 'form': form,
                'name': word, 'receiver': receiver, 'callee': callee, 'start_line': f.lines[start],
                'column': f.columns[start], 'end_line': f.lines[close], 'name_line': f.lines[index],
                'name_column': f.columns[index], 'argc': self._argc(f, paren, close),
                'arg_names': self._arg_names(f, paren, close), 'nested': inside, 'index': index, 'paren': paren,
                'start_index': start}

    def _expr(self, f, end, lo, by_paren, depth=0):
        """Descriptor of the primary expression ending at token end (inclusive); its last item is a start hint."""
        if end < lo or depth > 64:
            return ('complex', None)
        values, kinds = f.values, f.kinds
        if kinds[end] == 'name':
            if end - 1 >= lo and values[end - 1] == '.':
                return ('field', self._expr(f, end - 2, lo, by_paren, depth + 1), values[end])
            return ('name', values[end], end)
        if kinds[end] == 'literal':
            return ('literal', end)
        if values[end] == ')':
            opening = f.match[end]
            call = by_paren.get(opening)
            if call is not None:
                return ('new', call) if call['form'] == 'creation' else ('call', call)
            inner = opening + 1
            if values[inner] == '(' and f.match[inner] < end:
                ref, after = f.type_ref(inner + 1, f.match[inner])
                if ref is not None and after == f.match[inner]:
                    return ('cast', (ref[0], ref[1]), opening)
            return ('complex', opening)
        return ('complex', None)

    def _start(self, desc, fallback):
        while desc[0] == 'field':
            desc = desc[1]
        if desc[0] == 'name':
            return desc[2]
        if desc[0] in ('call', 'new'):
            return desc[1]['start_index']
        return desc[-1] if type(desc[-1]) is int else fallback

    def _render(self, desc):
        kind = desc[0]
        if kind == 'name':
            return desc[1]
        if kind == 'field':
            return self._render(desc[1]) + '.' + desc[2]
        if kind in ('call', 'new'):
            return desc[1]['callee'] + '(…)'
        if kind == 'cast':
            return '((' + desc[1][0] + ') …)'
        if kind == 'literal':
            return '"…"'
        return '(…)'

    @staticmethod
    def _argc(f, paren, close):
        if paren + 1 == close:
            return 0
        return len(f.split(paren + 1, close))

    @staticmethod
    def _arg_names(f, paren, close):
        return sorted({f.values[i] for i in range(paren + 1, close) if f.kinds[i] == 'name'
                       and f.values[i - 1] != '.' and f.values[i + 1] != '('})

    @staticmethod
    def _angle_open(f, gt, lo):
        values, kinds, depth = f.values, f.kinds, 0
        while gt >= lo:
            value = values[gt]
            if value == '>':
                depth += 1
            elif value == '<':
                depth -= 1
                if depth == 0:
                    return gt
            elif not (kinds[gt] == 'name' or value in ('.', ',', '?', '[', ']')):
                return None
            gt -= 1
        return None

    def _type_before(self, f, end, lo):
        """(base, dims) of a declaration type ending at end, or None when the tokens are an expression."""
        values, kinds, dims = f.values, f.kinds, 0
        while end - 1 >= lo and values[end] == ']' and values[end - 1] == '[':
            dims += 1
            end -= 2
        if end < lo:
            return None
        if values[end] == '>':
            opening = self._angle_open(f, end, lo)
            if opening is None or opening - 1 < lo:
                return None
            end = opening - 1
            if kinds[end] != 'name' or not values[end][:1].isupper():
                return None
        elif kinds[end] != 'name' or values[end] in _NOT_TYPES:
            return None
        parts, start = [values[end]], end
        while start - 2 >= lo and values[start - 1] == '.' and kinds[start - 2] == 'name':
            start -= 2
            parts.insert(0, values[start])
        if start - 1 >= lo and values[start - 1] == '.':
            return None
        return ('.'.join(parts), dims)

    @staticmethod
    def _initializer_type(f, index, hi):
        values = f.values
        if index >= hi:
            return None
        if values[index] == 'new':
            ref, after = f.type_ref(index + 1, hi)
            return (ref[0], 0) if ref is not None and after < hi and values[after] == '(' else None
        if values[index] == '(':
            close = f.match[index]
            ref, after = f.type_ref(index + 1, close)
            if ref is not None and after == close and close + 1 < hi and (
                    f.kinds[close + 1] in ('name', 'literal') or values[close + 1] == '('):
                return (ref[0], ref[1])
        return None

    def _scope_end(self, f, stack, hi):
        values, match = f.values, f.match
        if stack and values[stack[-1]] == '(':
            top = stack[-1]
            after = match[top] + 1
            if f.value(after) == '->':
                return self._lambda_end(f, after, hi)
            if f.value(top - 1) in ('catch', 'for', 'try'):
                return match[after] if f.value(after) == '{' else self._statement_end(f, after, hi)
        for opener in reversed(stack):
            if values[opener] == '{':
                return match[opener]
        return hi

    @staticmethod
    def _lambda_end(f, arrow, hi):
        index = arrow + 1
        if f.value(index) == '{':
            return f.match[index]
        while index < hi:
            value = f.values[index]
            if value in _OPEN:
                index = f.match[index] + 1
                continue
            if value in (',', ';', ')', ']', '}'):
                return index
            index += 1
        return hi

    @staticmethod
    def _statement_end(f, index, hi):
        while index < hi:
            value = f.values[index]
            if value == ';':
                return index
            index = f.match[index] + 1 if value in _OPEN else index + 1
        return hi

    def _lambda_parameters(self, f, member, arrow, hi):
        """Untyped lambda parameters shadow fields inside the lambda body; their type stays unknown."""
        values, kinds = f.values, f.kinds
        end = self._lambda_end(f, arrow, hi)
        before = arrow - 1
        if kinds[before] == 'name':
            member['locals'].append((values[before], None, before, end))
        elif values[before] == ')':
            opening = f.match[before]
            inner = list(range(opening + 1, before))
            if inner and all(kinds[i] == 'name' if (i - opening) % 2 else values[i] == ',' for i in inner):
                for position in inner[::2]:
                    member['locals'].append((values[position], None, position, end))

    # Type and member lookup.

    def _fqn(self, name):
        if name in self.duplicate_fqns:
            return ('unknown', 'Several admitted types share the name ' + name)
        return ('in', self.by_fqn[name]) if name in self.by_fqn else None

    def _supertypes(self, t):
        cached = self._super_cache.get(t)
        if cached is not None:
            return cached
        self._super_cache[t] = []  # cycle guard
        info = self.types[t]
        f = self.files[info['path']]
        resolved = []
        for name in info['extends'] + info['implements']:
            found = self._type_name(name, f, info['outer'])
            item = found if found[0] in ('in', 'ext') else ('ext', name)
            if item != ('in', t):
                resolved.append(item)
        self._super_cache[t] = resolved
        return resolved

    def _superclass(self, t):
        info = self.types[t]
        if info['kind'] == 'enum':
            return ('ext', 'java.lang.Enum')
        if info['kind'] == 'record':
            return ('ext', 'java.lang.Record')
        if info['kind'] != 'class' or not info['extends']:
            return None
        supers = self._supertypes(t)
        return supers[0] if supers else None

    def _interfaces(self, t):
        info, supers = self.types[t], self._supertypes(t)
        if info['kind'] == 'class' and info['extends']:
            return supers[1:]
        return supers if info['kind'] in ('class', 'interface', 'enum', 'record') else []

    def _children_map(self):
        if self._children is None:
            children = {}
            for t in sorted(self.types):
                for kind, parent in self._supertypes(t):
                    if kind == 'in':
                        children.setdefault(parent, []).append(t)
            self._children = children
        return self._children

    def _subtypes(self, t):
        cached = self._subtype_cache.get(t)
        if cached is not None:
            return list(cached)
        children, result, seen = self._children_map(), [], {t}
        queue = deque(children.get(t, ()))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            result.append(current)
            queue.extend(children.get(current, ()))
        result = sorted(result, key=lambda x: (self.types[x]['path'], self.types[x]['start_line'], x))
        self._subtype_cache[t] = tuple(result)
        return result

    def _member_type(self, t, name):
        queue, seen = deque([t]), set()
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            nested = self.types[current]['nested'].get(name)
            if nested is not None:
                return nested
            queue.extend(item[1] for item in self._supertypes(current) if item[0] == 'in')
        return None

    def _simple_type(self, name, f, tid):
        current = tid
        while current is not None:
            nested = self._member_type(current, name)
            if nested is not None:
                return ('in', nested)
            if self.types[current]['name'] == name:
                return ('in', current)
            current = self.types[current]['outer']
        for t in f.types:
            if self.types[t]['outer'] is None and self.types[t]['name'] == name:
                return ('in', t)
        for item in f.imports:
            if not item['static'] and not item['wildcard'] and item['name'].rsplit('.', 1)[-1] == name:
                return self._fqn(item['name']) or ('ext', item['name'])
        found = self._fqn((f.package + '.' if f.package else '') + name)
        if found is not None:
            return found
        inside, outside = [], []
        for item in f.imports:
            if item['wildcard'] and not item['static']:
                hit = self._fqn(item['name'] + '.' + name)
                if hit is not None:
                    inside.append(hit)
                elif item['name'] not in self.packages:
                    outside.append(item['name'] + '.' + name)
        if len(inside) == 1:
            return inside[0]
        if inside:
            return ('unknown', 'Several on-demand imports provide ' + name)
        if name in _JAVA_LANG and not outside:
            return ('ext', 'java.lang.' + name)
        if len(outside) == 1:
            return ('ext', outside[0])
        return None

    def _type_name(self, text, f, tid):
        """('in', type id), ('ext', name) or ('unknown', reason) of a dotted type name used in f/tid."""
        parts = text.split('.')
        head = self._simple_type(parts[0], f, tid)
        if head is None:
            for count in range(len(parts), 1, -1):
                found = self._fqn('.'.join(parts[:count]))
                if found is not None:
                    head, parts = found, [None] + parts[count:]
                    break
            else:
                return ('ext', text)
        if head[0] == 'ext':
            return ('ext', '.'.join([head[1]] + parts[1:]))
        if head[0] != 'in':
            return head
        current = head[1]
        for segment in parts[1:]:
            current = self._member_type(current, segment)
            if current is None:
                return ('unknown', 'Nested type is not declared in the admitted snapshot: ' + text)
        return ('in', current)

    def _typeref_value(self, ref, tid, member=None):
        if ref is None:
            return ('unknown', 'Declared type is not available')
        base, dims = ref
        if dims:
            return ('ext', 'array')
        if base in _PRIMITIVES:
            return ('prim', base)
        if base == 'var':
            return ('unknown', 'Local variable type is inferred')
        head = base.split('.')[0]
        if member is not None and head in member['type_params']:
            return ('unknown', 'Type parameter ' + head)
        current = tid
        while current is not None:
            if head in self.types[current]['type_params']:
                return ('unknown', 'Type parameter ' + head)
            current = self.types[current]['outer']
        found = self._type_name(base, self.files[self.types[tid]['path']], tid)
        if found[0] == 'in':
            return ('obj', found[1])
        return found

    def _field(self, t, name):
        queue, seen = deque([t]), set()
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            field = self.types[current]['fields'].get(name)
            if field is not None:
                return field, current
            queue.extend(item[1] for item in self._supertypes(current) if item[0] == 'in')
        return None

    def _field_in_scope(self, tid, name):
        current = tid
        while current is not None:
            found = self._field(current, name)
            if found is not None:
                return found
            current = self.types[current]['outer']
        return None

    @staticmethod
    def _local(member, name, position):
        best = None
        for item in member['locals']:
            if item[0] == name and item[2] < position <= item[3] and (best is None or item[2] > best[2]):
                best = item
        return best

    def _accepts(self, mid, argc):
        member = self.members[mid]
        return member['arity'] == argc or (member['varargs'] and argc >= member['arity'] - 1)

    def _declared(self, t, name, argc):
        methods = [m for m in self.types[t]['methods'] if self.members[m]['name'] == name]
        exact = [m for m in methods if self.members[m]['arity'] == argc]
        return exact or [m for m in methods if self.members[m]['varargs'] and argc >= self.members[m]['arity'] - 1]

    def _signature(self, mid):
        return tuple(('?', 0) if p['type'] is None else (p['type'][0].rsplit('.', 1)[-1], p['type'][1])
                     for p in self.members[mid]['params'])

    def _overloads(self, found, current, name, argc):
        """Same-name, same-arity methods of superclasses with other parameter types are overloads too."""
        signatures, result, seen = {self._signature(m) for m in found}, list(found), {current}
        parent = self._superclass(current)
        while parent is not None and parent[0] == 'in' and parent[1] not in seen:
            seen.add(parent[1])
            for mid in self._declared(parent[1], name, argc):
                signature = self._signature(mid)
                if signature not in signatures:
                    signatures.add(signature)
                    result.append(mid)
            parent = self._superclass(parent[1])
        return result

    def _lookup(self, t, name, argc):
        chain, external, current = [], None, t
        while current is not None and current not in chain:
            chain.append(current)
            found = self._declared(current, name, argc)
            if found:
                return {'methods': self._overloads(found, current, name, argc), 'owner': current, 'external': None}
            parent = self._superclass(current)
            if parent is not None and parent[0] != 'in':
                external = parent[1]
                break
            current = parent[1] if parent is not None else None
        queue, seen = deque(item for c in chain for item in self._interfaces(c)), set()
        while queue:
            kind, value = queue.popleft()
            if kind != 'in':
                external = external or value
                continue
            if value in seen:
                continue
            seen.add(value)
            found = self._declared(value, name, argc)
            if found:
                return {'methods': found, 'owner': value, 'external': None}
            queue.extend(self._interfaces(value))
        return {'methods': [], 'owner': None, 'external': external}

    def _overriding(self, t, method):
        return [m for m in self.types[t]['methods'] if self.members[m]['name'] == method['name']
                and self.members[m]['arity'] == method['arity'] and self.members[m]['varargs'] == method['varargs']
                and not self.members[m]['static']]

    def _hints(self, name, argc):
        return [m for m in self.by_name.get(name, ()) if self._accepts(m, argc)]

    # Call resolution.

    def _order(self, mid):
        member = self.members[mid]
        return member['path'], member['start_line'], mid

    def _result(self, status, targets=(), *, declared=None, basis='', hints=(), incomplete=False, external=None,
                root=True, receiver=None, implicit_type=None, family=()):
        hints = sorted(set(hints), key=self._order)
        return {'status': status, 'targets': sorted(set(targets), key=self._order), 'declared': declared,
                'basis': basis, 'hints': hints[:HINT_CAP], 'hints_total': len(hints), 'incomplete': incomplete,
                'external': external, 'root_in_repo': root, 'receiver': receiver, 'implicit_type': implicit_type,
                'family': tuple(family)}

    def resolve(self, call):
        cached = self._resolved.get(call['id'])
        if cached is None:
            try:
                cached = self._resolve(call)
            except RecursionError:
                cached = self._result('unresolved', basis='Resolution exceeded the nesting bound')
            except (IndexError, KeyError, TypeError, AttributeError):
                # An unforeseen layout is an explicit unknown, never a guessed edge.
                cached = self._result('unresolved', basis='Resolution failed on an unsupported construct')
            self._resolved[call['id']] = cached
        return cached

    def _resolve(self, call):
        member = self.members[call['member']]
        tid, form, name, argc = member['type'], call['form'], call['name'], call['argc']
        if form == 'creation':
            return self._construct(self._typeref_value(call['type_ref'], tid, member), call, 'exact')
        if form == 'super_ctor':
            parent = self._superclass(tid)
            if parent is None:
                return self._result('implicit', basis='java.lang.Object constructor')
            if parent[0] != 'in':
                return self._result('external', external=parent[1], basis='Constructor of an external superclass')
            return self._construct(('obj', parent[1]), call, 'super')
        if form == 'this_ctor':
            return self._construct(('obj', tid), call, 'exact')
        if form == 'implicit':
            return self._implicit(call, member)
        receiver = call['receiver']
        if receiver[0] == 'name' and receiver[1] == 'super':
            if call['nested']:
                return self._result('unresolved', basis='super inside a local or anonymous class body')
            return self._super_call(call, tid)
        if receiver[0] == 'field' and receiver[2] == 'super':
            base = self._value(receiver[1], call, member)
            if base[0] == 'static':
                found = self._lookup(base[1], name, argc)
                if found['methods']:
                    return self._choose(base[1], found, call, via='super')
            return self._result('unresolved', basis='Qualified super call is not resolved')
        return self._on_value(self._value(receiver, call, member), call)

    def _construct(self, value, call, status):
        argc = call['argc']
        if value[0] in ('ext', 'ext_static'):
            return self._result('external', external=value[1], receiver=value, basis='Type outside the admitted snapshot')
        if value[0] not in ('obj', 'static'):
            return self._result('unresolved', basis=value[1] if len(value) > 1 else 'Type is not resolved')
        t = value[1]
        info = self.types[t]
        if call.get('anonymous') and info['kind'] == 'interface':
            return self._result('implicit', basis='Anonymous implementation of an in-repo interface')
        constructors = info['constructors']
        exact = [c for c in constructors if self.members[c]['arity'] == argc]
        found = exact or [c for c in constructors if self.members[c]['varargs'] and argc >= self.members[c]['arity'] - 1]
        if len(found) == 1:
            return self._result(status, found, declared=found[0], basis='Declared constructor of ' + info['qualname'])
        if found:
            return self._result('ambiguous', hints=found,
                                basis='Several constructors of %s accept %d argument(s)' % (info['qualname'], argc))
        if (not constructors and argc == 0) or (info['kind'] == 'record' and argc == len(info['components'])):
            return self._result('implicit', implicit_type=t, basis='Compiler-generated constructor of ' + info['qualname'])
        return self._result('unresolved',
                            basis='No declared constructor of %s accepts %d argument(s)' % (info['qualname'], argc))

    def _implicit(self, call, member):
        name, argc = call['name'], call['argc']
        if call['nested'] and name in member['nested_methods']:
            return self._result('unresolved', basis='Method of a local or anonymous class body')
        external, current = None, member['type']
        while current is not None:
            found = self._lookup(current, name, argc)
            if found['methods']:
                return self._choose(current, found, call)
            external = external or found['external']
            current = self.types[current]['outer']
        outside = None
        for item in self.files[member['path']].imports:
            if not item['static']:
                continue
            owner, _, last = item['name'].rpartition('.')
            if item['wildcard']:
                owner = item['name']
            elif last != name:
                continue
            hit = self._fqn(owner)
            if hit is not None and hit[0] == 'in':
                found = self._lookup(hit[1], name, argc)
                if found['methods']:
                    return self._choose(hit[1], found, call, static=True)
                if not item['wildcard']:
                    return self._result('unresolved', basis='Statically imported member is not declared: ' + item['name'])
            elif hit is not None:
                return self._result('unresolved', basis=hit[1])
            elif not item['wildcard']:
                return self._result('external', external=owner, root=False,
                                    basis='Statically imported from outside the admitted snapshot')
            else:
                outside = outside or owner
        if name in _OBJECT_METHODS:
            return self._result('external', external='java.lang.Object', root=False, basis='java.lang.Object method')
        if self.types[member['type']]['kind'] == 'enum' and name in _ENUM_METHODS:
            return self._result('external', external='java.lang.Enum', root=False, basis='java.lang.Enum method')
        if external or outside:
            return self._result('external', external=external or outside, root=False,
                                basis='Method may be inherited or statically imported from outside the admitted snapshot')
        return self._result('unresolved', basis='Method is not declared in the enclosing in-repo types or static '
                                                'imports: ' + name)

    def _super_call(self, call, tid):
        name, argc = call['name'], call['argc']
        parent = self._superclass(tid)
        if parent is None:
            if name in _OBJECT_METHODS:
                return self._result('external', external='java.lang.Object', basis='java.lang.Object method')
            return self._result('unresolved', basis='super call without an in-repo superclass')
        if parent[0] != 'in':
            return self._result('external', external=parent[1], basis='Method of an external superclass')
        found = self._lookup(parent[1], name, argc)
        if found['methods']:
            return self._choose(parent[1], found, call, via='super')
        if found['external'] or name in _OBJECT_METHODS:
            return self._result('external', external=found['external'] or 'java.lang.Object',
                                basis='Method may be inherited from an external supertype')
        return self._result('unresolved', basis='The superclass does not declare ' + name)

    def _choose(self, receiver, found, call, *, static=False, via=None, root=True, value=None):
        methods, owner = found['methods'], found['owner']
        if len(methods) > 1:
            return self._result('ambiguous', hints=methods, root=root, receiver=value,
                                basis='Several same-name methods accept %d argument(s); argument types are not '
                                      'resolved' % call['argc'])
        target = methods[0]
        method = self.members[target]
        if via == 'super':
            if method['body'] is None:
                return self._result('unresolved', declared=target, root=root, basis='super call names an abstract method')
            return self._result('super', [target], declared=target, root=root, receiver=value,
                                basis='Non-virtual super call')
        base = 'exact' if owner == receiver else 'inherited'
        fixed = (static or method['static'] or 'private' in method['modifiers'] or 'final' in method['modifiers']
                 or 'final' in self.types[owner]['modifiers'])
        family = [] if fixed else [receiver] + self._subtypes(receiver)
        overrides = [o for s in family[1:] for o in self._overriding(s, method)]
        anonymous = sorted({self.types[t]['name'] for t in family} & self.anonymous_names)
        if not overrides and not anonymous:
            if method['body'] is None:
                return self._result('unresolved', declared=target, root=root, receiver=value,
                                    basis='Abstract or interface method without an in-repo implementation')
            return self._result(base, [target], declared=target, root=root, receiver=value,
                                basis='Declared in the resolved receiver type' if base == 'exact'
                                else 'Inherited from an in-repo supertype')
        candidates = ([target] if method['body'] is not None else []) + [
            o for o in overrides if self.members[o]['body'] is not None]
        if not candidates:
            return self._result('unresolved', declared=target, root=root, receiver=value, incomplete=True,
                                family=anonymous, basis='Only anonymous classes may implement this method')
        return self._result('dispatch', candidates, declared=target, root=root, receiver=value,
                            incomplete=bool(anonymous), family=anonymous,
                            basis='Runtime dispatch among %d in-repo implementation(s) is not proven' % len(candidates))

    def _on_value(self, value, call):
        name, argc, kind = call['name'], call['argc'], value[0]
        root = self._root_in_repo(call)
        if kind in ('obj', 'static'):
            t = value[1]
            info = self.types[t]
            found = self._lookup(t, name, argc)
            if found['methods']:
                return self._choose(t, found, call, static=(kind == 'static'), root=root, value=value)
            if info['kind'] == 'record' and argc == 0 and any(c['name'] == name for c in info['components']):
                return self._result('implicit', root=root, receiver=value, basis='Record component accessor')
            if name in _OBJECT_METHODS or (info['kind'] == 'enum' and name in _ENUM_METHODS):
                return self._result('external', root=root, receiver=value, basis='Platform method',
                                    external='java.lang.Object' if name in _OBJECT_METHODS else 'java.lang.Enum')
            if found['external']:
                return self._result('external', external=found['external'], root=root, receiver=value,
                                    basis='Method may be inherited from an external supertype')
            return self._result('unresolved', root=root, receiver=value,
                                basis='Method is not declared in the in-repo type ' + info['qualname'])
        if kind in ('ext', 'ext_static'):
            return self._result('external', external=value[1], root=root, receiver=value,
                                basis='Receiver type is outside the admitted snapshot')
        if kind == 'prim':
            return self._result('unresolved', root=root, basis='Primitive receiver')
        basis = value[1] if kind == 'unknown' else 'Receiver name is not a declared local, parameter, field or type: ' + value[1]
        return self._result('unresolved', root=root, hints=self._hints(name, argc), basis=basis)

    def _value(self, desc, call, member):
        kind, tid = desc[0], member['type']
        if kind == 'name':
            return self._name_value(desc[1], desc[2], call, member)
        if kind == 'field':
            return self._member_value(self._value(desc[1], call, member), desc[2])
        if kind == 'call':
            return self._result_value(desc[1])
        if kind == 'new':
            return self._typeref_value(desc[1]['type_ref'], tid, member)
        if kind == 'cast':
            return self._typeref_value(desc[1], tid, member)
        if kind == 'literal':
            return ('ext', 'literal')
        return ('unknown', 'Receiver expression is not a name, call, creation or cast')

    def _name_value(self, word, index, call, member):
        tid = member['type']
        if word == 'this':
            return ('unknown', 'this inside a local or anonymous class body') if call['nested'] else ('obj', tid)
        if word == 'super':
            parent = self._superclass(tid)
            if parent is None:
                return ('ext', 'java.lang.Object')
            return ('obj', parent[1]) if parent[0] == 'in' else ('ext', parent[1])
        local = self._local(member, word, index)
        if local is not None:
            if local[1] is None:
                return ('unknown', 'Local variable without a resolvable declared type: ' + word)
            return self._typeref_value(local[1], tid, member)
        parameter = next((p for p in member['params'] if p['name'] == word), None)
        if parameter is not None:
            return self._typeref_value(parameter['type'], tid, member)
        field = self._field_in_scope(tid, word)
        if field is not None:
            return self._typeref_value(field[0]['type'], field[1])
        found = self._simple_type(word, self.files[member['path']], tid)
        if found is not None:
            if found[0] == 'in':
                return ('static', found[1])
            return ('ext_static', found[1]) if found[0] == 'ext' else found
        if word[:1].isupper():
            return ('ext_static', word)
        return ('pkg', word)

    def _member_value(self, base, segment):
        kind = base[0]
        if segment == 'class' and kind in ('static', 'ext_static'):
            return ('ext', 'java.lang.Class')
        if kind in ('obj', 'static'):
            t = base[1]
            if segment == 'this' and kind == 'static':
                return ('obj', t)
            field = self._field(t, segment)
            if field is not None:
                return self._typeref_value(field[0]['type'], field[1])
            if kind == 'static':
                nested = self._member_type(t, segment)
                if nested is not None:
                    return ('static', nested)
            return ('unknown', 'Member is not declared in %s: %s' % (self.types[t]['qualname'], segment))
        if kind == 'ext_static':
            return ('ext_static' if segment[:1].isupper() else 'ext', base[1] + '.' + segment)
        if kind == 'ext':
            return ('ext', base[1] + '.' + segment)
        if kind == 'pkg':
            name = base[1] + '.' + segment
            if segment[:1].isupper():
                found = self._fqn(name)
                if found is not None:
                    return ('static', found[1]) if found[0] == 'in' else found
                if base[1].split('.')[0] in self.package_roots:
                    return ('unknown', 'Type is not in the admitted snapshot: ' + name)
                return ('ext_static', name)
            return ('pkg', name)
        return base

    def _result_value(self, call):
        result = self.resolve(call)
        if result['status'] == 'external':
            return ('ext', 'result of ' + call['callee'])
        declared = result['declared']
        if declared is not None and result['status'] in ('exact', 'inherited', 'super', 'dispatch'):
            member = self.members[declared]
            if member['result'] is None:
                return ('unknown', 'Called member has no declared result type')
            return self._typeref_value(member['result'], member['type'], member)
        return ('unknown', 'Receiver call is ' + result['status'])

    def _root_in_repo(self, call):
        """Whether the first qualifier of a qualified call names in-repo code (local, parameter, field or type)."""
        if call['form'] != 'qualified':
            return True
        desc = call['receiver']
        while desc[0] == 'field':
            desc = desc[1]
        if desc[0] != 'name':
            return True
        word, index = desc[1], desc[2]
        if word in ('this', 'super'):
            return True
        member = self.members[call['member']]
        if self._local(member, word, index) is not None or any(p['name'] == word for p in member['params']):
            return True
        if self._field_in_scope(member['type'], word) is not None:
            return True
        found = self._simple_type(word, self.files[member['path']], member['type'])
        return found is not None and found[0] == 'in'

    # Navigation queries.

    def follow(self, call):
        """Targets of a provable edge: exact/inherited/super, or a complete single-implementation dispatch."""
        result = self.resolve(call)
        if result['status'] in EDGE_STATUSES:
            return list(result['targets'])
        if result['status'] == 'dispatch' and len(result['targets']) == 1 and not result['incomplete']:
            return list(result['targets'])
        return []

    def route_targets(self, call):
        # A possible implementation is navigation, not an established invocation route.
        return self.follow(call)

    def standard_logger(self, call):
        """The standard logging API type of the receiver, resolved by import or fully qualified name, or None."""
        if call['form'] != 'qualified':
            return None
        receiver = self.resolve(call)['receiver']
        if receiver is not None and receiver[0] == 'ext' and call['name'] in STANDARD_LOGGERS.get(receiver[1], ()):
            return receiver[1]
        return None

    def incoming(self, mid):
        if self._incoming is None:
            index = {}
            for path in sorted(self.files):
                for identifier in self.files[path].calls:
                    call = self.calls[identifier]
                    result = self.resolve(call)
                    if result['status'] in EDGE_STATUSES:
                        for target in result['targets']:
                            index.setdefault(target, []).append(call)
            self._incoming = index
        return list(self._incoming.get(mid, []))

    def member_calls(self, mid):
        return [self.calls[c] for c in self.members[mid]['calls']]

    def file_calls(self, path):
        f = self.files.get(path)
        return [] if f is None else [self.calls[c] for c in f.calls]

    def owner(self, path, first, last):
        """Public record of the innermost member whose declaration span contains path:first-last, or None."""
        member = self.owner_member(path, first, last)
        return None if member is None else self.public(member['id'])

    def owner_member(self, path, first, last):
        f = self.files.get(path)
        if f is None or f.status == 'parse_failed' or type(first) is not int or type(last) is not int:
            return None
        choices = [self.members[m] for m in f.members
                   if self.members[m]['start_line'] <= first <= last <= self.members[m]['end_line']]
        return min(choices, key=lambda m: (m['end_line'] - m['start_line'], m['start_line'])) if choices else None

    def public(self, mid):
        member = self.members[mid]
        return {'id': mid, 'path': member['path'], 'start_line': member['start_line'], 'end_line': member['end_line'],
                'definition_line': member['definition_line'], 'kind': member['kind'], 'name': member['name'],
                'qualname': member['qualname'], 'arity': member['arity'], 'varargs': member['varargs'],
                'signature': member['signature']}

    def span(self, mid):
        member = self.members[mid]
        return member['path'], member['start_line'], member['end_line']

    def method_label(self, mid):
        member = self.members[mid]
        return self.types[member['type']]['qualname'] + '.' + member['signature']

    def contains_site(self, mid, sites):
        member = self.members[mid]
        return any(site.get('path') == member['path'] and member['start_line'] <= site.get('start_line', 0)
                   <= member['end_line'] for site in sites)

    def reaches_site(self, mid, sites):
        """Whether a site lies in mid or its provable callee closure; an exhausted bound counts as reachable."""
        queue, seen = deque([mid]), set()
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if len(seen) > EXPLORE_CAP:
                return True
            if self.contains_site(current, sites):
                return True
            for call in self.member_calls(current):
                queue.extend(self.follow(call))
        return False

    def anonymous_spans(self, names):
        return [(path, first, last) for name, path, first, last in self.anonymous if name in names]

    def init_unfollowed(self, mid=None, t=None):
        """Whether constructing an instance runs code this index does not follow (implicit super(), initializers)."""
        explicit = False
        if mid is not None:
            member = self.members[mid]
            if member['kind'] != 'java_constructor':
                return False
            t = member['type']
            explicit = any(self.calls[c]['form'] in ('super_ctor', 'this_ctor') for c in member['calls'])
        info = self.types[t]
        return info['init_code'] or (info['kind'] == 'class' and bool(info['extends']) and not explicit)

    def site_call(self, site):
        """(call, None) for the call at an inventory site, or (None, 'missing'|'ambiguous'|'parse_failed')."""
        f = self.files.get(site.get('path'))
        if f is None or f.status == 'parse_failed':
            return None, 'parse_failed'
        callee, line, column = site.get('callee'), site.get('start_line'), site.get('column')
        if type(callee) is not str or type(line) is not int:
            return None, 'missing'
        leaf = callee.rsplit('.', 1)[-1]
        # The detector renders only plain dotted chains; a different spelling must still name the same call.
        on_line = [c for c in self.file_calls(f.path) if c['name'] == leaf and line in (c['start_line'], c['name_line'])
                   and (c['callee'] == callee or c['callee'].endswith('.' + callee) or callee.endswith('.' + c['callee'])
                        or c['callee'] == 'new ' + callee)]
        for group in ([c for c in on_line if c['callee'] == callee], on_line):
            chosen = group
            if type(column) is int and len(group) > 1:
                chosen = [c for c in group if column in (c['column'], c['name_column'])]
            if len(chosen) == 1:
                return chosen[0], None
            if len(chosen) > 1 or len(group) > 1:
                return None, 'ambiguous'
        return None, 'missing'

    def calls_in(self, path, first, last):
        return [c for c in self.file_calls(path) if first <= c['start_line'] <= last]

    def describe(self, call):
        """A serializable view of one call and its resolution (diagnostics and tests)."""
        result = self.resolve(call)
        return {'callee': call['callee'], 'name': call['name'], 'form': call['form'], 'line': call['start_line'],
                'column': call['column'], 'end_line': call['end_line'], 'argc': call['argc'],
                'status': result['status'], 'targets': [self.public(m) for m in result['targets']],
                'hints': [self.public(m) for m in result['hints']], 'hints_total': result['hints_total'],
                'incomplete': result['incomplete'], 'external': result['external'], 'basis': result['basis']}


# LoggingNavigation entry points for Java spans.

def related(index, span, maximum, log_sites=(), focus=None):
    """(wanted, notes, pending) for one Java range; see LoggingNavigation.related."""
    from .upstream_masking import published_text
    wanted, notes, pending = [], [], []
    owner = index.owner_member(span['path'], span['start_line'], span['end_line'])
    if owner is None:
        return wanted, notes, [NOT_SUPPORTED]
    for call in index.member_calls(owner['id']):
        if focus and not any(f.get('path') == call['path'] and f.get('start_line', 0) <= call['start_line']
                             <= f.get('end_line', 0) for f in focus):
            continue
        result = index.resolve(call)
        status = result['status']
        if status in EDGE_STATUSES:
            wanted.extend(index.span(t) for t in result['targets'])
        elif status == 'dispatch':
            if len(result['targets']) > maximum:
                pending.append(HINTS_LIMITED)
            wanted.extend(index.span(t) for t in result['targets'][:maximum])
        elif status in ('ambiguous', 'unresolved') and result['hints']:
            hints = result['hints']
            if result['hints_total'] > len(hints):
                pending.append(HINTS_PARTIAL)
            if len(hints) > maximum:
                pending.append(HINTS_LIMITED)
            for hint in hints[:maximum]:
                wanted.append(index.span(hint))
                if index.contains_site(hint, log_sites):
                    pending.append(HINT_WITH_LOG + index.members[hint]['qualname'])
        elif status == 'ambiguous':
            pending.append(UNRESOLVED_TARGET + call['callee'])
        elif status != 'implicit' and not result['family']:
            notes.append('Внешний/динамический вызов ' + published_text(call['callee'])
                         + ': внутренняя реализация не установлена')
        if result['family']:
            # Anonymous classes of the receiver family are not indexed as implementations.
            for path, first, last in index.anonymous_spans(result['family'])[:maximum]:
                wanted.append((path, first, last))
                if any(site.get('path') == path and first <= site.get('start_line', 0) <= last for site in log_sites):
                    pending.append(HINT_WITH_LOG + 'анонимный класс %s (%s:%d)'
                                   % ('/'.join(result['family']), path, first))
    callers = index.incoming(owner['id'])
    if len(callers) > maximum:
        pending.append(CALLERS_LIMITED + owner['qualname'])
    for call in callers[:maximum]:
        wanted.append(index.span(call['member']))
    if focus:
        _transitive(index, owner, focus, log_sites, wanted, pending)
    return wanted, notes, pending


def _transitive(index, owner, focus, log_sites, wanted, pending):
    """Follow provable edges from calls inside the operation's own spans: depth 4, at most 16 more spans."""
    calls = [c for c in index.member_calls(owner['id'])
             if any(type(f) is dict and f.get('path') == c['path'] and f.get('start_line', 0) <= c['start_line']
                    <= f.get('end_line', 0) for f in focus)]
    visited, queue = {owner['id']}, deque()
    for call in calls:
        for target in index.follow(call):
            if target not in visited:
                visited.add(target)
                queue.append((target, 1, [owner['id'], target]))
    added, cut = 0, []
    while queue:
        current, depth, route = queue.popleft()
        for call in index.member_calls(current):
            for target in index.follow(call):
                if target in visited:
                    continue
                visited.add(target)
                if depth + 1 > TRANSITIVE_DEPTH or added >= TRANSITIVE_SPANS:
                    cut.append(route + [target])
                    continue
                wanted.append(index.span(target))
                added += 1
                queue.append((target, depth + 1, route + [target]))
    for route in cut:
        if index.reaches_site(route[-1], log_sites):
            pending.append(ROUTE_CUT.format(route=' → '.join(index.members[m]['qualname'] for m in route)))


def _covered_by(refs, path, first, last):
    return any(r['path'] == path and r['start_line'] <= first and r['end_line'] >= last for r in refs)


def invocation_route(index, invocation, site, argument_evidence, shown):
    """A resolved Java route from the invocation to the member containing the emit; every forwarding call was read."""
    from .source_tools import covering_ranges
    owner = index.owner_member(site['path'], site['start_line'], site['end_line'])
    if owner is None:
        raise LoggingReviewError('Invocation cannot resolve the function containing this physical emit')
    calls = [c for c in index.file_calls(invocation['path'])
             if invocation['start_line'] <= c['start_line'] <= c['end_line'] <= invocation['end_line']
             and ('column' not in invocation or invocation['column'] in (c['column'], c['name_column']))]
    if not _covered_by(argument_evidence, invocation['path'], invocation['start_line'], invocation['end_line']):
        raise LoggingReviewError('Argument evidence must cover the actual invocation')
    for call in calls:
        queue, seen = deque((target, [], [call]) for target in index.route_targets(call)), set()
        while queue:
            current, hops, route = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            if current == owner['id']:
                for hop in hops:
                    if (covering_ranges(shown, hop['path'], hop['start_line'], hop['end_line']) is None
                            or not _covered_by(argument_evidence, hop['path'], hop['start_line'], hop['end_line'])):
                        raise LoggingReviewError('The wrapper route contains an unread/unwitnessed forwarding call: '
                                                 '%s:%d-%d' % (hop['path'], hop['start_line'], hop['end_line']))
                limitations = []
                for step in route:
                    result = index.resolve(step)
                    if result['status'] == 'dispatch':
                        limitations.append(LIMIT_DISPATCH.format(count=len(result['targets']), callee=step['callee']))
                    if result['incomplete']:
                        limitations.append(LIMIT_ANONYMOUS.format(callee=step['callee']))
                return {'status': 'confirmed', 'hops': [{'path': step['path'], 'start_line': step['start_line'], 'end_line': step['end_line'],
                                  'kind': index.resolve(step)['status']} for step in route],
                        'limitations': list(dict.fromkeys(limitations))}
            if len(hops) >= MAX_ROUTE_HOPS:
                continue
            for edge in index.member_calls(current):
                queue.extend((target, hops + [edge], route + [edge]) for target in index.route_targets(edge))
    raise LoggingReviewError('Invocation does not resolve to the function containing this emit')


def target_implementation(index, site, evidence):
    """True when the evidence reads every resolved implementation of the call at site (or its named contract)."""
    call, _ = index.site_call(site)
    if call is not None:
        result = index.resolve(call)
        targets = result['targets'] if (result['status'] in EDGE_STATUSES or (
            result['status'] == 'dispatch' and not result['incomplete'])) else []
        def read(mid):
            member = index.members[mid]
            return any(r['path'] == member['path'] and member['start_line'] <= r['start_line'] <= r['end_line']
                       <= member['end_line'] for r in evidence)
        if targets and all(read(t) for t in targets):
            return True
    return any(r['path'].endswith(('.md', '.txt')) and site['callee'] in r.get('quote', '') for r in evidence)


def _contracts(contracts):
    if contracts is None:
        return {}
    if not isinstance(contracts, (list, tuple)):
        raise LoggingReviewError('Logging contracts must be a list of {callee, basis} objects')
    result = {}
    for item in contracts:
        if (type(item) is not dict or type(item.get('callee')) is not str or not _DOTTED.fullmatch(item['callee'])
                or type(item.get('basis')) is not str or not item['basis'].strip()):
            raise LoggingReviewError('A logging contract needs an exact dotted callee and a nonblank basis')
        result.setdefault(item['callee'], item['basis'])
    return result


def unsupported_plan(site, reason='Навигация цепочек записи реализована только для исходников Java'):
    return {'status': 'unsupported', 'site_id': site.get('id') if type(site) is dict else None, 'hops': [],
            'terminal': None, 'limitations': [], 'reason': reason, 'unresolved': []}


def _terminal_call(index, call, contracts):
    """(terminal, detail) when the call itself is a confirmed logging emission, else None."""
    terminal = {'path': call['path'], 'start_line': call['start_line'], 'end_line': call['end_line'],
                'callee': call['callee']}
    logger = index.standard_logger(call)
    if logger is not None:
        return dict(terminal, basis='standard_logging_type'), logger
    result = index.resolve(call)
    if (result['status'] == 'external' and not result['root_in_repo'] and call['form'] in ('qualified', 'implicit')
            and call['callee'] in contracts):
        return dict(terminal, basis='declared_contract'), contracts[call['callee']]
    return None


def _terminal_in(index, mid, contracts, direct):
    found = []
    for site in direct:
        if index.contains_site(mid, [site]):
            terminal = {'path': site['path'], 'start_line': site['start_line'], 'end_line': site['end_line'],
                        'callee': site['callee'], 'basis': 'detector_log_call'}
            found.append(((site['start_line'], site.get('column') or 0, 2), terminal, None, None))
    for call in index.member_calls(mid):
        hit = _terminal_call(index, call, contracts)
        if hit is not None:
            rank = 0 if hit[0]['basis'] == 'standard_logging_type' else 1
            found.append(((call['start_line'], call['column'], rank), hit[0], hit[1], call))
    return min(found, key=lambda item: item[0]) if found else None


def _forwards(index, mid, call):
    member = index.members[mid]
    names = {p['name'] for p in member['params'] if p['name']} | {item[0] for item in member['locals']}
    return bool(names & set(call['arg_names']))


def logging_chain(index, site, contracts=(), log_sites=()):
    """Deterministic static plan proving (or not) that a non-direct candidate call ends in a logging emission."""
    contracts = _contracts(contracts)
    plan = unsupported_plan(site)
    path = site.get('path') if type(site) is dict else None
    if type(path) is not str or not path.lower().endswith('.java'):
        return plan
    f = index.files.get(path)
    if f is None or f.status == 'parse_failed':
        plan['reason'] = 'Исходник Java не разобран навигатором; цепочка записи не установлена'
        return plan
    call, problem = index.site_call(site)
    if call is None:
        plan['status'] = 'ambiguous' if problem == 'ambiguous' else 'unresolved'
        plan['reason'] = 'Вызов кандидата не сопоставлен однозначно с разобранным вызовом Java'
        return plan
    direct = [s for s in log_sites if type(s) is dict and s.get('kind') == 'log_call']
    hit = _terminal_call(index, call, contracts)
    if hit is not None:
        return _confirmed(index, plan, [], hit[0], hit[1], call, [], contracts)
    issues, flags = [], {'depth': False, 'ambiguous': False, 'external': False, 'unresolved': False}
    queue, visited, explored = deque(), set(), []
    for target, kind in _chain_targets(index, call, issues, flags):
        if target not in visited:
            visited.add(target)
            queue.append([(target, kind, call)])
    while queue:
        route = queue.popleft()
        current = route[-1][0]
        explored.append((current, route[-1][1]))
        if len(explored) > EXPLORE_CAP:
            flags['depth'] = True
            break
        if index.init_unfollowed(mid=current):
            flags['unresolved'] = True
            issues.append(({'path': index.members[current]['path'], 'start_line': index.members[current]['start_line'],
                            'callee': index.members[current]['qualname']}, 'initialization'))
        terminal = _terminal_in(index, current, contracts, direct)
        if terminal is not None:
            return _confirmed(index, plan, route, terminal[1], terminal[2], terminal[3], issues, contracts)
        for line in index.members[current]['method_refs'][:1]:
            flags['unresolved'] = True
            issues.append(({'path': index.members[current]['path'], 'start_line': line, 'callee': '::'},
                           'method_reference'))
        for inner in index.member_calls(current):
            for target, kind in _chain_targets(index, inner, issues, flags):
                if target in visited:
                    continue
                if len(route) >= MAX_ROUTE_HOPS:
                    flags['depth'] = True
                    continue
                visited.add(target)
                queue.append(route + [(target, kind, inner)])
    plan['unresolved'] = _issue_rows(issues)
    externals = [item for item, status in issues if status == 'external']
    if flags['depth']:
        plan['status'] = 'depth_limit'
        plan['reason'] = ('Достигнут предел навигации (%d переходов или %d тел) без подтверждённой записи журнала'
                          % (MAX_ROUTE_HOPS, EXPLORE_CAP))
    elif flags['ambiguous']:
        first = next(item for item, status in issues if status in ('ambiguous', 'dispatch'))
        plan['status'] = 'ambiguous'
        plan['reason'] = 'Цель вызова неоднозначна: ' + first['callee']
    elif flags['external']:
        plan['status'] = 'unconfirmed_external'
        plan['reason'] = ('Цепочка доходит до внешнего вызова %s; контракт журналирования для него не объявлен'
                          % externals[0]['callee'])
    elif flags['unresolved']:
        first = next(item for item, status in issues if status not in ('external',))
        plan['status'] = 'unresolved'
        plan['reason'] = 'Не разрешена цель вызова или инициализация: ' + first['callee']
    else:
        # The whole resolved closure is what a reviewer must see to accept "not a log".
        plan['status'] = 'no_emission'
        plan['reason'] = 'Все вызовы цепочки разрешены внутри репозитория; подтверждённой записи журнала нет'
        plan['limitations'] = [LIMIT_NO_EMISSION]
        plan['hops'] = [{'path': index.members[mid]['path'], 'start_line': index.members[mid]['start_line'],
                         'end_line': index.members[mid]['end_line'], 'method': index.method_label(mid), 'kind': kind,
                         'witness': {'path': index.members[mid]['path'], 'start_line': index.members[mid]['start_line'],
                                     'end_line': index.members[mid]['end_line']}} for mid, kind in explored]
    plan['limitations'] = plan['limitations'] + [
        LIMIT_EXTERNAL.format(callee=item['callee'], path=item['path'], line=item['start_line'])
        for item in externals[:10]]
    return plan


def _chain_targets(index, call, issues, flags):
    result = index.resolve(call)
    status = result['status']
    if status in EDGE_STATUSES:
        return [(target, status) for target in result['targets']]
    if status == 'dispatch' and len(result['targets']) == 1 and not result['incomplete']:
        return [(result['targets'][0], 'dispatch')]
    if status == 'dispatch':
        flags['ambiguous'] = True
        issues.append((call, 'dispatch'))
    elif status == 'implicit':
        if result['implicit_type'] is not None and index.init_unfollowed(t=result['implicit_type']):
            flags['unresolved'] = True
            issues.append((call, 'initialization'))
    elif status == 'external':
        flags['external'] = True
        issues.append((call, 'external'))
    elif status == 'ambiguous':
        flags['ambiguous'] = True
        issues.append((call, 'ambiguous'))
    else:
        flags['unresolved'] = True
        issues.append((call, 'unresolved'))
    return []


def _issue_rows(issues):
    rows = []
    for item, status in issues:
        row = {'path': item['path'], 'line': item['start_line'], 'callee': item['callee'], 'status': status}
        if row not in rows:
            rows.append(row)
    return rows[:50]


def _confirmed(index, plan, route, terminal, detail, terminal_call, issues, contracts):
    hops, limitations = [], [LIMIT_STATIC]
    for position, (mid, kind, via) in enumerate(route):
        member = index.members[mid]
        end = route[position + 1][2]['end_line'] if position + 1 < len(route) else terminal['end_line']
        hops.append({'path': member['path'], 'start_line': member['start_line'], 'end_line': member['end_line'],
                     'method': index.method_label(mid), 'kind': kind,
                     'witness': {'path': member['path'], 'start_line': member['start_line'],
                                 'end_line': max(end, member['start_line'])}})
        if kind == 'dispatch':
            limitations.append(LIMIT_SINGLE_DISPATCH.format(method=index.method_label(mid)))
        if position > 0 and not _forwards(index, route[position - 1][0], via):
            limitations.append(LIMIT_NOT_FORWARDING.format(step=index.method_label(mid)))
    if route and terminal_call is not None and not _forwards(index, route[-1][0], terminal_call):
        limitations.append(LIMIT_NOT_FORWARDING.format(step=terminal['callee']))
    if terminal['basis'] == 'declared_contract':
        limitations.append(LIMIT_CONTRACT.format(callee=terminal['callee'], basis=detail))
    elif terminal['basis'] == 'source_declared_adapter':
        limitations.append('Назначение интерфейса и адаптера как журналирования оценивает модель по исходникам; '
                            'семантическая правильность этой оценки, внешний SDK, выполнение и доставка не проверены автоматически')
    elif terminal['basis'] == 'standard_logging_type':
        limitations.append(LIMIT_STANDARD.format(fqn=detail))
    else:
        limitations.append(LIMIT_DETECTOR)
    if issues:
        limitations.append(LIMIT_OTHER_CALLS.format(count=len(_issue_rows(issues))))
    labels = {'source_declared_adapter': 'интерпретация исходного интерфейса и адаптера моделью',
              'standard_logging_type': 'стандартный API журналирования по типу получателя',
              'declared_contract': 'объявленный контракт внешнего вызова',
              'detector_log_call': 'вызов журналирования, найденный детектором'}
    plan.update(status='confirmed', hops=hops, terminal=terminal, limitations=list(dict.fromkeys(limitations)),
                unresolved=_issue_rows(issues),
                reason='Цепочка из %d переход(ов) завершается подтверждённой записью журнала (%s): %s'
                       % (len(hops), labels[terminal['basis']], terminal['callee']))
    return plan


def verify_logging_chain(plan, refs, shown):
    """None when every hop witness and the terminal call were delivered and cited; LoggingReviewError otherwise."""
    from .source_tools import covering_ranges
    if type(plan) is not dict or plan.get('status') != 'confirmed' or type(plan.get('terminal')) is not dict:
        raise LoggingReviewError('The logging chain is not confirmed: '
                                 + str(plan.get('status') if type(plan) is dict else None))
    if type(refs) not in (list, tuple) or type(shown) not in (list, tuple):
        raise LoggingReviewError('Logging chain verification needs reference and delivery lists')
    spans = [hop.get('witness') for hop in plan.get('hops', [])] + [plan['terminal']]
    if plan.get('source_contract'):
        spans += [plan['source_contract']['declaration'], plan['source_contract']['implementation']]
    for span in spans:
        if (type(span) is not dict or type(span.get('path')) is not str or type(span.get('start_line')) is not int
                or type(span.get('end_line')) is not int):
            raise LoggingReviewError('The logging chain has a malformed witness span')
        path, first, last = span['path'], span['start_line'], span['end_line']
        label = '%s:%d-%d' % (path, first, last)
        if covering_ranges(shown, path, first, last) is None:
            raise LoggingReviewError('Logging chain span was not delivered to this review: ' + label)
        if covering_ranges(refs, path, first, last) is None:
            raise LoggingReviewError('Logging chain span is not cited by the binding evidence: ' + label)
    return None

def source_adapter_candidates(index, site):
    """Navigation only: source-declared interface adapters on one provable route.

    The model, not the parser, judges whether a contract describes logging. No method-name dictionary, external
    SDK name or interface name is treated as semantic proof. Only an exact interface signature implemented by the
    reached concrete method can be proposed; ambiguous dispatch/overloads cannot produce a route.
    """
    import hashlib
    call, _ = index.site_call(site)
    if call is None:
        return []
    queue = deque([(target, kind, call)] for target, kind in
                  ((target, index.resolve(call)['status']) for target in index.follow(call)))
    seen, candidates = set(), []
    def signature(mid):
        member = index.members[mid]
        return tuple(None if p['type'] is None else
                     (index._type_name(p['type'][0], index.files[member['path']], member['type']), p['type'][1])
                     for p in member['params'])
    while queue and len(seen) < EXPLORE_CAP:
        route = queue.popleft()
        mid = route[-1][0]
        if mid in seen:
            continue
        seen.add(mid)
        member = index.members[mid]
        parents, visited, declarations = deque(index._supertypes(member['type'])), set(), []
        while parents:
            kind, tid = parents.popleft()
            if kind != 'in' or tid in visited:
                continue
            visited.add(tid)
            if index.types[tid]['kind'] == 'interface':
                for abstract in index._declared(tid, member['name'], member['arity']):
                    contract = index.members[abstract]
                    if (contract['body'] is None and signature(abstract) == signature(mid)
                            and None not in signature(mid)):
                        declarations.append(contract)
            parents.extend(index._supertypes(tid))
        # Multiple unrelated declarations are not silently collapsed to an asserted contract.
        if len(declarations) == 1:
            declaration = declarations[0]
            for terminal_call in index.member_calls(mid):
                result = index.resolve(terminal_call)
                if result['status'] != 'external' or result['root_in_repo']:
                    continue
                terminal = {k: terminal_call[k] for k in ('path', 'start_line', 'end_line', 'callee')}
                terminal['basis'] = 'source_declared_adapter'
                plan = _confirmed(index, unsupported_plan(site), route, terminal, None, terminal_call, [], {})
                witness = lambda m: {k: m[k] for k in ('path', 'start_line', 'end_line')}
                identity = (site['id'], declaration['id'], mid, terminal_call['id'])
                plan['status'] = 'needs_interpretation'
                plan['source_contract'] = {
                    'candidate_id': 'adapter-' + hashlib.sha256(repr(identity).encode()).hexdigest()[:20],
                    'declaration': witness(declaration), 'implementation': witness(member),
                    'authority': 'model_interpretation_of_source'}
                plan['reason'] = ('Проверяемая цепочка к реализации интерфейса; назначение контракта как '
                                  'журналирования ещё не оценено. Это только навигация.')
                candidates.append(plan)
        if len(route) < MAX_ROUTE_HOPS:
            for inner in index.member_calls(mid):
                queue.extend(route + [(target, index.resolve(inner)['status'], inner)]
                             for target in index.follow(inner) if target not in seen)
    return candidates[:16]
