"""Export every page of one Phoenix trace to a local, masked JSON file.

Phoenix >= 13.9 supports trace_id filtering. No extra Python packages needed.
API contract: https://arize-phoenix.readthedocs.io/projects/client/api/spans.html
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pko.audit import sanitize
from pko.util.paths import harden_file


def fetch_trace(base_url, project, trace_id, *, opener=urlopen):
    if not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id):
        raise ValueError("trace-id должен содержать 32 шестнадцатеричных символа")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Нужен HTTP(S) URL Phoenix без пароля в адресе")
    trace_id = trace_id.lower()
    spans, cursors, seen = [], set(), set()
    cursor = None
    while True:
        query = {"trace_id": trace_id, "limit": 100}
        if cursor:
            query["cursor"] = cursor
        url = base_url.rstrip("/") + "/v1/projects/" + quote(project, safe="") + "/spans?" + urlencode(query)
        headers = {"Accept": "application/json"}
        if os.environ.get("PHOENIX_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["PHOENIX_API_KEY"]
        with opener(Request(url, headers=headers), timeout=60) as response:
            payload = json.load(response)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("Phoenix вернул неожиданный формат: нет массива data")
        for span in rows:
            context = span.get("context") or {}
            if str(context.get("trace_id", "")).lower() != trace_id:
                raise ValueError("Сервер не применил фильтр trace_id; нужен Phoenix >= 13.9")
            key = context.get("span_id")
            if not key:
                raise ValueError("У спана нет span_id")
            if key not in seen:
                spans.append(span)
                seen.add(key)
        cursor = payload.get("next_cursor")
        if not cursor:
            break
        if cursor in cursors:
            raise ValueError("Phoenix повторил курсор: выгрузка остановлена без неполного файла")
        cursors.add(cursor)
    if not spans:
        raise ValueError("Трасса не найдена в указанном проекте")
    return sorted(spans, key=lambda span: (span.get("start_time") or "", span["context"]["span_id"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:6006")
    parser.add_argument("--project", default=os.environ.get("PKO_PHOENIX_PROJECT", "belka-scenario"))
    parser.add_argument("--trace-id", required=True)
    parser.add_argument("--out", type=Path, default=Path("phoenix-trace.json"))
    args = parser.parse_args(argv)
    spans = fetch_trace(args.url, args.project, args.trace_id)
    envelope = sanitize({"project": args.project, "trace_id": args.trace_id,
        "exported_at": datetime.now(timezone.utc).isoformat(), "span_count": len(spans),
        "note": "Все страницы доступного снимка трассы. Экспортируйте после завершения и отправки спанов. Секреты маскируются; внутренние рассуждения исключены. Проверьте содержание проекта перед передачей.",
        "spans": spans})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects an earlier export from accidental overwrite.
    with args.out.open("x", encoding="utf-8") as stream:
        harden_file(args.out)
        json.dump(envelope, stream, ensure_ascii=False, indent=2)
    print(f"Выгружено спанов: {len(spans)}. Файл: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"Экспорт не выполнен ({type(exc).__name__}): {sanitize(str(exc))}", file=sys.stderr)
        raise SystemExit(1)
