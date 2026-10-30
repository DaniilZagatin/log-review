"""Промпты агента.

Содержимое промптов не извлекается и не публикуется — фиксируется только факт
наличия, размер и путь. Этого достаточно, чтобы отличить систему с внешними
правилами поведения от системы, где правила зашиты в код.
"""

from __future__ import annotations

from pko.extractors.base import Fact, FileTree, is_vendor

PROMPT_NAMES = {"prompts.py", "prompt.py", "system_prompt.txt", "system_prompt.md",
                "agent_prompt.md", "manager_prompt.md", "tool_prompts.md"}
PROMPT_MARKERS = ("prompt", "промпт")


def extract(tree: FileTree) -> list[Fact]:
    facts: list[Fact] = []
    for path in tree.files:
        if is_vendor(path):
            continue
        base = path.rsplit("/", 1)[-1].lower()
        looks_like_prompt = base in PROMPT_NAMES or (
            any(m in base for m in PROMPT_MARKERS) and path.lower().endswith((".py", ".md", ".txt"))
        )
        if not looks_like_prompt:
            continue
        text = tree.read(path)
        if text is None:
            continue
        facts.append(
            Fact(
                kind="PROMPT",
                key=path,
                value={"chars": len(text), "lines": text.count("\n") + 1},
                path=path,
                line=1,
                basis=f"файл промптов, {len(text)} символов",
            )
        )
    return facts
