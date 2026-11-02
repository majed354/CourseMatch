#!/usr/bin/env python3
"""Conservatively recover text from an embedded font's own character map.

Some Word-produced PDFs draw the correct glyphs while their PDF ``ToUnicode``
map reports different characters.  PyMuPDF exposes both the unreliable
Unicode value and the original glyph id through ``Page.get_texttrace()``.
For an embedded Type0/Identity-H font, the font program can independently map
that glyph id back to Unicode.

This module deliberately has a narrow contract: every visible glyph whose
centre falls in the requested region must have one unambiguous mapping in the
embedded font.  Otherwise recovery returns ``None``.  It never consults OCR,
language models, inherited outcome lists, or neighbouring course records.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import pymupdf


# These ranges cover Latin text, Arabic text, punctuation, and the Arabic
# presentation-form glyphs emitted by Microsoft Word.  Values are end-open.
_UNICODE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0020, 0x007F),
    (0x00A0, 0x0250),
    (0x0600, 0x0700),
    (0x0750, 0x0780),
    (0x0870, 0x0900),
    (0x2000, 0x2070),
    (0xFB50, 0xFE00),
    (0xFE70, 0xFF00),
    (0x1EE00, 0x1EF00),
)

_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")
_FONT_KEY_RE = re.compile(r"[^a-z0-9]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u0870-\u08ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NEUTRAL_RTL_RE = re.compile(r"^[A-Za-z0-9\u0660-\u0669\u06f0-\u06f9./:%+\-]+$")


@dataclass(frozen=True)
class _FontMap:
    xref: int
    glyphs: Mapping[int, str]
    ambiguous_glyphs: frozenset[int]


@dataclass(frozen=True)
class _RecoveredGlyph:
    text: str
    gid: int
    x0: float
    y0: float
    x1: float
    y1: float
    sequence: int

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)


def _font_key(value: Any) -> str:
    name = _SUBSET_PREFIX_RE.sub("", str(value or ""))
    return _FONT_KEY_RE.sub("", name.lower())


def _canonical_character(codepoint: int) -> str:
    return unicodedata.normalize("NFKC", chr(codepoint))


def _invert_embedded_cmap(font: Any) -> tuple[dict[int, str], frozenset[int]]:
    """Build a strict glyph-id map from the embedded font's own cmap.

    Multiple code points are harmless only when NFKC gives exactly the same
    logical text (for example a base Arabic letter and its presentation form).
    A glyph id with genuinely different readings is retained in the ambiguous
    set and can never be used for recovery.
    """

    candidates: dict[int, set[str]] = defaultdict(set)
    for start, stop in _UNICODE_RANGES:
        for codepoint in range(start, stop):
            try:
                gid = int(font.has_glyph(codepoint, fallback=0) or 0)
            except (TypeError, ValueError, RuntimeError):
                continue
            if gid <= 0:
                continue
            candidates[gid].add(_canonical_character(codepoint))

    glyphs: dict[int, str] = {}
    ambiguous: set[int] = set()
    for gid, values in candidates.items():
        if len(values) == 1:
            glyphs[gid] = next(iter(values))
        else:
            ambiguous.add(gid)
    return glyphs, frozenset(ambiguous)


def _embedded_font_maps(
    document: Any,
    page: Any,
    wanted_keys: Iterable[str],
) -> dict[str, list[_FontMap]]:
    wanted = {key for key in wanted_keys if key}
    output: dict[str, list[_FontMap]] = defaultdict(list)
    try:
        resources = page.get_fonts(full=True)
    except Exception:
        return {}

    seen: set[tuple[str, int]] = set()
    for resource in resources:
        if len(resource) < 6:
            continue
        xref, extension, font_type, base_name, _resource_name, encoding = resource[:6]
        key = _font_key(base_name)
        try:
            xref_number = int(xref)
        except (TypeError, ValueError):
            continue
        if (
            key not in wanted
            or not xref_number
            or str(font_type) != "Type0"
            or str(encoding) != "Identity-H"
            or (key, xref_number) in seen
        ):
            continue
        seen.add((key, xref_number))
        try:
            extracted = document.extract_font(xref_number)
            font_bytes = extracted[3]
            if not font_bytes or str(extension).lower() == "n/a":
                continue
            font = pymupdf.Font(fontbuffer=font_bytes)
            glyphs, ambiguous = _invert_embedded_cmap(font)
        except Exception:
            continue
        if glyphs or ambiguous:
            output[key].append(
                _FontMap(
                    xref=xref_number,
                    glyphs=glyphs,
                    ambiguous_glyphs=ambiguous,
                )
            )
    return dict(output)


def _region_coordinates(
    region: Sequence[float] | Any,
) -> tuple[float, float, float, float] | None:
    try:
        if all(hasattr(region, name) for name in ("x0", "y0", "x1", "y1")):
            values = (region.x0, region.y0, region.x1, region.y1)
        else:
            values = tuple(region)
        if len(values) != 4:
            return None
        x0, y0, x1, y1 = map(float, values)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(
        value == value and abs(value) != float("inf") for value in (x0, y0, x1, y1)
    ):
        return None
    if x0 >= x1 or y0 >= y1:
        return None
    return x0, y0, x1, y1


def _is_non_rendering_placeholder(character: Sequence[Any]) -> bool:
    """Recognize shaping placeholders that do not draw a glyph.

    PyMuPDF reports ``gid=-1`` records of zero geometric width next to some
    Arabic ligatures.  The adjacent real glyph's cmap entry expands to the
    full logical character sequence, so these records carry no independent
    visible content.  A missing glyph id with any drawn width is *not* a
    placeholder and remains a hard recovery failure.
    """

    try:
        gid = int(character[1])
        x0, _y0, x1, _y1 = map(float, character[3])
    except (IndexError, TypeError, ValueError, OverflowError):
        return False
    return gid < 0 and abs(x1 - x0) <= 1e-6


def _inside_region(
    bbox: Sequence[float],
    region: tuple[float, float, float, float],
) -> bool:
    try:
        x0, y0, x1, y1 = map(float, bbox)
    except (TypeError, ValueError, OverflowError):
        return False
    center_x = (x0 + x1) / 2.0
    center_y = (y0 + y1) / 2.0
    return region[0] <= center_x <= region[2] and region[1] <= center_y <= region[3]


def _decode_span(
    span: Mapping[str, Any],
    selected: Sequence[Sequence[Any]],
    font_maps: Mapping[str, Sequence[_FontMap]],
    sequence_start: int,
) -> list[_RecoveredGlyph] | None:
    key = _font_key(span.get("font"))
    candidates = font_maps.get(key, ())
    if not candidates:
        return None

    gids: list[int] = []
    for character in selected:
        try:
            gids.append(int(character[1]))
        except (IndexError, TypeError, ValueError):
            return None

    decoded_candidates: dict[tuple[str, ...], _FontMap] = {}
    for candidate in candidates:
        if any(gid in candidate.ambiguous_glyphs for gid in gids):
            continue
        if any(gid not in candidate.glyphs for gid in gids):
            continue
        decoded = tuple(candidate.glyphs[gid] for gid in gids)
        decoded_candidates.setdefault(decoded, candidate)
    if len(decoded_candidates) != 1:
        return None
    decoded = next(iter(decoded_candidates))

    output: list[_RecoveredGlyph] = []
    for offset, (character, text) in enumerate(zip(selected, decoded)):
        try:
            bbox = tuple(map(float, character[3]))
            if len(bbox) != 4:
                return None
        except (IndexError, TypeError, ValueError):
            return None
        output.append(
            _RecoveredGlyph(
                text=text,
                gid=gids[offset],
                x0=bbox[0],
                y0=bbox[1],
                x1=bbox[2],
                y1=bbox[3],
                sequence=sequence_start + offset,
            )
        )
    return output


def _line_groups(glyphs: Sequence[_RecoveredGlyph]) -> list[list[_RecoveredGlyph]]:
    heights = [glyph.height for glyph in glyphs if glyph.height > 0]
    tolerance = max(1.0, (statistics.median(heights) if heights else 10.0) * 0.35)
    lines: list[list[_RecoveredGlyph]] = []
    for glyph in sorted(glyphs, key=lambda item: (item.center_y, item.sequence)):
        if (
            lines
            and abs(
                glyph.center_y - statistics.median(item.center_y for item in lines[-1])
            )
            <= tolerance
        ):
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    return lines


def _is_neutral_rtl_glyph(value: str) -> bool:
    return bool(value and _NEUTRAL_RTL_RE.fullmatch(value))


def _logical_line(glyphs: Sequence[_RecoveredGlyph]) -> str:
    physical = sorted(glyphs, key=lambda item: (item.center_x, item.sequence))
    raw = "".join(item.text for item in physical)
    arabic = len(_ARABIC_RE.findall(raw))
    latin = len(_LATIN_RE.findall(raw))
    if not arabic or arabic < latin:
        logical = physical
    else:
        logical: list[_RecoveredGlyph] = []
        index = len(physical) - 1
        while index >= 0:
            if _is_neutral_rtl_glyph(physical[index].text):
                start = index
                while start - 1 >= 0 and _is_neutral_rtl_glyph(
                    physical[start - 1].text
                ):
                    start -= 1
                logical.extend(physical[start : index + 1])
                index = start - 1
            else:
                logical.append(physical[index])
                index -= 1
    # U+0640 is a visible justification extension, not source content.
    return "".join(item.text for item in logical).replace("\u0640", "")


def recover_embedded_font_text(
    document: Any,
    page_index: int,
    region: Sequence[float] | Any,
) -> str | None:
    """Recover logical text in ``region`` from embedded font glyph ids.

    Args:
        document: An open :class:`pymupdf.Document`-compatible object.
        page_index: Zero-based page index.
        region: ``(x0, y0, x1, y1)`` in page coordinates, or a Rect-like
            object exposing those four attributes.

    Returns:
        The recovered logical text, or ``None`` if the region is empty, a font
        is not embedded/eligible, or any used glyph id is missing or ambiguous.
    """

    coordinates = _region_coordinates(region)
    if coordinates is None:
        return None
    try:
        page = document[page_index]
        traces = page.get_texttrace()
    except Exception:
        return None

    relevant: list[tuple[Mapping[str, Any], list[Sequence[Any]]]] = []
    wanted_keys: set[str] = set()
    for span in traces:
        try:
            render_type = int(span.get("type", 0))
            opacity = float(span.get("opacity", 1.0))
        except (TypeError, ValueError):
            return None
        if render_type == 3 or opacity <= 0:
            continue
        direction = span.get("dir", (1.0, 0.0))
        try:
            if abs(float(direction[1])) > 0.01:
                return None
        except (IndexError, TypeError, ValueError):
            return None
        selected = [
            character
            for character in span.get("chars", ())
            if len(character) >= 4
            and _inside_region(character[3], coordinates)
            and not _is_non_rendering_placeholder(character)
        ]
        if not selected:
            continue
        key = _font_key(span.get("font"))
        if not key:
            return None
        wanted_keys.add(key)
        relevant.append((span, selected))
    if not relevant:
        return None

    cache_key = (page_index, tuple(sorted(wanted_keys)))
    try:
        cache = getattr(document, "_course_outcomes_font_map_cache", None)
        if cache is None:
            cache = {}
            setattr(document, "_course_outcomes_font_map_cache", cache)
    except Exception:
        cache = None
    font_maps = cache.get(cache_key) if cache is not None else None
    if font_maps is None:
        font_maps = _embedded_font_maps(document, page, wanted_keys)
        if cache is not None:
            cache[cache_key] = font_maps
    glyphs: list[_RecoveredGlyph] = []
    sequence = 0
    for span, selected in relevant:
        decoded = _decode_span(span, selected, font_maps, sequence)
        if decoded is None:
            return None
        glyphs.extend(decoded)
        sequence += len(decoded)
    if not glyphs:
        return None

    # Some PDFs paint the same glyph twice (fill and stroke).  De-duplicate
    # only exact geometric duplicates; repeated letters at different positions
    # remain untouched.
    unique: list[_RecoveredGlyph] = []
    seen: set[tuple[Any, ...]] = set()
    for glyph in glyphs:
        identity = (
            glyph.gid,
            glyph.text,
            round(glyph.x0, 3),
            round(glyph.y0, 3),
            round(glyph.x1, 3),
            round(glyph.y1, 3),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(glyph)

    lines = [_logical_line(line) for line in _line_groups(unique)]
    text = re.sub(r"\s+", " ", " ".join(line for line in lines if line)).strip()
    if not text or not any(
        unicodedata.category(character).startswith("L") for character in text
    ):
        return None
    return text


# Descriptive alias for callers that prefer the source before the mechanism.
recover_text_from_embedded_font = recover_embedded_font_text


__all__ = ["recover_embedded_font_text", "recover_text_from_embedded_font"]
