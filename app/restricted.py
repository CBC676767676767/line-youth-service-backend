"""Match a declared tool or receipt text against the published exclusions.

The catalogue only holds what the public notice names. The notice's region clause
is broader than any list, so a tool that is absent here is not thereby eligible;
the caseworker still decides. Nothing in this module infers a vendor's country or
ownership, because the notice turns on where a tool is developed or operated and
that is not something to guess from a product name.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent / "data/restricted-tools.json"
LATIN = re.compile(r"[a-z0-9]")


@dataclass(frozen=True)
class Restriction:
    id: str
    name: str
    kind: str
    clause_id: str
    clause_text: str
    note: str
    distinctive: bool


@dataclass(frozen=True)
class Catalog:
    version: str
    source_title: str
    source_url: str
    checked_date: str
    notice: str
    entries: tuple[Restriction, ...]
    patterns: tuple[tuple[str, re.Pattern[str]], ...]


def _normalize(value: str) -> str:
    # Width and case folding only. Spaces are kept so a Latin alias can be
    # anchored to word boundaries instead of matching inside a longer word.
    return unicodedata.normalize("NFKC", value or "").casefold()


def _pattern(alias: str) -> re.Pattern[str]:
    alias = _normalize(alias)
    escaped = re.escape(alias)
    # CJK has no word boundaries; Latin aliases must not match inside a word,
    # otherwise "manuscript" would trip the entry for Manus.
    if LATIN.search(alias):
        return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")
    return re.compile(escaped)


@lru_cache(maxsize=4)
def load_catalog(path: str | None = None) -> Catalog:
    document = json.loads(Path(path or CATALOG_PATH).read_text(encoding="utf-8"))
    clauses = document["clauses"]
    entries: list[Restriction] = []
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for item in document["entries"]:
        clause = next(value for value in clauses.values() if value["id"] == item["clause"])
        entries.append(Restriction(
            id=item["id"], name=item["name"], kind=item["kind"],
            clause_id=clause["id"], clause_text=clause["text"],
            note=item.get("note", ""), distinctive=bool(item.get("distinctive")),
        ))
        for alias in [item["name"], *item.get("aliases", [])]:
            patterns.append((item["id"], _pattern(alias)))
    source = document["source"]
    return Catalog(
        version=document["version"], source_title=source["title"], source_url=source["url"],
        checked_date=source["checked_date"], notice=document["notice"],
        entries=tuple(entries), patterns=tuple(patterns),
    )


def _by_id(catalog: Catalog, entry_id: str) -> Restriction:
    return next(entry for entry in catalog.entries if entry.id == entry_id)


def match_declared(value: str, catalog: Catalog | None = None) -> Restriction | None:
    """The applicant named this tool, so even a common word is unambiguous here."""
    catalog = catalog or load_catalog()
    text = _normalize(value)
    if not text.strip():
        return None
    for entry_id, pattern in catalog.patterns:
        if pattern.search(text):
            return _by_id(catalog, entry_id)
    return None


def scan_text(value: str, catalog: Catalog | None = None) -> list[Restriction]:
    """Free text from a receipt. Only distinctive names are reported.

    A receipt is incidental prose full of vendor names, plan names and addresses,
    and the reading comes from OCR, so a common word like "wink" appearing in it
    is not evidence of anything. Those entries are matched on the declared tool
    instead, where the applicant chose the word deliberately.
    """
    catalog = catalog or load_catalog()
    text = _normalize(value)
    if not text.strip():
        return []
    found: dict[str, Restriction] = {}
    for entry_id, pattern in catalog.patterns:
        if entry_id in found:
            continue
        entry = _by_id(catalog, entry_id)
        if entry.distinctive and pattern.search(text):
            found[entry_id] = entry
    return list(found.values())


def public_catalog(catalog: Catalog | None = None) -> dict:
    """Shape served to the browser so the entry page can warn before submission."""
    catalog = catalog or load_catalog()
    return {
        "version": catalog.version,
        "source": {"title": catalog.source_title, "url": catalog.source_url,
                   "checked_date": catalog.checked_date},
        "notice": catalog.notice,
        "entries": [{"id": entry.id, "name": entry.name, "kind": entry.kind,
                     "clause": entry.clause_text, "distinctive": entry.distinctive}
                    for entry in catalog.entries],
    }
