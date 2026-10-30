"""Объявленные ограничения и режимы: политики, лимиты, allowlist, eval-спеки.

Ограничения живут не только в коде. Таймаут, потолок стоимости, перечень
разрешённых доменов, режим исполнения и пороги приёмки обычно вынесены в
конфигурацию — и раньше PKO их не видел, из-за чего guardrail-ов у системы
«не было» ровно там, где их вынесли из кода.

Ключевое ограничение честности: объявленный лимит — не применённый лимит.
Значение `timeout: 30` доказывает, что параметр задан, и ничего не говорит о
том, что он передан в вызов. Поэтому все факты отсюда идут с
`gate_eligible=False`: они видны в паспортах и в пробелах, но допуск на них не
выдаётся. Требование «guardrail применяется перед действием» остаётся
`NEEDS_RUNTIME` в каталоге стандарта, и вынос лимита в YAML этого не меняет.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from pko.extractors.base import Fact, FileTree, is_vendor
from pko.model import taxonomy
from pko.util.yamlmini import YamlSubsetError, loads

# Ключи, за которыми стоит числовое ограничение.
# These are descriptions supplied by repository owners, not runtime settings.
# Keep the filename exclusion local; the retired intent/assessment loader is not
# part of the source-analysis foundation.
INTENT_PATHS = (
    "business_intent.yaml", "business_intent.json", "docs/business_intent.yaml",
    ".pko/business_intent.yaml", "backend/business_intent.yaml",
)

LIMIT_KEYS = (
    "timeout", "timeout_s", "timeout_ms", "max_retries", "retries", "max_tokens",
    "max_rows", "row_limit", "rate_limit", "max_cost", "budget", "max_steps",
    "max_iterations", "concurrency", "max_concurrency", "ttl", "deadline",
)

# Ключи с явным перечнем разрешённого.
ALLOWLIST_KEYS = (
    "allowed_hosts", "allowlist", "allow_list", "whitelist", "allowed_domains",
    "allowed_tools", "allowed_tables", "permitted", "allowed_operations",
)

# Ключи режима исполнения (§6.4). Режим — не лимит: он задаёт полномочия.
MODE_KEYS = ("mode", "execution_mode", "autonomy", "autonomy_level", "requested_mode")
KNOWN_MODES = ("HUMAN_ONLY", "ASSIST", "CONFIRM", "AUTO")

# Файлы приёмки: пороги качества, наборы проверок, эталоны.
EVAL_HINTS = ("eval", "evals", "benchmark", "acceptance", "quality", "scorecard")

MAX_DEPTH = 4
MAX_FACTS = 80


def extract(tree: FileTree) -> list[Fact]:
    facts: list[Fact] = []
    for path in tree.files:
        if is_vendor(path):
            continue
        lower = path.lower()
        if not lower.endswith((".yaml", ".yml", ".json")):
            continue
        # Заявление владельца — не конфигурация реализации. `requested_mode` из
        # `business_intent.yaml` уже обработан загрузчиком намерения, и второй
        # факт «в коде объявлен режим» здесь читался бы как свойство системы.
        if path in INTENT_PATHS or path.split("/", 1)[-1] in INTENT_PATHS:
            continue
        data = _load(tree, path)
        if not isinstance(data, dict):
            continue
        facts.extend(_walk(path, data))
        facts.extend(_eval_facts(path, data))
        if len(facts) >= MAX_FACTS:
            return facts[:MAX_FACTS]
    return facts


def _walk(path: str, data: Any, prefix: str = "", depth: int = 0) -> Iterator[Fact]:
    """Обойти конфигурацию вглубь. Вложенность важна: `agent.limits.timeout` — тоже лимит."""
    if depth > MAX_DEPTH or not isinstance(data, dict):
        return
    for raw_key, value in data.items():
        key = str(raw_key)
        full = f"{prefix}.{key}" if prefix else key
        low = key.lower()
        if isinstance(value, dict):
            yield from _walk(path, value, full, depth + 1)
            continue
        if low in LIMIT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            yield Fact(
                kind="LIMIT", key=full, value=value, path=path, line=1,
                basis=f"объявлено ограничение {full} = {value} в конфигурации",
                category=taxonomy.CONTROL, action="declare", mechanism="limit",
                gate_eligible=False,
            )
        elif low in ALLOWLIST_KEYS and isinstance(value, (list, tuple)) and value:
            yield Fact(
                kind="ALLOWLIST", key=full, value=[str(v) for v in value][:40],
                path=path, line=1,
                basis=f"объявлен перечень разрешённого {full}: позиций {len(value)}",
                category=taxonomy.CONTROL, action="declare", mechanism="allowlist",
                gate_eligible=False,
            )
        elif low in MODE_KEYS and isinstance(value, str) and value.upper() in KNOWN_MODES:
            # Режим — не ограничение: он задаёт объём полномочий, а не величину.
            # Категория `CONTROL` ему не положена и по инварианту таксономии:
            # у контроля механизм должен проверяться структурно (`limit`,
            # `allowlist`), а строка в конфигурации не доказывает, что режим
            # соблюдается. Поэтому это объявленный артефакт, и вердикт на него
            # не опирается — только читатель карточки видит заявленный режим.
            yield Fact(
                kind="SETTING", key=full, value=value.upper(), path=path, line=1,
                basis=f"объявлен режим исполнения {value.upper()} в {full}; "
                      "соблюдение режима статически не проверяется",
                category=taxonomy.ARTIFACT, action="declare", mechanism="config",
                gate_eligible=False,
            )


def _eval_facts(path: str, data: dict[str, Any]) -> Iterator[Fact]:
    """Спецификация приёмки. Это артефакт проверки, а не её результат.

    Наличие файла порогов не означает, что прогон был и пороги достигнуты:
    результат подтверждается только отчётом (`TEST_REPORT`).
    """
    base = path.rsplit("/", 1)[-1].lower()
    if not any(hint in base for hint in EVAL_HINTS):
        return
    cases = _cases(data)
    if cases is None:
        return
    yield Fact(
        kind="TEST", key=f"eval:{base}", value=cases, path=path, line=1,
        basis=(f"объявлена спецификация приёмки: сценариев {cases}; "
               "фактический результат подтверждается только отчётом о прогоне"),
        category=taxonomy.ARTIFACT, action="declare", mechanism="test",
        gate_eligible=False,
    )


def _cases(data: dict[str, Any]) -> int | None:
    for key in ("cases", "tests", "scenarios", "examples", "checks"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _load(tree: FileTree, path: str) -> Any:
    text = tree.read(path)
    if text is None:
        return None
    try:
        if path.lower().endswith(".json"):
            return json.loads(text)
        return loads(text)
    except (json.JSONDecodeError, YamlSubsetError, ValueError):
        return None
