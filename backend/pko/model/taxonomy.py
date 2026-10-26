"""Универсальная таксономия наблюдений: что именно нашли в коде.

Прежний перечень видов фактов смешивал две разные вещи — что наблюдение
означает («точка входа», «внешний эффект») и каким механизмом оно сделано
(«SQL», «LangGraph», «FastAPI»). Из-за этого проекту на другом стеке было
некуда положить находку: у UI-события, команды CLI или обработчика очереди
не было подходящего вида, и агент вынужден был либо молчать, либо врать
ближайшим по смыслу `ROUTE`.

Здесь разделены три независимых фасета:

  * `category`   — что это в терминах процесса (ENTRYPOINT, STEP, EFFECT, …);
  * `action`     — что оно делает (read, write, call, serve, transition);
  * `mechanism`  — чем именно сделано (sql, http_server, ui_event, queue, …).

SQL перестаёт быть отдельной сущностью и становится механизмом эффекта:
`EFFECT / write / sql`. Новая технология требует одного значения `mechanism`,
а не переделки модели.

Прежние виды (`SQL_READ`, `ROUTE`, `GRAPH_NODE`, …) сохраняются как
псевдонимы: у каждого есть точное разложение на фасеты, поэтому старый код,
тесты и отчёты продолжают работать, пока потребители переходят на предикаты.
"""

from __future__ import annotations

from typing import NamedTuple

# --- категории -------------------------------------------------------------
ENTRYPOINT = "ENTRYPOINT"        # откуда исполнение начинается
STEP = "STEP"                    # шаг траектории
EFFECT = "EFFECT"                # наблюдаемое воздействие вовне
CONTROL = "CONTROL"              # ограничение исполнения
COMPONENT = "COMPONENT"          # единица кода, из которой собирается блок
DEPENDENCY = "DEPENDENCY"        # внешняя зависимость и её версия
STATE = "STATE"                  # переход или хранимое состояние
ARTIFACT = "ARTIFACT"            # опора анализа: конфигурация, тест, промпт
UNKNOWN = "UNKNOWN"              # наблюдение есть, толкования нет
CONTRADICTION = "CONTRADICTION"  # код противоречит заявленному

CATEGORIES = (
    ENTRYPOINT, STEP, EFFECT, CONTROL, COMPONENT,
    DEPENDENCY, STATE, ARTIFACT, UNKNOWN, CONTRADICTION,
)

# --- действия --------------------------------------------------------------
# Пустая строка допустима: у компонента или зависимости действия нет.
ACTIONS = ("", "serve", "handle", "call", "read", "write", "emit", "transition", "declare")

# --- механизмы -------------------------------------------------------------
# Перечень открыт по смыслу, но закрыт для Gate: вердикт опирается только на
# то, что здесь названо и для чего в `pko.agent.verifiers` есть проверка.
MECHANISMS = (
    "http_server",     # объявленный HTTP-эндпоинт
    "http_client",     # обращение к чужому HTTP-сервису
    "ui_event",        # обработчик события интерфейса
    "cli",             # команда командной строки
    "cron",            # запуск по расписанию
    "queue",           # сообщение очереди, брокер
    "webhook",         # входящий вызов извне
    "graph",           # узел или переход графа исполнения
    "agent_tool",      # инструмент агента
    "sql",             # SQL-запрос
    "orm",             # тот же доступ к данным через ORM
    "nosql",
    "fs",              # файловая система
    "object_storage",  # S3 и аналоги
    "llm",             # обращение к языковой модели
    "integration",     # прочая внешняя система
    "state_store",     # хранилище состояния приложения
    "limit",           # числовое ограничение
    "allowlist",       # явный перечень разрешённого
    "config",          # параметр конфигурации
    "package",         # модуль или пакет
    "package_manager", # манифест зависимостей
    "prompt",
    "test",
    "test_report",
    "codeowners",
)

# Синонимы: модель и разные экосистемы называют одно и то же по-разному.
# Без приведения к общему написанию `stable_key` объектов начал бы плавать
# от прогона к прогону, а сравнение версий показывало бы ложные изменения.
_SYNONYMS = {
    "postgres": "sql", "postgresql": "sql", "mysql": "sql", "sqlite": "sql",
    "jdbc": "sql", "sqlalchemy": "orm", "django_orm": "orm", "prisma": "orm",
    "mongo": "nosql", "mongodb": "nosql", "redis": "state_store",
    "rest": "http_server", "http": "http_server", "endpoint": "http_server",
    "route": "http_server", "fastapi": "http_server", "flask": "http_server",
    "django": "http_server", "express": "http_server",
    "fetch": "http_client", "axios": "http_client", "requests": "http_client",
    "httpx": "http_client",
    "react": "ui_event", "vue": "ui_event", "dom": "ui_event",
    "onclick": "ui_event", "handler": "ui_event", "streamlit": "ui_event",
    "langgraph": "graph", "stategraph": "graph", "workflow": "graph",
    "celery": "queue", "kafka": "queue", "rabbitmq": "queue", "sqs": "queue",
    "scheduler": "cron", "schedule": "cron", "airflow": "cron",
    "s3": "object_storage", "minio": "object_storage", "boto3": "object_storage",
    "file": "fs", "filesystem": "fs", "storage": "fs",
    "openai": "llm", "anthropic": "llm", "model_call": "llm",
    "tool": "agent_tool", "function_call": "agent_tool",
    "timeout": "limit", "retry": "limit", "rate_limit": "limit",
    "whitelist": "allowlist", "permitted": "allowlist",
    "argparse": "cli", "click": "cli", "typer": "cli",
}

# Механизмы, которые Gate считает доступом к данным.
#
# `orm` здесь не расширение, а сохранение прежнего периметра: до разделения
# признаков конструкции `session.add`/`bulk_save` входили в шаблон `SQL_WRITE`
# и роняли проверку «только чтение». Оставить перечень равным `{"sql"}` значило
# бы молча выпустить репозиторий на SQLAlchemy, изменения данных в котором
# агент нашёл, — вердикт стал бы чище кода.
#
# Статические факты от этого не меняются: импорт `sqlalchemy` даёт `EXTERNAL`
# с действием `call`, а не `read`/`write`, и в проверку не попадает ни до, ни
# после.
#
# Остальные механизмы данных (`fs`, `object_storage`, `nosql`, `state_store`)
# в проверку по-прежнему не входят: это было бы расширением, меняющим вердикт
# там, где раньше его не было. Но молчать о них нельзя — найденные изменения
# в этих механизмах попадают в пробелы отчёта (`UNGUARDED_DATA_MECHANISMS`).
GATE_DATA_MECHANISMS = frozenset({"sql", "orm"})

# Механизмы изменения данных вне периметра проверки: вердикт их не учитывает,
# поэтому отчёт обязан назвать их прямо.
UNGUARDED_DATA_MECHANISMS = frozenset({"fs", "object_storage", "nosql", "state_store"})


class Facets(NamedTuple):
    """Разложение наблюдения на независимые признаки."""

    category: str
    action: str
    mechanism: str


# Какие категории осмысленны для механизма.
#
# Признаки независимы не полностью: `open(path, "w")` — это воздействие на
# данные, и никакой категорией, кроме `EFFECT`, оно быть не может. Без этого
# правила проверка распадалась на три отдельных: механизм подтверждался по
# конструкции, а категория принималась на слово. Тогда настоящая запись,
# объявленная точкой входа, проходила структурную проверку, исчезала из
# эффектов и попадала в доказательства восстановленной траектории — то есть
# ошибка классификации меняла вход детерминированного Gate.
MECHANISM_CATEGORIES: dict[str, frozenset[str]] = {
    "sql": frozenset({EFFECT}),
    "orm": frozenset({EFFECT}),
    "nosql": frozenset({EFFECT}),
    "fs": frozenset({EFFECT}),
    "object_storage": frozenset({EFFECT}),
    "state_store": frozenset({EFFECT}),
    "llm": frozenset({EFFECT}),
    "http_client": frozenset({EFFECT}),
    "integration": frozenset({EFFECT}),
    "http_server": frozenset({ENTRYPOINT}),
    "webhook": frozenset({ENTRYPOINT}),
    "cron": frozenset({ENTRYPOINT}),
    # Команда — точка входа, её отдельный шаг — шаг траектории.
    "cli": frozenset({ENTRYPOINT, STEP}),
    # Экран порождает и вход (нажатие), и переход состояния.
    "ui_event": frozenset({ENTRYPOINT, STATE}),
    # Очередь работает в обе стороны: отправка — эффект, обработчик — вход.
    "queue": frozenset({EFFECT, ENTRYPOINT}),
    "graph": frozenset({STEP, STATE, ARTIFACT}),
    "agent_tool": frozenset({COMPONENT, STEP}),
    "limit": frozenset({CONTROL}),
    "allowlist": frozenset({CONTROL}),
    "config": frozenset({ARTIFACT}),
    "prompt": frozenset({ARTIFACT}),
    "test": frozenset({ARTIFACT}),
    "test_report": frozenset({ARTIFACT}),
    "codeowners": frozenset({ARTIFACT}),
    "package": frozenset({COMPONENT}),
    "package_manager": frozenset({DEPENDENCY}),
}

# Какие действия осмысленны для категории. Пустое действие допустимо везде.
CATEGORY_ACTIONS: dict[str, frozenset[str]] = {
    EFFECT: frozenset({"read", "write", "call", "emit"}),
    ENTRYPOINT: frozenset({"serve", "handle"}),
    STEP: frozenset({"call", "handle"}),
    STATE: frozenset({"transition"}),
    CONTROL: frozenset(),
    COMPONENT: frozenset({"call"}),
    DEPENDENCY: frozenset(),
    ARTIFACT: frozenset({"declare"}),
}


# Категории, у которых механизма нет по смыслу: это не описание конструкции,
# а признание, что толкования нет или что код спорит с заявленным. Ни один
# селектор модели их не выбирает, поэтому в паспорт как наблюдение они не
# попадают — только в машинный срез и пробелы.
CATEGORIES_WITHOUT_MECHANISM = frozenset({UNKNOWN, CONTRADICTION})


def proposal_problem(
    kind: str, raw_category: str, raw_action: str, raw_mechanism: str
) -> str:
    """Причина отказа во входных признаках наблюдения; пустая строка — приемлемо.

    Одно правило на оба пути приёма (`note_fact` и финал), иначе они расходятся:
    инструмент отвечал «принято к проверке», а проверка молча отбрасывала — или
    наоборот, пропускала то, что инструмент бы не взял.

    Главное здесь — требование механизма. Без него структурной проверки не
    существует вовсе: `pattern_for` искать нечего, и наблюдение попадало в
    поля паспорта с происхождением `OBSERVED` («код это показывает»), имея за
    собой только совпадение слова из формулировки. Опечатка в действии по той
    же причине не стирается, а отклоняется: стёртое действие превращало
    `EFFECT/write/fs` в бесцветное `EFFECT//fs`, теряя направление.
    """
    explicit_category = normalize_category(raw_category)
    if raw_category and not explicit_category:
        return (f"неизвестная category «{raw_category}»; допустимы: "
                f"{', '.join(CATEGORIES)}")
    if raw_action and not normalize_action(raw_action):
        return (f"неизвестное action «{raw_action}»; допустимы: "
                f"{', '.join(a for a in ACTIONS if a)}")

    # Универсальную категорию можно передать и как `kind`: это форма
    # того же универсального наблюдения, а не legacy-вид с зашитым
    # механизмом. Поэтому `kind="ENTRYPOINT"` обязан пройти те же
    # правила, что и `category="ENTRYPOINT"`.
    kind_category = normalize_category(kind)
    if kind_category and explicit_category and kind_category != explicit_category:
        return (
            f"вид {kind} означает category={kind_category}, "
            f"а заявлено category={explicit_category}"
        )
    if kind and not kind_category:
        return ""
    category = explicit_category or kind_category
    if not category:
        return "наблюдение без вида: нужен либо kind, либо category с mechanism"
    if category in CATEGORIES_WITHOUT_MECHANISM:
        return ""
    if not normalize_mechanism(raw_mechanism):
        return (f"наблюдение category={category} без mechanism: подтвердить его "
                f"конструкцией в коде нечем")
    return ""


def conflict(facets: Facets) -> str:
    """Причина, по которой тройка признаков противоречива; пустая строка — согласована."""
    allowed = MECHANISM_CATEGORIES.get(facets.mechanism)
    if allowed is not None and facets.category and facets.category not in allowed:
        return (
            f"механизм «{facets.mechanism}» не бывает категории {facets.category}; "
            f"допустимо: {', '.join(sorted(allowed))}"
        )
    actions = CATEGORY_ACTIONS.get(facets.category)
    if actions is not None and facets.action and facets.action not in actions:
        allowed_actions = ", ".join(sorted(actions)) or "без действия"
        return (
            f"категория {facets.category} не сочетается с действием «{facets.action}»; "
            f"допустимо: {allowed_actions}"
        )
    return ""


def legacy_conflict(kind: str, explicit: Facets) -> str:
    """Причина, по которой явные признаки спорят с прежним видом.

    Вид `SQL_WRITE` уже означает `EFFECT/write/sql`. Позволить объявить его
    точкой входа значит дать переопределить смысл наблюдения формулировкой.
    """
    base = LEGACY_FACETS.get(kind)
    if base is None:
        return ""
    for name, given, expected in (
        ("category", explicit.category, base.category),
        ("action", explicit.action, base.action),
        ("mechanism", explicit.mechanism, base.mechanism),
    ):
        if given and given != expected:
            return f"вид {kind} означает {name}={expected}, а заявлено {given}"
    return ""


# --- прежние виды как псевдонимы -------------------------------------------
# Таблица обязана быть полной: `FACT_KINDS` без разложения означает факт,
# который новые предикаты не увидят, — он молча выпадет из модели.
LEGACY_FACETS: dict[str, Facets] = {
    "ROUTE": Facets(ENTRYPOINT, "serve", "http_server"),
    "GRAPH_NODE": Facets(STEP, "", "graph"),
    "GRAPH_EDGE": Facets(STATE, "transition", "graph"),
    # Объявление графа — не единица кода, а свидетельство, что граф вообще
    # есть: в кандидаты и блоки оно, как и прежде, не попадает.
    "GRAPH": Facets(ARTIFACT, "declare", "graph"),
    "TOOL": Facets(COMPONENT, "call", "agent_tool"),
    "MODULE": Facets(COMPONENT, "", "package"),
    "LIMIT": Facets(CONTROL, "", "limit"),
    "ALLOWLIST": Facets(CONTROL, "", "allowlist"),
    "SQL_READ": Facets(EFFECT, "read", "sql"),
    "SQL_WRITE": Facets(EFFECT, "write", "sql"),
    "EXTERNAL": Facets(EFFECT, "call", "integration"),
    "LLM_CALL": Facets(EFFECT, "call", "llm"),
    "SETTING": Facets(ARTIFACT, "", "config"),
    "DEP": Facets(DEPENDENCY, "", "package_manager"),
    "PROMPT": Facets(ARTIFACT, "", "prompt"),
    "OWNER": Facets(ARTIFACT, "", "codeowners"),
    "TEST": Facets(ARTIFACT, "", "test"),
    "TEST_REPORT": Facets(ARTIFACT, "", "test_report"),
}

_EMPTY = Facets(UNKNOWN, "", "")


def facets_for(kind: str) -> Facets:
    """Разложение прежнего вида. Категория сама по себе тоже допустима."""
    known = LEGACY_FACETS.get(kind)
    if known is not None:
        return known
    if kind in CATEGORIES:
        return Facets(kind, "", "")
    return _EMPTY


def normalize_mechanism(value: str) -> str:
    """Привести написание механизма к одному виду.

    Значение приходит от языковой модели, поэтому одно и то же приезжает как
    `SQL`, `sql`, `Postgres`, `sql query`. Без приведения эти написания стали
    бы разными механизмами и разошлись бы в отчётах и ключах сравнения.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("-", "_").replace(" ", "_").replace(".", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return _SYNONYMS.get(text, text)


def normalize_action(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {"reads": "read", "writes": "write", "mutate": "write",
               "update": "write", "insert": "write", "delete": "write",
               "select": "read", "query": "read", "get": "read",
               "invoke": "call", "publish": "emit", "send": "emit"}
    text = aliases.get(text, text)
    return text if text in ACTIONS else ""


def normalize_category(value: str) -> str:
    text = str(value or "").strip().upper()
    return text if text in CATEGORIES else ""


def is_known_mechanism(mechanism: str) -> bool:
    """Известен ли механизм настолько, чтобы на него опирался вердикт."""
    return mechanism in MECHANISMS


# --- предикаты -------------------------------------------------------------
# Потребители спрашивают именно их, а не сравнивают `kind` со строкой: только
# так новый механизм попадает в модель без правки каждого места.
def is_entrypoint(facets: Facets) -> bool:
    return facets.category == ENTRYPOINT


def is_step(facets: Facets) -> bool:
    return facets.category == STEP


def is_effect(facets: Facets) -> bool:
    return facets.category == EFFECT


def is_control(facets: Facets) -> bool:
    return facets.category == CONTROL


def is_component(facets: Facets) -> bool:
    return facets.category == COMPONENT


def is_transition(facets: Facets) -> bool:
    return facets.category == STATE and facets.action == "transition"


def is_capability(facets: Facets) -> bool:
    """Единица, которую можно отнести к бизнес-блоку."""
    return facets.category in (ENTRYPOINT, STEP, COMPONENT)


def is_constraint(facets: Facets) -> bool:
    return facets.category == CONTROL


def is_data_effect(facets: Facets, mechanisms: frozenset[str] = GATE_DATA_MECHANISMS) -> bool:
    """Эффект над данными в механизмах, на которые опирается Gate."""
    return facets.category == EFFECT and facets.mechanism in mechanisms


def is_mutating(facets: Facets, mechanisms: frozenset[str] = GATE_DATA_MECHANISMS) -> bool:
    return is_data_effect(facets, mechanisms) and facets.action == "write"
