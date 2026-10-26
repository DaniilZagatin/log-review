"""CODEOWNERS и метаданные сервиса.

Стандарт (§4.1.3) прямо говорит: CODEOWNERS подтверждает только технического
владельца реализации и не может назначить владельца объекта, отвечающего за
клиентский результат. Поэтому факты помечаются как OWNER и никогда не попадают
в поле «владелец объекта» без подтверждения в business_intent.yaml.
"""

from __future__ import annotations

from pko.extractors.base import Fact, FileTree

OWNER_FILES = {"codeowners", "owners", "service.yaml", "service.yml", "catalog-info.yaml"}


def extract(tree: FileTree) -> list[Fact]:
    facts: list[Fact] = []
    for path in tree.files:
        base = path.rsplit("/", 1)[-1].lower()
        if base not in OWNER_FILES:
            continue
        text = tree.read(path)
        if text is None:
            continue
        if base in {"codeowners", "owners"}:
            for i, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split()
                if len(parts) < 2:
                    continue
                facts.append(
                    Fact(
                        kind="OWNER",
                        key=parts[0],
                        value=parts[1:],
                        path=path,
                        line=i,
                        basis=f"техвладелец пути {parts[0]}",
                    )
                )
        else:
            facts.append(
                Fact(
                    kind="OWNER",
                    key="service-metadata",
                    value=path,
                    path=path,
                    line=1,
                    basis="метаданные сервиса",
                )
            )
    return facts
