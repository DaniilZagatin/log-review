"""Exact static navigation for addressed log review; never a business map.

This reuses the existing deterministic graph tool. It is built lazily over the
already admitted texts and is not a model/technical-review stage.

Java files dispatch to java_logging_graph, a source-bound index used only by this
map-first review path (owners, typed call resolution, wrapper routes and logging
chains). Every other file keeps the code_graph path unchanged.
"""
from .logging_contracts import LoggingReviewError


class LoggingNavigation:
    def __init__(self, snapshot):
        import warnings
        from .code_graph import build_graph
        from .source_tools import RepoSnapshot, SourceFile
        # Parse admitted original text privately for structure. Never export raw arguments/literals.
        # The model and its citations still receive the shared masked text, which may not be valid syntax.
        structural = RepoSnapshot({p: SourceFile(p, s.sha256, s._raw_text, s._raw_text) for p, s in snapshot.files.items()},
                                  dict(snapshot.scope))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            self.graph = build_graph(structural, {'sites': [], 'wrappers': []})
        self.data = self.graph.to_dict()
        self.nodes = {n['id']: n for n in self.data['nodes']}
        self.callable_kinds = {'function', 'async_function', 'method'}
        self._java_sources = {p: s._raw_text for p, s in snapshot.files.items() if self._is_java(p)}
        self._java_index = None

    @staticmethod
    def _is_java(path):
        return type(path) is str and path.lower().endswith('.java')

    @property
    def java(self):
        """The lazily built Java index of the admitted .java texts (java_logging_graph.JavaIndex)."""
        if self._java_index is None:
            from .java_logging_graph import JavaIndex
            self._java_index = JavaIndex(self._java_sources)
        return self._java_index

    def owner(self, ref):
        if self._is_java(ref['path']):
            return self.java.owner(ref['path'], ref['start_line'], ref['end_line'])
        choices = [n for n in self.graph.decls_in(ref['path'], ref['start_line'], ref['end_line'])
                   if n['kind'] in self.callable_kinds and n['start_line'] <= ref['start_line'] <= ref['end_line'] <= n['end_line']]
        return min(choices, key=lambda n: n['end_line'] - n['start_line']) if choices else None

    def related(self, ranges, maximum, log_sites=(), focus=None):
        """(wanted spans, notes, pending). focus (Java only): the operation's own anchor spans whose calls are
        followed transitively through provable edges (depth 4, at most 16 more method spans)."""
        wanted, notes, pending = [], [], []
        for span in ranges:
            if self._is_java(span['path']):
                from .java_logging_graph import related
                found, java_notes, java_pending = related(self.java, span, maximum, log_sites, focus)
                wanted.extend(found)
                notes.extend(java_notes)
                pending.extend(java_pending)
                continue
            owner = self.owner(span)
            if owner is None:
                pending.append('Навигация вызовов в этом участке не поддержана; отсутствие логов не установлено')
                continue
            outgoing = self.graph.outgoing(owner['id'])
            if focus:
                outgoing = [c for c in outgoing if any(f['path'] == c['path'] and f['start_line'] <= c['start_line']
                                                       <= f['end_line'] for f in focus)]
            for call in outgoing:
                target = self.nodes.get(call.get('target_id'))
                if target and target['kind'] in self.callable_kinds:
                    wanted.append((target['path'], target['start_line'], target['end_line']))
                elif call.get('status') in ('ambiguous_binding', 'unresolved_receiver') and call.get('not_edges'):
                    hints = [self.nodes.get(h.get('target_id')) for h in call['not_edges']]
                    hints = [h for h in hints if h is not None]
                    if call.get('not_edges_total', len(call['not_edges'])) > len(hints):
                        pending.append('Часть подсказок возможных реализаций не выдана статическим навигатором')
                    if len(hints) > maximum: pending.append('Ограничен список возможных реализаций получателя')
                    for hint in hints[:maximum]:
                        wanted.append((hint['path'], hint['start_line'], hint['end_line']))
                        if any(s['path'] == hint['path'] and hint['start_line'] <= s['start_line'] <= hint['end_line'] for s in log_sites):
                            pending.append('Не разрешена связь с возможной реализацией, содержащей запись: ' + hint.get('qualname', hint['id']))
                elif call.get('status') == 'ambiguous_binding':
                    pending.append('Не разрешена локальная цель вызова ' + call['callee'])
                else:
                    from .upstream_masking import published_text
                    notes.append('Внешний/динамический вызов ' + published_text(call['callee']) + ': внутренняя реализация не установлена')
            incoming = self.graph.incoming(owner['id'])
            if len(incoming) > maximum:
                pending.append('Ограничен точный список вызывающих мест для ' + owner.get('qualname', owner['id']))
            for call in incoming[:maximum]:
                caller = self.nodes.get(call.get('owner_id'))
                if caller and caller['kind'] in self.callable_kinds:
                    wanted.append((caller['path'], caller['start_line'], caller['end_line']))
                else:
                    wanted.append((call['path'], call['start_line'], call['end_line']))
        return wanted, list(dict.fromkeys(notes)), list(dict.fromkeys(pending))

    def candidate_scopes(self, ranges, sites, focus=None):
        """Separate operation/callee candidates from caller navigation; this establishes no business meaning.

        Walk only proved edges in the already built index. Ambiguous targets and exhausted navigation remain
        blocking in related(); a candidate in a caller is still available for inspection/binding, not a new
        completeness obligation for every operation that caller happens to invoke.
        """
        owners = [self.owner(r) for r in ranges]
        reachable = {o['id'] for o in owners if o}
        queue = [(o['id'], self._is_java(o['path']), 0) for o in owners if o]
        while queue:
            current, java, depth = queue.pop(0)
            if depth >= 6: continue  # binding route has the same bounded source navigation
            targets = []
            if java:
                calls = self.java.member_calls(current)
            else:
                calls = self.graph.outgoing(current)
            if depth == 0 and focus:
                calls = [c for c in calls if any(f['path'] == c['path'] and f['start_line'] <= c['start_line']
                                               <= f['end_line'] for f in focus)]
            if java:
                for call in calls: targets.extend(self.java.follow(call))
            else:
                targets = [c.get('target_id') for c in calls
                           if c.get('target_id') in self.nodes and self.nodes[c['target_id']]['kind'] in self.callable_kinds]
            for target in targets:
                if target not in reachable:
                    reachable.add(target); queue.append((target, java, depth + 1))
        result = {}
        for site in sites:
            if any(r['path'] == site['path'] and r['start_line'] <= site['start_line']
                   and r['end_line'] >= site['end_line'] for r in ranges):
                scope = 'operation_code'
            else:
                owner = self.owner(site)
                scope = 'callee' if owner and owner['id'] in reachable else 'navigation_context'
            result[site['id']] = scope
        return result

    def invocation_route(self, invocation, site, argument_evidence, shown):
        """A real resolved call route, whose forwarding sites were actually supplied.

        Python returns True. Java returns {'hops': [{'path', 'start_line', 'end_line', 'kind'}], 'limitations': [...]}
        where the first hop is the invocation itself and kind is its resolution status."""
        if self._is_java(site['path']) or self._is_java(invocation['path']):
            if not (self._is_java(site['path']) and self._is_java(invocation['path'])):
                raise LoggingReviewError('Invocation does not resolve to the function containing this emit')
            from .java_logging_graph import invocation_route
            return invocation_route(self.java, invocation, site, argument_evidence, shown)
        from .source_tools import covering_ranges
        owner = self.owner({'path': site['path'], 'start_line': site['start_line'], 'end_line': site['end_line']})
        if owner is None:
            raise LoggingReviewError('Invocation cannot resolve the function containing this physical emit')
        calls = [c for c in self.data['calls'] if c['path'] == invocation['path']
                 and invocation['start_line'] <= c['start_line'] <= c['end_line'] <= invocation['end_line']
                 and ('column' not in invocation or c.get('column') == invocation['column'])]
        if not any(r['path'] == invocation['path'] and r['start_line'] <= invocation['start_line']
                   and r['end_line'] >= invocation['end_line'] for r in argument_evidence):
            raise LoggingReviewError('Argument evidence must cover the actual invocation')
        for call in calls:
            queue = [(call.get('target_id'), [])]; seen = set()
            while queue:
                current, hops = queue.pop(0)
                if current is None or current in seen: continue
                seen.add(current)
                if current == owner['id']:
                    for hop in hops:
                        if (covering_ranges(shown, hop['path'], hop['start_line'], hop['end_line']) is None
                                or not any(r['path'] == hop['path'] and r['start_line'] <= hop['start_line']
                                           and r['end_line'] >= hop['end_line'] for r in argument_evidence)):
                            raise LoggingReviewError('The wrapper route contains an unread/unwitnessed forwarding call')
                    return True
                if len(hops) >= 6: continue
                queue.extend((edge.get('target_id'), hops + [edge]) for edge in self.graph.outgoing(current))
        raise LoggingReviewError('Invocation does not resolve to the function containing this emit')

    def target_implementation(self, site, evidence):
        if self._is_java(site['path']):
            from .java_logging_graph import target_implementation
            return target_implementation(self.java, site, evidence)
        calls = [c for c in self.data['calls'] if c['path'] == site['path'] and c['start_line'] == site['start_line']
                 and c.get('column') == site.get('column', c.get('column')) and c.get('callee') == site['callee']]
        for call in calls:
            owner = self.nodes.get(call.get('target_id'))
            if owner and any(r['path'] == owner['path'] and owner['start_line'] <= r['start_line'] <= r['end_line'] <= owner['end_line'] for r in evidence):
                return True
        return any(r['path'].endswith(('.md', '.txt')) and site['callee'] in r['quote'] for r in evidence)

    def logging_chain(self, site, contracts=(), log_sites=()):
        """Deterministic plan for a non-direct candidate site (configured_call / wrapper_call / generic_emit).

        Returns {'status', 'site_id', 'hops', 'terminal', 'limitations', 'reason', 'unresolved'}; status is one of
        confirmed, no_emission, unconfirmed_external, ambiguous, unresolved, depth_limit or unsupported (non-Java)."""
        from .java_logging_graph import logging_chain, unsupported_plan
        if type(site) is not dict or not self._is_java(site.get('path')):
            return unsupported_plan(site)
        return logging_chain(self.java, site, contracts, log_sites)

    def verify_logging_chain(self, plan, refs, shown):
        """None when every hop witness and the terminal call of a confirmed plan were delivered and cited."""
        from .java_logging_graph import verify_logging_chain
        return verify_logging_chain(plan, refs, shown)

    def source_adapter_candidates(self, site):
        if not self._is_java(site['path']): return []
        from .java_logging_graph import source_adapter_candidates
        return source_adapter_candidates(self.java, site)
