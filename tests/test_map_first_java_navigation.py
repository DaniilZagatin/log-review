"""Java navigation for the map-first logging review: owners, typed call resolution, related spans and routes.

Every Java class here is synthetic and independent of any analyzed target. The text is only lexed as data by our
own deterministic parser; nothing is compiled, imported or executed.
"""
from pathlib import Path
import tempfile
import unittest

from process_observability.log_discovery import discover_logging
from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_navigation import LoggingNavigation
from process_observability.source_tools import snapshot_repository

NOT_SUPPORTED = 'Навигация вызовов в этом участке не поддержана; отсутствие логов не установлено'
HINT_WITH_LOG = 'Не разрешена связь с возможной реализацией, содержащей запись: '
ROUTE_CUT = 'Маршрут вызовов прослежен не до конца из-за ограничения навигации'

# Orders: a wrapper inherited through a superclass field, overloads by arity, super hop, external SDK terminal.
ORDERS = 'src/com/example/orders/'
LEVEL = """package com.example.orders;

public enum Level {
    INFO, DEBUG
}
"""
OPTIONS = """package com.example.orders;

public class Options {
    private final String text;
    private final Level level;

    public Options(String text, Level level) {
        this.text = text;
        this.level = level;
    }

    public String getText() {
        return text;
    }

    public Level getLevel() {
        return level;
    }
}
"""
BASE_JOURNAL = """package com.example.orders;

import com.acme.telemetry.Vendor;

public abstract class BaseJournal {
    protected void write(Options options) {
        Vendor.Sdk.debugLog(options.getText(), options.getLevel());
    }
}
"""
JOURNAL = """package com.example.orders;

public class Journal extends BaseJournal {
    public void record(String text, Level level) {
        record(text, level, false);
    }

    public void record(String text, Level level, boolean withOrder) {
        Options options = new Options(text, level);
        super.write(options);
    }
}
"""
BASE_CLIENT = """package com.example.orders;

public class BaseClient {
    protected Journal journal = new Journal();
}
"""
FACADE = """package com.example.orders;

public class OrderFacade extends BaseClient {
    public void accept(String orderId) {
        String message = "accepted " + orderId;
        journal.record(message, Level.INFO, true);
    }

    public void touch(String orderId) {
        journal.record(orderId, Level.DEBUG);
    }
}
"""
ORDER_FILES = {ORDERS + 'Level.java': LEVEL, ORDERS + 'Options.java': OPTIONS,
               ORDERS + 'BaseJournal.java': BASE_JOURNAL, ORDERS + 'Journal.java': JOURNAL,
               ORDERS + 'BaseClient.java': BASE_CLIENT, ORDERS + 'OrderFacade.java': FACADE}

# Desk: same-name methods in unrelated classes, a same-arity overload, an external log-like SDK name,
# an in-repo-only sink and an in-repo class named like an operator contract.
DESK = 'src/com/example/desk/'
AUDIT_TRAIL = """package com.example.desk;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class AuditTrail {
    private static final Logger LOG = LoggerFactory.getLogger(AuditTrail.class);

    public void record(String entry) {
        LOG.info(entry);
    }
}
"""
INVENTORY = """package com.example.desk;

public class Inventory {
    private int count;

    public void record(String entry) {
        count = count + 1;
    }
}
"""
PRINTER = """package com.example.desk;

public class Printer {
    public void print(String text) {
        int size = text.length();
    }

    public void print(Integer number) {
        int value = number.intValue();
    }
}
"""
STATUS_JOURNAL = """package com.example.desk;

import com.acme.telemetry.Vendor;

public class StatusJournal {
    public void info(String message) {
        Vendor.Sdk.infoLog(message);
    }
}
"""
TELEMETRY = """package com.example.desk;

public class Telemetry {
    public static void emit(String message) {
    }
}
"""
EVENT_BUFFER = """package com.example.desk;

public class EventBuffer {
    private int size;

    public void push(String event) {
        size = size + 1;
    }
}
"""
NOTIFIER = """package com.example.desk;

public class Notifier {
    private final EventBuffer buffer;

    public Notifier(EventBuffer buffer) {
        this.buffer = buffer;
    }

    public void publish(String event) {
        buffer.push(event);
    }
}
"""
DESK_TEXT = """package com.example.desk;

public class Desk {
    private Inventory stock;
    private Printer printer;
    private StatusJournal status;
    private Notifier notifier;

    public void handle(String item) {
        stock.record(item);
        ledger.record(item);
        printer.print(item);
        status.info(item);
        notifier.publish(item);
        Telemetry.emit(item);
    }
}
"""
DESK_FILES = {DESK + 'AuditTrail.java': AUDIT_TRAIL, DESK + 'Inventory.java': INVENTORY, DESK + 'Printer.java': PRINTER,
              DESK + 'StatusJournal.java': STATUS_JOURNAL, DESK + 'Telemetry.java': TELEMETRY,
              DESK + 'EventBuffer.java': EVENT_BUFFER, DESK + 'Notifier.java': NOTIFIER, DESK + 'Desk.java': DESK_TEXT}
DESK_CALLS = ('stock.record', 'ledger.record', 'printer.print', 'status.info', 'notifier.publish', 'Telemetry.emit')

# Ship: interface dispatch and receiver forms.
SHIP = 'src/com/example/ship/'
DOCK = """package com.example.ship;

public class Dock {
    private Sink sink;
    private Channel channel;

    public Sink getSink() {
        return sink;
    }

    public static void stamp(String message) {
    }

    public void load(FileSink typed, String message) {
        sink.put(message);
        channel.send(message);
        new FileSink().put(message);
        var local = new FileSink();
        local.put(message);
        typed.put(message);
        getSink().put(message);
        Dock.stamp(message);
        stamp(message);
    }
}
"""
SHIP_FILES = {
    SHIP + 'Sink.java': 'package com.example.ship;\n\npublic interface Sink {\n    void put(String message);\n}\n',
    SHIP + 'FileSink.java': ('package com.example.ship;\n\npublic class FileSink implements Sink {\n'
                             '    public void put(String message) {\n        int size = message.length();\n    }\n}\n'),
    SHIP + 'Channel.java': 'package com.example.ship;\n\npublic interface Channel {\n    void send(String message);\n}\n',
    SHIP + 'MailChannel.java': ('package com.example.ship;\n\npublic class MailChannel implements Channel {\n'
                                '    public void send(String message) {\n    }\n}\n'),
    SHIP + 'SmsChannel.java': ('package com.example.ship;\n\npublic class SmsChannel implements Channel {\n'
                               '    public void send(String message) {\n    }\n}\n'),
    SHIP + 'Dock.java': DOCK}

# A thin command chain whose action logs four hops below the operation code.
COMMANDS_PATH = 'src/com/example/cmd/Commands.java'
COMMANDS = """package com.example.cmd;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class Screen {
    private CommandBus bus;

    void click() {
        bus.submit("go");
    }
}

class CommandBus {
    private Dispatcher dispatcher;

    void submit(String command) {
        dispatcher.route(command);
    }
}

class Dispatcher {
    private Handler handler;

    void route(String command) {
        handler.handle(command);
    }
}

class Handler {
    private Recorder recorder;

    void handle(String command) {
        recorder.store(command);
    }
}

class Recorder {
    private static final Logger LOG = LoggerFactory.getLogger(Recorder.class);

    void store(String command) {
        LOG.info(command);
    }
}
"""

# A chain one hop deeper than the bounded focus-following.
RELAY_PATH = 'src/com/example/relay/Relay.java'
RELAY = """package com.example.relay;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class Panel {
    private StepOne one;

    void press() {
        one.next("go");
    }
}

class StepOne {
    private StepTwo two;

    void next(String value) {
        two.next(value);
    }
}

class StepTwo { private StepThree three; void next(String value) { three.next(value); } }
class StepThree { private StepFour four; void next(String value) { four.next(value); } }
class StepFour { private StepFive five; void next(String value) { five.next(value); } }
class StepFive {
    private static final Logger LOG = LoggerFactory.getLogger(StepFive.class);

    void next(String value) {
        LOG.info(value);
    }
}
"""

LEDGER_PATH = 'src/com/example/ledger/Ledger.java'
LEDGER = """package com.example.ledger;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

class Clerk {
    private Ledger ledger;
    private Archive archive;

    void settle(String id) {
        ledger.post(id);
        archive.store(id);
    }

    void guess(String id) {
        books.post(id);
    }
}

class Ledger {
    private static final Logger LOG = LoggerFactory.getLogger(Ledger.class);

    void post(String id) {
        write(id);
    }

    private void write(String id) {
        LOG.info(id);
    }
}

class Archive {
    void store(String id) {
        int size = id.length();
    }
}
"""


def line_of(text, fragment):
    """1-based number of the first line containing fragment."""
    return next(number for number, line in enumerate(text.splitlines(), 1) if fragment in line)


def build_navigation(case, files, calls=()):
    """(LoggingNavigation, inventory sites with columns) over files written to a fresh temporary repository."""
    temp = tempfile.TemporaryDirectory()
    case.addCleanup(temp.cleanup)
    root = Path(temp.name)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    snapshot = snapshot_repository(root, max_files=None, max_chars=None)
    sites = discover_logging(snapshot, call_names=tuple(calls), max_sites=None, max_per_file=None, max_hints=None,
                             include_columns=True)['sites']
    return LoggingNavigation(snapshot), sites


def pick_site(sites, path, line):
    found = [site for site in sites if site['path'] == path and site['start_line'] == line]
    if len(found) != 1:
        raise AssertionError('Expected one inventory site at %s:%d, found %r' % (path, line, found))
    return found[0]


def resolved(case, navigation, path, line, name):
    calls = [call for call in navigation.java.calls_in(path, line, line) if call['name'] == name]
    case.assertEqual(len(calls), 1, calls)
    return navigation.java.describe(calls[0])


def qualnames(items):
    return [item['qualname'] for item in items]


class JavaResolutionTests(unittest.TestCase):
    def test_interface_dispatch_lists_single_and_multiple_implementations(self):
        navigation, _ = build_navigation(self, SHIP_FILES)
        dock = SHIP + 'Dock.java'
        single = resolved(self, navigation, dock, line_of(DOCK, 'sink.put(message)'), 'put')
        self.assertEqual((single['status'], qualnames(single['targets']), single['incomplete']),
                         ('dispatch', ['FileSink.put'], False))
        two = resolved(self, navigation, dock, line_of(DOCK, 'channel.send(message)'), 'send')
        self.assertEqual((two['status'], qualnames(two['targets'])), ('dispatch', ['MailChannel.send', 'SmsChannel.send']))
        # Only a complete single-implementation dispatch is followed as an edge.
        line = line_of(DOCK, 'sink.put(message)')
        [call] = [c for c in navigation.java.calls_in(dock, line, line) if c['name'] == 'put']
        self.assertEqual([navigation.java.members[m]['qualname'] for m in navigation.java.follow(call)], ['FileSink.put'])
        line = line_of(DOCK, 'channel.send(message)')
        [call] = [c for c in navigation.java.calls_in(dock, line, line) if c['name'] == 'send']
        self.assertEqual(navigation.java.follow(call), [])

    def test_receiver_types_come_from_creation_var_parameter_getter_and_static_type(self):
        navigation, _ = build_navigation(self, SHIP_FILES)
        dock = SHIP + 'Dock.java'
        for fragment in ('new FileSink().put(message)', 'local.put(message)', 'typed.put(message)'):
            result = resolved(self, navigation, dock, line_of(DOCK, fragment), 'put')
            self.assertEqual((result['status'], qualnames(result['targets'])), ('exact', ['FileSink.put']), fragment)
        getter = resolved(self, navigation, dock, line_of(DOCK, 'getSink().put(message)'), 'put')
        self.assertEqual((getter['status'], qualnames(getter['targets'])), ('dispatch', ['FileSink.put']))
        accessor = resolved(self, navigation, dock, line_of(DOCK, 'getSink().put(message)'), 'getSink')
        self.assertEqual((accessor['status'], qualnames(accessor['targets'])), ('exact', ['Dock.getSink']))
        for fragment in ('Dock.stamp(message)', '        stamp(message);'):
            result = resolved(self, navigation, dock, line_of(DOCK, fragment), 'stamp')
            self.assertEqual((result['status'], qualnames(result['targets'])), ('exact', ['Dock.stamp']), fragment)
        creation = resolved(self, navigation, dock, line_of(DOCK, 'new FileSink().put(message)'), 'FileSink')
        self.assertEqual((creation['status'], creation['targets']), ('implicit', []))

    def test_same_method_name_in_unrelated_classes_is_never_an_edge(self):
        navigation, _ = build_navigation(self, DESK_FILES)
        desk = DESK + 'Desk.java'
        typed = resolved(self, navigation, desk, line_of(DESK_TEXT, 'stock.record(item)'), 'record')
        self.assertEqual((typed['status'], qualnames(typed['targets']), typed['hints']), ('exact', ['Inventory.record'], []))
        unknown = resolved(self, navigation, desk, line_of(DESK_TEXT, 'ledger.record(item)'), 'record')
        self.assertEqual((unknown['status'], unknown['targets']), ('unresolved', []))
        self.assertEqual(qualnames(unknown['hints']), ['AuditTrail.record', 'Inventory.record'])
        line = line_of(DESK_TEXT, 'ledger.record(item)')
        [call] = [c for c in navigation.java.calls_in(desk, line, line) if c['name'] == 'record']
        self.assertEqual(navigation.java.follow(call), [])
        self.assertEqual(navigation.java.route_targets(call), [])

    def test_same_arity_overload_stays_ambiguous(self):
        navigation, _ = build_navigation(self, DESK_FILES)
        result = resolved(self, navigation, DESK + 'Desk.java', line_of(DESK_TEXT, 'printer.print(item)'), 'print')
        self.assertEqual((result['status'], result['targets']), ('ambiguous', []))
        self.assertEqual(qualnames(result['hints']), ['Printer.print', 'Printer.print'])

    def test_overloads_resolve_by_arity_through_an_inherited_field_and_super(self):
        navigation, _ = build_navigation(self, ORDER_FILES)
        facade = ORDERS + 'OrderFacade.java'
        three = resolved(self, navigation, facade, line_of(FACADE, 'Level.INFO, true'), 'record')
        self.assertEqual((three['status'], [t['signature'] for t in three['targets']]),
                         ('exact', ['record(String,Level,boolean)']))
        two = resolved(self, navigation, facade, line_of(FACADE, 'Level.DEBUG'), 'record')
        self.assertEqual((two['status'], [t['signature'] for t in two['targets']]), ('exact', ['record(String,Level)']))
        upward = resolved(self, navigation, ORDERS + 'Journal.java', line_of(JOURNAL, 'super.write(options)'), 'write')
        self.assertEqual((upward['status'], qualnames(upward['targets'])), ('super', ['BaseJournal.write']))
        sdk = resolved(self, navigation, ORDERS + 'BaseJournal.java', line_of(BASE_JOURNAL, 'debugLog'), 'debugLog')
        self.assertEqual((sdk['status'], sdk['callee'], sdk['external']),
                         ('external', 'Vendor.Sdk.debugLog', 'com.acme.telemetry.Vendor.Sdk'))


class JavaRelatedTests(unittest.TestCase):
    def test_java_owner_is_found_and_the_resolved_callee_is_delivered(self):
        navigation, sites = build_navigation(self, ORDER_FILES, ('journal.record',))
        facade = ORDERS + 'OrderFacade.java'
        first, last = line_of(FACADE, 'public void accept'), line_of(FACADE, 'Level.INFO, true') + 1
        owner = navigation.owner({'path': facade, 'start_line': first + 1, 'end_line': first + 2})
        self.assertEqual((owner['kind'], owner['qualname'], owner['arity'], owner['start_line'], owner['end_line']),
                         ('java_method', 'OrderFacade.accept', 1, first, last))
        wanted, notes, pending = navigation.related([{'path': facade, 'start_line': first, 'end_line': last}], 10, sites)
        self.assertEqual(pending, [])
        journal = ORDERS + 'Journal.java'
        self.assertIn((journal, line_of(JOURNAL, 'boolean withOrder'), line_of(JOURNAL, 'super.write') + 1), wanted)
        field = line_of(BASE_CLIENT, 'protected Journal journal')
        self.assertIsNone(navigation.owner({'path': ORDERS + 'BaseClient.java', 'start_line': field, 'end_line': field}))

    def test_unparsed_java_still_reports_unsupported_navigation(self):
        navigation, sites = build_navigation(self, {'src/Broken.java': 'class Broken {\n    void a() {\n'})
        _, _, pending = navigation.related([{'path': 'src/Broken.java', 'start_line': 2, 'end_line': 2}], 10, sites)
        self.assertEqual(pending, [NOT_SUPPORTED])

    def test_focus_follows_a_thin_command_chain_to_the_body_that_logs(self):
        navigation, sites = build_navigation(self, {COMMANDS_PATH: COMMANDS})
        first = line_of(COMMANDS, 'void click()')
        span = {'path': COMMANDS_PATH, 'start_line': first, 'end_line': first + 2}
        anchor = {'path': COMMANDS_PATH, 'start_line': first + 1, 'end_line': first + 1}
        store = (COMMANDS_PATH, line_of(COMMANDS, 'void store'), line_of(COMMANDS, 'LOG.info(command)') + 1)
        plain, _, pending = navigation.related([span], 10, sites)
        self.assertIn((COMMANDS_PATH, line_of(COMMANDS, 'void submit'), line_of(COMMANDS, 'dispatcher.route') + 1), plain)
        self.assertNotIn(store, plain)
        self.assertEqual(pending, [])
        followed, _, pending = navigation.related([span], 10, sites, focus=[anchor])
        for fragment, body in (('void route', 'handler.handle'), ('void handle', 'recorder.store')):
            self.assertIn((COMMANDS_PATH, line_of(COMMANDS, fragment), line_of(COMMANDS, body) + 1), followed)
        self.assertIn(store, followed)
        self.assertEqual(pending, [])

    def test_bounded_focus_reports_a_cut_route_that_reaches_a_log(self):
        navigation, sites = build_navigation(self, {RELAY_PATH: RELAY})
        first = line_of(RELAY, 'void press()')
        span = {'path': RELAY_PATH, 'start_line': first, 'end_line': first + 2}
        anchor = {'path': RELAY_PATH, 'start_line': first + 1, 'end_line': first + 1}
        followed, _, pending = navigation.related([span], 10, sites, focus=[anchor])
        cut = [item for item in pending if item.startswith(ROUTE_CUT)]
        self.assertEqual(len(cut), 1, pending)
        self.assertIn('Panel.press → StepOne.next → StepTwo.next → StepThree.next → StepFour.next → StepFive.next', cut[0])
        self.assertIn((RELAY_PATH, line_of(RELAY, 'class StepFour'), line_of(RELAY, 'class StepFour')), followed)
        log = line_of(RELAY, 'LOG.info(value)')
        self.assertNotIn((RELAY_PATH, log - 1, log + 1), followed)

    def test_unresolved_receiver_whose_hint_logs_keeps_the_absence_open(self):
        navigation, sites = build_navigation(self, DESK_FILES, DESK_CALLS)
        desk = DESK + 'Desk.java'
        first, last = line_of(DESK_TEXT, 'public void handle'), line_of(DESK_TEXT, 'Telemetry.emit(item)') + 1
        wanted, _, pending = navigation.related([{'path': desk, 'start_line': first, 'end_line': last}], 10, sites)
        self.assertEqual(pending, [HINT_WITH_LOG + 'AuditTrail.record'])
        self.assertIn((DESK + 'Inventory.java', line_of(INVENTORY, 'public void record'),
                       line_of(INVENTORY, 'count = count') + 1), wanted)
        # Mirror of the end-to-end control: two classes in one file, an undeclared receiver.
        code = ('class Service { void complete(String id) { audit.recordCompleted(id); } }\n'
                'class Audit { void recordCompleted(String id) { logger.info("completed"); } }\n')
        navigation, sites = build_navigation(self, {'app.java': code})
        _, _, pending = navigation.related([{'path': 'app.java', 'start_line': 1, 'end_line': 1}], 10, sites)
        self.assertIn(HINT_WITH_LOG + 'Audit.recordCompleted', pending)
        self.assertNotIn(NOT_SUPPORTED, pending)


class JavaRouteTests(unittest.TestCase):
    def setUp(self):
        self.navigation, sites = build_navigation(self, {LEDGER_PATH: LEDGER})
        self.site = pick_site(sites, LEDGER_PATH, line_of(LEDGER, 'LOG.info(id)'))
        self.shown = [{'path': LEDGER_PATH, 'start_line': 1, 'end_line': len(LEDGER.splitlines())}]
        self.invocation = self.span(line_of(LEDGER, 'ledger.post(id)'))
        self.forward = self.span(line_of(LEDGER, 'write(id);'))

    @staticmethod
    def span(line):
        return {'path': LEDGER_PATH, 'start_line': line, 'end_line': line}

    def test_route_through_a_private_forwarding_call_is_accepted_when_read_and_witnessed(self):
        route = self.navigation.invocation_route(self.invocation, self.site, [self.invocation, self.forward], self.shown)
        self.assertEqual(route['hops'], [dict(self.invocation, kind='exact'), dict(self.forward, kind='exact')])
        self.assertEqual(route['limitations'], [])

    def test_unwitnessed_or_unread_forwarding_call_is_rejected(self):
        with self.assertRaises(LoggingReviewError) as caught:
            self.navigation.invocation_route(self.invocation, self.site, [self.invocation], self.shown)
        self.assertIn('unread/unwitnessed', str(caught.exception))
        self.assertIn('%s:%d-%d' % (LEDGER_PATH, self.forward['start_line'], self.forward['end_line']), str(caught.exception))
        line = self.forward['start_line']
        unread = [{'path': LEDGER_PATH, 'start_line': 1, 'end_line': line - 1},
                  {'path': LEDGER_PATH, 'start_line': line + 1, 'end_line': len(LEDGER.splitlines())}]
        with self.assertRaises(LoggingReviewError):
            self.navigation.invocation_route(self.invocation, self.site, [self.invocation, self.forward], unread)

    def test_unrelated_or_unresolved_invocations_do_not_reach_the_emit(self):
        for fragment in ('archive.store(id)', 'books.post(id)'):
            invocation = self.span(line_of(LEDGER, fragment))
            with self.assertRaises(LoggingReviewError) as caught:
                self.navigation.invocation_route(invocation, self.site, [invocation, self.forward], self.shown)
            self.assertIn('does not resolve', str(caught.exception), fragment)
        with self.assertRaises(LoggingReviewError) as caught:
            self.navigation.invocation_route(self.invocation, self.site, [self.forward], self.shown)
        self.assertIn('Argument evidence must cover', str(caught.exception))

    def test_target_implementation_reads_the_overload_actually_called(self):
        navigation, sites = build_navigation(self, ORDER_FILES, ('journal.record',))
        site = pick_site(sites, ORDERS + 'OrderFacade.java', line_of(FACADE, 'Level.INFO, true'))
        journal = ORDERS + 'Journal.java'
        three = {'path': journal, 'start_line': line_of(JOURNAL, 'new Options'), 'end_line': line_of(JOURNAL, 'super.write'),
                 'quote': ''}
        two = {'path': journal, 'start_line': line_of(JOURNAL, 'record(text, level, false)'),
               'end_line': line_of(JOURNAL, 'record(text, level, false)'), 'quote': ''}
        self.assertTrue(navigation.target_implementation(site, [three]))
        self.assertFalse(navigation.target_implementation(site, [two]))


if __name__ == '__main__':
    unittest.main()
