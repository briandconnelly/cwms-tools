"""Tests for the bundled cwms-overview.md section loader."""

from __future__ import annotations

from cwms_tools.core import overview


def test_section_ids_includes_canonical_sections() -> None:
    ids = overview.section_ids()
    # Section IDs are auto-derived slugs from the doc headings; assert a few key
    # sections survive that derivation.
    assert "orientation" in ids
    assert "gotchas" in ids
    assert "core-entities" in ids
    assert "sources" in ids


def test_each_section_has_non_empty_body_and_summary() -> None:
    for section in overview.all_sections():
        assert section.body.strip(), f"empty body for {section.section_id}"
        assert section.size_bytes > 0
        # summary may be empty for table-only sections, but the section must
        # always carry a valid sha256.
        assert len(section.sha256) == 16


def test_chunks_cover_full_body_and_have_stable_ids() -> None:
    section = next(s for s in overview.all_sections() if s.chunk_count() > 1)
    chunks = section.chunks()
    # Stable IDs across two calls.
    second = section.chunks()
    assert [c.chunk_id for c in chunks] == [c.chunk_id for c in second]
    # Byte ranges are contiguous and cover the body.
    rebuilt = "".join(c.text for c in chunks)
    assert rebuilt == section.body
    assert chunks[-1].byte_range[1] == section.size_bytes
    assert not chunks[-1].has_more
    assert chunks[0].has_more


def test_get_chunk_returns_none_for_unknown_id() -> None:
    section = overview.all_sections()[0]
    assert section.get_chunk("does-not-exist") is None


def test_document_sha256_is_stable_64_hex() -> None:
    digest = overview.document_sha256()
    assert len(digest) == 64
    assert overview.document_sha256() == digest
