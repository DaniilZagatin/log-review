"""Прогон тестов с записью результата: `reports/junit.xml` и `reports/TESTS.txt`.

Зачем это нужно отдельно от `make test`. `make test` печатает результат в
терминал, и он живёт ровно до закрытия окна. Любой внешний контроль — CI,
ревью-гейт, приёмка — спрашивает не «прошло ли у кого-то», а «покажи команду,
код возврата и перечень тестов». Без записи ответ на этот вопрос звучит как
«я запускал, всё прошло», то есть утверждение без доказательства.

Ровно этого PKO не принимает от анализируемых систем: §5.2.3.2 — `PASS` без
доказательства не является результатом, а `CHK-TEST-001` требует готовый JUnit
XML и не запускает чужие тесты сам. Странно требовать отчёт от других и не
уметь выпустить его для себя, поэтому формат здесь тот же, который читает
`pko.extractors.test_reports.load_junit`: свой прогон PKO может разобрать своим
же экстрактором.

Зависимостей нет: `unittest` и `xml.etree` из стандартной библиотеки.

    make test-report                      # обычный путь
    python3 tests/run_tests.py --out reports
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"
DEFAULT_OUT = ROOT / "reports"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# Команда, которую воспроизводит этот скрипт. Печатается в отчёт дословно:
# «прогон» без названной команды невозможно повторить.
EQUIVALENT_COMMAND = "python3 -m unittest discover -s tests"
DEFAULT_PATTERN = "test*.py"
NO_TESTS_DIAGNOSTIC = (
    "тесты не обнаружены; проверьте каталог tests "
    "и значение --pattern"
)


@dataclass
class Case:
    """Исход одного теста. Пропуск — не успех: он ничего не проверил."""

    module: str
    classname: str
    name: str
    outcome: str
    seconds: float
    detail: str = ""

    def __post_init__(self) -> None:
        # Чистка здесь, а не в месте записи: `ET.tostring` экранирует `&<>`, но
        # управляющие символы пишет как есть, и такой XML потом не разбирается
        # вовсе — отчёт пропадает целиком из-за одного байта в трассе. Инвариант
        # должен держать сам тип, иначе следующий его создатель о нём не узнает.
        self.detail = _xml_safe(self.detail)


@dataclass
class Report:
    cases: list[Case] = field(default_factory=list)
    seconds: float = 0.0
    pattern: str = DEFAULT_PATTERN

    def count(self, outcome: str) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome)

    @property
    def ok(self) -> bool:
        # Ноль падений не означает успешную проверку, если discovery не нашёл
        # ни одного теста. Иначе опечатка в `--pattern` выпускала зелёные
        # JUnit/TESTS артефакты, хотя ни одна проверка не запускалась.
        return bool(self.cases) and self.count(FAILED) == 0


class _Recorder(unittest.TextTestResult):
    """Собирает исход каждого теста, а не только сводку.

    Штатный `TestResult` хранит только упавшие и пропущенные: восстановить по
    нему перечень прошедших нельзя, а отчёт без них не отличает «все прошли»
    от «все только запустились».
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases: list[Case] = []
        self._started = 0.0

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, PASSED)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, FAILED, self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, FAILED, self._exc_info_to_string(err, test))

    def addSubTest(self, test, subtest, err):
        """Записать неуспешный subTest как самостоятельный исход.

        `unittest` не вызывает `addFailure`/`addError` для родительского теста,
        если assertion или исключение случились внутри `subTest`: единственный
        callback с ошибкой — этот. Успешные subTest отдельно не записываем:
        когда все они прошли, `unittest` завершает родителя через `addSuccess`.
        """
        super().addSubTest(test, subtest, err)
        if err is None:
            return

        parent_id = test.id()
        subtest_id = subtest.id()
        parameters = (
            subtest_id[len(parent_id):].strip()
            if subtest_id.startswith(parent_id)
            else str(subtest)
        )
        trace = self._exc_info_to_string(err, test)
        detail = f"Subtest {parameters}\n{trace}" if parameters else trace
        # И assertion failure, и произвольное исключение блокируют gate. Как и
        # addFailure/addError выше, оба исхода представлены единым FAILED в
        # нашем компактном JUnit-контракте.
        # Идентичность берём у родителя, а параметры добавляем только к имени:
        # точка внутри значения (`timeout=1.5`, URL и т.п.) иначе ломала бы
        # разбиение `module.class.method` в `_record`.
        self._record(test, FAILED, detail, name_suffix=parameters)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, SKIPPED, reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, SKIPPED, "ожидаемое падение")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        # Тест, помеченный как ожидаемо падающий, но прошедший, — это
        # рассогласование ожиданий с кодом, и молчать о нём нельзя.
        self._record(test, FAILED, "неожиданный успех при @expectedFailure")

    def _record(
        self,
        test,
        outcome: str,
        detail: str = "",
        name_suffix: str = "",
    ) -> None:
        parts = test.id().split(".")
        name = parts[-1] if parts else test.id()
        if name_suffix:
            name += f" {name_suffix}"
        self.cases.append(Case(
            module=parts[0] if parts else "",
            classname=".".join(parts[:-1]),
            name=name,
            outcome=outcome,
            seconds=time.perf_counter() - self._started,
            detail=detail,
        ))


def run(pattern: str = DEFAULT_PATTERN) -> Report:
    """Прогнать те же тесты, что и `make test`, но с записью исходов."""
    # Те же три места, что видит `python3 -m unittest discover -s tests` из корня
    # репозитория: пакет (`src`), сами тесты (они импортируют `fixture_support`
    # напрямую) и корень (`tests/test_bench.py` импортирует `bench`). Первый
    # записанный прогон упал именно на корне: `python3 tests/run_tests.py` кладёт
    # в `sys.path` каталог скрипта, а не текущий, — и запись сразу показала
    # расхождение с документированной командой.
    for path in (ROOT, ROOT / "src", TESTS_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR), pattern=pattern, top_level_dir=str(TESTS_DIR))
    runner = unittest.TextTestRunner(resultclass=_Recorder, verbosity=1, stream=sys.stderr)
    started = time.perf_counter()
    result = runner.run(suite)
    return Report(
        cases=list(result.cases),
        seconds=time.perf_counter() - started,
        pattern=pattern,
    )


def junit_xml(report: Report, name: str = "pko") -> str:
    """JUnit XML в том виде, который читает `load_junit`."""
    discovery_errors = 0 if report.cases else 1
    suite = ET.Element("testsuite", {
        "name": name,
        "tests": str(len(report.cases)),
        "failures": str(report.count(FAILED)),
        "skipped": str(report.count(SKIPPED)),
        # Пустой suite нельзя публиковать как зелёный. `tests=0` сохраняет
        # правду о discovery, а suite-level error делает отсутствие проверки
        # явным для любого JUnit consumer, даже если он смотрит только шапку.
        "errors": str(discovery_errors),
        "time": f"{report.seconds:.3f}",
    })
    for case in report.cases:
        node = ET.SubElement(suite, "testcase", {
            "classname": case.classname,
            "name": case.name,
            "time": f"{case.seconds:.3f}",
        })
        if case.outcome == FAILED:
            ET.SubElement(node, "failure", {"message": _first_line(case.detail)}).text = case.detail
        elif case.outcome == SKIPPED:
            ET.SubElement(node, "skipped", {"message": _first_line(case.detail)})
    if not report.cases:
        ET.SubElement(suite, "system-err").text = NO_TESTS_DIAGNOSTIC
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def summary_text(report: Report, generated_at: str, exit_code: int) -> str:
    """Человекочитаемая запись прогона: команда, код возврата, состав."""
    command, equivalent = _commands(report.pattern)
    lines = [
        "Прогон тестов PKO",
        "",
        f"Команда:       {command}  (эквивалент: {equivalent})",
        f"Код возврата:  {exit_code}",
        f"Когда:         {generated_at}",
        f"Python:        {sys.version.split()[0]}",
        f"Каталог:       {ROOT}",
        "",
        f"Всего тестов:  {len(report.cases)}",
        f"  прошло:      {report.count(PASSED)}",
        f"  провалено:   {report.count(FAILED)}",
        f"  пропущено:   {report.count(SKIPPED)}",
        f"Длительность:  {report.seconds:.1f} с",
        "",
        "По модулям:",
    ]
    modules: dict[str, dict[str, int]] = {}
    for case in report.cases:
        counts = modules.setdefault(case.module, {PASSED: 0, FAILED: 0, SKIPPED: 0})
        counts[case.outcome] += 1
    width = max((len(m) for m in modules), default=0)
    for module in sorted(modules):
        counts = modules[module]
        row = f"  {module.ljust(width)}  прошло {counts[PASSED]}"
        if counts[FAILED]:
            row += f", провалено {counts[FAILED]}"
        if counts[SKIPPED]:
            row += f", пропущено {counts[SKIPPED]}"
        lines.append(row)

    failed = [c for c in report.cases if c.outcome == FAILED]
    lines.append("")
    if not report.cases:
        lines.append("Проверка не выполнена: " + NO_TESTS_DIAGNOSTIC + ".")
    elif failed:
        lines.append("Провалено:")
        lines.extend(f"  {c.classname}.{c.name} — {_first_line(c.detail)}" for c in failed)
    else:
        lines.append("Провалов нет.")

    skipped = [c for c in report.cases if c.outcome == SKIPPED]
    if skipped:
        # Пропущенный тест ничего не доказывает, и прятать его в сводке нельзя:
        # «все прошли» при трёх пропусках — неверное утверждение.
        lines.append("")
        lines.append("Пропущено (проверка не выполнялась):")
        lines.extend(f"  {c.classname}.{c.name} — {_first_line(c.detail)}" for c in skipped)

    lines.append("")
    lines.append("Машинный вид того же прогона: junit.xml рядом с этим файлом.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Прогон тестов с записью результата")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="каталог для отчётов")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN)
    args = parser.parse_args(argv)

    report = run(args.pattern)
    exit_code = 0 if report.ok else 1
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S %z")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "junit.xml").write_text(junit_xml(report), encoding="utf-8")
    (out / "TESTS.txt").write_text(summary_text(report, generated_at, exit_code), encoding="utf-8")

    print(f"\nТестов: {len(report.cases)} · прошло {report.count(PASSED)} · "
          f"провалено {report.count(FAILED)} · пропущено {report.count(SKIPPED)}")
    if not report.cases:
        print("Ошибка: " + NO_TESTS_DIAGNOSTIC + ".", file=sys.stderr)
    print(f"Запись прогона: {(out / 'TESTS.txt').resolve()}")
    print(f"Машинный отчёт: {(out / 'junit.xml').resolve()}")
    return exit_code


def _commands(pattern: str) -> tuple[str, str]:
    """Точная выполненная команда и её эквивалент через unittest.

    При стандартном pattern оператор действительно запускает `make
    test-report`. Пользовательский pattern нельзя записывать под этим именем:
    артефакт тогда утверждал бы выполнение другого набора тестов.
    """
    if pattern == DEFAULT_PATTERN:
        return "make test-report", EQUIVALENT_COMMAND
    quoted = shlex.quote(pattern)
    return (
        f"python3 tests/run_tests.py --pattern {quoted}",
        f"{EQUIVALENT_COMMAND} -p {quoted}",
    )


def _first_line(text: str) -> str:
    line = (text or "").strip().splitlines()
    return line[-1][:200] if line else ""


def _xml_safe(text: str) -> str:
    """Убрать управляющие символы: XML их не допускает, и файл станет нечитаемым."""
    return "".join(ch for ch in (text or "")
                   if ch in "\t\n\r" or ord(ch) >= 32)


if __name__ == "__main__":
    raise SystemExit(main())
