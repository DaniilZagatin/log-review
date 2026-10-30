"""Versioned local resume files; never a transcript or a credentials store.

The digest detects corruption, not malicious forgery. Checkpoints are user data:
restoration independently validates source identity, provenance and graph state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from pko.audit import sanitize
from pko.errors import PkoError
from pko.llm.registry import DEFAULT_CONTEXT_WINDOW

FORMAT = "belka-scenario-resume"
VERSION = 1
MAX_CHECKPOINT_BYTES = 128 * 1024 * 1024


class CheckpointError(PkoError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def compatibility(spec, system_hash: str, tools_hash: str, summary_mode: str, summary_max_tokens: int) -> dict:
    # Credentials and certificate paths can rotate. Do not persist them, even
    # as hashes. Endpoint/options bind the same model configuration without
    # revealing an endpoint URL that may contain authentication parameters.
    return {
        "system_prompt_sha256": system_hash,
        "tools_sha256": tools_hash,
        "model": spec.model,
        "endpoint_sha256": _digest(str(spec.base_url).rstrip("/")),
        "options_sha256": _digest(getattr(spec, "extra_body", None) or {}),
        "context_window": spec.context_window or DEFAULT_CONTEXT_WINDOW,
        "summary_mode": summary_mode,
        "summary_max_tokens": summary_max_tokens,
    }


def make_checkpoint(state, contract: dict, snapshot: dict | None = None) -> dict:
    from pko.scenario_map.checkpoint_state import export_state

    durable = export_state(state)
    result = sanitize(snapshot if snapshot is not None else state.snapshot())
    # The public and internal copies of model fields must be identical. Runtime
    # providers' private reasoning is absent from both copies.
    result.update(deepcopy(durable["model"]))
    body = {"compatibility": deepcopy(contract), "state": durable}
    result["_resume"] = {"format": FORMAT, "version": VERSION, **body, "state_sha256": _digest(body)}
    if len(_json(result).encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        raise CheckpointError("Checkpoint превышает допустимый размер 128 МиБ.")
    return result


def validate_checkpoint(payload: dict) -> dict:
    try:
        meta = payload.get("_resume") if isinstance(payload, dict) else None
        if not isinstance(meta, dict) or meta.get("format") != FORMAT or (type(meta.get("version")) is not int or meta.get("version") != VERSION):
            raise CheckpointError("Файл не содержит поддерживаемого состояния продолжения.",
                                  "Нужен scenario_map.checkpoint.json, созданный версией Белки с поддержкой --resume. Старые снимки и опубликованная карта не подходят.")
        if set(meta) != {"format", "version", "compatibility", "state", "state_sha256"}:
            raise CheckpointError("Некорректный контракт checkpoint.")
        body = {key: meta[key] for key in ("compatibility", "state")}
        if not isinstance(body["compatibility"], dict) or not isinstance(body["state"], dict):
            raise CheckpointError("Checkpoint не содержит конфигурации и состояния.")
        if _digest(body) != meta["state_sha256"]:
            raise CheckpointError("Checkpoint повреждён: отпечаток состояния не совпадает.")
        model = body["state"].get("model")
        if not isinstance(model, dict) or any(payload.get(key) != value for key, value in model.items()):
            raise CheckpointError("Публичная карта и состояние checkpoint расходятся.")
        if not isinstance(model.get("run"), dict) or model["run"].get("status") not in {"running", "partial", "failed", "completed"}:
            raise CheckpointError("Checkpoint содержит неизвестное состояние запуска.")
        run = model['run']
        for field in ('steps', 'total_steps', 'attempt', 'attempt_history_omitted'):
            if field in run and (type(run[field]) is not int or run[field] < (1 if field == 'attempt' else 0)):
                raise CheckpointError("Некорректный счётчик запуска в checkpoint: " + field)
        for field in ('duration_seconds', 'total_duration_seconds'):
            if field in run and (type(run[field]) not in (int, float) or run[field] < 0):
                raise CheckpointError("Некорректная длительность в checkpoint: " + field)
        completion = run.get('research_completion')
        if completion is not None and (not isinstance(completion, dict) or completion.get('status') not in ('completed', 'partial')):
            raise CheckpointError('Некорректная отметка завершения исследования в checkpoint.')
        history = run.get('attempt_history', [])
        if not isinstance(history, list) or len(history) > 100 or any(not isinstance(item, dict) for item in history):
            raise CheckpointError("Некорректная история попыток в checkpoint.")
        scenario = model.get("scenario", {}).get("description")
        if not isinstance(scenario, str) or not scenario.strip():
            raise CheckpointError("Checkpoint не содержит исходного описания варианта.")
        return meta
    except CheckpointError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise CheckpointError("Некорректная структура checkpoint.") from exc


def load_checkpoint(path) -> dict:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
        if len(raw) > MAX_CHECKPOINT_BYTES:
            raise CheckpointError("Checkpoint превышает допустимый размер 128 МиБ.")
        payload = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        validate_checkpoint(payload)
        return payload
    except CheckpointError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise CheckpointError("Не удалось прочитать checkpoint.", "Укажите сохранённый scenario_map.checkpoint.json; файл не будет изменён.") from exc


def checkpoint_scenario(payload: dict) -> str:
    return validate_checkpoint(payload)["state"]["model"]["scenario"]["description"]


def validate_compatibility(payload: dict, spec, system_hash: str, tools_hash: str,
                           summary_mode: str, summary_max_tokens: int) -> None:
    saved = validate_checkpoint(payload)["compatibility"]
    current = compatibility(spec, system_hash, tools_hash, summary_mode, summary_max_tokens)
    if saved != current:
        changed = sorted(key for key in set(saved) | set(current) if saved.get(key) != current.get(key))
        raise CheckpointError("Продолжение несовместимо с текущими настройками: " + ", ".join(changed),
                              "Используйте ту же модель, endpoint, режим, окно, настройки суммаризации и контракт инструментов. Секреты подключения можно обновлять; шаги и время задают новый бюджет попытки.")
