"""Section-loaded access to the bundled `cwms-overview.md` document.

The overview ships in `cwms_tools/data/cwms-overview.md` and is parsed at
import time into stable `section_id` slugs (`orientation`, `entities`,
`publishers`, `gotchas`, ...) so MCP resources and the `cwms_get_overview_section`
tool can serve targeted reads instead of dumping the whole document.

Sections >`CHUNK_SIZE` bytes are exposed in chunks with stable identifiers
derived from `(section_id, ordinal, sha256)` so agents can request chunk N+1
without re-fetching N.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Final

CHUNK_SIZE: Final[int] = 8 * 1024  # 8 KB chunks


def _slug(text: str) -> str:
    """Lowercase, hyphen-separated slug for a heading."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")


@dataclass(frozen=True)
class OverviewChunk:
    """A single byte-bounded slice of a section body."""

    chunk_id: str
    byte_range: tuple[int, int]  # half-open [start, end)
    sha256: str
    text: str
    has_more: bool


@dataclass(frozen=True)
class OverviewSection:
    """One top-level (## ...) section of the overview document."""

    section_id: str
    title: str
    body: str
    size_bytes: int
    sha256: str
    # first 1-3 prose sentences, or "Reference section: <title>." when a
    # section has no prose paragraph
    summary: str

    def chunk_count(self) -> int:
        return max(1, -(-len(self.body.encode("utf-8")) // CHUNK_SIZE))

    def chunks(self) -> list[OverviewChunk]:
        encoded = self.body.encode("utf-8")
        out: list[OverviewChunk] = []
        for ordinal, start in enumerate(range(0, max(1, len(encoded)), CHUNK_SIZE)):
            end = min(start + CHUNK_SIZE, len(encoded))
            piece = encoded[start:end].decode("utf-8", errors="replace")
            digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()[:16]
            out.append(
                OverviewChunk(
                    chunk_id=f"{self.section_id}-{ordinal:03d}-{digest}",
                    byte_range=(start, end),
                    sha256=digest,
                    text=piece,
                    has_more=end < len(encoded),
                )
            )
        return out

    def get_chunk(self, chunk_id: str) -> OverviewChunk | None:
        for c in self.chunks():
            if c.chunk_id == chunk_id:
                return c
        return None


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")


def _first_summary(body: str, *, title: str) -> str:
    """Summarise a section as the first prose paragraph, at most 3 sentences.

    Works on blank-line-separated paragraphs so a list block (including its
    indented continuation lines), table, heading, horizontal rule, or fenced
    code is skipped as a unit. A section with no prose paragraph gets
    ``Reference section: <title>.``
    """
    for paragraph in re.split(r"\n\s*\n", _FENCE.sub("", body)):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        while lines and lines[0].startswith("#"):
            lines.pop(0)
        if not lines:
            continue
        first = lines[0]
        is_table = first.startswith("|") or (
            "|" in first and len(lines) > 1 and bool(_TABLE_DELIM.match(lines[1]))
        )
        if is_table or _RULE.match(first) or _LIST_ITEM.match(first):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(lines))
        text = " ".join(sentences[:3]).strip().rstrip(":").rstrip()
        return text if text.endswith((".", "!", "?")) else text + "."
    return f"Reference section: {title}."


def _load_raw() -> str:
    return (files("cwms_tools.data") / "cwms-overview.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _parse_sections() -> dict[str, OverviewSection]:
    raw = _load_raw()
    # Split on top-level `## ` headings, preserving everything before the first
    # heading as the `front-matter` section.
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(raw))
    sections: dict[str, OverviewSection] = {}

    if not matches:
        body = raw
        sections["overview"] = _build_section("overview", "Overview", body)
        return sections

    pre = raw[: matches[0].start()].strip()
    if pre:
        sections["front-matter"] = _build_section("front-matter", "Front matter", pre)

    for i, m in enumerate(matches):
        title_line = m.group(1).strip()
        # Strip numeric prefixes like "1. Orientation" → "Orientation".
        title = re.sub(r"^\d+\.\s*", "", title_line)
        section_id = _slug(title)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        sections[section_id] = _build_section(section_id, title, body)
    return sections


def _build_section(section_id: str, title: str, body: str) -> OverviewSection:
    encoded = body.encode("utf-8")
    return OverviewSection(
        section_id=section_id,
        title=title,
        body=body,
        size_bytes=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest()[:16],
        summary=_first_summary(body, title=title),
    )


def section_ids() -> list[str]:
    """Stable sorted list of section IDs."""
    return sorted(_parse_sections().keys())


def get_section(section_id: str) -> OverviewSection | None:
    """Return a section by its stable slug ID, or None if not found."""
    return _parse_sections().get(section_id)


def all_sections() -> list[OverviewSection]:
    """Return all sections in stable order."""
    return [_parse_sections()[sid] for sid in section_ids()]


def document_sha256() -> str:
    """SHA-256 of the bundled overview document; part of the capability fingerprint."""
    return hashlib.sha256(_load_raw().encode("utf-8")).hexdigest()


__all__ = [
    "CHUNK_SIZE",
    "OverviewChunk",
    "OverviewSection",
    "all_sections",
    "document_sha256",
    "get_section",
    "section_ids",
]
