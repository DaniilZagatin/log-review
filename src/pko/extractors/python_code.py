"""Разбор Python-кода через `ast`: эндпоинты, граф, инструменты, ограничения, внешние системы.

Всё делается статически и без импорта анализируемого кода. Старые коммиты
разбираются тем же кодом, что и новые: файл, который не парсится, пропускается с
пометкой, а не роняет анализ.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Iterator

from pko.extractors.base import Fact, FileTree, is_vendor

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Имена параметров, которые почти всегда означают ограничение исполнения.
LIMIT_HINTS = re.compile(
    r"(timeout|max_rows|row_limit|max_retries|retries|max_rounds|max_iterations|"
    r"max_tokens|max_attempts|limit|ttl|max_size|max_files|max_depth)",
    re.IGNORECASE,
)
ALLOWLIST_HINTS = re.compile(r"(allowed|allowlist|whitelist|permitted)", re.IGNORECASE)
SECRET_HINTS = re.compile(r"(key|token|secret|password|passwd|keytab|credential)", re.IGNORECASE)

# После голых глаголов обязателен объект SQL: иначе обычная фраза в docstring
# («Drop the temporary upload after processing») превращалась в факт «SQL изменяет
# данные», проверка «только чтение» падала, и Gate выдавал DENY по тексту комментария.
SQL_WRITE = re.compile(
    r"\b("
    r"insert\s+into"
    r"|update\s+\w+\s+set"
    r"|delete\s+from"
    r"|drop\s+(table|view|index|schema|database|materialized)"
    r"|truncate\s+table"
    r"|alter\s+(table|view|schema|role|user|database)"
    r")",
    re.IGNORECASE,
)
# В SELECT-списке нужна SQL-форма: `*`, идентификатор(ы) через запятую или
# вызов функции. Это отсеивает docstring вроде «Select the best candidate from
# the list», который раньше создавал ложный SQL_READ и блокировал Gate.
_SQL_IDENT = r'[A-Za-z_"`][\w$"`]*(?:\s*\.\s*[A-Za-z_"`][\w$"`]*)?'
_SQL_EXPR = rf'(?:{_SQL_IDENT}|{_SQL_IDENT}\s*\([^)]*\))'
SQL_READ = re.compile(
    rf"\bselect\s+(?:distinct\s+)?(?:\*|{_SQL_EXPR}(?:\s*,\s*{_SQL_EXPR})*)"
    rf"\s+from\s+{_SQL_IDENT}\b",
    re.IGNORECASE,
)

# Импорт → внешняя система, о которой это говорит.
EXTERNAL_BY_IMPORT = {
    "boto3": "Объектное хранилище S3",
    "botocore": "Объектное хранилище S3",
    "opensearchpy": "OpenSearch",
    "elasticsearch": "Elasticsearch",
    "lancedb": "LanceDB",
    "psycopg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "asyncpg": "PostgreSQL",
    "sqlalchemy": "Реляционная БД через SQLAlchemy",
    "sqlite3": "SQLite",
    "redis": "Redis",
    "pymongo": "MongoDB",
    "kafka": "Kafka",
    "aiokafka": "Kafka",
    "requests": "Внешний HTTP-сервис",
    "httpx": "Внешний HTTP-сервис",
    "openai": "LLM через OpenAI-совместимый API",
    "anthropic": "LLM Anthropic",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "opentelemetry": "OpenTelemetry",
    "phoenix": "Phoenix tracing",
    "kerberos": "Kerberos",
    "gssapi": "Kerberos",
}

# Механизм внешней системы. Вид `EXTERNAL` один, а технологии за ним разные:
# `boto3` — объектное хранилище, `requests` — чужой HTTP, `sqlalchemy` — доступ
# к данным. Без этого различения все они выглядели бы одним «интеграционным»
# эффектом, и отчёт не отвечал бы на вопрос, куда именно система ходит.
EXTERNAL_MECHANISM = {
    "boto3": "object_storage", "botocore": "object_storage",
    "opensearchpy": "nosql", "elasticsearch": "nosql", "lancedb": "nosql",
    "pymongo": "nosql",
    "psycopg": "sql", "psycopg2": "sql", "asyncpg": "sql", "sqlite3": "sql",
    "sqlalchemy": "orm",
    "redis": "state_store",
    "kafka": "queue", "aiokafka": "queue",
    "requests": "http_client", "httpx": "http_client",
    "openai": "llm", "anthropic": "llm",
    # Импорт фреймворка графа — обращение к внешней библиотеке, а не сам узел:
    # механизм `graph` означал бы `EFFECT/call/graph`, а это сочетание
    # `taxonomy.MECHANISM_CATEGORIES` запрещает. Пак `agents` при этом всё
    # равно подключается — по конструкциям `add_node(` в коде.
    "langchain": "integration", "langgraph": "integration",
}

LLM_CALL_ATTRS = {"create", "parse", "invoke", "ainvoke", "stream", "complete"}


def extract(tree: FileTree) -> tuple[list[Fact], list[str], list[str]]:
    """Вернуть (факты, разобранные файлы, файлы с ошибкой разбора)."""
    facts: list[Fact] = []
    parsed: list[str] = []
    failed: list[str] = []

    py_files = [p for p in tree.files if p.endswith(".py") and not is_vendor(p)]
    for path in py_files:
        source = tree.read(path)
        if source is None:
            failed.append(path)
            continue
        try:
            module = ast.parse(source)
        except (SyntaxError, ValueError):
            failed.append(path)
            continue
        parsed.append(path)
        facts.extend(_walk_module(module, path))

    facts.extend(_module_facts(parsed))
    return _own_calls_only(facts), parsed, failed


# Виды фактов, которые называют объявление кодом этого репозитория.
_DECLARING_KINDS = ("FUNCTION", "CLASS_DEF", "TOOL", "GRAPH_NODE")


def _own_calls_only(facts: list[Fact]) -> list[Fact]:
    """Оставить рёбра графа только к именам, объявленным в этом репозитории.

    Вопрос, ради которого граф строится, один: задействован ли **наш** код.
    Вызовы `len`, `print` и функций библиотек на него не отвечают, а объём
    дают основной — на этом проекте 73% всех фактов. Лишние рёбра замедляют
    экстракцию, раздувают память и мешают агенту в `show_facts`.
    """
    declared = {
        str(fact.key) for fact in facts if fact.kind in _DECLARING_KINDS
    }
    return [
        fact for fact in facts
        if fact.kind != "CALLS"
        or (isinstance(fact.value, dict) and fact.value.get("callee") in declared)
    ]


# --- обход одного файла ----------------------------------------------------
def _walk_module(module: ast.Module, path: str) -> list[Fact]:
    facts: list[Fact] = []
    is_tools_module = path.rsplit("/", 1)[-1] in {"tools.py", "memory_tools.py"}

    for node in ast.walk(module):
        facts.extend(_imports(node, path))
        facts.extend(_calls(node, path))
        facts.extend(_assignments(node, path))
        facts.extend(_strings(node, path))
        facts.extend(_functions(node, path, is_tools_module))
        facts.extend(_classes(node, path))
    facts.extend(_declarations(module, path))
    facts.extend(_call_edges(module, path))
    return facts


# Служебные методы, которые ничего не говорят о возможностях системы.
_DUNDER_SKIP = {"__init__", "__repr__", "__str__", "__eq__", "__hash__", "__post_init__"}


def _declarations(module: ast.Module, path: str) -> Iterator[Fact]:
    """Объявления функций и классов с сигнатурой и первой строкой докстроки.

    Раньше в фактах были только точки входа, тулы и узлы графа — то есть
    каркас. Обычная функция, в которой и лежит вся работа, не была
    представлена ничем, и агент не мог сопоставить буллит с кодом иначе как
    поиском слова. Докстрока здесь и есть мост между языком бизнеса и кодом:
    «Найти подходящие таблицы по описанию» сходится с буллитом по смыслу,
    а имя `search_schema` — нет.
    """
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef):
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            yield Fact(
                kind="CLASS_DEF",
                key=node.name,
                value={"doc": doc[0][:160] if doc else ""},
                path=path,
                line=getattr(node, "lineno", None),
                basis=f"класс «{node.name}»",
            )
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _DUNDER_SKIP:
            continue
        doc = (ast.get_docstring(node) or "").strip().splitlines()
        args = [a.arg for a in node.args.args if a.arg not in {"self", "cls"}]
        yield Fact(
            kind="FUNCTION",
            key=node.name,
            value={"args": args[:8], "doc": doc[0][:160] if doc else ""},
            path=path,
            line=getattr(node, "lineno", None),
            basis=f"функция «{node.name}»",
        )


def _call_edges(module: ast.Module, path: str) -> Iterator[Fact]:
    """Кто кого вызывает — по одному факту на пару «вызывающий → вызываемый».

    На этом держится разница между «Работает» и «Код реализован, но не
    используется»: 50 пунктов готовности прототипа и все 100 промышленной.
    Без графа вызовов это суждение модель делает вручную и ничем не
    подтверждает — самое дорогое решение отчёта оказывается единственным
    непроверяемым.

    Вызов приписывается **ближайшей** объемлющей функции. Обход через
    `ast.walk` по каждой функции приписывал вызов ещё и всем внешним: во
    вложенной функции `inner` вызов `helper` выглядел как вызов из `outer`.
    Фантомное ребро опаснее отсутствующего — оно показывает мёртвый код
    достижимым, а это ровно то суждение, ради которого граф и строится.

    Разбор приблизительный: динамическая диспетчеризация, реестры и
    декораторы через AST не видны. Поэтому факты идут агенту как подсказка,
    а не как приговор — гейтом здесь ничего не закрывается.
    """
    seen: set[tuple[str, str]] = set()
    for owner, node in _calls_with_owner(module, ""):
        callee = _callee_name(node.func)
        if not callee or not owner or callee == owner:
            continue
        if (owner, callee) in seen:
            continue
        seen.add((owner, callee))
        yield Fact(
            kind="CALLS",
            key=f"{owner} -> {callee}",
            value={"caller": owner, "callee": callee},
            path=path,
            line=getattr(node, "lineno", None),
            basis=f"вызов «{callee}» из «{owner}»",
        )


def _calls_with_owner(node: ast.AST, owner: str) -> Iterator[tuple[str, ast.Call]]:
    """Вызовы вместе с именем ближайшей объемлющей функции."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield from _calls_with_owner(child, child.name)
            continue
        if isinstance(child, ast.Call):
            yield owner, child
        yield from _calls_with_owner(child, owner)


def _callee_name(node: ast.AST) -> str:
    """Короткое имя вызываемого: `mod.helper(...)` → `helper`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _imports(node: ast.AST, path: str) -> Iterator[Fact]:
    roots: list[str] = []
    if isinstance(node, ast.Import):
        roots = [a.name.split(".")[0] for a in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module:
        roots = [node.module.split(".")[0]]
    for root in roots:
        system = EXTERNAL_BY_IMPORT.get(root)
        if system:
            yield Fact(
                kind="EXTERNAL",
                key=system,
                value=root,
                path=path,
                line=getattr(node, "lineno", None),
                basis=f"импорт {root}",
                mechanism=EXTERNAL_MECHANISM.get(root, "integration"),
            )


def _calls(node: ast.AST, path: str) -> Iterator[Fact]:
    if not isinstance(node, ast.Call):
        return
    line = getattr(node, "lineno", None)
    func = node.func

    # FastAPI: @router.get("/path") — декоратор это тоже Call
    if isinstance(func, ast.Attribute) and func.attr in HTTP_METHODS:
        holder = _name_of(func.value)
        if holder and ("router" in holder.lower() or "app" in holder.lower()):
            route = _const_str(node.args[0]) if node.args else None
            if route:
                yield Fact(
                    kind="ROUTE",
                    key=f"{func.attr.upper()} {route}",
                    value={"method": func.attr.upper(), "path": route, "holder": holder},
                    path=path,
                    line=line,
                    basis=f"эндпоинт {func.attr.upper()} {route}",
                )

    if isinstance(func, ast.Attribute):
        # LangGraph: builder.add_node("name", fn) / add_edge(a, b)
        if func.attr == "add_node" and node.args:
            name = _const_str(node.args[0]) or _name_of(node.args[0])
            if name:
                yield Fact(
                    kind="GRAPH_NODE",
                    key=str(name),
                    value=str(name),
                    path=path,
                    line=line,
                    basis=f"узел графа «{name}»",
                )
        if func.attr in {"add_edge", "add_conditional_edges"} and node.args:
            src = _const_str(node.args[0]) or _name_of(node.args[0]) or "?"
            dst = "?"
            if len(node.args) > 1:
                dst = _const_str(node.args[1]) or _name_of(node.args[1]) or "?"
            yield Fact(
                kind="GRAPH_EDGE",
                key=f"{src}→{dst}",
                value={"from": str(src), "to": str(dst), "conditional": func.attr != "add_edge"},
                path=path,
                line=line,
                basis=f"переход {src} → {dst}",
            )
        if func.attr in LLM_CALL_ATTRS:
            holder = _name_of(func.value) or ""
            if any(t in holder.lower() for t in ("chat", "completions", "client", "llm", "model")):
                yield Fact(
                    kind="LLM_CALL",
                    key=f"{holder}.{func.attr}",
                    value=func.attr,
                    path=path,
                    line=line,
                    basis="вызов языковой модели",
                )

    name = _name_of(func)
    if name and name.endswith("StateGraph"):
        yield Fact(
            kind="GRAPH",
            key="StateGraph",
            value="langgraph",
            path=path,
            line=line,
            basis="объявлен граф исполнения LangGraph",
        )

    # Ограничения, переданные аргументом: timeout=60, max_retries=2
    for kw in node.keywords or []:
        if kw.arg and LIMIT_HINTS.search(kw.arg):
            num = _const_num(kw.value)
            if num is not None:
                yield Fact(
                    kind="LIMIT",
                    key=kw.arg,
                    value=num,
                    path=path,
                    line=line,
                    basis=f"{kw.arg} = {num}",
                )


def _assignments(node: ast.AST, path: str) -> Iterator[Fact]:
    targets: list[str] = []
    value: ast.AST | None = None
    if isinstance(node, ast.Assign):
        targets = [t for t in (_name_of(x) for x in node.targets) if t]
        value = node.value
    elif isinstance(node, ast.AnnAssign) and node.target is not None:
        name = _name_of(node.target)
        targets = [name] if name else []
        value = node.value
    if not targets or value is None:
        return

    line = getattr(node, "lineno", None)
    for name in targets:
        short = name.rsplit(".", 1)[-1]
        if SECRET_HINTS.search(short):
            # Значение не сохраняем: в отчёт попадает только имя параметра.
            yield Fact(
                kind="SETTING",
                key=short,
                value="<скрыто>",
                path=path,
                line=line,
                basis=f"параметр {short} (значение скрыто)",
            )
            continue
        num = _const_num(value)
        if num is not None and LIMIT_HINTS.search(short):
            yield Fact(
                kind="LIMIT", key=short, value=num, path=path, line=line,
                basis=f"{short} = {num}",
            )
        elif ALLOWLIST_HINTS.search(short):
            items = _const_list(value)
            yield Fact(
                kind="ALLOWLIST",
                key=short,
                value=items if items is not None else "определён в коде",
                path=path,
                line=line,
                basis=f"перечень {short}" + (f" ({len(items)} элементов)" if items else ""),
            )
        elif num is not None or _const_str(value) is not None:
            yield Fact(
                kind="SETTING",
                key=short,
                value=num if num is not None else _const_str(value),
                path=path,
                line=line,
                basis=f"параметр {short}",
            )


def _strings(node: ast.AST, path: str) -> Iterator[Fact]:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return
    text = node.value
    if len(text) < 12:
        return
    line = getattr(node, "lineno", None)
    if SQL_WRITE.search(text):
        yield Fact(
            kind="SQL_WRITE", key="sql", value=_sql_head(text), path=path, line=line,
            basis="SQL, изменяющий данные",
        )
    elif SQL_READ.search(text):
        yield Fact(
            kind="SQL_READ", key="sql", value=_sql_head(text), path=path, line=line,
            basis="SQL-запрос на чтение",
        )


def _functions(node: ast.AST, path: str, is_tools_module: bool) -> Iterator[Fact]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return
    line = getattr(node, "lineno", None)
    decorators = [_name_of(d.func) if isinstance(d, ast.Call) else _name_of(d)
                  for d in node.decorator_list]
    decorators = [d for d in decorators if d]
    marked = any(d.rsplit(".", 1)[-1] in {"tool", "function_tool", "register_tool"}
                 for d in decorators)
    if marked or (is_tools_module and not node.name.startswith("_")):
        doc = ast.get_docstring(node) or ""
        yield Fact(
            kind="TOOL",
            key=node.name,
            value={"summary": doc.strip().splitlines()[0][:160] if doc else ""},
            path=path,
            line=line,
            basis=f"инструмент агента «{node.name}»",
        )


def _classes(node: ast.AST, path: str) -> Iterator[Fact]:
    if not isinstance(node, ast.ClassDef):
        return
    bases = {(_name_of(b) or "").rsplit(".", 1)[-1] for b in node.bases}
    if "BaseSettings" in bases:
        yield Fact(
            kind="SETTING",
            key=f"class:{node.name}",
            value="конфигурация приложения",
            path=path,
            line=getattr(node, "lineno", None),
            basis=f"класс настроек {node.name}",
        )


def _module_facts(parsed: list[str]) -> list[Fact]:
    """Прикладные пакеты — это кандидаты в переиспользуемые блоки."""
    by_package: dict[str, list[str]] = {}
    for path in parsed:
        parts = path.split("/")
        if len(parts) < 2:
            continue
        pkg = "/".join(parts[:-1])
        by_package.setdefault(pkg, []).append(path)

    facts: list[Fact] = []
    for pkg, files in sorted(by_package.items()):
        facts.append(
            Fact(
                kind="MODULE",
                key=pkg,
                value={"files": len(files)},
                path=sorted(files)[0],
                line=1,
                basis=f"пакет {pkg}, файлов: {len(files)}",
            )
        )
    return facts


# --- мелкие помощники ------------------------------------------------------
def _name_of(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_num(node: ast.AST | None) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _const_num(node.operand)
        return -inner if inner is not None else None
    return None


def _const_list(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_const_str(e) for e in node.elts]
        return [i for i in items if i]
    return None


def _sql_head(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:80]
