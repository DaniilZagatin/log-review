"""Logging-chain plans for non-direct Java candidates and the opt-in declaration filter of discovery.

Synthetic, independent Java classes only (see test_map_first_java_navigation for the shared fixtures). A wrapper
is confirmed only by a terminal standard logging type, an operator-declared external contract or a detector
log_call; a method name, a configured call name or a *Log suffix is never sufficient.
"""
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.java_logging_graph import LIMIT_NO_EMISSION, LIMIT_STATIC
from process_observability.log_discovery import discover_logging
from process_observability.logging_contracts import LoggingReviewError
from process_observability.source_tools import snapshot_repository
from test_map_first_java_navigation import (BASE_JOURNAL, DESK, DESK_CALLS, DESK_FILES, DESK_TEXT, FACADE, JOURNAL,
                                            ORDER_FILES, ORDERS, build_navigation, line_of, pick_site)

CONTRACT = [{'callee': 'Vendor.Sdk.debugLog', 'basis': 'Объявление оператора: внешний журнал SDK поставщика'}]

BILLING = 'src/com/example/billing/'
RECEIPT_JOURNAL = """package com.example.billing;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class ReceiptJournal {
    private static final Logger LOG = LoggerFactory.getLogger(ReceiptJournal.class);

    public void note(String text) {
        String line = "receipt " + text;
        LOG.info(line);
    }
}
"""
UNTYPED_JOURNAL = """package com.example.billing;

public class UntypedJournal {
    private Logger log;

    public void note(String text) {
        log.info(text);
    }
}
"""
BILLING_FACADE = """package com.example.billing;

public class BillingFacade {
    private final ReceiptJournal receipts = new ReceiptJournal();
    private UntypedJournal untyped;

    public void close(String invoiceId) {
        receipts.note(invoiceId);
    }

    public void audit(String invoiceId) {
        untyped.note(invoiceId);
    }
}
"""
BILLING_FILES = {BILLING + 'ReceiptJournal.java': RECEIPT_JOURNAL, BILLING + 'UntypedJournal.java': UNTYPED_JOURNAL,
                 BILLING + 'BillingFacade.java': BILLING_FACADE}


class WrapperChainTests(unittest.TestCase):
    def setUp(self):
        self.navigation, self.sites = build_navigation(self, ORDER_FILES, ('journal.record',))
        self.facade, self.journal, self.base = (ORDERS + 'OrderFacade.java', ORDERS + 'Journal.java',
                                                ORDERS + 'BaseJournal.java')
        self.site = pick_site(self.sites, self.facade, line_of(FACADE, 'Level.INFO, true'))
        self.start = line_of(JOURNAL, 'boolean withOrder')
        self.forward = line_of(JOURNAL, 'super.write')
        self.write = line_of(BASE_JOURNAL, 'protected void write')
        self.call = line_of(BASE_JOURNAL, 'Vendor.Sdk.debugLog')

    def test_inherited_wrapper_chain_ending_in_a_declared_contract_is_confirmed_with_witnesses(self):
        plan = self.navigation.logging_chain(self.site, CONTRACT, self.sites)
        self.assertEqual(plan['status'], 'confirmed', plan)
        self.assertEqual(plan['site_id'], self.site['id'])
        self.assertEqual([hop['kind'] for hop in plan['hops']], ['exact', 'super'])
        self.assertEqual([hop['method'] for hop in plan['hops']],
                         ['Journal.record(String,Level,boolean)', 'BaseJournal.write(Options)'])
        self.assertEqual(plan['hops'][0]['witness'], {'path': self.journal, 'start_line': self.start, 'end_line': self.forward})
        self.assertEqual(plan['hops'][1]['witness'], {'path': self.base, 'start_line': self.write, 'end_line': self.call})
        self.assertEqual(plan['terminal'], {'path': self.base, 'start_line': self.call, 'end_line': self.call,
                                            'callee': 'Vendor.Sdk.debugLog', 'basis': 'declared_contract'})
        self.assertIn(LIMIT_STATIC, plan['limitations'])
        self.assertTrue(any('объявленном оператором контракте' in item and CONTRACT[0]['basis'] in item
                            and 'отсутствует в репозитории' in item for item in plan['limitations']))
        self.assertTrue(plan['reason'])

    def test_two_argument_overload_forwards_to_the_three_argument_overload(self):
        site = pick_site(self.sites, self.facade, line_of(FACADE, 'Level.DEBUG'))
        plan = self.navigation.logging_chain(site, CONTRACT, self.sites)
        self.assertEqual(plan['status'], 'confirmed', plan)
        self.assertEqual([hop['method'] for hop in plan['hops']],
                         ['Journal.record(String,Level)', 'Journal.record(String,Level,boolean)',
                          'BaseJournal.write(Options)'])
        self.assertEqual(plan['hops'][0]['witness']['end_line'], line_of(JOURNAL, 'record(text, level, false)'))

    def test_without_a_declared_contract_the_external_sdk_stays_unconfirmed(self):
        for contracts in ((), [{'callee': 'Vendor.Sdk.traceLog', 'basis': 'Другой внешний вызов'}]):
            plan = self.navigation.logging_chain(self.site, contracts, self.sites)
            self.assertEqual(plan['status'], 'unconfirmed_external', plan)
            self.assertIsNone(plan['terminal'])
            self.assertTrue(any('Vendor.Sdk.debugLog' in item and 'контракт журналирования не объявлен' in item
                                for item in plan['limitations']))
            self.assertIn({'path': self.base, 'line': self.call, 'callee': 'Vendor.Sdk.debugLog', 'status': 'external'},
                          plan['unresolved'])

    def test_verification_requires_every_witness_to_be_delivered_and_cited(self):
        plan = self.navigation.logging_chain(self.site, CONTRACT, self.sites)
        terminal = {key: plan['terminal'][key] for key in ('path', 'start_line', 'end_line')}
        spans = [hop['witness'] for hop in plan['hops']] + [terminal]
        self.assertIsNone(self.navigation.verify_logging_chain(plan, spans, spans))
        uncited = [span for span in spans if span != plan['hops'][1]['witness']]
        with self.assertRaises(LoggingReviewError) as caught:
            self.navigation.verify_logging_chain(plan, uncited, spans)
        self.assertIn('not cited', str(caught.exception))
        self.assertIn('%s:%d-%d' % (self.base, self.write, self.call), str(caught.exception))
        unread = [span for span in spans if span != plan['hops'][0]['witness']]
        with self.assertRaises(LoggingReviewError) as caught:
            self.navigation.verify_logging_chain(plan, spans, unread)
        self.assertIn('not delivered', str(caught.exception))
        self.assertIn('%s:%d-%d' % (self.journal, self.start, self.forward), str(caught.exception))
        unconfirmed = self.navigation.logging_chain(self.site, (), self.sites)
        with self.assertRaises(LoggingReviewError):
            self.navigation.verify_logging_chain(unconfirmed, spans, spans)

    def test_malformed_contracts_and_non_java_sites_are_explicit(self):
        with self.assertRaises(LoggingReviewError):
            self.navigation.logging_chain(self.site, [{'callee': 'not a callee', 'basis': 'x'}], self.sites)
        with self.assertRaises(LoggingReviewError):
            self.navigation.logging_chain(self.site, [{'callee': 'Vendor.Sdk.debugLog', 'basis': ' '}], self.sites)
        plan = self.navigation.logging_chain({'id': 'py-site', 'path': 'tool.py', 'start_line': 1, 'end_line': 1,
                                              'column': 0, 'callee': 'emit', 'kind': 'generic_emit'})
        self.assertEqual((plan['status'], plan['site_id'], plan['hops'], plan['terminal']),
                         ('unsupported', 'py-site', [], None))


class StandardLoggerTests(unittest.TestCase):
    def setUp(self):
        self.navigation, self.sites = build_navigation(self, BILLING_FILES, ('receipts.note', 'untyped.note'))
        self.facade = BILLING + 'BillingFacade.java'

    def test_wrapper_ending_in_an_imported_standard_logger_is_confirmed_by_type(self):
        site = pick_site(self.sites, self.facade, line_of(BILLING_FACADE, 'receipts.note'))
        plan = self.navigation.logging_chain(site, (), ())
        self.assertEqual(plan['status'], 'confirmed', plan)
        log = line_of(RECEIPT_JOURNAL, 'LOG.info(line)')
        self.assertEqual(plan['terminal'], {'path': BILLING + 'ReceiptJournal.java', 'start_line': log, 'end_line': log,
                                            'callee': 'LOG.info', 'basis': 'standard_logging_type'})
        self.assertEqual(plan['hops'][0]['witness'], {'path': BILLING + 'ReceiptJournal.java',
                                                      'start_line': line_of(RECEIPT_JOURNAL, 'public void note'),
                                                      'end_line': log})
        self.assertIn(LIMIT_STATIC, plan['limitations'])
        self.assertTrue(any('org.slf4j.Logger' in item for item in plan['limitations']))
        # The detector also sees this call; the type-proven basis is preferred at the same position.
        self.assertEqual(self.navigation.logging_chain(site, (), self.sites)['terminal']['basis'], 'standard_logging_type')

    def test_an_unimported_logger_name_is_not_a_standard_logger(self):
        site = pick_site(self.sites, self.facade, line_of(BILLING_FACADE, 'untyped.note'))
        plan = self.navigation.logging_chain(site, (), ())
        self.assertEqual(plan['status'], 'unconfirmed_external', plan)
        self.assertTrue(any('log.info' in item for item in plan['limitations']))
        # A detector log_call is a separate, explicitly labelled basis whose receiver type is not verified.
        detected = self.navigation.logging_chain(site, (), self.sites)
        self.assertEqual((detected['status'], detected['terminal']['basis']), ('confirmed', 'detector_log_call'))
        self.assertTrue(any('тип получателя навигатором не проверялся' in item for item in detected['limitations']))


class NegativeChainTests(unittest.TestCase):
    def setUp(self):
        self.navigation, self.sites = build_navigation(self, DESK_FILES, DESK_CALLS)
        self.desk = DESK + 'Desk.java'

    def plan(self, fragment, contracts=()):
        return self.navigation.logging_chain(pick_site(self.sites, self.desk, line_of(DESK_TEXT, fragment)), contracts,
                                             self.sites)

    def test_external_terminal_named_like_a_log_is_not_confirmed_without_its_contract(self):
        for contracts in ((), CONTRACT):
            plan = self.plan('status.info(item)', contracts)
            self.assertEqual(plan['status'], 'unconfirmed_external', plan)
            self.assertTrue(any('Vendor.Sdk.infoLog' in item and 'контракт журналирования не объявлен' in item
                                for item in plan['limitations']))

    def test_in_repo_only_publish_is_no_emission_with_its_whole_closure(self):
        plan = self.plan('notifier.publish(item)')
        self.assertEqual(plan['status'], 'no_emission', plan)
        self.assertEqual([hop['method'] for hop in plan['hops']], ['Notifier.publish(String)', 'EventBuffer.push(String)'])
        self.assertTrue(all(hop['witness'] == {k: hop[k] for k in ('path', 'start_line', 'end_line')}
                            for hop in plan['hops']))
        self.assertIn(LIMIT_NO_EMISSION, plan['limitations'])
        self.assertIsNone(plan['terminal'])

    def test_typed_receiver_never_borrows_a_same_name_method_that_logs(self):
        plan = self.plan('stock.record(item)')
        self.assertEqual(plan['status'], 'no_emission', plan)
        self.assertEqual([hop['method'] for hop in plan['hops']], ['Inventory.record(String)'])

    def test_contract_naming_an_in_repo_class_cannot_override_its_code(self):
        plan = self.plan('Telemetry.emit(item)', [{'callee': 'Telemetry.emit', 'basis': 'Ошибочное объявление'}])
        self.assertEqual(plan['status'], 'no_emission', plan)
        self.assertEqual([hop['method'] for hop in plan['hops']], ['Telemetry.emit(String)'])

    def test_ambiguous_overload_and_unknown_receiver_do_not_become_chains(self):
        ambiguous = self.plan('printer.print(item)')
        self.assertEqual((ambiguous['status'], ambiguous['hops'], ambiguous['terminal']), ('ambiguous', [], None))
        unknown = self.plan('ledger.record(item)')
        self.assertEqual((unknown['status'], unknown['hops'], unknown['terminal']), ('unresolved', [], None))
        self.assertEqual(unknown['unresolved'][0]['callee'], 'ledger.record')


CLERK = """public class Clerk {
    public void publish(String event) {
        publish(event, 1);
    }

    void publish(String event, int count) {
        emit(event);
    }

    public abstract void emit(String event);

    @Override
    public java.util.List<String> publish(
            java.util.Map<String, Integer> values) throws Exception {
        return null;
    }

    void run() { publish("x"); if (ready) emit("y"); }
}
"""
PUBLISHER = """public interface Publisher {
    @Deprecated
    <T> java.util.List<T> publish(T event) throws java.io.IOException;

    void emit(final String event, Object... extras);

    default void emit() { publish(null); }
}
"""


class DeclarationFilterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'src').mkdir()
        (root / 'src' / 'Clerk.java').write_text(CLERK, encoding='utf-8')
        (root / 'src' / 'Publisher.java').write_text(PUBLISHER, encoding='utf-8')
        self.snapshot = snapshot_repository(root, max_files=None, max_chars=None)

    @staticmethod
    def lines(inventory, path):
        return [site['start_line'] for site in inventory['sites'] if site['path'] == path]

    def test_default_discovery_is_unchanged_and_reports_declaration_headers(self):
        default = discover_logging(self.snapshot)
        explicit = discover_logging(self.snapshot, exclude_declarations=False)
        self.assertEqual(json.dumps(default, ensure_ascii=False), json.dumps(explicit, ensure_ascii=False))
        self.assertNotIn('declaration_filter', default['scope'])
        self.assertEqual(self.lines(default, 'src/Clerk.java'), [2, 3, 6, 7, 10, 13, 18, 18])
        self.assertEqual(self.lines(default, 'src/Publisher.java'), [3, 5, 7, 7])
        with self.assertRaises(ValueError):
            discover_logging(self.snapshot, exclude_declarations=1)

    def test_opt_in_filter_drops_declaration_headers_and_keeps_invocations(self):
        default = discover_logging(self.snapshot, include_columns=True)
        filtered = discover_logging(self.snapshot, include_columns=True, exclude_declarations=True)
        self.assertEqual(filtered['scope']['declaration_filter'], 'c_like_declarations/1')
        self.assertEqual(self.lines(filtered, 'src/Clerk.java'), [3, 7, 18, 18])
        self.assertEqual(self.lines(filtered, 'src/Publisher.java'), [7])
        kept = {('src/Clerk.java', 3), ('src/Clerk.java', 7), ('src/Clerk.java', 18), ('src/Publisher.java', 7)}
        expected = [site for site in default['sites'] if (site['path'], site['start_line']) in kept
                    and not (site['path'] == 'src/Publisher.java' and site['callee'] == 'emit')]
        self.assertEqual(filtered['sites'], expected)
        self.assertEqual([site['callee'] for site in filtered['sites'] if site['path'] == 'src/Publisher.java'], ['publish'])


if __name__ == '__main__':
    unittest.main()
