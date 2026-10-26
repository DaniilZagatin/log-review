"""Запись прогона тестов: отчёт должен быть проверяемым, а не убедительным.

`run_tests.run()` здесь намеренно не вызывается: он обходит каталог `tests`,
то есть запустил бы и этот файл. Проверяется всё, что делает запись
доказательством, — формат, различение исходов и совпадение с документированной
командой.
"""

import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import run_tests
from pko.extractors.test_reports import load_junit

ROOT = Path(__file__).resolve().parent.parent


def _report(*cases: run_tests.Case) -> run_tests.Report:
    return run_tests.Report(cases=list(cases), seconds=1.5)


def _case(name: str, outcome: str, detail: str = "") -> run_tests.Case:
    return run_tests.Case(module="test_x", classname="test_x.Case", name=name,
                          outcome=outcome, seconds=0.01, detail=detail)


class JunitFormatTest(unittest.TestCase):
    """Отчёт о себе пишется в том же формате, который PKO требует от чужих систем."""

    def test_pko_reads_its_own_report(self):
        xml = run_tests.junit_xml(_report(
            _case("a", run_tests.PASSED),
            _case("b", run_tests.FAILED, "AssertionError: не сошлось"),
            _case("c", run_tests.SKIPPED, "нет фикстуры"),
        ))
        with _written(self, xml) as path:
            facts = load_junit(path)
        self.assertEqual(len(facts), 1)
        value = facts[0].value
        self.assertEqual((value["total"], value["passed"], value["failed"], value["skipped"]),
                         (3, 1, 1, 1))

    def test_skipped_is_not_counted_as_passed(self):
        """Пропущенный тест ничего не проверил; засчитать его успехом — подделка."""
        xml = run_tests.junit_xml(_report(_case("c", run_tests.SKIPPED, "нет фикстуры")))
        with _written(self, xml) as path:
            value = load_junit(path)[0].value
        self.assertEqual(value["passed"], 0)
        self.assertEqual(value["skipped"], 1)

    def test_failure_carries_its_reason(self):
        xml = run_tests.junit_xml(_report(
            _case("b", run_tests.FAILED, "Traceback...\nAssertionError: 1 != 2")))
        node = ET.fromstring(xml).find("testcase/failure")
        self.assertIsNotNone(node)
        self.assertIn("AssertionError", node.get("message"))
        self.assertIn("Traceback", node.text)

    def test_counts_in_the_header_match_the_cases(self):
        xml = run_tests.junit_xml(_report(
            _case("a", run_tests.PASSED), _case("b", run_tests.FAILED)))
        suite = ET.fromstring(xml)
        self.assertEqual(suite.get("tests"), "2")
        self.assertEqual(suite.get("failures"), "1")

    def test_control_characters_do_not_break_the_file(self):
        """Управляющий символ в трассе делает XML нечитаемым, и отчёт пропадает целиком."""
        xml = run_tests.junit_xml(_report(_case("b", run_tests.FAILED, "до\x00\x07после")))
        ET.fromstring(xml)  # не должно бросить
        self.assertNotIn("\x00", xml)

    def test_empty_discovery_is_an_explicit_junit_error(self):
        """`tests=0, errors=0` выглядит зелёным, хотя ничего не запускалось."""
        suite = ET.fromstring(run_tests.junit_xml(_report()))

        self.assertEqual(suite.get("tests"), "0")
        self.assertEqual(suite.get("errors"), "1")
        self.assertIn("тесты не обнаружены", suite.findtext("system-err", ""))


class SummaryTest(unittest.TestCase):
    def test_command_and_exit_code_are_recorded(self):
        """«Прогон» без команды и кода возврата невозможно ни повторить, ни проверить."""
        text = run_tests.summary_text(_report(_case("a", run_tests.PASSED)),
                                      "2026-08-12 12:00:00 +0300", 0)
        self.assertIn("make test-report", text)
        self.assertIn(run_tests.EQUIVALENT_COMMAND, text)
        self.assertIn("Код возврата:  0", text)
        self.assertIn("2026-08-12 12:00:00 +0300", text)

    def test_failures_are_listed_by_name(self):
        text = run_tests.summary_text(
            _report(_case("b", run_tests.FAILED, "AssertionError: 1 != 2")),
            "2026-08-12 12:00:00 +0300", 1)
        self.assertIn("test_x.Case.b", text)
        self.assertIn("AssertionError", text)
        self.assertNotIn("Провалов нет", text)

    def test_skipped_tests_are_named_not_hidden(self):
        """«Прошло 2 из 2» при одном пропуске — неверное утверждение."""
        text = run_tests.summary_text(
            _report(_case("a", run_tests.PASSED), _case("c", run_tests.SKIPPED, "нет сети")),
            "2026-08-12 12:00:00 +0300", 0)
        self.assertIn("Пропущено (проверка не выполнялась)", text)
        self.assertIn("нет сети", text)

    def test_clean_run_says_so_plainly(self):
        text = run_tests.summary_text(_report(_case("a", run_tests.PASSED)),
                                      "2026-08-12 12:00:00 +0300", 0)
        self.assertIn("Провалов нет.", text)

    def test_empty_discovery_is_not_described_as_no_failures(self):
        text = run_tests.summary_text(
            _report(), "2026-08-12 12:00:00 +0300", 1)

        self.assertIn("Проверка не выполнена", text)
        self.assertIn("тесты не обнаружены", text)
        self.assertIn("Код возврата:  1", text)
        self.assertNotIn("Провалов нет", text)


class ExitCodeTest(unittest.TestCase):
    def test_report_with_a_failure_is_not_ok(self):
        self.assertFalse(_report(_case("b", run_tests.FAILED)).ok)

    def test_report_with_only_skips_is_ok(self):
        """Пропуск не проваливает прогон, но и не доказывает поведение — он в сводке."""
        self.assertTrue(_report(_case("c", run_tests.SKIPPED)).ok)

    def test_empty_report_is_not_ok(self):
        """Ни одного обнаруженного теста — это отсутствие проверки, не успех."""
        self.assertFalse(_report().ok)

    def test_nonmatching_pattern_exits_nonzero_and_records_the_reason(self):
        """Регрессия полного CLI-пути: pattern с опечаткой не выпускает зелёный gate artifact."""
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "report"
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = run_tests.main([
                    "--out", str(out),
                    "--pattern", "__no_such_tests_*.py",
                ])

            self.assertNotEqual(code, 0)
            self.assertIn("тесты не обнаружены", stderr.getvalue())
            summary = (out / "TESTS.txt").read_text(encoding="utf-8")
            self.assertIn("__no_such_tests_*.py", summary)
            self.assertIn("Проверка не выполнена", summary)
            suite = ET.parse(out / "junit.xml").getroot()
            self.assertEqual(suite.get("errors"), "1")

    def test_failing_subtest_blocks_report_junit_and_runner(self):
        """Падение subTest нельзя потерять между unittest и gate-артефактом."""

        class Probe(unittest.TestCase):
            def test_passes(self):
                self.assertTrue(True)

            def test_has_failing_subtest(self):
                with self.subTest(component="payments", attempt=2):
                    self.assertEqual("actual", "expected")

        stream = StringIO()
        result = unittest.TextTestRunner(
            stream=stream,
            verbosity=0,
            resultclass=run_tests._Recorder,
        ).run(unittest.defaultTestLoader.loadTestsFromTestCase(Probe))
        report = run_tests.Report(cases=list(result.cases), seconds=0.01)

        self.assertEqual(report.count(run_tests.PASSED), 1)
        self.assertEqual(report.count(run_tests.FAILED), 1)
        self.assertFalse(report.ok)
        failed = next(case for case in report.cases
                      if case.outcome == run_tests.FAILED)
        self.assertIn("component='payments'", failed.name)
        self.assertIn("attempt=2", failed.detail)

        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "report"
            stdout, stderr = StringIO(), StringIO()
            with patch.object(run_tests, "run", return_value=report), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = run_tests.main(["--out", str(out)])

            self.assertNotEqual(code, 0)
            suite = ET.parse(out / "junit.xml").getroot()
            self.assertEqual(suite.get("failures"), "1")
            self.assertIsNotNone(suite.find("testcase/failure"))


class DocumentedCommandTest(unittest.TestCase):
    """Записанная команда обязана совпадать с той, что действительно выполняется."""

    def test_equivalent_command_matches_the_makefile(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(run_tests.EQUIVALENT_COMMAND.replace("python3", "$(PYTHON)"), makefile)

    def test_makefile_has_the_target_the_report_names(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("test-report:", makefile)
        self.assertIn("tests/run_tests.py", makefile)

    def test_reports_directory_is_not_committed(self):
        """Записанный прогон — артефакт запуска; в репозитории он однажды соврёт."""
        self.assertIn("reports/", (ROOT / ".gitignore").read_text(encoding="utf-8"))


class _written:
    """Временный файл отчёта: `load_junit` работает с путём, а не со строкой."""

    def __init__(self, case: unittest.TestCase, text: str):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "junit.xml"
        self._path.write_text(text, encoding="utf-8")

    def __enter__(self) -> Path:
        return self._path

    def __exit__(self, *exc) -> bool:
        return False


if __name__ == "__main__":
    unittest.main()
