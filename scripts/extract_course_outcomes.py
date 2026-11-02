#!/usr/bin/env python3
"""Deterministically extract pinned course outcomes from published PDFs.

The active input set is the collection of ``pdf_url`` values in ``data.json``.
Every other PDF below ``assets/course-specifications`` is fingerprinted in the
``excluded_sources`` ledger so that a new, removed, or changed file cannot pass
verification unnoticed.

Human corrections live only in ``overrides``.  Re-running this program replaces
``extracted`` wholesale while carrying the old override arrays forward by the
stable ``variant_id``.  An override whose variant disappears stops the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import pdfplumber
except ImportError as exc:  # pragma: no cover - dependency error is actionable
    raise SystemExit(
        "pdfplumber is required; run this script with the documented Python environment"
    ) from exc

try:
    import pymupdf
except ImportError as exc:  # pragma: no cover - dependency error is actionable
    raise SystemExit(
        "PyMuPDF is required for vector-overlay recovery; install the pymupdf package"
    ) from exc

try:
    from pdf_font_recovery import recover_embedded_font_text
except ModuleNotFoundError:  # pragma: no cover - package-style test/import path
    from scripts.pdf_font_recovery import recover_embedded_font_text


SCHEMA_VERSION = "course-outcomes-v1"
EXTRACTOR_NAME = "course-outcomes-extractor"
EXTRACTOR_VERSION = "1.1.0"
SPLIT_OUTCOME_TABLE_SOURCE_SHA256 = frozenset(
    {
        "f019611c2345e5a0e477e2f26de6b23db4d4dd07377795c5f4863327ecdf4a9c",
        "5d4f05de99aa6d7a7d0e5efd2373086b45678b66d66dc474916221d783f15da8",
        "53d2fb5157a972ba279b4193c4f1248c5626df7eaf3f06a1dfbd3fae2b8ecd03",
        "52c5b6568e00e83cd981ff8e443ed15422c881b2db27a7b53bdea3ebef4eeaae",
        "fe9375cacd755098965a43e3eab8546a08adc3ddc8d87d978e54137c9e18ae4c",
        "5c81d64d19889256d8a46fd35d4a302cb0e224de0e6dae87d9169125d9f5f060",
        "b3ac65c88a6c4380e901c01b17e387eff61eb4f3bb192d5835e5043818d12355",
        "51269b1a7e2246e118111a2411852e99ade9030349aa8e4ec5dbfdcc1d8270a0",
        "4627f85d286a978cedc7801b6f41cdafa2b373ae94578d08733c68f0ab0bfdfd",
        "e77767fab6f42f40055197ac0d349ffc53c1e3cb8f2193ffd7666b88333c4dc3",
        # Visually reviewed Quran-program tables whose last CLO continues on
        # the immediately following page.  Construction remains hash-bound so
        # a replaced PDF must be reviewed again before stitching is enabled.
        "7524ed21f60b23ed48473dbae05f52c6132bdb70f3ae4c69be0562a3df7f96bb",
        "f293de8dafc2be63484f7e83d858d7978aa025789664fff7ff8da83c735ee39f",
        "017e6db4201747c176bb2cdb102e6cd741df3e8ea7b1daef2ae9b64d0fc0e4c4",
        "875707c0195289d63e367a99ae899582d2d6e2f304e7187aee136de7ddd6299c",
        "24064fa09ccc11b56aa5e7fcd58a88fcf024db64011a3b2cb10fe0d5834e162c",
        "80f1f1c217e02a561240c401d904281bd705558f81a158642537e0c26f47e0c9",
        # Official Quran-program sources of the two blanked copies above
        # (2002232-2, 2002456-2), visually reviewed on 2026-10-28: row 3.1
        # continues on PDF page 4 under a repeated header.
        "f3a245130ed9a23e5f83d74d6c994b419b6e86658d2701216e5db789c454cd16",
        "5bd5124dd8ca980f88f27be92769992b97dce58065f19246baae7055196816ad",
    }
)
EMBEDDED_FONT_CMAP_SOURCE_SHA256 = frozenset(
    {
        "51269b1a7e2246e118111a2411852e99ade9030349aa8e4ec5dbfdcc1d8270a0",
        "e77767fab6f42f40055197ac0d349ffc53c1e3cb8f2193ffd7666b88333c4dc3",
    }
)
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
ARABIC_EQUIVALENTS = str.maketrans({"ی": "ي", "ک": "ك", "ھ": "ه", "ہ": "ه"})
CID_RE = re.compile(r"\(cid\s*:\s*\d+\)", re.IGNORECASE)
CLO_NUMERIC_EXACT_RE = re.compile(r"^\s*([123])\s*[.,،٫\-–—]\s*([0-9]{1,2})\s*$")
CLO_LEGACY_EXACT_RE = re.compile(
    r"^\s*(?:([عمقك])\s*[-.]?\s*([0-9]{1,2})|"
    r"([0-9]{1,2})\s*[-.]?\s*([عمقك]))\s*$"
)
SOURCE_CLO_MARKER_RE = re.compile(r"^\s*(?:([123])\s*)?(?:\.{2,}|…+)\s*$")
PLO_RE = re.compile(
    r"(?<![A-Za-z0-9\u0600-\u06ff])([عمقك]|K|S|V|P)\s*[-.]?\s*"
    r"([0-9]{1,2}(?:[.]\d+)?)",
    re.IGNORECASE,
)
INVALID_ARABIC_PLO_EXACT_RE = re.compile(
    r"^\s*(?:([\u0621-\u064a])\s*[-.]?\s*([0-9]{1,2}(?:[.]\d+)?)|"
    r"([0-9]{1,2}(?:[.]\d+)?)\s*[-.]?\s*([\u0621-\u064a]))\s*$"
)
PRESENTATION_RE = re.compile(r"[\ufb50-\ufdff\ufe70-\ufeff]")
REPEATED_ARABIC_RE = re.compile(r"([\u0621-\u064a])\1{2,}")
TASHKEEL_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")

CATALOG_FIELDS = (
    "title",
    "summary",
    "specification_code",
    "match_status",
    "match_note",
    "catalog_id",
)
PROGRAM_ORDER = (
    "القرآن وعلومه",
    "القراءات",
    "الدراسات الإسلامية",
    "الشريعة",
    "الأنظمة",
)
TEXT_HINTS = (
    "أن",
    "الطالب",
    "الطالبة",
    "المقرر",
    "التعلم",
    "المعرفة",
    "المهارات",
    "القيم",
    "المباشر",
    "الاختبار",
    "التقييم",
    "التقويم",
    "المحاضرة",
    "البرنامج",
    "القرآن",
    "القراءات",
    "الدراسات الإسلامية",
    "الشريعة",
    "الأنظمة",
)
COURSE_NAME_ALIASES = ("اسم المقرر", "مسمى المقرر", "course name", "course title")
OUTCOME_HEADER_ALIASES = (
    "نواتج التعلم",
    "مخرجات التعلم",
    "learning outcomes",
    "course outcomes",
)
PLO_HEADER_ALIASES = (
    "رمز ناتج التعلم المرتبط بالبرنامج",
    "رمز مخرج التعلم المرتبط بالبرنامج",
    "program outcome",
)
TEACHING_ALIASES = (
    "استراتيجيات التدريس",
    "استراتيجيات التعليم",
    "المحاضرة",
    "المناقشة",
    "المناقشات",
    "الحوار",
    "الحوار",
    "التعلم الذاتي",
    "التعلم التعاوني",
    "العصف الذهني",
    "حل المشكلات",
    "العروض العملية",
    "teaching strategies",
)
ASSESSMENT_ALIASES = (
    "طرق التقييم",
    "طرق التقويم",
    "طرق التقييم",
    "طرق التقويم",
    "اختبار",
    "الاختبار",
    "التقييم",
    "التقويم",
    "تكليف",
    "واجب",
    "بحث",
    "عرض",
    "مشروع",
    "نشاط",
    "شفهي",
    "عروض الطلبة",
    "المناقشات",
    "المشاركة",
    "تقارير",
    "أبحاث",
    "ورقة عمل",
    "assessment",
    "exam",
    "quiz",
    "assignment",
    "project",
)
ASSESSMENT_PLAN_ALIASES = (
    "أنشطة التقييم",
    "نش ة التقييم",
    "نشاط التقييم",
    "نشاطات التقييم",
    "أنشطة التق م",
    "أنشطة التم م",
    "أن طة التقييل",
    "عناصر التقييم",
    "عنصر التقييم",
    "assessment activities",
    "assessment task",
)
WEIGHT_ALIASES = (
    "النسبة من إجمالي درجة التقييم",
    "النس ة",
    "ن إج ال درجة التقييم",
    "ال سبة",
    "من إجمالا درجة",
    "من إج الي درجة التقييل",
    "الفسبة",
    "من إجمالي درجة",
    "النسبة",
    "الوزن",
    "weight",
    "percentage",
)


class SourceChangedError(RuntimeError):
    """Raised when an input changes during one extraction run."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject_duplicate_json_keys(
    pairs: Sequence[tuple[str, Any]],
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        output[key] = value
    return output


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\u00a0", " ").translate(ARABIC_EQUIVALENTS)
    text = "".join(
        character
        for character in text
        if unicodedata.category(character) not in {"Cf", "Cc"} or character in "\n\t"
    )
    text = text.replace("ـ", "").translate(ARABIC_DIGITS)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\s+([،؛:,.%])", r"\1", text)
    text = re.sub(r"\)\s*([0-9]+)\s*\(", r"(\1)", text)
    return text.strip()


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    text = TASHKEEL_RE.sub("", text)
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text = text.replace("ى", "ي")
    return re.sub(r"[^\w\u0600-\u06ff]+", " ", text).strip()


def compact(value: Any) -> str:
    return normalized(value).replace(" ", "")


def contains_alias(value: Any, aliases: Sequence[str]) -> bool:
    token = normalized(value)
    squashed = token.replace(" ", "")
    return any(
        normalized(alias) in token or normalized(alias).replace(" ", "") in squashed
        for alias in aliases
        if normalized(alias)
    )


def normalize_course_name(value: Any) -> str:
    text = normalized(value)
    text = re.sub(r"\b(?:اسم|مسمى)\s+المقرر\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_similarity(left: Any, right: Any) -> float:
    a, b = normalize_course_name(left), normalize_course_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9 * min(len(a), len(b)) / max(len(a), len(b)) + 0.1
    return SequenceMatcher(None, a, b).ratio()


def normalize_scope(
    raw: Mapping[str, Any],
    degree_lookup: Mapping[tuple[str, str, str], set[str]],
) -> dict[str, str]:
    program = clean_text(raw.get("program"))
    plan_type = clean_text(raw.get("plan_type"))
    version = clean_text(raw.get("version"))
    key = (program, plan_type, version)
    expected_degrees = degree_lookup.get(key, set())
    degree = clean_text(raw.get("degree"))
    if not degree and len(expected_degrees) == 1:
        degree = next(iter(expected_degrees))
    if not program or not plan_type or not version or not degree:
        raise ValueError(f"incomplete or unknown scope: {dict(raw)!r}")
    if expected_degrees and degree not in expected_degrees:
        raise ValueError(
            f"scope degree {degree!r} conflicts with programs degrees {sorted(expected_degrees)!r}: {dict(raw)!r}"
        )
    return {
        "program": program,
        "plan_type": plan_type,
        "version": version,
        "degree": degree,
    }


def scope_sort_key(scope: Mapping[str, str]) -> tuple[Any, ...]:
    try:
        version: Any = int(scope["version"])
    except (ValueError, TypeError):
        version = scope["version"]
    return (scope["program"], scope["degree"], scope["plan_type"], version)


def catalog_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in CATALOG_FIELDS if key in raw}


def logical_variants(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    programs = data.get("programs") or []
    degree_lookup: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    program_courses: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for program in programs:
        key = (
            clean_text(program.get("name")),
            clean_text(program.get("plan_type")),
            clean_text(program.get("version")),
        )
        degree = clean_text(program.get("degree"))
        degree_lookup[key].add(degree)
        for course in program.get("courses") or []:
            program_courses.append((program, course))

    result: list[dict[str, Any]] = []
    for course_code, details in (data.get("course_details") or {}).items():
        raw_variants = details.get("variants")
        legacy = not isinstance(raw_variants, list)
        if legacy:
            raw_variants = [details]
        for raw in raw_variants:
            if isinstance(raw.get("scopes"), list):
                raw_scopes = raw["scopes"]
            elif isinstance(raw.get("scope"), dict):
                raw_scopes = [raw["scope"]]
            elif legacy:
                raw_scopes = []
                target_title = raw.get("title")
                for program, course in program_courses:
                    if clean_text(course.get("code")) != clean_text(course_code):
                        continue
                    if normalize_course_name(
                        course.get("name")
                    ) != normalize_course_name(target_title):
                        continue
                    raw_scopes.append(
                        {
                            "program": program.get("name"),
                            "degree": program.get("degree"),
                            "plan_type": program.get("plan_type"),
                            "version": program.get("version"),
                        }
                    )
                if not raw_scopes:
                    raise ValueError(
                        f"legacy course_details entry {course_code} has no exact code+title program scope"
                    )
            else:
                raw_scopes = []

            scopes = sorted(
                {
                    canonical_json(
                        normalize_scope(scope, degree_lookup)
                    ): normalize_scope(scope, degree_lookup)
                    for scope in raw_scopes
                }.values(),
                key=scope_sort_key,
            )
            source_pdf = clean_text(raw.get("pdf_url"))
            if not source_pdf:
                raise ValueError(f"course {course_code} has a variant without pdf_url")
            catalog = catalog_record(raw)
            identity = {
                "course_code": clean_text(course_code),
                "source_pdf": source_pdf,
                "scopes": scopes,
                "catalog": catalog,
            }
            result.append(
                {
                    **identity,
                    "variant_id": hashlib.sha256(
                        canonical_json(identity).encode("utf-8")
                    ).hexdigest(),
                }
            )
    result.sort(key=lambda item: (item["course_code"], item["variant_id"]))
    if len({item["variant_id"] for item in result}) != len(result):
        raise ValueError("duplicate logical variant_id generated from data.json")
    return result


def normalize_plo(value: Any) -> str | None:
    text = unicodedata.normalize("NFKC", clean_text(value))
    matches = list(PLO_RE.finditer(text))
    if len(matches) != 1:
        return None
    prefix, number = matches[0].groups()
    prefix = prefix.upper() if prefix.isascii() else prefix
    return f"{prefix}{number}"


def unique_plos(value: Any) -> list[str]:
    text = unicodedata.normalize("NFKC", clean_text(value))
    result: list[str] = []
    for match in PLO_RE.finditer(text):
        prefix, number = match.groups()
        prefix = prefix.upper() if prefix.isascii() else prefix
        code = f"{prefix}{number}"
        if code not in result:
            result.append(code)
    return result


def normalize_clo(value: Any, *, allow_legacy: bool = False) -> str | None:
    text = clean_text(value)
    numeric = CLO_NUMERIC_EXACT_RE.fullmatch(text)
    if numeric:
        return f"{numeric.group(1)}.{int(numeric.group(2))}"
    legacy = CLO_LEGACY_EXACT_RE.fullmatch(text) if allow_legacy else None
    if legacy:
        prefix = legacy.group(1) or legacy.group(4)
        number = legacy.group(2) or legacy.group(3)
        return f"{prefix}{int(number)}"
    return None


def normalize_source_clo_marker(value: Any) -> str | None:
    """Preserve an explicit source ellipsis without inventing a CLO number."""
    text = clean_text(value)
    match = SOURCE_CLO_MARKER_RE.fullmatch(text)
    if not match:
        return None
    return f"{match.group(1) or ''}..."


def clo_sort_key(code: str) -> tuple[int, int]:
    numeric = CLO_NUMERIC_EXACT_RE.fullmatch(code)
    if numeric:
        return int(numeric.group(1)), int(numeric.group(2))
    legacy = CLO_LEGACY_EXACT_RE.fullmatch(code)
    if legacy:
        prefix = legacy.group(1) or legacy.group(4)
        number = legacy.group(2) or legacy.group(3)
        return {"ع": 1, "م": 2, "ق": 3, "ك": 3}[str(prefix)], int(number)
    return 99, 99


def _neutral_character(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9٠-٩۰-۹./:%+\-]", value))


def reverse_visual_arabic(value: Any) -> str:
    """Reverse each visual-Arabic line without reversing top-to-bottom order."""
    text = str(value or "").replace("\u00a0", " ")

    def reverse_line(line: str) -> str:
        if not re.search(r"[\u0600-\u06ff\ufb50-\ufdff\ufe70-\ufeff]", line):
            return clean_text(unicodedata.normalize("NFKC", line))
        protected: list[str] = []

        def protect(match: re.Match[str]) -> str:
            protected.append(match.group(0))
            return chr(0xE000 + len(protected) - 1)

        masked = re.sub(
            r"[A-Za-z0-9٠-٩۰-۹]+(?:[./:%+\-][A-Za-z0-9٠-٩۰-۹]+)*",
            protect,
            line,
        )
        reversed_line = masked[::-1]
        for index, token in enumerate(protected):
            reversed_line = reversed_line.replace(chr(0xE000 + index), token)
        return clean_text(unicodedata.normalize("NFKC", reversed_line))

    return clean_text("\n".join(reverse_line(line) for line in text.splitlines()))


def direction_score(value: str) -> tuple[int, int, int]:
    token = normalized(value)
    words = set(token.split())
    hint_hits = 0
    for hint in TEXT_HINTS:
        target = normalized(hint)
        if (len(target) <= 3 and target in words) or (
            len(target) > 3 and target in token
        ):
            hint_hits += 1
    program_hits = sum(normalized(program) in token for program in PROGRAM_ORDER)
    artefacts = len(PRESENTATION_RE.findall(value)) + len(
        REPEATED_ARABIC_RE.findall(value)
    )
    return hint_hits + 3 * program_hits, -artefacts, len(token.split())


def logical_cell(value: Any, *, geometric: bool) -> tuple[str, int]:
    raw_source = str(value or "")
    cid_count = len(CID_RE.findall(raw_source))
    without_cid = CID_RE.sub(" ", raw_source)
    raw = clean_text(unicodedata.normalize("NFKC", without_cid))
    reversed_value = reverse_visual_arabic(without_cid)
    raw_score = direction_score(raw)
    reverse_score = direction_score(reversed_value)
    if reverse_score > raw_score or (reverse_score == raw_score and not geometric):
        chosen = reversed_value
    else:
        chosen = raw
    # A decomposed lam-alef glyph can survive a plain-table fallback as ``امل``.
    chosen = re.sub(r"\bامل(?=[\u0621-\u064a])", "الم", chosen)
    chosen = chosen.replace("اال", "الا")
    return clean_text(chosen), cid_count


def extraction_issue(value: Any) -> str | None:
    text = str(value or "")
    if CID_RE.search(text):
        return "cid"
    if PRESENTATION_RE.search(text):
        return "presentation_forms"
    if REPEATED_ARABIC_RE.search(text):
        return "repeated_glyphs"
    if len(normalized(text)) < 8:
        return "too_short"
    return None


def _glyph_groups(items: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    groups: list[list[Mapping[str, Any]]] = []
    for character in items:
        width = float(character["x1"]) - float(character["x0"])
        if groups:
            previous = groups[-1][-1]
            previous_width = float(previous["x1"]) - float(previous["x0"])
            touching = abs(float(previous["x1"]) - float(character["x0"])) < 0.06
            reverse_touching = (
                abs(float(character["x1"]) - float(previous["x0"])) < 0.06
            )
            if (width <= 0.01 and touching) or (
                previous_width <= 0.01 and reverse_touching
            ):
                groups[-1].append(character)
                continue
        groups.append([character])
    return groups


def _is_pdf_extension_glyph(group: Sequence[Mapping[str, Any]]) -> bool:
    """Recognise a producer's narrow justification glyph by geometry alone.

    Several Word/PDF font subsets draw Arabic kashida extensions as repeated
    sub-glyphs but map the extension glyph to an ordinary Arabic letter in the
    ToUnicode table.  Across the affected embedded Sakkal/Arial subsets those
    extensions advance by at most 0.125 em; the narrowest real Arabic base
    glyph in the visually checked QA set advances by 0.147 em.  Keep a small
    margin and never touch decomposed multi-codepoint ligature groups.
    """
    if len(group) != 1:
        return False
    character = group[0]
    value = str(character.get("text") or "")
    if value == "ـ" or not re.fullmatch(r"[\u0621-\u064a]", value):
        return False
    if character.get("upright") is False:
        return False
    size = abs(float(character.get("size") or 0.0))
    width = abs(float(character["x1"]) - float(character["x0"]))
    advance = abs(float(character.get("adv") or width))
    return bool(size and max(width, advance) / size <= 0.14)


def _logical_line(
    items: Sequence[Mapping[str, Any]],
    space_gap: float,
    diagnostics: dict[str, int] | None = None,
) -> str:
    groups = [
        group
        for group in _glyph_groups(sorted(items, key=lambda item: float(item["x0"])))
        if "".join(str(character.get("text") or "") for character in group).strip()
    ]
    if not groups:
        return ""
    source_texts = [
        "".join(str(character.get("text") or "") for character in group)
        for group in groups
    ]
    suppressed = [_is_pdf_extension_glyph(group) for group in groups]
    if diagnostics is not None:
        diagnostics["extension_glyphs_suppressed"] = diagnostics.get(
            "extension_glyphs_suppressed", 0
        ) + sum(suppressed)
    arabic = sum(bool(re.search(r"[\u0600-\u06ff]", token)) for token in source_texts)
    letters = sum(
        bool(re.search(r"[^\W\d_]", token, re.UNICODE)) for token in source_texts
    )
    if not letters or arabic * 2 < letters:
        order = list(range(len(groups)))
    else:
        order: list[int] = []
        index = len(groups) - 1
        while index >= 0:
            if all(_neutral_character(character) for character in source_texts[index]):
                start = index
                while start - 1 >= 0 and all(
                    _neutral_character(character)
                    for character in source_texts[start - 1]
                ):
                    start -= 1
                order.extend(range(start, index + 1))
                index = start - 1
            else:
                order.append(index)
                index -= 1
    output: list[str] = []
    pending_space = False
    for position, group_index in enumerate(order):
        if position:
            previous, current = groups[order[position - 1]], groups[group_index]
            left, right = (
                (previous, current)
                if min(float(item["x0"]) for item in previous)
                <= min(float(item["x0"]) for item in current)
                else (current, previous)
            )
            gap = min(float(item["x0"]) for item in right) - max(
                float(item["x1"]) for item in left
            )
            combining = all(
                unicodedata.category(character) == "Mn"
                for character in source_texts[group_index]
            )
            if (
                gap > space_gap
                and not combining
                and not suppressed[order[position - 1]]
                and not suppressed[group_index]
            ):
                pending_space = True
        if suppressed[group_index]:
            # The suppressed glyph still bridges the physical gap between its
            # neighbouring letters; omitting its box as well as its text would
            # invent a word boundary.
            continue
        if pending_space and output:
            output.append(" ")
        output.append(source_texts[group_index])
        pending_space = False
    return clean_text("".join(output))


def logical_region_text(
    page: Any,
    bbox: Sequence[float] | None = None,
    *,
    diagnostics: dict[str, int] | None = None,
) -> str:
    if bbox:
        px0, py0, px1, py1 = map(float, page.bbox)
        x0, y0, x1, y1 = map(float, bbox)
        clipped = (max(px0, x0), max(py0, y0), min(px1, x1), min(py1, y1))
        if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
            return ""
        characters = [
            character
            for character in page.chars
            if str(character.get("text") or "").strip()
            and clipped[0]
            <= (float(character["x0"]) + float(character["x1"])) / 2
            < clipped[2]
            and clipped[1]
            <= (float(character["top"]) + float(character["bottom"])) / 2
            < clipped[3]
        ]
    else:
        characters = [
            character
            for character in page.chars
            if str(character.get("text") or "").strip()
        ]
    if not characters:
        return ""
    heights = [
        float(character["height"])
        for character in characters
        if float(character.get("height") or 0) > 0
    ]
    widths = [
        float(character["x1"]) - float(character["x0"])
        for character in characters
        if float(character["x1"]) - float(character["x0"]) > 0.01
    ]
    line_tolerance = max(1.0, (statistics.median(heights) if heights else 10.0) * 0.4)
    space_gap = (statistics.median(widths) if widths else 4.0) * 0.32
    lines: list[tuple[float, list[Mapping[str, Any]]]] = []
    for character in sorted(characters, key=lambda item: float(item["top"])):
        if lines and abs(float(character["top"]) - lines[-1][0]) <= line_tolerance:
            lines[-1][1].append(character)
        else:
            lines.append((float(character["top"]), [character]))
    rendered = [
        _logical_line(items, space_gap, diagnostics=diagnostics) for _, items in lines
    ]
    return "\n".join(line for line in rendered if line)


def make_warning(
    code: str,
    message: str,
    *,
    source_page: int | None = None,
    clo_code: str | None = None,
    clo_index: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if source_page is not None:
        item["source_page"] = source_page
    if clo_code is not None:
        item["clo_code"] = clo_code
    if clo_index is not None:
        item["clo_index"] = clo_index
    return item


def warning_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("source_page") or 0,
        item.get("clo_index") if item.get("clo_index") is not None else -1,
        item.get("clo_code") or "",
        item.get("code") or "",
        item.get("message") or "",
    )


def load_auxiliary_sources(
    repo_root: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """Load only hash-bound, repository-pinned edit manifests.

    The manifests are not allowed to float independently of the PDFs.  A hash
    mismatch is therefore a hard error rather than a lower-confidence parse.
    """
    specifications = (
        (
            "assets/course-specifications/unified-new-program-mappings-20260901.json",
            "published multi-program PLO mapping cells",
            "unified",
        ),
        (
            "assets/course-specifications/clo-plo-corrections-20260901/manifest.json",
            "published CLO/PLO correction cells",
            "correction",
        ),
        (
            "assets/course-specifications/shared-course-completions-20260904/manifest.json",
            "published shared-course CLO completions and scoped PLO mappings",
            "shared",
        ),
        (
            "assets/course-specifications/shared-course-blank-plo-20260905/manifest.json",
            "published shared-course specifications with intentionally blank PLO cells",
            "shared_blank",
        ),
    )
    lookup: dict[str, dict[str, Any]] = {}
    ledger: list[dict[str, str]] = []
    for relative, purpose, kind in specifications:
        path = repo_root / relative
        if not path.is_file():
            raise FileNotFoundError(
                f"required auxiliary manifest is missing: {relative}"
            )
        manifest_sha256 = sha256_file(path)
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        if sha256_file(path) != manifest_sha256:
            raise SourceChangedError(
                f"auxiliary manifest changed while reading: {relative}"
            )
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("records"), list
        ):
            raise ValueError(f"invalid {kind} manifest root: {relative}")
        ledger.append({"path": relative, "sha256": manifest_sha256, "purpose": purpose})
        for record in payload["records"]:
            if not isinstance(record, Mapping):
                raise ValueError(f"invalid {kind} manifest record: {record!r}")
            raw_source = record.get("path") if kind == "unified" else record.get("output")
            raw_expected = record.get("output_sha256")
            source = clean_text(raw_source)
            expected = clean_text(raw_expected)
            course_code = clean_text(record.get("course_key"))
            raw_edits = record.get("edits")
            if (
                not isinstance(raw_source, str)
                or not source
                or not isinstance(raw_expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or not course_code
                or not isinstance(raw_edits, list)
            ):
                raise ValueError(f"invalid {kind} manifest record: {record!r}")
            actual_path = repo_root / source
            if not actual_path.is_file():
                raise FileNotFoundError(
                    f"{kind} manifest references a missing PDF: {source}"
                )
            actual = sha256_file(actual_path)
            if actual != expected:
                raise ValueError(
                    f"stale {kind} manifest for {source}: expected {expected}, got {actual}"
                )
            raw_selectors = record.get("selectors", [])
            if not isinstance(raw_selectors, list):
                raise ValueError(f"invalid selectors for {source}")
            selectors: list[dict[str, str]] = []
            allowed_selector_fields = {"program", "plan_type", "version", "degree"}
            for selector in raw_selectors:
                if (
                    not isinstance(selector, Mapping)
                    or not selector
                    or not set(selector).issubset(allowed_selector_fields)
                ):
                    raise ValueError(f"invalid selector for {source}: {selector!r}")
                normalized_selector: dict[str, str] = {}
                for key, value in selector.items():
                    if not isinstance(value, (str, int)) or not clean_text(value):
                        raise ValueError(
                            f"invalid selector value for {source}: {selector!r}"
                        )
                    normalized_selector[str(key)] = clean_text(value)
                selectors.append(normalized_selector)
            if kind in {"correction", "shared", "shared_blank"} and not selectors:
                raise ValueError(f"{kind} manifest record has no selectors: {source}")
            prior_auxiliary = None
            if kind == "shared_blank":
                prior_auxiliary = lookup.get(clean_text(record.get("source")))
            edits: dict[str, dict[str, Any]] = {}
            for edit in raw_edits:
                if not isinstance(edit, Mapping):
                    raise ValueError(f"invalid edit for {source}: {edit!r}")
                effective_edit = dict(edit)
                code = normalize_clo(
                    effective_edit.get("clo")
                    if kind in {"unified", "shared", "shared_blank"}
                    else (
                        effective_edit.get("clo_to")
                        or effective_edit.get("clo_from")
                    )
                )
                if not code or code.endswith(".0"):
                    raise ValueError(
                        f"invalid CLO selector in {kind} manifest: {edit!r}"
                    )
                if code in edits:
                    raise ValueError(f"duplicate {source} {code} in {kind} manifest")
                if prior_auxiliary:
                    prior_edit = prior_auxiliary.get("edits", {}).get(code, {})
                    for field in ("text_to", "source_blank", "assessment_to"):
                        if field in prior_edit:
                            effective_edit.setdefault(field, prior_edit[field])
                page_number = effective_edit.get("page_1_based")
                if not isinstance(page_number, int) or page_number < 1:
                    raise ValueError(f"invalid edit page for {source}: {edit!r}")
                if "text_to" in effective_edit:
                    raw_text_to = effective_edit.get("text_to")
                    if (
                        not isinstance(raw_text_to, str)
                        or not clean_text(raw_text_to)
                        or _candidate_issue(clean_text(raw_text_to))
                    ):
                        raise ValueError(
                            f"invalid replacement CLO text for {source} {code}"
                        )
                if "source_blank" in effective_edit:
                    if (
                        effective_edit.get("source_blank") is not True
                        or "text_to" in effective_edit
                    ):
                        raise ValueError(
                            f"invalid source_blank assertion for {source} {code}"
                        )
                if "assessment_to" in effective_edit:
                    raw_assessment_to = effective_edit.get("assessment_to")
                    assessment_to = clean_text(raw_assessment_to)
                    if (
                        not isinstance(raw_assessment_to, str)
                        or not assessment_to
                        or _text_artifact_issue(assessment_to)
                    ):
                        raise ValueError(
                            f"invalid replacement assessment text for {source} {code}"
                        )
                if kind in {"unified", "shared"}:
                    per_program = effective_edit.get("per_program")
                    if not isinstance(per_program, Mapping) or not per_program:
                        raise ValueError(
                            f"invalid per_program mapping for {source} {code}"
                        )
                    for program, raw_plo in per_program.items():
                        value = clean_text(raw_plo)
                        if (
                            not isinstance(program, str)
                            or not clean_text(program)
                            or not isinstance(raw_plo, str)
                            or (
                                value not in {"—", "–", "-"}
                                and not PLO_RE.fullmatch(value)
                            )
                        ):
                            raise ValueError(
                                f"invalid per_program value for {source} {code}: "
                                f"{program!r}={raw_plo!r}"
                            )
                elif kind == "correction":
                    raw_plo = effective_edit.get("plo_to")
                    if not isinstance(raw_plo, str) or not PLO_RE.fullmatch(
                        clean_text(raw_plo)
                    ):
                        raise ValueError(
                            f"invalid corrected PLO for {source} {code}: {raw_plo!r}"
                        )
                elif (
                    effective_edit.get("blank_by_policy") is not True
                    or effective_edit.get("plo_to") != []
                ):
                    raise ValueError(
                        f"invalid shared-course blank assertion for {source} {code}"
                    )
                edits[code] = effective_edit
            if source in lookup:
                raise ValueError(f"multiple auxiliary manifests claim {source}")
            lookup[source] = {
                "kind": kind,
                "course_code": course_code,
                "source_sha256": expected,
                "selectors": selectors,
                "edits": edits,
            }
    return lookup, sorted(ledger, key=lambda item: item["path"])


def _table_score(rows: Sequence[Sequence[str]]) -> tuple[int, int, int, int]:
    flat = " ".join(cell for row in rows for cell in row if cell)
    anchors = sum(
        contains_alias(flat, aliases)
        for aliases in (
            COURSE_NAME_ALIASES,
            OUTCOME_HEADER_ALIASES,
            PLO_HEADER_ALIASES,
            ASSESSMENT_PLAN_ALIASES,
        )
    )
    clos = sum(bool(normalize_clo(cell)) for row in rows for cell in row if cell)
    artefacts = len(PRESENTATION_RE.findall(flat)) + len(
        REPEATED_ARABIC_RE.findall(flat)
    )
    arabic = len(re.findall(r"[\u0600-\u06ff]", flat))
    return anchors, clos, -artefacts, arabic


def _merge_table_cell(
    geometric_value: Any,
    plain_value: Any,
) -> tuple[str, str]:
    """Merge two readings of one physical PDF table cell conservatively.

    Geometry is authoritative for Arabic prose because ``table.extract()`` can
    preserve visual (rather than logical) line order.  The plain reading is
    used only when geometry is empty or when it alone recovers an exact
    structural CLO/ellipsis token.  The returned provenance is kept aligned
    with the cell so parsers can report when a value used the fallback.
    """
    geometric = clean_text(geometric_value)
    plain, _ = logical_cell(plain_value, geometric=False)
    if not geometric and not plain:
        return "", "empty"
    if not geometric:
        return plain, "plain_cell_fallback"
    if not plain or normalized(geometric) == normalized(plain):
        return geometric, "geometric"

    geometric_code = normalize_clo(geometric)
    plain_code = normalize_clo(plain)
    if geometric_code is None and plain_code is not None:
        return plain, "plain_structural_fallback"
    if not normalize_source_clo_marker(geometric) and normalize_source_clo_marker(
        plain
    ):
        return plain, "plain_structural_fallback"
    return geometric, "geometric"


def _word_box(word: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return tuple(float(word[key]) for key in ("x0", "top", "x1", "bottom"))


def _bundle_bbox(
    bundle: Mapping[str, Any],
) -> tuple[float, float, float, float] | None:
    return _bbox_union(bbox for row in bundle.get("boxes", []) for bbox in row if bbox)


def _same_horizontal_cluster(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_box = _word_box(left)
    right_box = _word_box(right)
    left_center = (left_box[0] + left_box[2]) / 2
    right_center = (right_box[0] + right_box[2]) / 2
    return abs(left_center - right_center) <= 36


def _word_baseline_outcome_bundles(
    page: Any,
    bundles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Split collapsed dotted-row tables using explicit code-word baselines.

    Some published Word tables expose their outer borders to pdfplumber while
    every dotted inner row separator disappears.  ``table.extract()`` then
    joins several CLO codes and several outcome sentences into one cell.  This
    fallback uses only individually positioned source words in the right-hand
    code band; it never manufactures a missing code or fills a sequence.
    """
    try:
        words = page.extract_words(
            x_tolerance=1,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:  # noqa: BLE001 - ordinary table extraction remains usable
        return []
    if not words:
        return []

    extents = [bbox for bundle in bundles if (bbox := _bundle_bbox(bundle))]
    output: list[dict[str, Any]] = []
    for bundle in bundles:
        rows = bundle.get("rows") or []
        flat = " ".join(cell for row in rows for cell in row if cell)
        if not contains_alias(flat, OUTCOME_HEADER_ALIASES):
            continue
        extent = _bundle_bbox(bundle)
        outcome_header = _header_band(bundle, OUTCOME_HEADER_ALIASES)
        if not extent or not outcome_header:
            continue
        later_tops = [bbox[1] for bbox in extents if bbox[1] > extent[3] + 2]
        region_bottom = min(later_tops, default=float(page.height) - 65)
        region_bottom = max(region_bottom, extent[3])

        candidates: list[dict[str, Any]] = []
        for raw_word in words:
            bbox = _word_box(raw_word)
            if bbox[1] < extent[1] or bbox[1] >= region_bottom:
                continue
            code = normalize_clo(raw_word.get("text"), allow_legacy=True)
            source_code = normalize_source_clo_marker(raw_word.get("text"))
            if not code and not source_code:
                continue
            candidates.append(
                {
                    **raw_word,
                    "code": code,
                    "source_code": source_code,
                }
            )
        if not candidates:
            continue

        clusters: list[list[dict[str, Any]]] = []
        for candidate in sorted(
            candidates,
            key=lambda item: (_word_box(item)[0] + _word_box(item)[2]) / 2,
        ):
            if clusters and _same_horizontal_cluster(clusters[-1][-1], candidate):
                clusters[-1].append(candidate)
            else:
                clusters.append([candidate])
        eligible = [
            cluster
            for cluster in clusters
            if sum(bool(item["code"]) for item in cluster) >= 2
        ]
        if not eligible:
            continue
        anchors = max(
            eligible,
            key=lambda cluster: statistics.median(
                (_word_box(item)[0] + _word_box(item)[2]) / 2 for item in cluster
            ),
        )
        anchors.sort(key=lambda item: (_word_box(item)[1], _word_box(item)[0]))

        distinct_anchors: list[dict[str, Any]] = []
        seen_anchors: set[tuple[str, int]] = set()
        for anchor in anchors:
            identity = str(anchor["code"] or anchor["source_code"])
            key = (identity, round(_word_box(anchor)[1] * 2))
            if key in seen_anchors:
                continue
            seen_anchors.add(key)
            distinct_anchors.append(anchor)
        anchors = distinct_anchors
        existing = {
            str(group["code"])
            for group in _table_code_groups(rows)
            if not str(group["code"]).endswith(".0")
        }
        printed = {
            str(anchor["code"])
            for anchor in anchors
            if anchor["code"] and not str(anchor["code"]).endswith(".0")
        }
        has_source_marker = any(anchor["source_code"] for anchor in anchors)
        if printed and printed <= existing and not has_source_marker:
            continue

        anchor_boxes = [_word_box(anchor) for anchor in anchors]
        median_width = statistics.median(box[2] - box[0] for box in anchor_boxes)
        code_band = (
            max(
                extent[0],
                min(box[0] for box in anchor_boxes) - max(6.0, median_width * 0.55),
            ),
            extent[1],
            min(extent[2], max(box[2] for box in anchor_boxes) + 5.0),
            region_bottom,
        )
        structural: list[dict[str, Any]] = []
        for raw_word in words:
            bbox = _word_box(raw_word)
            center = (bbox[0] + bbox[2]) / 2
            if not (code_band[0] <= center <= code_band[2]):
                continue
            if bbox[1] < extent[1] or bbox[1] >= region_bottom:
                continue
            token = clean_text(raw_word.get("text"))
            if (
                normalize_clo(token, allow_legacy=True)
                or normalize_source_clo_marker(token)
                or re.fullmatch(r"(?:[123عمقك]|[.]?[123][.]?)", token)
            ):
                structural.append(raw_word)
        structural.sort(key=lambda item: (_word_box(item)[1], _word_box(item)[0]))

        plo_band = _header_band(bundle, PLO_HEADER_ALIASES)
        if plo_band is None:
            left_header_cells = [
                (clean_text(row[column]), bundle["boxes"][row_index][column])
                for row_index, row in enumerate(rows[:5])
                for column in range(len(row))
                if column < len(bundle["boxes"][row_index])
                and bundle["boxes"][row_index][column]
                and bundle["boxes"][row_index][column][2] <= outcome_header[0] + 5
                and clean_text(row[column])
            ]
            left_header_text = " ".join(value for value, _ in left_header_cells)
            if "رمز" in normalized(left_header_text) and "برنامج" in normalized(
                left_header_text
            ):
                left_boxes = [bbox for _, bbox in left_header_cells if bbox]
                left_union = _bbox_union(left_boxes)
                if left_union:
                    plo_band = (
                        extent[0],
                        left_union[1],
                        min(outcome_header[0], extent[2]),
                        left_union[3],
                    )
        assessment_band = _header_band(
            bundle,
            ("طرق التقييم", "طرق التقويم", "assessment methods"),
        )
        outcome_x0 = max(extent[0], outcome_header[0])
        outcome_x1 = min(code_band[0], extent[2])
        if outcome_x1 - outcome_x0 < 60:
            continue

        outcome_cues = [
            raw_word
            for raw_word in words
            if outcome_x0
            <= (_word_box(raw_word)[0] + _word_box(raw_word)[2]) / 2
            <= outcome_x1
            and extent[1] <= _word_box(raw_word)[1] < region_bottom
            and clean_text(raw_word.get("text")) in {"أن", "ان", "نأ", "نا"}
        ]
        outcome_cues.sort(key=lambda item: _word_box(item)[1])
        cue_for_anchor: dict[tuple[str, int], Mapping[str, Any]] = {}
        last_cue_top = float("-inf")
        for anchor in anchors:
            anchor_box = _word_box(anchor)
            if str(anchor.get("code") or "").endswith(".0"):
                continue
            eligible_cues = [
                cue
                for cue in outcome_cues
                if last_cue_top < _word_box(cue)[1] <= anchor_box[3] + 1
                and anchor_box[1] - _word_box(cue)[1] <= 42
            ]
            if eligible_cues:
                cue = eligible_cues[-1]
                key = (
                    str(anchor["code"] or anchor["source_code"]),
                    round(anchor_box[1] * 2),
                )
                cue_for_anchor[key] = cue
                last_cue_top = _word_box(cue)[1]

        synthetic_rows: list[list[str]] = [
            [
                "طرق التقييم",
                "",
                "",
                "مخرجات التعلم",
                "",
                "الرمز",
                "",
                "رمز مخرج التعلم المرتبط بالبرنامج",
            ]
        ]
        synthetic_boxes: list[list[tuple[float, float, float, float] | None]] = [
            [
                assessment_band,
                None,
                None,
                outcome_header,
                None,
                code_band,
                None,
                plo_band,
            ]
        ]
        synthetic_provenance: list[list[str]] = [
            [
                "word_baseline" if assessment_band else "empty",
                "empty",
                "empty",
                "word_baseline",
                "empty",
                "word_baseline",
                "empty",
                "word_baseline" if plo_band else "empty",
            ]
        ]
        for anchor in anchors:
            anchor_box = _word_box(anchor)
            anchor_key = (
                str(anchor["code"] or anchor["source_code"]),
                round(anchor_box[1] * 2),
            )
            previous = [
                item for item in structural if _word_box(item)[1] < anchor_box[1] - 0.5
            ]
            following = [
                item for item in structural if _word_box(item)[1] > anchor_box[1] + 0.5
            ]
            cue = cue_for_anchor.get(anchor_key)
            if cue:
                row_top = max(extent[1], _word_box(cue)[1] - 2)
            elif previous:
                previous_box = _word_box(previous[-1])
                row_top = (previous_box[3] + anchor_box[1]) / 2
            else:
                row_top = max(extent[1], anchor_box[1] - 18)
            later_cues = [
                _word_box(candidate)[1]
                for key, candidate in cue_for_anchor.items()
                if key != anchor_key and _word_box(candidate)[1] > row_top + 0.5
            ]
            boundary_candidates = list(later_cues)
            if following:
                boundary_candidates.append(_word_box(following[0])[1])
            row_bottom = (
                max(row_top + 1, min(boundary_candidates) - 1.5)
                if boundary_candidates
                else region_bottom
            )
            outcome_box = (outcome_x0, row_top, outcome_x1, row_bottom)
            outcome = clean_text(logical_region_text(page, outcome_box))
            assessment_box = (
                (assessment_band[0], row_top, assessment_band[2], row_bottom)
                if assessment_band
                else None
            )
            assessment = (
                clean_text(logical_region_text(page, assessment_box))
                if assessment_box
                else ""
            )
            plo_box = (
                (plo_band[0], row_top, plo_band[2], row_bottom) if plo_band else None
            )
            plo = clean_text(logical_region_text(page, plo_box)) if plo_box else ""
            code_value = str(anchor["code"] or anchor["source_code"])
            synthetic_rows.append(
                [assessment, "", "", outcome, "", code_value, "", plo]
            )
            synthetic_boxes.append(
                [
                    assessment_box,
                    None,
                    None,
                    outcome_box,
                    None,
                    anchor_box,
                    None,
                    plo_box,
                ]
            )
            synthetic_provenance.append(
                [
                    "word_baseline" if assessment else "empty",
                    "empty",
                    "empty",
                    "word_baseline" if outcome else "empty",
                    "empty",
                    "word_baseline",
                    "empty",
                    "word_baseline" if plo else "empty",
                ]
            )
        if len(synthetic_rows) <= 1:
            continue
        output.append(
            {
                "rows": synthetic_rows,
                "boxes": synthetic_boxes,
                "geometric": True,
                "cell_provenance": synthetic_provenance,
                "plain_fallback_cells": 0,
                "extraction_provenance": "word_baseline",
                "word_baseline_fallback": True,
            }
        )
    return output


def _word_baseline_assessment_bundles(
    page: Any,
    bundles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover assessment rows collapsed into one cell by missing dotted lines."""
    try:
        words = page.extract_words(
            x_tolerance=1,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:  # noqa: BLE001
        return []
    extents = [bbox for bundle in bundles if (bbox := _bundle_bbox(bundle))]
    output: list[dict[str, Any]] = []
    for bundle in bundles:
        rows = bundle.get("rows") or []
        flat = " ".join(cell for row in rows for cell in row if cell)
        if not contains_alias(flat, ASSESSMENT_PLAN_ALIASES):
            continue
        label_band = _header_band(bundle, ASSESSMENT_PLAN_ALIASES)
        weight_band = _header_band(bundle, WEIGHT_ALIASES)
        if not label_band or not weight_band:
            continue
        extent = _bundle_bbox(bundle)
        if not extent:
            continue
        header_bottom = max(label_band[3], weight_band[3])
        region_bottom = min(float(page.height) - 65, header_bottom + 220)
        weight_words: list[dict[str, Any]] = []
        for word in words:
            bbox = _word_box(word)
            center = (bbox[0] + bbox[2]) / 2
            if not (weight_band[0] <= center <= weight_band[2]):
                continue
            if bbox[1] < header_bottom - 1 or bbox[1] >= region_bottom:
                continue
            values = list(
                dict.fromkeys(_weight_values(word.get("text"), exact_number=True))
            )
            if len(values) == 1:
                weight_words.append({**word, "weight": values[0]})
        if not weight_words:
            continue
        weight_words.sort(key=lambda item: (_word_box(item)[1], _word_box(item)[0]))
        anchors: list[dict[str, Any]] = []
        for word in weight_words:
            if anchors and abs(_word_box(anchors[-1])[1] - _word_box(word)[1]) <= 2:
                if anchors[-1]["weight"] == word["weight"]:
                    continue
                # Two different numbers on one baseline are not a safe row anchor.
                anchors.pop()
                continue
            anchors.append(word)
        if not anchors:
            continue
        last_anchor_box = _word_box(anchors[-1])
        containing_bottoms = [
            bbox[3] + 1
            for bbox in extents
            if bbox[1] <= last_anchor_box[1] and bbox[3] + 2 >= last_anchor_box[3]
        ]
        if containing_bottoms:
            region_bottom = min(region_bottom, min(containing_bottoms))

        existing_weight_rows = 0
        for row_index, row in enumerate(rows):
            found = False
            for column, raw in enumerate(row):
                bbox = bundle["boxes"][row_index][column]
                if _horizontal_overlap(bbox, weight_band) < 0.55:
                    continue
                if _weight_values(raw, exact_number=True):
                    found = True
                    break
            existing_weight_rows += int(found)
        if existing_weight_rows >= len(anchors):
            continue

        synthetic_rows: list[list[str]] = [
            [
                "النسبة من إجمالي درجة التقييم",
                "",
                "",
                "أنشطة التقييم",
            ]
        ]
        synthetic_boxes: list[list[tuple[float, float, float, float] | None]] = [
            [
                weight_band,
                None,
                None,
                label_band,
            ]
        ]
        synthetic_provenance: list[list[str]] = [
            [
                "word_baseline",
                "empty",
                "empty",
                "word_baseline",
            ]
        ]
        for index, anchor in enumerate(anchors):
            bbox = _word_box(anchor)
            previous_bottom = (
                _word_box(anchors[index - 1])[3] if index else header_bottom
            )
            row_top = max(header_bottom, (previous_bottom + bbox[1]) / 2)
            row_bottom = (
                max(row_top + 1, _word_box(anchors[index + 1])[1] - 1.5)
                if index + 1 < len(anchors)
                else region_bottom
            )
            label_box = (label_band[0], row_top, label_band[2], row_bottom)
            label = clean_text(logical_region_text(page, label_box)).strip(" .،؛:-")
            if any(
                alias in normalized(label) for alias in ("الاجمالي", "المجموع", "total")
            ):
                continue
            synthetic_rows.append([str(anchor["weight"]), "", "", label])
            synthetic_boxes.append([bbox, None, None, label_box])
            synthetic_provenance.append(
                ["word_baseline", "empty", "empty", "word_baseline"]
            )
        if len(synthetic_rows) <= 1:
            continue
        bundle["suppress_assessment_plan"] = True
        output.append(
            {
                "rows": synthetic_rows,
                "boxes": synthetic_boxes,
                "geometric": True,
                "cell_provenance": synthetic_provenance,
                "plain_fallback_cells": 0,
                "extraction_provenance": "word_baseline",
                "word_baseline_fallback": True,
            }
        )
    return output


def _cell_line_boxes(
    page: Any,
    bbox: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    characters = [
        character
        for character in page.chars
        if bbox[0] <= (float(character["x0"]) + float(character["x1"])) / 2 <= bbox[2]
        and bbox[1]
        <= (float(character["top"]) + float(character["bottom"])) / 2
        <= bbox[3]
    ]
    if not characters:
        return []
    heights = [float(item["bottom"]) - float(item["top"]) for item in characters]
    tolerance = max(1.0, statistics.median(heights) * 0.35)
    lines: list[list[Mapping[str, Any]]] = []
    for character in sorted(characters, key=lambda item: float(item["top"])):
        if (
            lines
            and abs(float(character["top"]) - float(lines[-1][0]["top"])) <= tolerance
        ):
            lines[-1].append(character)
        else:
            lines.append([character])
    return [
        (
            bbox[0],
            min(float(item["top"]) for item in line),
            bbox[2],
            max(float(item["bottom"]) for item in line),
        )
        for line in lines
        if len(
            re.findall(
                r"[\u0600-\u06ffA-Za-z]",
                "".join(str(item.get("text") or "") for item in line),
            )
        )
        >= 2
    ]


def _image_curve_grid_bundle(page: Any) -> dict[str, Any] | None:
    """Recover a five-column table drawn as filled curves over a page raster."""
    page_area = float(page.width) * float(page.height)
    if not any(
        float(image.get("width") or 0) * float(image.get("height") or 0)
        >= page_area * 0.8
        for image in page.images
    ):
        return None
    grouped: dict[tuple[float, float], list[tuple[float, float, float, float]]] = (
        defaultdict(list)
    )
    for curve in page.curves:
        bbox = (
            float(curve["x0"]),
            float(curve["top"]),
            float(curve["x1"]),
            float(curve["bottom"]),
        )
        if (
            not curve.get("fill")
            or not curve.get("path")
            or curve["path"][-1][0] != "h"
            or bbox[2] - bbox[0] < 25
            or bbox[3] - bbox[1] < 12
        ):
            continue
        grouped[(round(bbox[1], 1), round(bbox[3], 1))].append(bbox)

    row_boxes: list[list[tuple[float, float, float, float]]] = []
    for boxes in grouped.values():
        ordered = sorted(boxes, key=lambda item: item[0])
        if len(ordered) != 5:
            continue
        gaps = [right[0] - left[2] for left, right in zip(ordered, ordered[1:])]
        if ordered[-1][2] - ordered[0][0] < float(page.width) * 0.75:
            continue
        if any(gap < -0.5 or gap > 3 for gap in gaps):
            continue
        row_boxes.append(ordered)
    if not row_boxes:
        return None
    outer_rows = [
        boxes
        for boxes in row_boxes
        if not any(
            other[0][1] <= boxes[0][1] + 0.2
            and other[0][3] >= boxes[0][3] - 0.2
            and other[0][3] - other[0][1] > boxes[0][3] - boxes[0][1] + 2
            for other in row_boxes
        )
    ]
    outer_rows.sort(key=lambda boxes: boxes[0][1])
    header_index = next(
        (
            index
            for index, boxes in enumerate(outer_rows)
            if contains_alias(
                logical_region_text(page, boxes[3]), OUTCOME_HEADER_ALIASES
            )
            and "رمز" in normalized(logical_region_text(page, boxes[4]))
        ),
        None,
    )
    if header_index is None:
        return None
    data_boxes = [
        boxes
        for boxes in outer_rows[header_index + 1 :]
        if clean_text(logical_region_text(page, boxes[3]))
    ]
    if len(data_boxes) < 2:
        return None

    header = outer_rows[header_index]
    rows: list[list[str]] = [
        [
            "طرق التقييم",
            "",
            "",
            "مخرجات التعلم",
            "",
            "الرمز",
            "",
            "رمز مخرج التعلم المرتبط بالبرنامج",
        ]
    ]
    boxes: list[list[tuple[float, float, float, float] | None]] = [
        [
            header[0],
            None,
            None,
            header[3],
            None,
            header[4],
            None,
            header[2],
        ]
    ]
    provenance: list[list[str]] = [
        [
            "image_curve_grid",
            "empty",
            "empty",
            "image_curve_grid",
            "empty",
            "image_curve_grid",
            "empty",
            "image_curve_grid",
        ]
    ]
    unreadable_code_rows: list[int] = []
    ocr_line_boxes: dict[str, list[tuple[float, float, float, float]]] = {}
    for source_boxes in data_boxes:
        assessment = clean_text(logical_region_text(page, source_boxes[0]))
        outcome = clean_text(logical_region_text(page, source_boxes[3]))
        code = normalize_clo(logical_region_text(page, source_boxes[4])) or ""
        plo = clean_text(logical_region_text(page, source_boxes[2]))
        row_index = len(rows)
        if not code:
            unreadable_code_rows.append(row_index)
        rows.append([assessment, "", "", outcome, "", code, "", plo])
        boxes.append(
            [
                source_boxes[0],
                None,
                None,
                source_boxes[3],
                None,
                source_boxes[4],
                None,
                source_boxes[2],
            ]
        )
        provenance.append(
            [
                "image_curve_grid" if assessment else "empty",
                "empty",
                "empty",
                "image_curve_grid",
                "empty",
                "image_curve_grid" if code else "empty",
                "empty",
                "image_curve_grid" if plo else "empty",
            ]
        )
        ocr_line_boxes[f"{row_index}:0"] = _cell_line_boxes(page, source_boxes[0])
        ocr_line_boxes[f"{row_index}:3"] = _cell_line_boxes(page, source_boxes[3])
    return {
        "rows": rows,
        "boxes": boxes,
        "geometric": True,
        "cell_provenance": provenance,
        "plain_fallback_cells": 0,
        "extraction_provenance": "image_curve_grid",
        "image_curve_grid_fallback": True,
        "unreadable_code_rows": unreadable_code_rows,
        "ocr_line_boxes": ocr_line_boxes,
    }


def extract_page_tables(page: Any) -> list[dict[str, Any]]:
    """Return one hybrid bundle per physical table, with per-cell provenance."""
    bundles: list[dict[str, Any]] = []
    try:
        located = page.find_tables()
    except Exception:  # noqa: BLE001 - the plain fallback remains usable
        located = []
    for table in located:
        try:
            plain_rows = table.extract() or []
        except Exception:  # noqa: BLE001 - geometry remains authoritative
            plain_rows = []
        rows: list[list[str]] = []
        boxes: list[list[tuple[float, float, float, float] | None]] = []
        provenance: list[list[str]] = []
        geometric_rows = list(table.rows)
        row_count = max(len(geometric_rows), len(plain_rows))
        for row_index in range(row_count):
            geometric_row = (
                geometric_rows[row_index] if row_index < len(geometric_rows) else None
            )
            plain_row = plain_rows[row_index] if row_index < len(plain_rows) else []
            values: list[str] = []
            row_boxes: list[tuple[float, float, float, float] | None] = []
            row_provenance: list[str] = []
            geometric_cells = list(geometric_row.cells) if geometric_row else []
            width = max(len(geometric_cells), len(plain_row))
            for column in range(width):
                bbox = (
                    geometric_cells[column] if column < len(geometric_cells) else None
                )
                geometry_diagnostics: dict[str, int] = {}
                if bbox:
                    try:
                        geometric_value = logical_region_text(
                            page,
                            bbox,
                            diagnostics=geometry_diagnostics,
                        ).replace("\n", " ")
                    except Exception:  # noqa: BLE001 - clamp/crop quirks are local
                        geometric_value = ""
                else:
                    geometric_value = ""
                plain_value = plain_row[column] if column < len(plain_row) else ""
                value, source = _merge_table_cell(geometric_value, plain_value)
                if source == "geometric" and geometry_diagnostics.get(
                    "extension_glyphs_suppressed", 0
                ):
                    source = "geometric_extension_filtered"
                values.append(value)
                row_provenance.append(source)
                row_boxes.append(tuple(map(float, bbox)) if bbox else None)
            if any(values):
                rows.append(values)
                boxes.append(row_boxes)
                provenance.append(row_provenance)
        if rows:
            fallback_cells = sum(
                source.startswith("plain_") for row in provenance for source in row
            )
            bundles.append(
                {
                    "rows": rows,
                    "boxes": boxes,
                    "geometric": True,
                    "cell_provenance": provenance,
                    "plain_fallback_cells": fallback_cells,
                    "extraction_provenance": (
                        "geometric_with_plain_cell_fallback"
                        if fallback_cells
                        else "geometric"
                    ),
                }
            )

    if not bundles:
        try:
            plain_tables = page.extract_tables() or []
        except Exception:  # noqa: BLE001
            plain_tables = []
        for table in plain_tables:
            rows = [
                [logical_cell(cell, geometric=False)[0] for cell in row]
                for row in table
                if any(clean_text(cell) for cell in row)
            ]
            if rows:
                bundles.append(
                    {
                        "rows": rows,
                        "boxes": [[None for _ in row] for row in rows],
                        "geometric": False,
                        "cell_provenance": [
                            ["plain_table" if cell else "empty" for cell in row]
                            for row in rows
                        ],
                        "plain_fallback_cells": sum(
                            bool(cell) for row in rows for cell in row
                        ),
                        "extraction_provenance": "plain_table",
                    }
                )
    original_bundles = tuple(bundles)
    bundles.extend(_word_baseline_outcome_bundles(page, original_bundles))
    bundles.extend(_word_baseline_assessment_bundles(page, original_bundles))
    image_grid = _image_curve_grid_bundle(page)
    if image_grid:
        bundles.append(image_grid)
    return bundles


def _header_column(rows: Sequence[Sequence[str]], aliases: Sequence[str]) -> int | None:
    width = max((len(row) for row in rows), default=0)
    scored: list[tuple[int, int]] = []
    for column in range(width):
        value = " ".join(
            row[column] for row in rows[:20] if column < len(row) and row[column]
        )
        token = normalized(value)
        compacted = token.replace(" ", "")
        score = 0
        for alias in aliases:
            target = normalized(alias)
            if target and target in token:
                score = max(score, 1000 + len(target))
            elif target.replace(" ", "") in compacted:
                score = max(score, len(target))
        if score:
            scored.append((score, column))
    return max(scored)[1] if scored else None


def _header_band(
    bundle: Mapping[str, Any], aliases: Sequence[str]
) -> tuple[float, float, float, float] | None:
    rows = bundle["rows"]
    boxes = bundle["boxes"]
    column = _header_column(rows, aliases)
    if column is None:
        return None
    candidates: list[tuple[float, float, float, float]] = []
    exact_candidates: list[tuple[float, float, float, float]] = []
    for row_index, row in enumerate(rows[:20]):
        if column >= len(row) or column >= len(boxes[row_index]):
            continue
        bbox = boxes[row_index][column]
        if bbox and clean_text(row[column]):
            candidates.append(bbox)
            if contains_alias(row[column], aliases):
                exact_candidates.append(bbox)
    if exact_candidates:
        candidates = exact_candidates
    if not candidates:
        return None
    # Header cells precede the data rows; the first matching column box gives
    # the exact x band even when the PDF inserts virtual empty columns.
    first = min(candidates, key=lambda bbox: (bbox[1], -abs(bbox[2] - bbox[0])))
    return first


def _horizontal_overlap(
    bbox: tuple[float, float, float, float] | None,
    band: tuple[float, float, float, float] | None,
) -> float:
    if not bbox or not band:
        return 0.0
    overlap = max(0.0, min(bbox[2], band[2]) - max(bbox[0], band[0]))
    denominator = max(0.001, min(bbox[2] - bbox[0], band[2] - band[0]))
    return overlap / denominator


def _bbox_union(
    boxes: Iterable[tuple[float, float, float, float] | None],
) -> tuple[float, float, float, float] | None:
    present = [bbox for bbox in boxes if bbox]
    if not present:
        return None
    return (
        min(item[0] for item in present),
        min(item[1] for item in present),
        max(item[2] for item in present),
        max(item[3] for item in present),
    )


def _physical_outcome_region(
    outcome_band: tuple[float, float, float, float] | None,
    code_bbox: tuple[float, float, float, float] | None,
    text_boxes: Iterable[tuple[float, float, float, float] | None],
) -> tuple[float, float, float, float] | None:
    """Return the complete physical outcome cell, not just its text fragments.

    PDF producers often split one rendered cell into several pdfplumber rows or
    put the CLO code on the last baseline.  The outcome header is authoritative
    for the horizontal band; the union of the code cell and every selected text
    fragment is authoritative for the vertical extent.  This recovers clipped
    first/last lines without expanding into an adjacent table column.
    """
    selected = [bbox for bbox in text_boxes if bbox is not None]
    vertical = _bbox_union([code_bbox, *selected])
    if vertical is None:
        return None
    if outcome_band is None:
        text_union = _bbox_union(selected)
        if text_union is None:
            return None
        x0, x1 = text_union[0], text_union[2]
    else:
        x0, x1 = outcome_band[0], outcome_band[2]
    if x1 - x0 < 20 or vertical[3] - vertical[1] < 2:
        return None
    return x0, vertical[1], x1, vertical[3]


def _substantive_source_text(value: Any) -> bool:
    text = str(value or "")
    return bool(
        CID_RE.search(text)
        or re.search(r"[\u0600-\u06ffA-Za-z0-9]", text)
        or PRESENTATION_RE.search(text)
    )


def _source_outcome_cell_is_blank(
    page: Any,
    region: tuple[float, float, float, float] | None,
    raw_text: Any,
) -> bool:
    """Prove a source CLO cell blank from its exact physical region.

    A failed text read is not evidence of a blank source.  The classification
    is made only when the region is known, contains no visible non-whitespace
    glyph, and is not covered by raster or vector artwork whose text layer
    could be absent.  A reviewed hash-matched manifest may separately attest
    that a punctuation-only template mark is an intentionally blank cell.
    """
    if region is None or _substantive_source_text(raw_text):
        return False
    x0, top, x1, bottom = region
    for character in getattr(page, "chars", []):
        try:
            center_x = (float(character["x0"]) + float(character["x1"])) / 2
            center_y = (float(character["top"]) + float(character["bottom"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
        if (
            x0 <= center_x <= x1
            and top <= center_y <= bottom
            and str(character.get("text") or "").strip()
        ):
            return False
    for attribute in ("curves", "lines", "rects"):
        for drawing in getattr(page, attribute, []):
            try:
                center_x = (float(drawing["x0"]) + float(drawing["x1"])) / 2
                center_y = (float(drawing["top"]) + float(drawing["bottom"])) / 2
            except (KeyError, TypeError, ValueError):
                continue
            if x0 < center_x < x1 and top < center_y < bottom:
                return False
    for image in getattr(page, "images", []):
        try:
            image_box = (
                float(image["x0"]),
                float(image["top"]),
                float(image["x1"]),
                float(image["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        overlap_width = max(0.0, min(x1, image_box[2]) - max(x0, image_box[0]))
        overlap_height = max(0.0, min(bottom, image_box[3]) - max(top, image_box[1]))
        region_area = max(1.0, (x1 - x0) * (bottom - top))
        if overlap_width * overlap_height >= region_area * 0.05:
            return False
    return True


def _run_tesseract(png: bytes, *, psm: int) -> str:
    executable = shutil.which("tesseract")
    if not executable:
        return ""
    try:
        process = subprocess.run(
            [executable, "stdin", "stdout", "-l", "ara+eng", "--psm", str(psm)],
            input=png,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if process.returncode:
        return ""
    return clean_text(process.stdout.decode("utf-8", errors="replace"))


def _recover_exact_plo_with_ocr_prefix(
    pdf_page: Any,
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
    value: Any,
) -> str | None:
    """Recover an invalid/missing ToUnicode prefix without re-reading digits.

    A few Word-produced PDFs render a valid Arabic PLO prefix while their
    embedded ToUnicode map reports a different Arabic letter or maps that
    glyph to a space.  Whole-cell OCR is unreliable because the two-character
    code occupies only a tiny corner of a wide merged table cell.  Recovery is
    therefore limited to otherwise reliable vector digits plus exactly one
    adjacent glyph box: either the invalid letter itself or a same-baseline,
    sufficiently wide whitespace placeholder immediately to the digit's RTL
    side.  Only that prefix glyph is rasterised.

    This function never guesses a number, never changes an already valid PLO,
    and rejects ambiguous OCR consensus.
    """
    if bbox is None or _exact_plo(value) is not None:
        return None
    text = unicodedata.normalize("NFKC", clean_text(value))
    match = INVALID_ARABIC_PLO_EXACT_RE.fullmatch(text)
    digit_only = re.fullmatch(r"\s*([0-9]{1,2}(?:[.]\d+)?)\s*", text)
    if match:
        prefix = match.group(1) or match.group(4)
        number = match.group(2) or match.group(3)
        if not prefix or not number or prefix in "عمقك":
            return None
    elif digit_only:
        prefix = None
        number = digit_only.group(1)
    else:
        return None

    x0, top, x1, bottom = bbox
    cell_chars: list[tuple[Mapping[str, Any], tuple[float, float, float, float]]] = []
    for character in getattr(pdf_page, "chars", []):
        try:
            char_box = (
                float(character["x0"]),
                float(character["top"]),
                float(character["x1"]),
                float(character["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        center_x = (char_box[0] + char_box[2]) / 2
        center_y = (char_box[1] + char_box[3]) / 2
        if not (x0 <= center_x <= x1 and top <= center_y <= bottom):
            continue
        cell_chars.append((character, char_box))

    prefix_chars: list[Mapping[str, Any]] = []
    if prefix is not None:
        prefix_chars = [
            character
            for character, _ in cell_chars
            if clean_text(character.get("text")) == prefix
        ]
    else:
        digit_boxes = [
            char_box
            for character, char_box in cell_chars
            if re.fullmatch(r"[0-9.]", clean_text(character.get("text")))
        ]
        digit_bbox = _bbox_union(digit_boxes)
        if digit_bbox is None:
            return None
        digit_height = digit_bbox[3] - digit_bbox[1]
        for character, char_box in cell_chars:
            raw_character = str(character.get("text") or "")
            width = char_box[2] - char_box[0]
            gap = char_box[0] - digit_bbox[2]
            if not raw_character.isspace():
                continue
            if width < max(2.0, 0.25 * digit_height):
                continue
            if abs(char_box[1] - digit_bbox[1]) > 1.5:
                continue
            if abs(char_box[3] - digit_bbox[3]) > 1.5:
                continue
            if not -1.0 <= gap <= max(4.0, 0.35 * digit_height):
                continue
            prefix_chars.append(character)
    if len(prefix_chars) != 1:
        return None

    character = prefix_chars[0]
    try:
        page = document[page_index]
        page_rect = page.rect
        glyph = pymupdf.Rect(
            max(float(page_rect.x0), float(character["x0"])),
            max(float(page_rect.y0), float(character["top"])),
            min(float(page_rect.x1), float(character["x1"])),
            min(float(page_rect.y1), float(character["bottom"])),
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if glyph.width <= 0 or glyph.height <= 0:
        return None

    exact_votes: dict[str, set[tuple[float, int, float]]] = defaultdict(set)
    collapsed_votes: dict[str, set[tuple[float, int, float]]] = defaultdict(set)

    def record_vote(reading: str, scale: float, psm: int, padding: float) -> None:
        token = unicodedata.normalize("NFKC", clean_text(reading))
        exact = re.fullmatch(r"[عمقك]", token)
        repeated = re.fullmatch(r"([عمقك])\1*", token)
        if exact:
            exact_votes[token].add((scale, psm, padding))
        if repeated:
            collapsed_votes[repeated.group(1)].add((scale, psm, padding))

    for scale in (3.0, 4.0, 5.0):
        try:
            png = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), clip=glyph, alpha=False
            ).tobytes("png")
        except Exception:  # noqa: BLE001 - a failed crop leaves the value unresolved
            continue
        for psm in (6, 7):
            record_vote(_run_tesseract(png, psm=psm), scale, psm, 0.0)

    eligible: list[str] = []
    for candidate, observations in exact_votes.items():
        scales = {scale for scale, _, _ in observations}
        psms = {psm for _, psm, _ in observations}
        if len(observations) >= 4 and len(scales) >= 2 and psms == {6, 7}:
            eligible.append(candidate)
    if len(eligible) == 1:
        return f"{eligible[0]}{number}"
    if len(eligible) > 1:
        return None

    # Repeated recognition (for example ``مم`` for one tiny isolated glyph)
    # is accepted only when both segmentation modes agree across scales.
    robust_repeated = [
        candidate
        for candidate, observations in collapsed_votes.items()
        if len(observations) >= 4
        and len({scale for scale, _, _ in observations}) >= 2
        and {psm for _, psm, _ in observations} == {6, 7}
    ]
    if len(robust_repeated) == 1:
        return f"{robust_repeated[0]}{number}"
    if len(robust_repeated) > 1:
        return None
    observed_across_scales = [
        candidate
        for candidate, observations in collapsed_votes.items()
        if len({scale for scale, _, _ in observations}) >= 2
    ]
    if len(observed_across_scales) > 1:
        return None

    padded = pymupdf.Rect(
        max(float(page_rect.x0), glyph.x0 - 2.0),
        max(float(page_rect.y0), glyph.y0 - 2.0),
        min(float(page_rect.x1), glyph.x1 + 2.0),
        min(float(page_rect.y1), glyph.y1 + 2.0),
    )
    for scale in (2.5, 3.0, 3.5, 4.0, 4.5):
        try:
            png = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), clip=padded, alpha=False
            ).tobytes("png")
        except Exception:  # noqa: BLE001 - unresolved is the safe fallback
            continue
        record_vote(_run_tesseract(png, psm=13), scale, 13, 2.0)

    padded_eligible = [
        candidate
        for candidate, observations in collapsed_votes.items()
        if len(
            {
                scale
                for scale, psm, padding in observations
                if psm == 13 and padding == 2.0
            }
        )
        >= 4
    ]
    if len(padded_eligible) != 1:
        return None
    return f"{padded_eligible[0]}{number}"


def _sanitize_ocr_text(value: str) -> str:
    lines = [clean_text(line) for line in value.splitlines() if clean_text(line)]
    arabic = sum(len(re.findall(r"[\u0600-\u06ff]", line)) for line in lines)
    latin = sum(len(re.findall(r"[A-Za-z]", line)) for line in lines)
    kept: list[str] = []
    if arabic >= latin:
        for line in lines:
            if len(re.findall(r"[\u0600-\u06ff]", line)) < 2:
                continue
            line = re.sub(r"[A-Za-z]{2,}", " ", line)
            line = re.sub(r"[{}<>@|]+", " ", line)
            kept.append(clean_text(line))
            if re.search(r"[.؟!]\s*$", line):
                break
    else:
        kept = [line for line in lines if len(re.findall(r"[A-Za-z]", line)) >= 2]
    return clean_text("\n".join(kept))


def ocr_page(document: Any, page_index: int) -> str:
    page = document[page_index]
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(3.0, 3.0), alpha=False)
    return _run_tesseract(pixmap.tobytes("png"), psm=3)


def ocr_region(
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
    *,
    prefer_outcome: bool = True,
) -> str:
    if not bbox:
        return ""
    page = document[page_index]
    page_rect = page.rect
    rect = pymupdf.Rect(
        max(page_rect.x0, bbox[0] - 12),
        max(page_rect.y0, bbox[1] - 8),
        min(page_rect.x1, bbox[2] + 12),
        min(page_rect.y1, bbox[3] + 8),
    )
    if rect.is_empty:
        return ""
    png4 = page.get_pixmap(
        matrix=pymupdf.Matrix(4.0, 4.0), clip=rect, alpha=False
    ).tobytes("png")
    png6 = page.get_pixmap(
        matrix=pymupdf.Matrix(6.0, 6.0), clip=rect, alpha=False
    ).tobytes("png")
    png8 = page.get_pixmap(
        matrix=pymupdf.Matrix(8.0, 8.0), clip=rect, alpha=False
    ).tobytes("png")
    options = [
        _sanitize_ocr_text(_run_tesseract(png4, psm=3)),
        _sanitize_ocr_text(_run_tesseract(png4, psm=6)),
        _sanitize_ocr_text(_run_tesseract(png4, psm=11)),
        _sanitize_ocr_text(_run_tesseract(png6, psm=3)),
        _sanitize_ocr_text(_run_tesseract(png8, psm=11)),
    ]
    if prefer_outcome:
        outcome_options: list[str] = []
        for value in options:
            lines = [line for line in value.splitlines() if clean_text(line)]
            cue = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if _starts_like_outcome(clean_text(line).lstrip("-–—•▪◦* "))
                ),
                None,
            )
            outcome_options.append(
                clean_text("\n".join(lines[cue:])) if cue is not None else value
            )
        options = outcome_options
    usable = [
        value
        for value in options
        if value
        and (
            _ocr_candidate_issue(_clean_outcome(value, "0.0")) is None
            if prefer_outcome
            else _text_artifact_issue(value) is None
        )
    ]
    agreement = 0.90 if prefer_outcome else 0.80
    consensus = [
        value
        for index, value in enumerate(usable)
        if any(
            SequenceMatcher(None, normalized(value), normalized(other)).ratio()
            >= agreement
            and normalized(value).split()[:2] == normalized(other).split()[:2]
            and normalized(value).split()[-2:] == normalized(other).split()[-2:]
            for other_index, other in enumerate(usable)
            if other_index != index
        )
    ]
    if not consensus:
        return ""
    return max(
        consensus,
        key=lambda value: (
            (
                _candidate_issue(_clean_outcome(value, "0.0")) is None
                if prefer_outcome
                else _text_artifact_issue(value) is None
            ),
            normalized(value).startswith("ان ") if prefer_outcome else True,
            len(re.findall(r"[\u0600-\u06ffA-Za-z]", value)),
        ),
    )


def _tight_ocr_candidate_issue(value: str) -> str | None:
    """Validate exact-cell OCR without requiring a particular opening verb."""
    issue = extraction_issue(value) or _text_artifact_issue(value)
    if issue:
        return issue
    if _looks_like_table_header(value) or _is_section_heading(value):
        return "table_header"
    if re.search(r"[0-9]", clean_text(value)):
        return "numeric_ocr_noise"
    words = normalized(value).split()
    letters = len(re.findall(r"[\u0600-\u06ffA-Za-z]", value))
    if len(words) < 4 or letters < 12:
        return "too_short"
    if any(len(word) >= 4 and word.startswith("ة") for word in words):
        return "visually_reversed_ocr_token"
    if any(left == right for left, right in zip(words, words[1:])):
        return "duplicated_ocr_token"
    if words[-1] in {
        "في",
        "من",
        "الى",
        "على",
        "عن",
        "مع",
        "او",
        "و",
        "ثم",
        "ب",
        "ف",
        "ل",
    }:
        return "truncated_ocr_text"
    isolated = re.findall(r"(?:^|\s)([\u0621-\u064a])(?=\s|$)", value)
    if any(token not in {"و", "ف", "ب", "ك", "ل"} for token in isolated):
        return "fragmented_ocr_glyphs"
    punctuation = re.findall(r"[^\w\s\u0600-\u06ff.,،؛:()\[\]\-–—؟!]", value)
    if punctuation:
        return "unexpected_ocr_glyphs"
    return None


def ocr_tight_outcome_consensus(
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
    code: str,
) -> str:
    """Recover a CLO only when exact-cell OCR agrees across scales and PSMs."""
    if not bbox or not shutil.which("tesseract"):
        return ""
    page = document[page_index]
    rect = pymupdf.Rect(*bbox) & page.rect
    if rect.is_empty:
        return ""
    readings: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    for scale, psm in (
        (2.1, 3),
        (2.4, 3),
        (2.1, 6),
        (2.4, 6),
        (3.0, 3),
        (3.0, 6),
        (4.0, 3),
        (4.0, 4),
        (4.0, 6),
    ):
        png = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False
        ).tobytes("png")
        value = _clean_outcome(_sanitize_ocr_text(_run_tesseract(png, psm=psm)), code)
        if _tight_ocr_candidate_issue(value) is not None:
            continue
        identity = normalized(value)
        readings[identity].append((scale, psm, value))
    eligible: list[tuple[tuple[int, int, int], str, list[tuple[float, int, str]]]] = []
    for identity, agreeing in readings.items():
        scales = {item[0] for item in agreeing}
        psms = {item[1] for item in agreeing}
        if len(agreeing) < 4 or len(scales) < 2 or len(psms) < 2:
            continue
        eligible.append(((len(agreeing), len(scales), len(psms)), identity, agreeing))
    eligible.sort(key=lambda item: item[0], reverse=True)
    if not eligible or (len(eligible) > 1 and eligible[0][0] == eligible[1][0]):
        return ""
    winning = eligible[0][2]
    return max(
        (item[2] for item in winning),
        key=lambda item: len(re.findall(r"[\u0600-\u06ffA-Za-z]", item)),
    )


def _ocr_line_candidate_issue(value: str) -> str | None:
    """Validate one physical OCR line without requiring a full CLO sentence."""
    text = clean_text(value)
    if not text or len(re.findall(r"[\u0600-\u06ffA-Za-z]", text)) < 2:
        return "too_short"
    if re.search(r"[0-9]", text):
        return "numeric_ocr_noise"
    if _looks_like_table_header(text) or _is_section_heading(text):
        return "table_header"
    issue = extraction_issue(text) or _text_artifact_issue(text)
    if issue:
        return issue
    if re.search(r"[^\w\s\u0600-\u06ff.,،؛:()\[\]\-–—؟!]", text):
        return "unexpected_ocr_glyphs"
    return None


def _source_terminal(value: str) -> str:
    match = re.search(r"([.؟!])\s*$", clean_text(value))
    return match.group(1) if match else ""


def _ocr_physical_line_consensus(
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float],
    vector_line: str,
) -> str:
    """Read one exact line with cross-scale/cross-segmentation consensus.

    A strong OCR-only value needs two scales and two page-segmentation modes.
    A weaker two-reading cluster is usable only when it exactly corroborates
    the PDF text layer.  This permits short final lines while preventing one
    stable-but-truncated whole-cell OCR reading from becoming pinned data.
    """
    if not shutil.which("tesseract"):
        return ""
    page = document[page_index]
    rect = pymupdf.Rect(*bbox) & page.rect
    if rect.is_empty:
        return ""
    readings: dict[str, list[tuple[float, int, str]]] = defaultdict(list)
    # The first four runs are the common fast path (two scales x two PSMs).
    # PSM 13 and one third scale are bounded fallbacks for short lines; no
    # page-wide OCR is added here.
    ordered_runs = (
        (2.4, 6),
        (2.4, 7),
        (3.0, 6),
        (3.0, 7),
        (2.4, 13),
        (3.0, 13),
        (4.0, 6),
        (4.0, 7),
        (4.0, 13),
    )
    vector_identity = normalized(vector_line)

    def choose_cluster() -> list[tuple[float, int, str]] | None:
        eligible: list[
            tuple[tuple[int, int, int], str, list[tuple[float, int, str]]]
        ] = []
        for identity, agreeing in readings.items():
            scales = {item[0] for item in agreeing}
            psms = {item[1] for item in agreeing}
            strong = len(scales) >= 2 and len(psms) >= 2
            corroborated = (
                identity == vector_identity
                and len(agreeing) >= 2
                and (len(scales) >= 2 or len(psms) >= 2)
            )
            if strong or corroborated:
                eligible.append(
                    (
                        (int(strong), len(scales) * len(psms), len(agreeing)),
                        identity,
                        agreeing,
                    )
                )
        if not eligible:
            return None
        eligible.sort(key=lambda item: item[0], reverse=True)
        if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
            return None
        return eligible[0][2]

    winning: list[tuple[float, int, str]] | None = None
    for run_index, (scale, psm) in enumerate(ordered_runs):
        png = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False
        ).tobytes("png")
        value = _sanitize_ocr_text(_run_tesseract(png, psm=psm))
        if _ocr_line_candidate_issue(value) is not None:
            continue
        identity = normalized(value)
        if not identity:
            continue
        readings[identity].append((scale, psm, value))
        if run_index in {3, 5, 8}:
            winning = choose_cluster()
            if winning is not None:
                break
    if winning is None:
        winning = choose_cluster()
    if winning is None:
        return ""
    terminal = _source_terminal(vector_line)
    representatives = [item[2] for item in winning]
    chosen = max(
        representatives,
        key=lambda value: (
            bool(terminal and _source_terminal(value) == terminal),
            len(re.findall(r"[\u0600-\u06ffA-Za-z]", value)),
            -len(value),
        ),
    )
    if terminal and not _source_terminal(chosen):
        chosen = f"{chosen.rstrip()}{terminal}"
    return clean_text(chosen)


def ocr_tight_outcome_line_consensus(
    pdf_page: Any,
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
    code: str,
) -> str:
    """Recover a CLO only when every physical source line is accounted for."""
    if not bbox:
        return ""
    line_boxes = _cell_line_boxes(pdf_page, bbox)
    if not line_boxes:
        return ""
    recovered: list[str] = []
    for line_bbox in line_boxes:
        diagnostics: dict[str, int] = {}
        vector_line = clean_text(
            logical_region_text(pdf_page, line_bbox, diagnostics=diagnostics)
        )
        vector_issue = _text_artifact_issue(vector_line)
        extension_filtered = bool(diagnostics.get("extension_glyphs_suppressed", 0))
        ocr_line = _ocr_physical_line_consensus(
            document, page_index, line_bbox, vector_line
        )
        if ocr_line:
            recovered.append(ocr_line)
        elif vector_line and vector_issue is None and not extension_filtered:
            # A short terminal word can be below Tesseract's segmentation
            # threshold.  The unaffected PDF text layer still accounts for
            # that physical line, so coverage remains complete.
            recovered.append(vector_line)
        else:
            return ""
    outcome = _clean_outcome("\n".join(recovered), code)
    return outcome if _tight_ocr_candidate_issue(outcome) is None else ""


def ocr_strict_line_consensus(
    document: Any,
    page_index: int,
    line_boxes: Sequence[tuple[float, float, float, float]],
) -> str:
    """OCR exact line crops only when two scales and two PSMs agree."""
    if not line_boxes or not shutil.which("tesseract"):
        return ""
    page = document[page_index]
    recovered_lines: list[str] = []
    for bbox in line_boxes:
        rect = pymupdf.Rect(*bbox) & page.rect
        if rect.is_empty:
            return ""
        readings: list[tuple[int, int, str]] = []
        for scale in (4, 5, 6):
            png = page.get_pixmap(
                matrix=pymupdf.Matrix(float(scale), float(scale)),
                clip=rect,
                alpha=False,
            ).tobytes("png")
            for psm in (6, 7):
                value = _sanitize_ocr_text(_run_tesseract(png, psm=psm))
                if value:
                    readings.append((scale, psm, value))
        winning: list[tuple[int, int, str]] = []
        for scale, psm, value in readings:
            cluster = [
                candidate
                for candidate in readings
                if normalized(value) == normalized(candidate[2])
                or SequenceMatcher(
                    None, normalized(value), normalized(candidate[2])
                ).ratio()
                >= 0.98
            ]
            if (
                len({candidate[0] for candidate in cluster}) >= 2
                and len({candidate[1] for candidate in cluster}) >= 2
                and len(cluster) > len(winning)
            ):
                winning = cluster
        if not winning:
            return ""
        recovered_lines.append(
            max(
                (candidate[2] for candidate in winning),
                key=lambda value: len(normalized(value)),
            )
        )
    return clean_text("\n".join(recovered_lines))


def ocr_numeric_region(
    document: Any,
    page_index: int,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    if not bbox:
        return ""
    executable = shutil.which("tesseract")
    if not executable:
        return ""
    page = document[page_index]
    page_rect = page.rect
    rect = pymupdf.Rect(
        max(page_rect.x0, bbox[0] - 4),
        max(page_rect.y0, bbox[1] - 4),
        min(page_rect.x1, bbox[2] + 4),
        min(page_rect.y1, bbox[3] + 4),
    )
    if rect.is_empty:
        return ""
    png = page.get_pixmap(
        matrix=pymupdf.Matrix(6.0, 6.0), clip=rect, alpha=False
    ).tobytes("png")
    try:
        process = subprocess.run(
            [executable, "stdin", "stdout", "-l", "ara+eng", "--psm", "7"],
            input=png,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if process.returncode:
        return ""
    return clean_text(process.stdout.decode("utf-8", errors="replace"))


def _value_after_label(value: str, aliases: Sequence[str]) -> str:
    candidate = logical_cell(value, geometric=True)[0]
    for alias in aliases:
        match = re.search(re.escape(alias), candidate, re.IGNORECASE)
        if not match:
            continue
        tail = candidate[match.end() :]
        tail = re.sub(r"^[\s:：\-–—]+", "", tail)
        tail = re.split(
            r"\s+(?:رمز|كود)\s+المقرر|\s+(?:course\s+code)",
            tail,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        if clean_text(tail):
            return clean_text(tail)
    return ""


def title_from_tables(pages: Sequence[Sequence[Mapping[str, Any]]]) -> str:
    for bundles in pages[:2]:
        for bundle in bundles:
            rows = bundle["rows"]
            for row_index, row in enumerate(rows):
                for column, cell in enumerate(row):
                    if not contains_alias(cell, COURSE_NAME_ALIASES):
                        continue
                    value = _value_after_label(cell, COURSE_NAME_ALIASES)
                    if value:
                        return value
                    for offset in (1, -1):
                        target = column + offset
                        if 0 <= target < len(row) and clean_text(row[target]):
                            return logical_cell(row[target], geometric=True)[0]
                    if row_index + 1 < len(rows) and column < len(rows[row_index + 1]):
                        value = logical_cell(
                            rows[row_index + 1][column], geometric=True
                        )[0]
                        if value:
                            return value
    return ""


def title_from_ocr(value: str) -> str:
    lines = [clean_text(line) for line in value.splitlines() if clean_text(line)]
    for index, line in enumerate(lines):
        if not contains_alias(line, COURSE_NAME_ALIASES):
            continue
        candidate = _value_after_label(line, COURSE_NAME_ALIASES)
        if candidate:
            return candidate
        if index + 1 < len(lines) and not any(
            contains_alias(lines[index + 1], aliases)
            for aliases in (
                COURSE_NAME_ALIASES,
                ("رمز المقرر", "كود المقرر", "course code"),
            )
        ):
            return lines[index + 1]
    return ""


def title_from_text(value: str) -> str:
    for line in value.splitlines():
        if contains_alias(line, COURSE_NAME_ALIASES):
            candidate = _value_after_label(line, COURSE_NAME_ALIASES)
            if candidate:
                return candidate
    return ""


def _is_section_heading(value: Any) -> bool:
    token = normalized(value)
    return any(
        phrase in token
        for phrase in (
            "المعرفة والفهم",
            "المعرفه والفهم",
            "المهارات",
            "القيم والاستقلالية والمسؤولية",
            "القيم والاستقلاليه والمسؤوليه",
            "knowledge and understanding",
            "skills",
            "values autonomy and responsibility",
        )
    )


def _clean_outcome(value: Any, code: str) -> str:
    text = clean_text(value)
    text = re.sub(rf"^\s*{re.escape(code)}\s*[:.\-–—]*\s*", "", text)
    text = re.sub(r"^[\s\-–—•▪◦*]+", "", text)
    first_outcome = re.search(r"(?:^|\s)(أن\s+)", text)
    if first_outcome and first_outcome.start(1) > 12:
        text = text[first_outcome.start(1) :]
    # Terminal punctuation is part of the published cell.  It is harmless
    # layout for comparison purposes, but silently deleting it makes the
    # pinned transcription less faithful and hides crop truncation.
    return re.sub(r"\s+", " ", text).strip()


def _text_artifact_issue(value: str) -> str | None:
    text = str(value or "")
    if CID_RE.search(text):
        return "cid"
    if PRESENTATION_RE.search(text):
        return "presentation_forms"
    if REPEATED_ARABIC_RE.search(text):
        return "repeated_glyphs"
    if len(re.findall(r"([\u0621-\u064a])\1", text)) >= 4:
        return "repeated_glyph_pairs"
    tokenized = normalized(text).split()
    if re.search(r"\b(?:المنا\s+ات|الان\s+طة)\b", normalized(text)):
        return "known_font_mapping_artifact"
    if "يي" in tokenized:
        return "suspicious_doubled_glyphs"
    if "ةؤ" in normalized(text):
        return "mapped_punctuation_artifact"
    broken_tokens = {
        "الاتةاهات",
        "ايةاد",
        "ببعا",
        "تطوراهها",
        "يفية",
        "يشارج",
        "ينةز",
        "مةال",
        "مةموعات",
        "يصددنف",
        "يمة",
        "وقافع",
    }
    if {"الطال", "والطال"}.intersection(tokenized):
        return "known_font_mapping_artifact"
    token_forms = set(tokenized)
    for token in tokenized:
        without_clitic = (
            token[1:] if len(token) > 1 and token[0] in {"و", "ف"} else token
        )
        token_forms.add(without_clitic)
        if len(without_clitic) > 2 and without_clitic.startswith("ال"):
            token_forms.add(without_clitic[2:])
    if broken_tokens.intersection(token_forms) or "ص ى" in normalized(text):
        return "known_font_mapping_artifact"
    if any(
        len(token) >= 18 for token in re.findall(r"[\u0621-\u064a]+", normalized(text))
    ):
        return "implausibly_long_arabic_token"
    isolated = re.findall(r"(?:^|\s)([\u0621-\u064a])(?=\s|$)", text)
    if len(isolated) >= 3:
        return "fragmented_arabic_glyphs"
    if text.count("(") != text.count(")") or text.count("[") != text.count("]"):
        return "unbalanced_delimiters"
    arabic = len(re.findall(r"[\u0600-\u06ff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if arabic >= 8 and 0 < latin < arabic:
        return "mixed_script_noise"
    return None


def _has_severe_orphan_combining_run(value: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[\s([{«])(?:[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]){4,}",
            str(value or ""),
        )
    )


def _remove_orphan_combining_marks(value: str) -> str:
    mark = r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]"
    return clean_text(
        re.sub(
            rf"(^|[\s،؛:,.!?؟()\[\]{{}}«»])(?:{mark})+",
            r"\1",
            str(value or ""),
        )
    )


def _candidate_issue(value: str) -> str | None:
    issue = extraction_issue(value)
    if issue:
        return issue
    issue = _text_artifact_issue(value)
    if issue:
        return issue
    token = normalized(value)
    if not re.search(r"[\u0600-\u06ffA-Za-z]", token):
        return "no_letters"
    arabic = len(re.findall(r"[\u0600-\u06ff]", value))
    if arabic >= 8 and not _starts_like_outcome(value):
        return "missing_outcome_cue"
    return None


def _ocr_candidate_issue(value: str) -> str | None:
    """Apply a deliberately conservative gate to OCR-only CLO text."""
    issue = _candidate_issue(value)
    if issue:
        return issue
    if re.search(r"[0-9]", clean_text(value)):
        return "numeric_ocr_noise"
    words = normalized(value).split()
    if any(len(word) >= 4 and word.startswith("ة") for word in words):
        return "visually_reversed_ocr_token"
    if any(left == right for left, right in zip(words, words[1:])):
        return "duplicated_ocr_token"
    if words and words[-1] in {
        "في",
        "من",
        "الى",
        "على",
        "عن",
        "مع",
        "او",
        "و",
        "ثم",
        "ب",
        "ف",
        "ل",
    }:
        return "truncated_ocr_text"
    isolated = re.findall(r"(?:^|\s)([\u0621-\u064a])(?=\s|$)", value)
    if (
        len([token for token in isolated if token not in {"و", "ف", "ب", "ك", "ل"}])
        >= 1
    ):
        return "fragmented_ocr_glyphs"
    punctuation = re.findall(r"[^\w\s\u0600-\u06ff.,،؛:()\[\]\-–—]", value)
    if punctuation:
        return "unexpected_ocr_glyphs"
    return None


def _ocr_agrees_with_vector(ocr: str, vector: str) -> bool:
    def collapsed(value: str) -> str:
        value = re.sub(r"([\u0621-\u064a])\1+", r"\1", normalized(value))
        return re.sub(r"[^\w\u0600-\u06ff]+", "", value)

    left = collapsed(ocr)
    right = collapsed(vector)
    return bool(left and right) and left == right


def _program_map(value: Any, programs: Iterable[str]) -> dict[str, str | None]:
    raw, _ = logical_cell(value, geometric=True)
    output: dict[str, str | None] = {}
    for program in sorted(set(programs), key=len, reverse=True):
        match = re.search(
            rf"{re.escape(program)}\s*[:：]\s*(—|–|-|(?:[عمقكKSVP])\s*[-.]?\s*[0-9]+(?:[.]\d+)?)",
            raw,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        token = match.group(1)
        output[program] = None if token in {"—", "–", "-"} else normalize_plo(token)
    return output


def _same_physical_cell(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> bool:
    if left is None or right is None:
        return False
    tolerance = 0.75
    if all(abs(a - b) <= tolerance for a, b in zip(left, right)):
        return True

    # pdfplumber can expose the same merged table cell twice: once with the
    # full spanning bbox and once with a nested bbox around a repeated text
    # fragment.  Treat only full geometric containment as the same cell.  A
    # mere overlap or vertical proximity is deliberately insufficient because
    # a source may publish the same CLO code in two distinct adjacent rows.
    def contains(
        outer: tuple[float, float, float, float],
        inner: tuple[float, float, float, float],
    ) -> bool:
        return (
            outer[0] <= inner[0] + tolerance
            and outer[1] <= inner[1] + tolerance
            and outer[2] + tolerance >= inner[2]
            and outer[3] + tolerance >= inner[3]
        )

    return contains(left, right) or contains(right, left)


def _physical_row_anchor(
    source_page: int,
    bbox: tuple[float, float, float, float] | None,
    bundle_index: int,
    start: int,
) -> str:
    if bbox is not None:
        coordinates = ":".join(f"{value:.1f}" for value in bbox)
        return f"{source_page}:bbox:{coordinates}"
    return f"{source_page}:table:{bundle_index}:row:{start}"


def _document_order_key(
    source_page: int,
    bundle_index: int,
    start: int,
    boxes: Iterable[tuple[float, float, float, float] | None],
) -> tuple[int, int, float, int, int]:
    present = [bbox for bbox in boxes if bbox is not None]
    if present:
        return (
            source_page,
            0,
            round(min(bbox[1] for bbox in present), 3),
            bundle_index,
            start,
        )
    return source_page, 1, float(bundle_index), bundle_index, start


def _table_code_groups(
    rows: Sequence[Sequence[str]],
    *,
    boxes: Sequence[Sequence[tuple[float, float, float, float] | None]] | None = None,
    allow_legacy: bool = False,
) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    legacy_columns = [
        column
        for row in rows
        for column, value in enumerate(row)
        if normalized(value) in {"الرمز", "code"}
    ]
    legacy_column = round(statistics.median(legacy_columns)) if legacy_columns else None
    for row_index, row in enumerate(rows):
        found: list[tuple[str, int]] = []
        for column, value in enumerate(row):
            code = normalize_clo(value, allow_legacy=False)
            if (
                not code
                and allow_legacy
                and legacy_column is not None
                and column == legacy_column
            ):
                code = normalize_clo(value, allow_legacy=True)
            if code:
                found.append((code, column))
        for code, column in found:
            bbox = (
                boxes[row_index][column]
                if boxes is not None
                and row_index < len(boxes)
                and column < len(boxes[row_index])
                else None
            )
            if (
                groups
                and groups[-1]["code"] == code
                and (
                    groups[-1]["rows"][-1] == row_index
                    or _same_physical_cell(groups[-1].get("code_bbox"), bbox)
                )
            ):
                groups[-1]["rows"].append(row_index)
                groups[-1]["columns"].append(column)
            else:
                groups.append(
                    {
                        "code": code,
                        "rows": [row_index],
                        "columns": [column],
                        "code_bbox": bbox,
                    }
                )
    return groups


def _starts_like_outcome(value: Any) -> bool:
    token = normalized(value)
    prefixes = (
        "ان",
        "قدرة",
        "القدرة",
        "معرفة",
        "المعرفة",
        "اتقان",
        "تطبيق",
        "التمييز",
        "اكتساب",
        "الالمام",
        "التعرف",
        "بيان",
        "وصف",
        "يصف",
        "مناقشة",
        "مواكبة",
        "يلم",
        "يفهم",
        "يعرف",
        "يوضح",
        "يذكر",
        "يحدد",
        "يميز",
        "يقارن",
        "يستنبط",
        "يبرهن",
        "يطبق",
        "يستخرج",
        "يكتسب",
        "يتحمل",
        "يمارس",
        "يظهر",
        "يلتزم",
        "يشارك",
        "يعبر",
        "يستخدم",
        "يحضر",
    )
    if any(token.startswith(prefix) for prefix in prefixes):
        return True
    latin = len(re.findall(r"[A-Za-z]", str(value or "")))
    arabic = len(re.findall(r"[\u0600-\u06ff]", str(value or "")))
    return latin >= 8 and latin > arabic


def _starts_like_outcome_boundary(value: Any) -> bool:
    """Permit a narrow font-map loss only for locating a physical row start."""
    return _starts_like_outcome(value) or normalized(value).startswith("حلل ")


def _group_starts(
    groups: Sequence[Mapping[str, Any]],
    rows: Sequence[Sequence[str]] | None = None,
    outcome_column: int | None = None,
) -> list[int]:
    starts: list[int] = []
    for index, group in enumerate(groups):
        first = min(group["rows"])
        if rows is not None and outcome_column is not None:
            anchor = min(group["rows"])
            if index:
                lower = max(groups[index - 1]["rows"]) + 1
            else:
                header_rows = [
                    row_index
                    for row_index, row in enumerate(rows[:anchor])
                    if any(
                        contains_alias(cell, OUTCOME_HEADER_ALIASES)
                        and _looks_like_table_header(cell)
                        for cell in row
                        if cell
                    )
                ]
                lower = max(header_rows) + 1 if header_rows else anchor
            cue_rows: list[int] = []
            for row_index in range(lower, anchor + 1):
                row = rows[row_index]
                if any(
                    column < len(row) and _starts_like_outcome_boundary(row[column])
                    for column in range(
                        max(0, outcome_column - 1),
                        min(len(row), outcome_column + 4),
                    )
                ):
                    cue_rows.append(row_index)
            if index and cue_rows:
                first = min(first, cue_rows[0])
            elif not index and len(cue_rows) == 1:
                # A single cue after the repeated table header is a split cell
                # whose code lands on a later pdfplumber row.  Requiring both
                # the header and a unique cue avoids swallowing a continuation
                # belonging to the preceding page's last CLO.
                first = min(first, cue_rows[0])
        if not index:
            starts.append(first)
            continue
        starts.append(max(starts[-1], first))
    return starts


def _bundle_cell_provenance(
    bundle: Mapping[str, Any], row_index: int, column: int
) -> str:
    provenance = bundle.get("cell_provenance") or []
    if row_index < len(provenance) and column < len(provenance[row_index]):
        return str(provenance[row_index][column])
    return "geometric" if bundle.get("geometric") else "plain_table"


def _is_plain_provenance(value: str) -> bool:
    return value == "plain_table" or value.startswith("plain_")


def _is_extension_filtered_provenance(value: str) -> bool:
    return value == "geometric_extension_filtered"


def _is_inferred_plo_provenance(value: str) -> bool:
    return value == "geometric_inferred_plo_column"


def _is_ocr_prefix_plo_provenance(value: str) -> bool:
    return value == "targeted_ocr_invalid_plo_prefix"


def _exact_plo(value: Any) -> str | None:
    """Return a PLO only when the entire physical cell is exactly that code."""
    for candidate in (clean_text(value), reverse_visual_arabic(value)):
        if candidate and PLO_RE.fullmatch(candidate):
            return normalize_plo(candidate)
    return None


def _infer_plo_column(
    rows: Sequence[Sequence[str]],
    groups: Sequence[Mapping[str, Any]],
    starts: Sequence[int],
) -> tuple[int, int, int] | None:
    """Infer one PLO column from repeated exact codes aligned to CLO blocks.

    This fallback is only for PDFs whose PLO header has a corrupt ToUnicode
    map.  It never scans arbitrary prose for embedded codes: a candidate must
    be an exact PLO cell, occur in at least half of the non-heading CLO blocks,
    and be the unique densest non-CLO column.  A one-row table is accepted only
    at 100% coverage.
    """
    nonzero_indexes = [
        index
        for index, group in enumerate(groups)
        if not str(group["code"]).endswith(".0")
    ]
    if not nonzero_indexes:
        return None
    code_columns = {
        int(column) for group in groups for column in group.get("columns", [])
    }
    aligned: dict[int, set[int]] = defaultdict(set)
    for group_index in nonzero_indexes:
        start = starts[group_index]
        end = starts[group_index + 1] if group_index + 1 < len(starts) else len(rows)
        for row in rows[start:end]:
            for column, value in enumerate(row):
                if column in code_columns or not _exact_plo(value):
                    continue
                aligned[column].add(group_index)
    if not aligned:
        return None
    ranked = sorted(
        ((len(group_indexes), column) for column, group_indexes in aligned.items()),
        reverse=True,
    )
    best_count, best_column = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best_count:
        return None
    total = len(nonzero_indexes)
    minimum = 1 if total == 1 else 2
    if best_count < minimum or best_count / total < 0.5:
        return None
    return best_column, best_count, total


def _interval_union_length(intervals: Iterable[tuple[float, float]]) -> float:
    ordered = sorted((left, right) for left, right in intervals if right > left)
    if not ordered:
        return 0.0
    total = 0.0
    start, end = ordered[0]
    for left, right in ordered[1:]:
        if left <= end + 0.75:
            end = max(end, right)
        else:
            total += end - start
            start, end = left, right
    return total + end - start


def _wide_horizontal_boundaries(
    page: Any,
    extent: tuple[float, float, float, float],
    *,
    required_bands: Sequence[tuple[float, float, float, float]] = (),
) -> list[float]:
    """Return physical row rules that span nearly the whole outcome table.

    Several older Word PDFs expose every text baseline as a tiny filled
    rectangle.  ``pdfplumber.find_tables`` consequently splits one visual row
    into many pseudo-rows.  The real row rules remain distinguishable because
    their adjacent edge segments cover the full table width.  Work only from
    those wide rules; a prose baseline in one column must never become a row
    boundary.
    """
    x0, top, x1, bottom = extent
    width = x1 - x0
    if width <= 20:
        return []
    edge_rows: list[tuple[float, float, float]] = []
    for edge in getattr(page, "edges", []):
        if edge.get("orientation") != "h":
            continue
        try:
            y = float(edge["top"])
            left = max(x0, float(edge["x0"]))
            right = min(x1, float(edge["x1"]))
        except (KeyError, TypeError, ValueError):
            continue
        if y < top - 2 or y > bottom + 2 or right <= left:
            continue
        edge_rows.append((y, left, right))

    # Cluster before testing coverage.  Word-generated borders are commonly
    # repeated at y offsets below one point, and their adjacent column
    # segments need to be considered as one physical rule.
    clusters: list[list[tuple[float, float, float]]] = []
    for edge_row in sorted(edge_rows):
        if clusters and edge_row[0] - clusters[-1][-1][0] <= 2.5:
            clusters[-1].append(edge_row)
        else:
            clusters.append([edge_row])

    def covers(
        intervals: Sequence[tuple[float, float]], left: float, right: float
    ) -> bool:
        cursor = left
        for interval_left, interval_right in sorted(intervals):
            if interval_right < cursor - 0.75:
                continue
            if interval_left > cursor + 0.75:
                return False
            cursor = max(cursor, interval_right)
            if cursor >= right - 0.75:
                return True
        return False

    output: list[float] = []
    for cluster in clusters:
        intervals = [(left, right) for _, left, right in cluster]
        if required_bands:
            # True table-row borders overrun both the PLO and outcome cells.
            # Text-line rectangles usually end exactly at one cell edge, so a
            # three-point margin rejects them without relying on their text.
            accepted = all(
                covers(
                    intervals,
                    max(x0, band[0] - 3.0),
                    min(x1, band[2] + 3.0),
                )
                for band in required_bands
            )
        else:
            accepted = _interval_union_length(intervals) / width >= 0.90
        if accepted:
            output.append(statistics.median(y for y, _, _ in cluster))
    return output


def _plo_band_has_internal_separator(
    page: Any,
    plo_band: tuple[float, float, float, float] | None,
    bbox: tuple[float, float, float, float],
) -> bool:
    """Whether a purported rowspan is visibly split inside the PLO column."""
    if plo_band is None:
        return False
    for edge in getattr(page, "edges", []):
        if edge.get("orientation") != "h":
            continue
        try:
            y = float(edge["top"])
            left = float(edge["x0"])
            right = float(edge["x1"])
        except (KeyError, TypeError, ValueError):
            continue
        if not bbox[1] + 2.5 < y < bbox[3] - 2.5:
            continue
        if left <= plo_band[0] - 3.0 and right >= plo_band[2] + 3.0:
            return True
    return False


def _geometric_group_spans(
    page: Any,
    bundle: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    *,
    plo_band: tuple[float, float, float, float] | None,
    boundary_mode: str = "banded",
) -> dict[int, tuple[float, float]]:
    extent = _bundle_bbox(bundle)
    if extent is None:
        return {}
    outcome_band = _header_band(bundle, OUTCOME_HEADER_ALIASES)
    required_bands = tuple(
        band for band in (plo_band, outcome_band) if band is not None
    )
    boundaries = _wide_horizontal_boundaries(
        page,
        extent,
        required_bands=(
            required_bands
            if boundary_mode == "banded" and len(required_bands) == 2
            else ()
        ),
    )
    spans: dict[int, tuple[float, float]] = {}
    for group_index, group in enumerate(groups):
        if str(group.get("code") or "").endswith(".0"):
            continue
        bbox = group.get("code_bbox")
        if bbox is None:
            continue
        bbox = tuple(map(float, bbox))
        height = bbox[3] - bbox[1]
        center = (bbox[1] + bbox[3]) / 2
        above = [value for value in boundaries if value <= center]
        below = [value for value in boundaries if value >= center]
        if above and below:
            row_top, row_bottom = max(above), min(below)
            if (
                row_bottom - row_top >= 6
                and bbox[1] >= row_top - 3
                and bbox[3] <= row_bottom + 3
            ):
                spans[group_index] = (row_top, row_bottom)
                continue
        # Some simple tables expose the complete CLO cell and no dependable
        # horizontal rules.  A tall source cell remains useful as a fallback,
        # but never outranks an enclosing physical row boundary.
        if height >= 24:
            spans[group_index] = (bbox[1], bbox[3])
    return spans


def _plo_atom_code(value: Any) -> str | None:
    """Accept one positioned PLO atom, allowing only terminal punctuation."""
    for candidate in (clean_text(value), reverse_visual_arabic(value)):
        matches = list(PLO_RE.finditer(candidate))
        if len(matches) != 1:
            continue
        remainder = PLO_RE.sub("", candidate)
        if remainder.strip(" \t\r\n.,،؛;:()[]{}-/–—"):
            continue
        prefix, number = matches[0].groups()
        prefix = prefix.upper() if prefix.isascii() else prefix
        return f"{prefix}{number}"
    return None


def _geometric_plo_values_by_group(
    page: Any,
    bundle: Mapping[str, Any],
    groups: Sequence[Mapping[str, Any]],
    *,
    plo_column: int | None,
    plo_band: tuple[float, float, float, float] | None,
    _boundary_mode: str | None = None,
) -> dict[int, list[tuple[str, str]]]:
    """Align exact PLO cells/atoms with published CLO row geometry.

    This is a conservative supplement to the ordinary block parser.  It
    handles two source layouts without inventing a sequence:

    * old tables whose visual row is fragmented into text baselines, using
      only horizontal rules covering at least 90% of the table width; and
    * genuine row-spanned PLO cells, propagating one exact scalar value to
      every CLO cell physically covered by that source cell.

    A multi-code cell is split only when positioned PDF words give exactly one
    atom to distinct CLO spans.  Ambiguous atoms are ignored.
    """
    if not bundle.get("geometric") or bundle.get("word_baseline_fallback"):
        return {}
    extent = _bundle_bbox(bundle)
    if extent is None or (plo_band is None and plo_column is None):
        return {}

    if _boundary_mode is None:
        alternatives = [
            _geometric_plo_values_by_group(
                page,
                bundle,
                groups,
                plo_column=plo_column,
                plo_band=plo_band,
                _boundary_mode=mode,
            )
            for mode in ("banded", "full")
        ]

        def alignment_score(
            assignments: Mapping[int, Sequence[tuple[str, str]]],
        ) -> tuple[int, int, int]:
            exact = sum(
                len({code for code, _ in values}) == 1
                for values in assignments.values()
            )
            ambiguous = sum(
                len({code for code, _ in values}) > 1 for values in assignments.values()
            )
            return exact, -ambiguous, len(assignments)

        best_score = max(map(alignment_score, alternatives))
        best = [item for item in alternatives if alignment_score(item) == best_score]
        if len(best) == 1 or all(item == best[0] for item in best[1:]):
            return best[0]
        # Equal-quality but different boundary interpretations are not enough
        # evidence to select one.  Retain only occurrence assignments on which
        # every physical model agrees exactly.
        return {
            group_index: values
            for group_index, values in best[0].items()
            if all(item.get(group_index) == values for item in best[1:])
        }

    spans = _geometric_group_spans(
        page,
        bundle,
        groups,
        plo_band=plo_band,
        boundary_mode=_boundary_mode,
    )
    if not spans:
        return {}

    def covered_groups(
        bbox: tuple[float, float, float, float],
    ) -> list[int]:
        matches: list[int] = []
        for group_index, (row_top, row_bottom) in spans.items():
            overlap = min(bbox[3], row_bottom) - max(bbox[1], row_top)
            row_height = row_bottom - row_top
            if overlap > 1.5 and overlap / row_height >= 0.45:
                matches.append(group_index)
                continue
            center = (bbox[1] + bbox[3]) / 2
            if row_top <= center <= row_bottom:
                matches.append(group_index)
        return matches

    assignments: dict[int, list[tuple[str, str]]] = defaultdict(list)
    seen_cells: set[tuple[float, float, float, float, str]] = set()
    try:
        words = page.extract_words(
            x_tolerance=1,
            y_tolerance=2,
            keep_blank_chars=False,
            use_text_flow=False,
        )
    except Exception:  # noqa: BLE001 - exact single-cell recovery still works
        words = []

    for row_index, row in enumerate(bundle.get("rows", [])):
        box_row = bundle.get("boxes", [])[row_index]
        for column, raw in enumerate(row):
            bbox = box_row[column] if column < len(box_row) else None
            if bbox is None:
                continue
            bbox = tuple(map(float, bbox))
            value, _ = logical_cell(raw, geometric=True)
            if not value:
                continue
            in_plo_band = bool(
                (plo_band and _horizontal_overlap(bbox, plo_band) >= 0.55)
                or (
                    plo_band is None and plo_column is not None and column == plo_column
                )
            )
            if not in_plo_band:
                continue
            identity = (*[round(number, 2) for number in bbox], normalized(value))
            if identity in seen_cells:
                continue
            seen_cells.add(identity)
            scalar = _plo_atom_code(value)
            matches = covered_groups(bbox)
            if scalar and matches:
                if len(matches) > 1:
                    # Propagation is allowed only for an actual unsplit PLO
                    # rowspan and may never bridge a published x.0 domain row.
                    if _plo_band_has_internal_separator(page, plo_band, bbox):
                        continue
                    matched_top = min(spans[index][0] for index in matches)
                    matched_bottom = max(spans[index][1] for index in matches)
                    if bbox[3] - bbox[1] < 0.80 * (matched_bottom - matched_top):
                        continue
                    crosses_domain_row = any(
                        str(group.get("code") or "").endswith(".0")
                        and group.get("code_bbox") is not None
                        and matched_top
                        < (float(group["code_bbox"][1]) + float(group["code_bbox"][3]))
                        / 2
                        < matched_bottom
                        for group in groups
                    )
                    if crosses_domain_row:
                        continue
                for group_index in matches:
                    pair = (scalar, "geometric_row_alignment")
                    if pair not in assignments[group_index]:
                        assignments[group_index].append(pair)
                continue

            atoms: list[tuple[str, tuple[float, float, float, float]]] = []
            for word in words:
                word_bbox = _word_box(word)
                center_x = (word_bbox[0] + word_bbox[2]) / 2
                center_y = (word_bbox[1] + word_bbox[3]) / 2
                if not (
                    bbox[0] <= center_x <= bbox[2] and bbox[1] <= center_y <= bbox[3]
                ):
                    continue
                code = _plo_atom_code(word.get("text"))
                if code:
                    atoms.append((code, word_bbox))
            atom_targets: list[tuple[int, str]] = []
            for code, atom_bbox in atoms:
                atom_matches = covered_groups(atom_bbox)
                if len(atom_matches) != 1:
                    atom_targets = []
                    break
                atom_targets.append((atom_matches[0], code))
            if not atom_targets:
                continue
            target_groups = [group_index for group_index, _ in atom_targets]
            if len(target_groups) != len(set(target_groups)):
                # Two different atoms landing in one row may be a legitimate
                # multi-PLO mapping, but the existing explicit-cell rules are
                # the authority for that case.
                continue
            for group_index, code in atom_targets:
                pair = (code, "geometric_row_alignment")
                if pair not in assignments[group_index]:
                    assignments[group_index].append(pair)
    return dict(assignments)


def _geometric_page_plo_values_by_occurrence(
    page: Any,
    bundles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recover PLOs for CLO occurrences split across pdfplumber bundles.

    The physical table is the authority.  Other bundles contribute only exact
    CLO-code boxes inside its right-hand code band; their cell contents are
    never merged.  Results remain keyed by the printed occurrence bbox, not by
    CLO code, so a repeated code in another row cannot inherit a mapping.
    """
    recovered: list[dict[str, Any]] = []
    for host in bundles:
        if (
            not host.get("geometric")
            or host.get("word_baseline_fallback")
            or host.get("image_curve_grid_fallback")
        ):
            continue
        plo_band = _header_band(host, PLO_HEADER_ALIASES)
        outcome_band = _header_band(host, OUTCOME_HEADER_ALIASES)
        extent = _bundle_bbox(host)
        host_groups = _table_code_groups(host.get("rows", []), boxes=host.get("boxes"))
        host_boxes = [
            group.get("code_bbox")
            for group in host_groups
            if not str(group.get("code") or "").endswith(".0")
            and group.get("code_bbox") is not None
        ]
        code_band = _bbox_union(host_boxes)
        if (
            plo_band is None
            or outcome_band is None
            or extent is None
            or code_band is None
        ):
            continue

        physical_groups: list[dict[str, Any]] = []
        for bundle in bundles:
            for group in _table_code_groups(
                bundle.get("rows", []),
                boxes=bundle.get("boxes"),
                allow_legacy=bool(bundle.get("word_baseline_fallback")),
            ):
                code = str(group.get("code") or "")
                bbox = group.get("code_bbox")
                if code.endswith(".0") or bbox is None:
                    continue
                bbox = tuple(map(float, bbox))
                center_y = (bbox[1] + bbox[3]) / 2
                if not extent[1] - 2 <= center_y <= extent[3] + 2:
                    continue
                if _horizontal_overlap(bbox, code_band) < 0.55:
                    continue
                candidate = {"code": code, "code_bbox": bbox}
                duplicate_index = next(
                    (
                        index
                        for index, existing in enumerate(physical_groups)
                        if existing["code"] == code
                        and _same_physical_cell(existing["code_bbox"], bbox)
                    ),
                    None,
                )
                if duplicate_index is None:
                    physical_groups.append(candidate)
                    continue
                previous_bbox = physical_groups[duplicate_index]["code_bbox"]
                previous_area = (previous_bbox[2] - previous_bbox[0]) * (
                    previous_bbox[3] - previous_bbox[1]
                )
                candidate_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                if candidate_area > previous_area:
                    physical_groups[duplicate_index] = candidate

        physical_groups.sort(
            key=lambda group: (
                float(group["code_bbox"][1]),
                float(group["code_bbox"][0]),
                str(group["code"]),
            )
        )
        if not physical_groups:
            continue
        assignments = _geometric_plo_values_by_group(
            page,
            host,
            physical_groups,
            plo_column=_header_column(host.get("rows", []), PLO_HEADER_ALIASES),
            plo_band=plo_band,
        )
        spans = _geometric_group_spans(
            page,
            host,
            physical_groups,
            plo_band=plo_band,
        )
        for group_index, values in assignments.items():
            group = physical_groups[group_index]
            existing = next(
                (
                    item
                    for item in recovered
                    if item["code"] == group["code"]
                    and _same_physical_cell(item["code_bbox"], group["code_bbox"])
                ),
                None,
            )
            if existing is None:
                recovered.append(
                    {
                        "code": group["code"],
                        "code_bbox": group["code_bbox"],
                        "values": list(values),
                        "row_top": spans.get(group_index, (group["code_bbox"][1], 0))[
                            0
                        ],
                    }
                )
                continue
            if tuple(existing["values"]) != tuple(values):
                # Two pdfplumber table interpretations disagree about the
                # same printed occurrence.  Their union is not evidence for
                # a genuine multi-PLO cell, so make the page-level recovery
                # unusable rather than manufacture a mapping.
                existing["conflict"] = True
            existing["row_top"] = min(
                float(existing["row_top"]),
                float(spans.get(group_index, (group["code_bbox"][1], 0))[0]),
            )
    return recovered


def _page_plo_values_for_group(
    recovered: Sequence[Mapping[str, Any]],
    group: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], float | None]:
    code = str(group.get("code") or "")
    bbox = group.get("code_bbox")
    if bbox is None:
        return [], None
    matches = [
        item
        for item in recovered
        if item.get("code") == code and _same_physical_cell(item.get("code_bbox"), bbox)
    ]
    if not matches:
        return [], None
    if any(item.get("conflict") for item in matches):
        return [], None
    distinct = {
        tuple(tuple(value) for value in item.get("values", [])) for item in matches
    }
    if len(distinct) != 1:
        return [], None
    values = list(next(iter(distinct)))
    return values, min(float(item["row_top"]) for item in matches)


def _reconcile_geometric_plo_values(
    local_values: Sequence[tuple[str, str]],
    page_values: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Use page recovery only when it supplements or agrees with local geometry."""
    if not page_values:
        return list(local_values)
    if local_values and list(local_values) != list(page_values):
        return []
    return list(page_values)


def _ellipsis_clo_blocks(
    rows: Sequence[Sequence[str]],
    groups: Sequence[Mapping[str, Any]],
    outcome_column: int | None,
    *,
    word_baseline: bool = False,
) -> list[dict[str, Any]]:
    """Locate only strongly bounded substantive CLO rows coded as ellipses.

    An ellipsis is accepted only in the inferred code column, after a labelled
    non-heading CLO, before the next ``x.0`` section heading, and in a block
    that independently contains both an outcome cue and a PLO token.  These
    conditions intentionally reject trailing template ellipses.
    """
    if outcome_column is None:
        return []
    code_columns = [
        int(column) for group in groups for column in group.get("columns", [])
    ]
    if not code_columns:
        return []
    code_column = round(statistics.median(code_columns))
    output: list[dict[str, Any]] = []
    seen_rows: set[int] = set()
    for row_index, row in enumerate(rows):
        markers = [
            (column, normalize_source_clo_marker(row[column]))
            for column in range(max(0, code_column - 1), min(len(row), code_column + 2))
            if normalize_source_clo_marker(row[column])
        ]
        ellipsis_columns = [column for column, _ in markers]
        if not ellipsis_columns or row_index in seen_rows:
            continue
        source_code = next(str(marker) for _, marker in markers if marker)
        if word_baseline and source_code != "...":
            values = [
                clean_text(row[column])
                for column in range(
                    max(0, outcome_column - 1),
                    min(len(row), outcome_column + 4),
                )
                if column < len(row)
                and column <= code_column
                and clean_text(row[column])
                and not normalize_source_clo_marker(row[column])
            ]
            substantive = clean_text(" ".join(values))
            if _candidate_issue(_clean_outcome(substantive, source_code)) is None:
                output.append(
                    {
                        "start": row_index,
                        "end": row_index + 1,
                        "ellipsis_row": row_index,
                        "code_column": code_column,
                        "source_code": source_code,
                    }
                )
                seen_rows.add(row_index)
            continue
        previous = [
            group
            for group in groups
            if not str(group["code"]).endswith(".0") and max(group["rows"]) < row_index
        ]
        following = [group for group in groups if min(group["rows"]) > row_index]
        if not previous or not following:
            continue
        previous_group = max(previous, key=lambda group: max(group["rows"]))
        next_group = min(following, key=lambda group: min(group["rows"]))
        if not str(next_group["code"]).endswith(".0"):
            continue
        lower = max(previous_group["rows"]) + 1
        cue_row: int | None = None
        for candidate_row in range(lower, row_index + 1):
            candidate = rows[candidate_row]
            if any(
                column < len(candidate) and _starts_like_outcome(candidate[column])
                for column in range(
                    max(0, outcome_column - 1),
                    min(len(candidate), outcome_column + 4),
                )
            ):
                cue_row = candidate_row
                break
        if cue_row is None:
            continue
        has_plo = any(
            unique_plos(value) or unique_plos(reverse_visual_arabic(value))
            for candidate in rows[lower : row_index + 1]
            for value in candidate
            if clean_text(value)
        )
        if not has_plo:
            continue
        output.append(
            {
                "start": cue_row,
                "end": row_index + 1,
                "ellipsis_row": row_index,
                "code_column": code_column,
                "source_code": source_code,
            }
        )
        seen_rows.add(row_index)
    return output


def _assessment_from_block(
    rows: Sequence[Sequence[str]],
    start: int,
    end: int,
    assessment_column: int | None,
) -> str | None:
    if assessment_column is None:
        return None
    candidates: list[str] = []
    for row in rows[start:end]:
        for column in range(max(0, assessment_column - 1), assessment_column + 2):
            if column >= len(row):
                continue
            value, _ = logical_cell(row[column], geometric=True)
            if not value or contains_alias(value, OUTCOME_HEADER_ALIASES):
                continue
            normal = normalized(value)
            if "غير المباشر" in normal or "indirect" in normal:
                prefix = re.split(
                    r"غير\s*المباشر|indirect", value, maxsplit=1, flags=re.IGNORECASE
                )[0]
                value = prefix
                normal = normalized(value)
            value = re.sub(
                r"^.*?(?:المباشر|direct(?:\s+assessment)?)\s*[:：]?\s*",
                "",
                value,
                flags=re.IGNORECASE,
            )
            value = clean_text(value).strip(" .،؛:-")
            if len(normalized(value)) < 3:
                continue
            if contains_alias(value, ASSESSMENT_ALIASES) or (
                5 <= len(normal) < 100 and not contains_alias(value, TEACHING_ALIASES)
            ):
                candidates.append(value)
    unique: list[str] = []
    identities: set[str] = set()
    for candidate in candidates:
        identity = normalized(candidate)
        if identity and identity not in identities:
            identities.add(identity)
            unique.append(candidate)
    return "؛ ".join(unique) if unique else None


def _sidecar_text(edit: Mapping[str, Any] | None) -> str:
    if not edit:
        return ""
    # ``text_from`` is a locator/audit transcription and can itself preserve
    # corrupt or truncated source text.  Only an explicit published replacement
    # is eligible to replace geometry extracted from the final PDF.
    return clean_text(edit.get("text_to"))


def _looks_like_table_header(value: str) -> bool:
    token = normalized(value)
    if len(token) > 70:
        return False
    return any(
        token == alias or token.startswith(f"{alias} ") or token.endswith(f" {alias}")
        for aliases in (
            OUTCOME_HEADER_ALIASES,
            PLO_HEADER_ALIASES,
            TEACHING_ALIASES,
            ASSESSMENT_ALIASES,
        )
        for raw_alias in aliases
        if (alias := normalized(raw_alias))
    )


def _is_course_topics_heading_fragment(value: str) -> bool:
    """Reject the next-section heading, including a left-clipped first word."""
    token = normalized(value)
    heading = normalized("موضوعات المقرر")
    if token == heading:
        return True
    words = token.split()
    if len(words) != 2 or words[-1] != normalized("المقرر"):
        return False
    published_headword = normalized("موضوعات")
    clipped_headword = words[-2]
    return len(clipped_headword) >= 4 and published_headword.endswith(clipped_headword)


def _leading_outcome_continuation(
    bundles: Sequence[Mapping[str, Any]],
) -> str:
    """Recover text above the first code when a CLO cell crosses a page break."""
    for bundle in bundles:
        rows = bundle["rows"]
        flat = " ".join(cell for row in rows for cell in row if cell)
        if not contains_alias(flat, OUTCOME_HEADER_ALIASES):
            continue
        outcome_column = _header_column(rows, OUTCOME_HEADER_ALIASES)
        groups = _table_code_groups(
            rows,
            boxes=bundle.get("boxes"),
            allow_legacy=bool(bundle.get("word_baseline_fallback")),
        )
        if outcome_column is None or not groups:
            continue
        limit = min(min(group["rows"]) for group in groups)
        header_rows = [
            row_index
            for row_index, row in enumerate(rows[:limit])
            if any(contains_alias(cell, OUTCOME_HEADER_ALIASES) for cell in row)
        ]
        start = max(header_rows, default=-1) + 1
        parts: list[str] = []
        seen: set[str] = set()
        for row_index in range(start, limit):
            row = rows[row_index]
            for column in range(
                max(0, outcome_column - 1),
                min(len(row), outcome_column + 4),
            ):
                value, _ = logical_cell(row[column], geometric=bundle["geometric"])
                identity = normalized(value)
                if (
                    len(identity) < 4
                    or normalize_clo(
                        value,
                        allow_legacy=bool(bundle.get("word_baseline_fallback")),
                    )
                    or _looks_like_table_header(value)
                    or _is_course_topics_heading_fragment(value)
                    or identity in seen
                ):
                    continue
                seen.add(identity)
                parts.append(value)
        continuation = clean_text(" ".join(parts)).strip(" .،؛:-")
        if (
            continuation
            and not _starts_like_outcome(continuation)
            and not _is_course_topics_heading_fragment(continuation)
        ):
            return continuation
    return ""


def _bundle_column_boundaries(bundle: Mapping[str, Any]) -> list[float]:
    """Return stable x-boundaries for one physical table interpretation."""
    raw = sorted(
        value
        for row in bundle.get("boxes", [])
        for bbox in row
        if bbox is not None
        for value in (float(bbox[0]), float(bbox[2]))
    )
    clusters: list[list[float]] = []
    for value in raw:
        if clusters and value - clusters[-1][-1] <= 0.75:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [statistics.median(cluster) for cluster in clusters]


def _matching_grid_boundaries(left: Sequence[float], right: Sequence[float]) -> int:
    used: set[int] = set()
    matches = 0
    for value in left:
        candidate = next(
            (
                index
                for index, other in enumerate(right)
                if index not in used and abs(value - other) <= 2.0
            ),
            None,
        )
        if candidate is not None:
            used.add(candidate)
            matches += 1
    return matches


def _split_outcome_table_links(
    pages: Sequence[Sequence[Mapping[str, Any]]],
    pdf_pages: Sequence[Any],
) -> dict[tuple[int, int], tuple[int, int]]:
    """Link two page-local bundles only when their physical table grids agree.

    Older Word exports repeat the table header after a page break, but their
    ToUnicode map can make every header alias unreadable.  The row grid remains
    exact, so use it as the continuation proof.  No CLO code is inferred here:
    a readable code must still exist on one of the linked physical rows.
    """
    links: dict[tuple[int, int], tuple[int, int]] = {}
    for page_index in range(len(pages) - 1):
        if (
            page_index >= len(pdf_pages)
            or page_index + 1 >= len(pdf_pages)
            or pdf_pages[page_index] is None
            or pdf_pages[page_index + 1] is None
        ):
            continue
        height = float(pdf_pages[page_index].height)
        next_height = float(pdf_pages[page_index + 1].height)
        candidates: list[tuple[tuple[int, int, float], int, int]] = []
        for left_index, left in enumerate(pages[page_index]):
            left_bbox = _bundle_bbox(left)
            if left_bbox is None or left_bbox[3] < height * 0.90:
                continue
            left_groups = _table_code_groups(
                left.get("rows", []),
                boxes=left.get("boxes"),
                allow_legacy=bool(left.get("word_baseline_fallback")),
            )
            left_nonzero = [
                group
                for group in left_groups
                if not str(group.get("code") or "").endswith(".0")
            ]
            if len(left_nonzero) < 2:
                continue
            left_boundaries = _bundle_column_boundaries(left)
            for right_index, right in enumerate(pages[page_index + 1]):
                right_bbox = _bundle_bbox(right)
                if right_bbox is None or right_bbox[1] > next_height * 0.18:
                    continue
                if (
                    abs(left_bbox[0] - right_bbox[0]) > 2.0
                    or abs(left_bbox[2] - right_bbox[2]) > 2.0
                ):
                    continue
                right_groups = _table_code_groups(
                    right.get("rows", []),
                    boxes=right.get("boxes"),
                    allow_legacy=bool(right.get("word_baseline_fallback")),
                )
                right_nonzero = [
                    group
                    for group in right_groups
                    if not str(group.get("code") or "").endswith(".0")
                ]
                if len(right_nonzero) > 1:
                    continue
                right_boundaries = _bundle_column_boundaries(right)
                shared = _matching_grid_boundaries(left_boundaries, right_boundaries)
                denominator = max(1, min(len(left_boundaries), len(right_boundaries)))
                ratio = shared / denominator
                if shared < 6 or ratio < 0.60:
                    continue
                right_values = [
                    clean_text(value)
                    for row in right.get("rows", [])
                    for value in row
                    if clean_text(value)
                ]
                right_has_outcome_cue = any(
                    _starts_like_outcome_boundary(value) for value in right_values
                )
                right_has_continuation_text = any(
                    len(re.findall(r"[\u0600-\u06ffA-Za-z]", value)) >= 4
                    and not normalize_clo(value)
                    for value in right_values
                )
                if not right_has_outcome_cue and not right_has_continuation_text:
                    continue
                candidates.append(
                    ((shared, int(ratio * 1000), -right_index), left_index, right_index)
                )
        if not candidates:
            continue
        candidates.sort(reverse=True)
        best_score, left_index, right_index = candidates[0]
        if len(candidates) > 1 and candidates[1][0] == best_score:
            continue
        links[(page_index, left_index)] = (page_index + 1, right_index)
    return links


def _inferred_outcome_band(
    bundle: Mapping[str, Any],
) -> tuple[float, float, float, float] | None:
    candidates: list[tuple[float, float, float, float]] = []
    for row_index, row in enumerate(bundle.get("rows", [])):
        boxes = bundle.get("boxes", [])
        box_row = boxes[row_index] if row_index < len(boxes) else []
        for column, raw in enumerate(row):
            value, _ = logical_cell(raw, geometric=bool(bundle.get("geometric")))
            bbox = box_row[column] if column < len(box_row) else None
            if bbox is not None and _starts_like_outcome_boundary(value):
                candidates.append(tuple(map(float, bbox)))
    if not candidates:
        return None
    grouped: dict[tuple[int, int], list[tuple[float, float, float, float]]] = (
        defaultdict(list)
    )
    for bbox in candidates:
        grouped[(round(bbox[0]), round(bbox[2]))].append(bbox)
    best = max(
        grouped.values(), key=lambda items: (len(items), items[0][2] - items[0][0])
    )
    x0 = statistics.median(item[0] for item in best)
    x1 = statistics.median(item[2] for item in best)
    return x0, 0.0, x1, 0.0


def _linked_outcome_continuation(
    bundle: Mapping[str, Any],
    outcome_band: tuple[float, float, float, float] | None,
    *,
    require_outcome_start: bool,
) -> str:
    """Read only the outcome band at the head of a grid-matched next page."""
    rows = bundle.get("rows", [])
    boxes = bundle.get("boxes", [])
    groups = _table_code_groups(
        rows,
        boxes=boxes,
        allow_legacy=bool(bundle.get("word_baseline_fallback")),
    )
    limit = min((min(group["rows"]) for group in groups), default=len(rows))
    active_band = outcome_band or _inferred_outcome_band(bundle)
    header_bottom = 0.0
    if boxes:
        header_bottom = max(
            (float(bbox[3]) for bbox in boxes[0] if bbox is not None),
            default=0.0,
        )
    parts: list[str] = []
    seen: set[str] = set()
    started = not require_outcome_start
    for row_index, row in enumerate(rows[:limit]):
        box_row = boxes[row_index] if row_index < len(boxes) else []
        for column, raw in enumerate(row):
            value, _ = logical_cell(raw, geometric=bool(bundle.get("geometric")))
            bbox = box_row[column] if column < len(box_row) else None
            if not value or bbox is None:
                continue
            if float(bbox[1]) < header_bottom - 0.75:
                continue
            if (
                active_band is not None
                and _horizontal_overlap(bbox, active_band) < 0.55
            ):
                continue
            if require_outcome_start and not started:
                if not _starts_like_outcome_boundary(value):
                    continue
                started = True
                if active_band is None:
                    active_band = tuple(map(float, bbox))
            identity = normalized(value)
            if (
                not started
                or len(identity) < 4
                or normalize_clo(
                    value,
                    allow_legacy=bool(bundle.get("word_baseline_fallback")),
                )
                or _looks_like_table_header(value)
                or _is_section_heading(value)
                or _is_course_topics_heading_fragment(value)
                or (unique_plos(value) and len(identity) < 20)
                or identity in seen
            ):
                continue
            seen.add(identity)
            parts.append(value)
    continuation = clean_text(" ".join(parts)).strip(" ،؛:-")
    if not continuation or _is_course_topics_heading_fragment(continuation):
        return ""
    if require_outcome_start:
        return continuation if _starts_like_outcome(continuation) else ""
    return "" if _starts_like_outcome(continuation) else continuation


def _build_plo_mappings(
    scopes: Sequence[Mapping[str, str]],
    *,
    source_page: int,
    scalar_plos: Sequence[str],
    parsed_program_map: Mapping[str, Sequence[str] | None],
    auxiliary: Mapping[str, Any] | None,
    edit: Mapping[str, Any] | None,
    scalar_confidence: str = "high",
    parsed_program_conflicts: Iterable[str] = (),
    parsed_program_plain: Iterable[str] = (),
    parsed_program_inferred: Iterable[str] = (),
    scalar_plain: bool = False,
    scalar_inferred: bool = False,
    scalar_ocr_prefix: bool = False,
    scalar_row_aligned: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    kind = auxiliary.get("kind") if auxiliary else None
    per_program = (
        edit.get("per_program") if edit and kind in {"unified", "shared"} else None
    )
    correction = (
        unique_plos(edit.get("plo_to")) if edit and kind == "correction" else []
    )
    selectors = auxiliary.get("selectors", []) if auxiliary else []
    unique_scalar = list(dict.fromkeys(scalar_plos))
    conflicted_programs = set(parsed_program_conflicts)
    plain_programs = set(parsed_program_plain)
    inferred_programs = set(parsed_program_inferred)
    multiple_programs = len({scope["program"] for scope in scopes}) > 1
    for scope in scopes:
        program = scope["program"]
        values: list[str] | None = None
        status = "missing"
        confidence = "low"
        evidence = "no unambiguous PLO value was recovered from the published cell"
        selector_scope_covered = not (
            kind in {"correction", "shared", "shared_blank"} and selectors
        ) or any(
            all(
                clean_text(scope.get(key)) == clean_text(expected)
                for key, expected in selector.items()
            )
            for selector in selectors
        )
        if kind in {"correction", "shared", "shared_blank"} and not selector_scope_covered:
            status = "not_present_for_scope"
            confidence = "medium"
            evidence = f"the {kind} manifest selectors do not cover this scope"
        elif kind == "shared_blank" and edit:
            values = []
            status = "explicitly_unmapped"
            confidence = "high"
            evidence = (
                "hash-matched shared-course blanking manifest confirms that the "
                "published PLO cell is intentionally empty"
            )
        elif isinstance(per_program, Mapping):
            if kind == "unified" and scope["plan_type"] != "جديدة":
                status = "not_present_for_scope"
                confidence = "medium"
                evidence = "the unified published cell is explicitly limited to new-plan programs"
            elif program in per_program:
                raw = clean_text(per_program[program])
                if raw in {"—", "–", "-"}:
                    values = []
                    status = "explicitly_unmapped"
                    confidence = "medium"
                    evidence = f"hash-matched {kind} mapping manifest contains an explicit dash"
                else:
                    parsed = unique_plos(raw)
                    values = parsed or None
                    status = "mapped" if parsed else "missing"
                    confidence = "medium" if parsed else "low"
                    evidence = (
                        f"hash-matched {kind} mapping manifest and published overlay"
                        if parsed
                        else "the unified manifest value was empty or invalid"
                    )
            else:
                status = "not_present_for_scope"
                confidence = "medium"
                evidence = f"program is absent from the {kind} published mapping cell"
        elif correction:
            values = correction
            status = "mapped"
            confidence = "medium"
            evidence = (
                "hash-matched correction manifest selector and published corrected cell"
            )
        elif program in conflicted_programs:
            status = "conflict"
            confidence = "low"
            evidence = "multiple conflicting program-labelled PLO values were read from the published block"
        elif program in parsed_program_map:
            parsed = parsed_program_map[program]
            values = list(parsed) if parsed else []
            status = "mapped" if values else "explicitly_unmapped"
            confidence = (
                "medium"
                if program in plain_programs or program in inferred_programs
                else "high"
            )
            if program in plain_programs:
                evidence = "program-labelled value recovered from the same physical table's plain cell fallback"
            elif program in inferred_programs:
                evidence = "program-labelled value read from a geometrically inferred PLO column"
            else:
                evidence = "program-labelled value read geometrically from the PDF cell"
        elif unique_scalar:
            if multiple_programs:
                status = "ambiguous_for_scope"
                evidence = "a scalar document PLO cannot be assigned across multiple programs without labels"
            else:
                values = unique_scalar
                status = "mapped"
                confidence = scalar_confidence
                if scalar_plain:
                    evidence = "single exact PLO code recovered from the same physical table's plain cell fallback"
                elif scalar_inferred:
                    evidence = "single exact PLO code read from the unique geometrically inferred PLO column"
                elif scalar_ocr_prefix:
                    evidence = (
                        "PLO prefix recovered by multi-scale targeted OCR of its "
                        "glyph after the PDF ToUnicode map returned a missing or invalid prefix; "
                        "digits came from the vector text layer"
                    )
                elif scalar_row_aligned:
                    evidence = (
                        "exact PLO code aligned to the CLO by published cell/row "
                        "geometry after pdfplumber split or merged the visual row"
                    )
                elif scalar_confidence == "medium":
                    evidence = "single exact PLO code recovered by targeted OCR from a numerically untrusted text layer"
                else:
                    evidence = (
                        "single exact PLO code read geometrically from the PDF cell"
                    )
        output.append(
            {
                "scope": dict(scope),
                "plo_codes": values,
                "status": status,
                "confidence": confidence,
                "source_page": source_page,
                "evidence": evidence,
            }
        )
    return output


def _corrupt_competency_summary_sentences(
    pages: Sequence[Sequence[Mapping[str, Any]]],
) -> dict[int, str]:
    """Find a summary row whose duplicated competency codes lost kaf in CMap."""
    recovered: dict[int, str] = {}
    for bundles in pages:
        for bundle in bundles:
            rows = bundle.get("rows") or []
            for row_index, row in enumerate(rows):
                nearby = " ".join(
                    cell
                    for previous in rows[max(0, row_index - 2) : row_index + 1]
                    for cell in previous
                    if cell
                )
                if "الكفاءات" not in normalized(nearby):
                    continue
                marker_lists: list[list[int]] = []
                sentence_lists: list[list[str]] = []
                for raw in row:
                    value, _ = logical_cell(
                        raw, geometric=bool(bundle.get("geometric"))
                    )
                    markers = [
                        int(number)
                        for number in re.findall(
                            r"(?<![\u0600-\u06ffA-Za-z0-9])ا\s*[-.]?\s*([1-9][0-9]?)(?!\d)",
                            value,
                        )
                    ]
                    if len(markers) >= 2:
                        marker_lists.append(markers)
                    sentences = [
                        clean_text(part).strip(" .،؛:-")
                        for part in re.split(
                            r"(?=(?<![\u0600-\u06ff])(?:أن|ان)\s)", value
                        )
                        if _starts_like_outcome(clean_text(part))
                    ]
                    if len(sentences) >= 2:
                        sentence_lists.append(sentences)
                if len(marker_lists) < 2 or not sentence_lists:
                    continue
                markers = marker_lists[0]
                if any(candidate != markers for candidate in marker_lists[1:]):
                    continue
                sentences = max(sentence_lists, key=len)
                if len(markers) != len(sentences) or len(set(markers)) != len(markers):
                    continue
                for number, sentence in zip(markers, sentences):
                    existing = recovered.get(number)
                    if existing is not None and normalized(existing) != normalized(
                        sentence
                    ):
                        recovered.pop(number, None)
                        continue
                    recovered[number] = sentence
    return recovered


def _same_document_competency_match(
    code: str,
    text: str | None,
    summary_sentences: Mapping[int, str],
) -> bool:
    match = re.fullmatch(r"ك([1-9][0-9]?)", code)
    if not match or not text:
        return False
    number = int(match.group(1))
    target = summary_sentences.get(number)
    if not target:
        return False
    left = normalized(text)
    right = normalized(target)
    left_words = set(left.split())
    right_words = set(right.split())
    overlap = len(left_words & right_words) / max(
        1, min(len(left_words), len(right_words))
    )
    similarity = SequenceMatcher(None, left, right).ratio()
    if overlap < 0.65 or similarity < 0.70:
        return False
    competing = [
        SequenceMatcher(None, left, normalized(candidate)).ratio()
        for candidate_number, candidate in summary_sentences.items()
        if candidate_number != number
    ]
    return not competing or similarity - max(competing) >= 0.15


def _geometric_source_clo_row_count(
    pages: Sequence[Sequence[Mapping[str, Any]]],
    split_table_links: Mapping[tuple[int, int], tuple[int, int]],
) -> int | None:
    """Count printed CLO-code cells before any outcome record is constructed.

    This audit deliberately runs on the page-local table geometry, not on the
    final ``clos`` list.  It therefore continues to see a code-only row that a
    later text, assessment, or mapping rule might reject.  Overlapping
    pdfplumber interpretations are collapsed only when they point to the same
    physical code cell.  Distinct repeated tables remain counted; that can
    conservatively make a variant ``partial``, but can never certify a missing
    source row as complete.
    """

    linked_bundles = set(split_table_links) | set(split_table_links.values())
    occurrences: list[
        tuple[
            int,
            str | None,
            tuple[float, float, float, float] | None,
            int,
            int,
        ]
    ] = []
    for page_index, bundles in enumerate(pages):
        page_number = page_index + 1
        for bundle_index, bundle in enumerate(bundles):
            rows = bundle.get("rows", [])
            groups = _table_code_groups(
                rows,
                boxes=bundle.get("boxes"),
                allow_legacy=bool(bundle.get("word_baseline_fallback")),
            )
            nonzero = [
                group for group in groups if not str(group["code"]).endswith(".0")
            ]
            if not nonzero:
                continue
            flat = " ".join(cell for row in rows for cell in row if cell)
            if not (
                len(nonzero) >= 2
                or contains_alias(flat, OUTCOME_HEADER_ALIASES)
                or contains_alias(flat, PLO_HEADER_ALIASES)
                or (page_index, bundle_index) in linked_bundles
            ):
                continue
            for group in nonzero:
                occurrences.append(
                    (
                        page_number,
                        str(group.get("code") or "") or None,
                        group.get("code_bbox"),
                        bundle_index,
                        min(group.get("rows") or [0]),
                    )
                )

            outcome_column = _header_column(rows, OUTCOME_HEADER_ALIASES)
            for block in _ellipsis_clo_blocks(
                rows,
                groups,
                outcome_column,
                word_baseline=bool(bundle.get("word_baseline_fallback")),
            ):
                marker_row = int(block["ellipsis_row"])
                code_column = int(block["code_column"])
                boxes = bundle.get("boxes", [])
                marker_bbox = (
                    boxes[marker_row][code_column]
                    if marker_row < len(boxes) and code_column < len(boxes[marker_row])
                    else None
                )
                occurrences.append(
                    (
                        page_number,
                        None,
                        marker_bbox,
                        bundle_index,
                        marker_row,
                    )
                )

    unique: list[
        tuple[
            int,
            str | None,
            tuple[float, float, float, float] | None,
            int,
            int,
        ]
    ] = []
    for occurrence in occurrences:
        page_number, code, bbox, bundle_index, row_index = occurrence
        duplicate = False
        for existing in unique:
            (
                existing_page,
                existing_code,
                existing_bbox,
                existing_bundle,
                existing_row,
            ) = existing
            if page_number != existing_page or code != existing_code:
                continue
            if bbox is not None and _same_physical_cell(bbox, existing_bbox):
                duplicate = True
                break
            if (
                bbox is None
                and existing_bbox is None
                and bundle_index == existing_bundle
                and row_index == existing_row
            ):
                duplicate = True
                break
        if not duplicate:
            unique.append(occurrence)
    return len(unique) or None


def _verified_source_clo_row_count(
    physical_count: int | None, captured_count: int
) -> int | None:
    """Return only an independently usable source-row count.

    A count derived from final records is intentionally not accepted here.  A
    physical count below the number already retained is itself inconsistent,
    so it cannot certify completeness and is published as unverified instead.
    """
    if physical_count is None or physical_count < captured_count:
        return None
    return physical_count


def parse_outcome_tables(
    pages: Sequence[Sequence[Mapping[str, Any]]],
    scopes: Sequence[Mapping[str, str]],
    auxiliary: Mapping[str, Any] | None,
    document: Any,
    pdf_pages: Sequence[Any],
    *,
    numeric_text_untrusted: bool = False,
    capture_audit: dict[str, Any] | None = None,
    allow_split_table_stitch: bool = False,
    allow_embedded_font_recovery: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unlabeled_candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    aux_edits = auxiliary.get("edits", {}) if auxiliary else {}
    programs = [scope["program"] for scope in scopes]
    competency_summary = _corrupt_competency_summary_sentences(pages)
    # Detect split tables for the independent source-row audit in every file.
    # Only reviewed, hash-allowlisted sources may use the links to construct or
    # extend output records; an unreviewed split therefore becomes ``partial``
    # rather than silently certifying a truncated record list as complete.
    audit_split_table_links = _split_outcome_table_links(pages, pdf_pages)
    split_table_links = audit_split_table_links if allow_split_table_stitch else {}
    geometric_source_clo_row_count = _geometric_source_clo_row_count(
        pages, audit_split_table_links
    )
    continuation_targets = {
        target: source for source, target in split_table_links.items()
    }
    for page_index, bundles in enumerate(pages):
        page_number = page_index + 1
        page_row_aligned_plos = (
            _geometric_page_plo_values_by_occurrence(
                pdf_pages[page_index],
                bundles,
            )
            if not numeric_text_untrusted
            else []
        )
        for bundle_index, bundle in enumerate(bundles):
            rows = bundle["rows"]
            flat = " ".join(cell for row in rows for cell in row if cell)
            allow_legacy = bool(bundle.get("word_baseline_fallback"))
            groups = _table_code_groups(
                rows,
                boxes=bundle.get("boxes"),
                allow_legacy=allow_legacy,
            )
            multi_cell_counts: dict[frozenset[str], int] = defaultdict(int)
            explicit_multi_cells: set[frozenset[str]] = set()
            for row in rows:
                for raw in row:
                    value, _ = logical_cell(raw, geometric=bundle["geometric"])
                    cell_plos = list(
                        dict.fromkeys(
                            unique_plos(value)
                            + unique_plos(reverse_visual_arabic(value))
                        )
                    )
                    if len(cell_plos) < 2:
                        continue
                    identity = frozenset(cell_plos)
                    multi_cell_counts[identity] += 1
                    if re.search(r"[,،;/؛]", value):
                        explicit_multi_cells.add(identity)
            trusted_multi_cells = explicit_multi_cells | {
                identity for identity, count in multi_cell_counts.items() if count >= 2
            }
            nonzero = [
                group for group in groups if not str(group["code"]).endswith(".0")
            ]
            if not nonzero:
                continue
            grid_matched_continuation = (
                page_index,
                bundle_index,
            ) in continuation_targets
            if not (
                len(nonzero) >= 2
                or contains_alias(flat, OUTCOME_HEADER_ALIASES)
                or contains_alias(flat, PLO_HEADER_ALIASES)
                or grid_matched_continuation
            ):
                continue
            assessment_column = _header_column(rows, ASSESSMENT_ALIASES)
            plo_column = _header_column(rows, PLO_HEADER_ALIASES)
            outcome_column = _header_column(rows, OUTCOME_HEADER_ALIASES)
            plo_band = _header_band(bundle, PLO_HEADER_ALIASES)
            outcome_band = _header_band(bundle, OUTCOME_HEADER_ALIASES)
            if outcome_band is None:
                outcome_band = _inferred_outcome_band(bundle)
            starts = _group_starts(groups, rows, outcome_column)
            inferred_plo = None
            if plo_column is None:
                inferred_plo = _infer_plo_column(rows, groups, starts)
                if inferred_plo:
                    plo_column, aligned_count, outcome_count = inferred_plo
                    warnings.append(
                        make_warning(
                            "plo_column_inferred_from_exact_codes",
                            "the PLO header text was unreadable; selected the unique "
                            f"column containing exact PLO codes in {aligned_count}/"
                            f"{outcome_count} CLO blocks",
                            source_page=page_number,
                        )
                    )
            row_aligned_plos = (
                _geometric_plo_values_by_group(
                    pdf_pages[page_index],
                    bundle,
                    groups,
                    plo_column=plo_column,
                    plo_band=plo_band,
                )
                if not numeric_text_untrusted
                else {}
            )
            ellipsis_blocks = _ellipsis_clo_blocks(
                rows,
                groups,
                outcome_column,
                word_baseline=bool(bundle.get("word_baseline_fallback")),
            )
            if bundle.get("image_curve_grid_fallback"):
                code_columns = [
                    int(column)
                    for group in groups
                    for column in group.get("columns", [])
                ]
                unreadable_code_column = (
                    round(statistics.median(code_columns)) if code_columns else None
                )
                if unreadable_code_column is not None:
                    for row_index in bundle.get("unreadable_code_rows", []):
                        ellipsis_blocks.append(
                            {
                                "start": int(row_index),
                                "end": int(row_index) + 1,
                                "ellipsis_row": int(row_index),
                                "code_column": unreadable_code_column,
                                "source_code": None,
                                "code_unreadable": True,
                            }
                        )
                ellipsis_blocks.sort(key=lambda item: int(item["start"]))
            for group_index, group in enumerate(groups):
                code = str(group["code"])
                if code.endswith(".0"):
                    continue
                start = starts[group_index]
                end = (
                    starts[group_index + 1]
                    if group_index + 1 < len(starts)
                    else len(rows)
                )
                ellipsis_boundaries = [
                    block["start"]
                    for block in ellipsis_blocks
                    if start < block["start"] < end
                ]
                if ellipsis_boundaries:
                    end = min(end, min(ellipsis_boundaries))
                if end <= start:
                    continue
                code_column = round(statistics.median(group["columns"]))
                selected: list[
                    tuple[
                        str,
                        tuple[float, float, float, float] | None,
                        str,
                    ]
                ] = []
                mapping_values: list[tuple[str, str]] = []
                for row_index in range(start, end):
                    row = rows[row_index]
                    box_row = bundle["boxes"][row_index]
                    for column, raw in enumerate(row):
                        value, cid_count = logical_cell(
                            raw, geometric=bundle["geometric"]
                        )
                        cell_source = _bundle_cell_provenance(bundle, row_index, column)
                        if cid_count:
                            warnings.append(
                                make_warning(
                                    "cid_removed",
                                    f"removed {cid_count} unmapped CID token(s) before parsing",
                                    source_page=page_number,
                                    clo_code=code,
                                )
                            )
                        bbox = box_row[column] if column < len(box_row) else None
                        in_plo_band = bool(
                            value
                            and (
                                (
                                    bbox
                                    and plo_band
                                    and _horizontal_overlap(bbox, plo_band) >= 0.55
                                )
                                or (
                                    (not bbox or not plo_band)
                                    and plo_column is not None
                                    and (
                                        column == plo_column
                                        if inferred_plo
                                        else abs(column - plo_column) <= 1
                                    )
                                )
                            )
                        )
                        if in_plo_band:
                            mapping_source = cell_source
                            if inferred_plo and not _is_plain_provenance(cell_source):
                                mapping_source = "geometric_inferred_plo_column"
                            recovered_plo = (
                                _recover_exact_plo_with_ocr_prefix(
                                    pdf_pages[page_index],
                                    document,
                                    page_index,
                                    bbox,
                                    value,
                                )
                                if not numeric_text_untrusted
                                else None
                            )
                            if recovered_plo:
                                value = recovered_plo
                                mapping_source = "targeted_ocr_invalid_plo_prefix"
                                warnings.append(
                                    make_warning(
                                        "plo_prefix_recovered_by_ocr",
                                        "recovered a missing or invalid ToUnicode PLO prefix by "
                                        "multi-scale OCR of the isolated prefix glyph; "
                                        "the digits remain from the vector text layer",
                                        source_page=page_number,
                                        clo_code=code,
                                    )
                                )
                            mapping_values.append((value, mapping_source))
                        if not value or normalize_clo(value, allow_legacy=allow_legacy):
                            continue
                        if _is_section_heading(value):
                            continue
                        if _looks_like_table_header(value):
                            continue
                        codes_here = unique_plos(value) + unique_plos(
                            reverse_visual_arabic(value)
                        )
                        if codes_here and len(normalized(value)) < 20:
                            continue
                        in_outcome_column = False
                        if outcome_column is not None:
                            in_outcome_column = (
                                outcome_column - 1 <= column <= outcome_column + 3
                                and column <= code_column
                            )
                        elif plo_column is not None:
                            in_outcome_column = plo_column < column < code_column
                        else:
                            in_outcome_column = (
                                max(0, code_column - 3) <= column < code_column
                            )
                        if in_outcome_column and len(normalized(value)) >= 4:
                            bbox = box_row[column] if column < len(box_row) else None
                            selected.append(
                                (
                                    value,
                                    bbox,
                                    cell_source,
                                )
                            )

                aligned_values = row_aligned_plos.get(group_index, [])
                page_aligned_values, aligned_row_top = _page_plo_values_for_group(
                    page_row_aligned_plos,
                    group,
                )
                aligned_values = _reconcile_geometric_plo_values(
                    aligned_values,
                    page_aligned_values,
                )
                if aligned_values and not any(
                    _program_map(value, programs) for value, _ in mapping_values
                ):
                    current_codes: list[str] = []
                    for value, _ in mapping_values:
                        for candidate in (value, reverse_visual_arabic(value)):
                            for mapped_code in unique_plos(candidate):
                                if mapped_code not in current_codes:
                                    current_codes.append(mapped_code)
                    aligned_codes = [value for value, _ in aligned_values]
                    if current_codes != aligned_codes:
                        mapping_values = list(aligned_values)
                        warnings.append(
                            make_warning(
                                "plo_mapping_recovered_from_row_geometry",
                                "recovered exact PLO code(s) only after aligning the "
                                "published PLO cell/atom with physical CLO row bounds",
                                source_page=page_number,
                                clo_code=code,
                            )
                        )

                outcome_parts: list[str] = []
                seen_parts: set[str] = set()
                for value, _, _ in selected:
                    identity = normalized(value)
                    if identity and identity not in seen_parts:
                        seen_parts.add(identity)
                        outcome_parts.append(value)
                raw_outcome = clean_text(" ".join(outcome_parts))
                outcome = _clean_outcome(raw_outcome, code)
                extension_filtered = any(
                    _is_extension_filtered_provenance(source)
                    for _, _, source in selected
                )
                region = _physical_outcome_region(
                    outcome_band,
                    group.get("code_bbox"),
                    (bbox for _, bbox, _ in selected),
                )
                if (
                    region
                    and bundle.get("geometric")
                    and not bundle.get("word_baseline_fallback")
                    and not bundle.get("image_curve_grid_fallback")
                ):
                    physical_diagnostics: dict[str, int] = {}
                    physical_raw = logical_region_text(
                        pdf_pages[page_index],
                        region,
                        diagnostics=physical_diagnostics,
                    )
                    physical_outcome = _clean_outcome(physical_raw, code)
                    current_issue = _candidate_issue(outcome)
                    physical_issue = _candidate_issue(physical_outcome)
                    if physical_outcome and (
                        physical_issue is None
                        or (
                            current_issue is not None
                            and len(normalized(physical_outcome))
                            >= len(normalized(outcome))
                        )
                    ):
                        raw_outcome = physical_raw
                        outcome = physical_outcome
                    extension_filtered = extension_filtered or bool(
                        physical_diagnostics.get("extension_glyphs_suppressed", 0)
                    )
                source_terminated = bool(re.search(r"[.؟!]\s*$", raw_outcome))
                continuation_target = split_table_links.get((page_index, bundle_index))
                if (
                    continuation_target is None
                    and group_index == len(groups) - 1
                    and page_index + 1 < len(pages)
                    and outcome
                    and not source_terminated
                ):
                    continuation = _leading_outcome_continuation(pages[page_index + 1])
                    if continuation:
                        outcome = clean_text(f"{outcome} {continuation}")
                pending_continuation = ""
                if (
                    group_index == len(groups) - 1
                    and continuation_target is not None
                    and not source_terminated
                ):
                    target_page, target_bundle = continuation_target
                    pending_continuation = _linked_outcome_continuation(
                        pages[target_page][target_bundle],
                        outcome_band,
                        require_outcome_start=not bool(outcome),
                    )
                used_plain_fallback = any(
                    source.startswith("plain_") or source == "plain_table"
                    for _, _, source in selected
                )
                if used_plain_fallback:
                    outcome_source = "plain_table"
                    confidence = "low"
                else:
                    outcome_source = (
                        "geometric_pdf" if bundle["geometric"] else "plain_table"
                    )
                    confidence = "medium" if bundle["geometric"] else "low"
                edit = aux_edits.get(code)
                if edit and int(edit.get("page_1_based") or 0) != page_number:
                    edit = None
                manifest_text = _sidecar_text(edit)
                if manifest_text:
                    outcome = _clean_outcome(manifest_text, code)
                    outcome_source = "hash_matched_manifest"
                    confidence = "high"
                issue = _candidate_issue(outcome)
                if (
                    not issue
                    and extension_filtered
                    and outcome_source != "hash_matched_manifest"
                ):
                    # Geometry proves that the narrow justification glyphs are
                    # not letters, but it cannot prove the remaining ToUnicode
                    # map.  Pin the text only when OCR independently agrees.
                    issue = "extension_glyphs_suppressed"
                if (
                    not issue
                    and numeric_text_untrusted
                    and outcome_source != "hash_matched_manifest"
                    and _has_severe_orphan_combining_run(raw_outcome)
                ):
                    issue = "severe_orphan_combining_run"
                if issue:
                    if region is None:
                        region = _bbox_union(bbox for _, bbox, _ in selected)
                    embedded_font_text = ""
                    if (
                        allow_embedded_font_recovery
                        and not bundle.get("image_curve_grid_fallback")
                        and region
                    ):
                        embedded_font_text = _clean_outcome(
                            recover_embedded_font_text(
                                document,
                                page_index,
                                region,
                            ),
                            code,
                        )
                    embedded_font_issue = _candidate_issue(embedded_font_text)
                    if (
                        not embedded_font_issue
                        and len(normalized(embedded_font_text)) >= 8
                    ):
                        outcome = embedded_font_text
                        outcome_source = "embedded_font_cmap"
                        confidence = "high"
                    else:
                        if bundle.get("image_curve_grid_fallback"):
                            line_boxes = [
                                bbox
                                for row_index in range(start, end)
                                for bbox in bundle.get("ocr_line_boxes", {}).get(
                                    f"{row_index}:{outcome_column}", []
                                )
                            ]
                            ocr = _clean_outcome(
                                ocr_strict_line_consensus(
                                    document, page_index, line_boxes
                                ),
                                code,
                            )
                        else:
                            ocr = ocr_tight_outcome_line_consensus(
                                pdf_pages[page_index],
                                document,
                                page_index,
                                region,
                                code,
                            )
                            if not ocr:
                                whole_cell_ocr = ocr_tight_outcome_consensus(
                                    document,
                                    page_index,
                                    region,
                                    code,
                                )
                                source_terminal = _source_terminal(raw_outcome)
                                if (
                                    whole_cell_ocr
                                    and source_terminal
                                    and _source_terminal(whole_cell_ocr)
                                    == source_terminal
                                ):
                                    ocr = whole_cell_ocr
                        ocr_issue = (
                            _ocr_candidate_issue(ocr)
                            if bundle.get("image_curve_grid_fallback")
                            else _tight_ocr_candidate_issue(ocr)
                        )
                        if not ocr_issue and len(normalized(ocr)) >= 8:
                            outcome = ocr
                            outcome_source = "targeted_ocr"
                            confidence = "medium"
                        else:
                            outcome = ""
                            confidence = "low"
                            warnings.append(
                                make_warning(
                                    "clo_text_unreadable",
                                    "CLO text was left null after embedded-font and targeted OCR "
                                    f"checks (vector={issue}; font={embedded_font_issue or 'too_short'}; "
                                    f"ocr={ocr_issue or 'too_short'})",
                                    source_page=page_number,
                                    clo_code=code,
                                )
                            )
                outcome = _remove_orphan_combining_marks(outcome)
                if pending_continuation:
                    outcome = clean_text(
                        f"{outcome} {pending_continuation}"
                        if outcome
                        else pending_continuation
                    )
                    if outcome_source == "embedded_font_cmap":
                        outcome_source = (
                            "embedded_font_cmap_with_geometric_continuation"
                        )
                        confidence = "medium"
                    elif outcome_source != "hash_matched_manifest":
                        outcome_source = "geometric_pdf"
                        confidence = "medium"

                if outcome:
                    source_status = "present"
                elif bool(edit and edit.get("source_blank")) or (
                    _source_outcome_cell_is_blank(
                        pdf_pages[page_index], region, raw_outcome
                    )
                ):
                    source_status = "source_blank"
                    warnings.append(
                        make_warning(
                            "clo_text_blank_in_source",
                            "the published CLO row was captured, but its outcome-text cell is blank",
                            source_page=page_number,
                            clo_code=code,
                        )
                    )
                else:
                    source_status = "unreadable"
                    warnings.append(
                        make_warning(
                            "clo_text_unreadable",
                            "the published CLO row was captured, but its outcome text could not be read reliably",
                            source_page=page_number,
                            clo_code=code,
                        )
                    )

                program_values: dict[str, set[tuple[str, ...] | None]] = defaultdict(
                    set
                )
                program_sources: dict[str, set[str]] = defaultdict(set)
                scalar_plos: list[str] = []
                scalar_sources: dict[str, set[str]] = defaultdict(set)
                for value, cell_source in mapping_values:
                    mapping = _program_map(value, programs)
                    for program, mapped_plo in mapping.items():
                        program_values[program].add(
                            (mapped_plo,) if mapped_plo else None
                        )
                        program_sources[program].add(cell_source)
                    if mapping:
                        continue
                    for candidate in (value, reverse_visual_arabic(value)):
                        for plo in unique_plos(candidate):
                            if plo not in scalar_plos:
                                scalar_plos.append(plo)
                            scalar_sources[plo].add(cell_source)
                program_mapping = {
                    program: next(iter(values))
                    for program, values in program_values.items()
                    if len(values) == 1
                }
                program_conflicts = {
                    program
                    for program, values in program_values.items()
                    if len(values) > 1
                }
                program_plain = {
                    program
                    for program, sources in program_sources.items()
                    if sources
                    and all(_is_plain_provenance(source) for source in sources)
                }
                program_inferred = {
                    program
                    for program, sources in program_sources.items()
                    if any(_is_inferred_plo_provenance(source) for source in sources)
                }
                scalar_source_set = (
                    scalar_sources[scalar_plos[0]] if len(scalar_plos) == 1 else set()
                )
                scalar_plain = any(
                    _is_plain_provenance(source) for source in scalar_source_set
                )
                scalar_inferred = any(
                    _is_inferred_plo_provenance(source) for source in scalar_source_set
                )
                scalar_ocr_prefix = any(
                    _is_ocr_prefix_plo_provenance(source)
                    for source in scalar_source_set
                )
                scalar_row_aligned = any(
                    source == "geometric_row_alignment" for source in scalar_source_set
                )
                scalar_confidence = (
                    "medium"
                    if len(scalar_plos) == 1
                    and scalar_source_set
                    and (
                        scalar_plain
                        or scalar_inferred
                        or scalar_ocr_prefix
                        or scalar_row_aligned
                    )
                    else "high"
                )
                if (
                    len(scalar_plos) > 1
                    and frozenset(scalar_plos) not in trusted_multi_cells
                ):
                    scalar_plos = []
                    warnings.append(
                        make_warning(
                            "plo_multi_code_cell_unresolved",
                            "multiple PLO tokens crossed physical cells or an unseparated extended cell; no row assignment was inferred",
                            source_page=page_number,
                            clo_code=code,
                        )
                    )
                if numeric_text_untrusted:
                    program_mapping = {}
                    program_conflicts = set()
                    program_plain = set()
                    scalar_plos = []
                    warnings.append(
                        make_warning(
                            "plo_numeric_text_untrusted",
                            "the PDF's PLO digit map is known to be corrupt; OCR was not reliable enough to pin a code",
                            source_page=page_number,
                            clo_code=code,
                        )
                    )
                plo_mappings = _build_plo_mappings(
                    scopes,
                    source_page=page_number,
                    scalar_plos=scalar_plos,
                    parsed_program_map=program_mapping,
                    auxiliary=auxiliary,
                    edit=edit,
                    scalar_confidence=scalar_confidence,
                    parsed_program_conflicts=program_conflicts,
                    parsed_program_plain=program_plain,
                    parsed_program_inferred=program_inferred,
                    scalar_plain=scalar_plain,
                    scalar_inferred=scalar_inferred,
                    scalar_ocr_prefix=scalar_ocr_prefix,
                    scalar_row_aligned=scalar_row_aligned,
                )
                assessment = _assessment_from_block(rows, start, end, assessment_column)
                manifest_assessment = (
                    clean_text(edit.get("assessment_to")) if edit else ""
                )
                if manifest_assessment:
                    assessment = manifest_assessment
                if bundle.get("image_curve_grid_fallback"):
                    assessment_line_boxes = [
                        bbox
                        for row_index in range(start, end)
                        for bbox in bundle.get("ocr_line_boxes", {}).get(
                            f"{row_index}:{assessment_column}", []
                        )
                    ]
                    recovered_assessment = ocr_strict_line_consensus(
                        document, page_index, assessment_line_boxes
                    )
                    if recovered_assessment:
                        assessment = recovered_assessment
                assessment = _remove_orphan_combining_marks(assessment or "") or None
                assessment_extension_filtered = bool(
                    assessment_column is not None
                    and any(
                        _is_extension_filtered_provenance(
                            _bundle_cell_provenance(bundle, row_index, column)
                        )
                        for row_index in range(start, end)
                        for column in range(
                            max(0, assessment_column - 1), assessment_column + 2
                        )
                        if column < len(rows[row_index])
                    )
                )
                if assessment_extension_filtered:
                    assessment = None
                if assessment and _text_artifact_issue(assessment):
                    assessment = None
                missing_mapping = any(
                    item["status"] in {"missing", "conflict", "ambiguous_for_scope"}
                    for item in plo_mappings
                )
                if missing_mapping:
                    warnings.append(
                        make_warning(
                            "plo_mapping_unresolved",
                            "one or more applicable scopes have no unambiguous PLO mapping",
                            source_page=page_number,
                            clo_code=code,
                        )
                    )
                candidates[code].append(
                    {
                        "code": code,
                        "text": outcome or None,
                        "source_status": source_status,
                        "assessment": assessment,
                        "plo_mappings": plo_mappings,
                        "document_plo_codes": scalar_plos,
                        "confidence": confidence,
                        "source_page": page_number,
                        "extraction_method": outcome_source,
                        "_document_order": (
                            (
                                page_number,
                                0,
                                round(aligned_row_top, 3),
                                bundle_index,
                                start,
                            )
                            if aligned_row_top is not None
                            else _document_order_key(
                                page_number,
                                bundle_index,
                                start,
                                (
                                    [group.get("code_bbox")]
                                    if group.get("code_bbox") is not None
                                    else [bbox for _, bbox, _ in selected]
                                ),
                            )
                        ),
                        "_physical_anchor": _physical_row_anchor(
                            page_number,
                            group.get("code_bbox"),
                            bundle_index,
                            start,
                        ),
                        "_code_unique_in_bundle": sum(
                            other["code"] == code for other in groups
                        )
                        == 1,
                        "_synthetic_word_baseline": bool(
                            bundle.get("word_baseline_fallback")
                        ),
                        "_source_substantive": _substantive_source_text(raw_outcome),
                    }
                )

            for block in ellipsis_blocks:
                start = block["start"]
                end = block["end"]
                code_column = block["code_column"]
                raw_source_code = block.get("source_code")
                source_code = (
                    str(raw_source_code) if raw_source_code is not None else None
                )
                selected_unlabeled: list[
                    tuple[
                        str,
                        tuple[float, float, float, float] | None,
                        str,
                    ]
                ] = []
                mapping_values_unlabeled: list[tuple[str, str]] = []
                for row_index in range(start, end):
                    row = rows[row_index]
                    box_row = bundle["boxes"][row_index]
                    for column, raw in enumerate(row):
                        value, cid_count = logical_cell(
                            raw, geometric=bundle["geometric"]
                        )
                        cell_source = _bundle_cell_provenance(bundle, row_index, column)
                        if cid_count:
                            warnings.append(
                                make_warning(
                                    "cid_removed",
                                    f"removed {cid_count} unmapped CID token(s) before parsing",
                                    source_page=page_number,
                                )
                            )
                        bbox = box_row[column] if column < len(box_row) else None
                        in_plo_band = bool(
                            value
                            and (
                                (
                                    bbox
                                    and plo_band
                                    and _horizontal_overlap(bbox, plo_band) >= 0.55
                                )
                                or (
                                    (not bbox or not plo_band)
                                    and plo_column is not None
                                    and (
                                        column == plo_column
                                        if inferred_plo
                                        else abs(column - plo_column) <= 1
                                    )
                                )
                            )
                        )
                        if in_plo_band:
                            mapping_source = cell_source
                            if inferred_plo and not _is_plain_provenance(cell_source):
                                mapping_source = "geometric_inferred_plo_column"
                            recovered_plo = (
                                _recover_exact_plo_with_ocr_prefix(
                                    pdf_pages[page_index],
                                    document,
                                    page_index,
                                    bbox,
                                    value,
                                )
                                if not numeric_text_untrusted
                                else None
                            )
                            if recovered_plo:
                                value = recovered_plo
                                mapping_source = "targeted_ocr_invalid_plo_prefix"
                                warnings.append(
                                    make_warning(
                                        "plo_prefix_recovered_by_ocr",
                                        "recovered a missing or invalid ToUnicode PLO prefix by "
                                        "multi-scale OCR of the isolated prefix glyph; "
                                        "the digits remain from the vector text layer",
                                        source_page=page_number,
                                    )
                                )
                            mapping_values_unlabeled.append((value, mapping_source))
                        if (
                            not value
                            or normalize_clo(value, allow_legacy=allow_legacy)
                            or normalize_source_clo_marker(value)
                            or _is_section_heading(value)
                            or _looks_like_table_header(value)
                        ):
                            continue
                        codes_here = unique_plos(value) + unique_plos(
                            reverse_visual_arabic(value)
                        )
                        if codes_here and len(normalized(value)) < 20:
                            continue
                        in_outcome_column = (
                            outcome_column is not None
                            and outcome_column - 1 <= column <= outcome_column + 3
                            and column <= code_column
                        )
                        if in_outcome_column and len(normalized(value)) >= 4:
                            selected_unlabeled.append(
                                (
                                    value,
                                    bbox,
                                    cell_source,
                                )
                            )

                outcome_parts = []
                seen_parts: set[str] = set()
                for value, _, _ in selected_unlabeled:
                    identity = normalized(value)
                    if identity and identity not in seen_parts:
                        seen_parts.add(identity)
                        outcome_parts.append(value)
                raw_unlabeled_outcome = clean_text(" ".join(outcome_parts))
                outcome = _clean_outcome(raw_unlabeled_outcome, source_code or "")
                marker_bbox = None
                marker_row = int(block["ellipsis_row"])
                if marker_row < len(bundle["boxes"]) and code_column < len(
                    bundle["boxes"][marker_row]
                ):
                    marker_bbox = bundle["boxes"][marker_row][code_column]
                used_plain_fallback = any(
                    source.startswith("plain_") or source == "plain_table"
                    for _, _, source in selected_unlabeled
                )
                extension_filtered = any(
                    _is_extension_filtered_provenance(source)
                    for _, _, source in selected_unlabeled
                )
                region = _physical_outcome_region(
                    outcome_band,
                    marker_bbox,
                    (bbox for _, bbox, _ in selected_unlabeled),
                )
                if (
                    region
                    and bundle.get("geometric")
                    and not bundle.get("word_baseline_fallback")
                    and not bundle.get("image_curve_grid_fallback")
                ):
                    physical_diagnostics: dict[str, int] = {}
                    physical_raw = logical_region_text(
                        pdf_pages[page_index],
                        region,
                        diagnostics=physical_diagnostics,
                    )
                    physical_outcome = _clean_outcome(physical_raw, source_code or "")
                    current_issue = _candidate_issue(outcome)
                    physical_issue = _candidate_issue(physical_outcome)
                    if physical_outcome and (
                        physical_issue is None
                        or (
                            current_issue is not None
                            and len(normalized(physical_outcome))
                            >= len(normalized(outcome))
                        )
                    ):
                        raw_unlabeled_outcome = physical_raw
                        outcome = physical_outcome
                    extension_filtered = extension_filtered or bool(
                        physical_diagnostics.get("extension_glyphs_suppressed", 0)
                    )
                if used_plain_fallback:
                    outcome_source = "plain_table"
                    confidence = "low"
                else:
                    outcome_source = (
                        "geometric_pdf" if bundle["geometric"] else "plain_table"
                    )
                    confidence = "medium" if bundle["geometric"] else "low"
                issue = _candidate_issue(outcome)
                if not issue and extension_filtered:
                    issue = "extension_glyphs_suppressed"
                if (
                    not issue
                    and numeric_text_untrusted
                    and _has_severe_orphan_combining_run(raw_unlabeled_outcome)
                ):
                    issue = "severe_orphan_combining_run"
                if issue:
                    if region is None:
                        region = _bbox_union(bbox for _, bbox, _ in selected_unlabeled)
                    if bundle.get("image_curve_grid_fallback"):
                        line_boxes = [
                            bbox
                            for row_index in range(start, end)
                            for bbox in bundle.get("ocr_line_boxes", {}).get(
                                f"{row_index}:{outcome_column}", []
                            )
                        ]
                        ocr = _clean_outcome(
                            ocr_strict_line_consensus(document, page_index, line_boxes),
                            source_code or "",
                        )
                    else:
                        ocr = ocr_tight_outcome_line_consensus(
                            pdf_pages[page_index],
                            document,
                            page_index,
                            region,
                            source_code or "...",
                        )
                        if not ocr:
                            whole_cell_ocr = ocr_tight_outcome_consensus(
                                document,
                                page_index,
                                region,
                                source_code or "...",
                            )
                            source_terminal = _source_terminal(raw_unlabeled_outcome)
                            if (
                                source_terminal
                                and _source_terminal(whole_cell_ocr) == source_terminal
                            ):
                                ocr = whole_cell_ocr
                    ocr_issue = (
                        _ocr_candidate_issue(ocr)
                        if bundle.get("image_curve_grid_fallback")
                        else _tight_ocr_candidate_issue(ocr)
                    )
                    if not ocr_issue and len(normalized(ocr)) >= 8:
                        outcome = ocr
                        outcome_source = "targeted_ocr"
                        confidence = "medium"
                    else:
                        outcome = ""
                        confidence = "low"
                        warnings.append(
                            make_warning(
                                "clo_text_unreadable",
                                "unlabelled CLO text was left null after vector and "
                                "targeted OCR checks "
                                f"(vector={issue}; ocr={ocr_issue or 'too_short'})",
                                source_page=page_number,
                            )
                        )
                if outcome and len(normalized(outcome).split()) < 6:
                    outcome = ""
                    confidence = "low"
                    warnings.append(
                        make_warning(
                            "clo_text_unreadable",
                            "unlabelled CLO text was left null because the published "
                            "row contains fewer than six logical words",
                            source_page=page_number,
                        )
                    )
                outcome = _remove_orphan_combining_marks(outcome)

                if outcome:
                    source_status = "present"
                elif _source_outcome_cell_is_blank(
                    pdf_pages[page_index], region, raw_unlabeled_outcome
                ):
                    source_status = "source_blank"
                    warnings.append(
                        make_warning(
                            "clo_text_blank_in_source",
                            "the published unlabelled CLO row was captured, but its outcome-text cell is blank",
                            source_page=page_number,
                        )
                    )
                else:
                    source_status = "unreadable"
                    warnings.append(
                        make_warning(
                            "clo_text_unreadable",
                            "the published unlabelled CLO row was captured, but its outcome text could not be read reliably",
                            source_page=page_number,
                        )
                    )

                program_values: dict[str, set[tuple[str, ...] | None]] = defaultdict(
                    set
                )
                program_sources: dict[str, set[str]] = defaultdict(set)
                scalar_plos: list[str] = []
                scalar_sources: dict[str, set[str]] = defaultdict(set)
                for value, cell_source in mapping_values_unlabeled:
                    mapping = _program_map(value, programs)
                    for program, mapped_plo in mapping.items():
                        program_values[program].add(
                            (mapped_plo,) if mapped_plo else None
                        )
                        program_sources[program].add(cell_source)
                    if mapping:
                        continue
                    for candidate in (value, reverse_visual_arabic(value)):
                        for plo in unique_plos(candidate):
                            if plo not in scalar_plos:
                                scalar_plos.append(plo)
                            scalar_sources[plo].add(cell_source)
                program_mapping = {
                    program: next(iter(values))
                    for program, values in program_values.items()
                    if len(values) == 1
                }
                program_conflicts = {
                    program
                    for program, values in program_values.items()
                    if len(values) > 1
                }
                program_plain = {
                    program
                    for program, sources in program_sources.items()
                    if sources
                    and all(_is_plain_provenance(source) for source in sources)
                }
                program_inferred = {
                    program
                    for program, sources in program_sources.items()
                    if any(_is_inferred_plo_provenance(source) for source in sources)
                }
                scalar_source_set = (
                    scalar_sources[scalar_plos[0]] if len(scalar_plos) == 1 else set()
                )
                scalar_plain = any(
                    _is_plain_provenance(source) for source in scalar_source_set
                )
                scalar_inferred = any(
                    _is_inferred_plo_provenance(source) for source in scalar_source_set
                )
                scalar_ocr_prefix = any(
                    _is_ocr_prefix_plo_provenance(source)
                    for source in scalar_source_set
                )
                scalar_confidence = (
                    "medium"
                    if len(scalar_plos) == 1
                    and scalar_source_set
                    and (scalar_plain or scalar_inferred or scalar_ocr_prefix)
                    else "high"
                )
                if (
                    len(scalar_plos) > 1
                    and frozenset(scalar_plos) not in trusted_multi_cells
                ):
                    scalar_plos = []
                    warnings.append(
                        make_warning(
                            "plo_multi_code_cell_unresolved",
                            "multiple PLO tokens crossed physical cells or an unseparated extended cell; no row assignment was inferred",
                            source_page=page_number,
                        )
                    )
                if numeric_text_untrusted:
                    program_mapping = {}
                    program_conflicts = set()
                    program_plain = set()
                    scalar_plos = []
                    warnings.append(
                        make_warning(
                            "plo_numeric_text_untrusted",
                            "the PDF's PLO digit map is known to be corrupt; OCR was not reliable enough to pin a code",
                            source_page=page_number,
                        )
                    )
                plo_mappings = _build_plo_mappings(
                    scopes,
                    source_page=page_number,
                    scalar_plos=scalar_plos,
                    parsed_program_map=program_mapping,
                    auxiliary=auxiliary,
                    edit=None,
                    scalar_confidence=scalar_confidence,
                    parsed_program_conflicts=program_conflicts,
                    parsed_program_plain=program_plain,
                    parsed_program_inferred=program_inferred,
                    scalar_plain=scalar_plain,
                    scalar_inferred=scalar_inferred,
                    scalar_ocr_prefix=scalar_ocr_prefix,
                )
                assessment = _assessment_from_block(rows, start, end, assessment_column)
                if bundle.get("image_curve_grid_fallback"):
                    assessment_line_boxes = [
                        bbox
                        for row_index in range(start, end)
                        for bbox in bundle.get("ocr_line_boxes", {}).get(
                            f"{row_index}:{assessment_column}", []
                        )
                    ]
                    recovered_assessment = ocr_strict_line_consensus(
                        document, page_index, assessment_line_boxes
                    )
                    if recovered_assessment:
                        assessment = recovered_assessment
                assessment = _remove_orphan_combining_marks(assessment or "") or None
                assessment_extension_filtered = bool(
                    assessment_column is not None
                    and any(
                        _is_extension_filtered_provenance(
                            _bundle_cell_provenance(bundle, row_index, column)
                        )
                        for row_index in range(start, end)
                        for column in range(
                            max(0, assessment_column - 1), assessment_column + 2
                        )
                        if column < len(rows[row_index])
                    )
                )
                if assessment_extension_filtered:
                    assessment = None
                if assessment and _text_artifact_issue(assessment):
                    assessment = None
                if any(
                    item["status"] in {"missing", "conflict", "ambiguous_for_scope"}
                    for item in plo_mappings
                ):
                    warnings.append(
                        make_warning(
                            "plo_mapping_unresolved",
                            "one or more applicable scopes have no unambiguous PLO mapping",
                            source_page=page_number,
                        )
                    )
                unlabeled = {
                    "code": None,
                    "text": outcome or None,
                    "source_status": source_status,
                    "assessment": assessment,
                    "plo_mappings": plo_mappings,
                    "document_plo_codes": scalar_plos,
                    "confidence": confidence,
                    "source_page": page_number,
                    "extraction_method": outcome_source,
                    "_document_order": _document_order_key(
                        page_number,
                        bundle_index,
                        start,
                        [
                            marker_bbox,
                            *(bbox for _, bbox, _ in selected_unlabeled),
                        ],
                    ),
                    "_source_substantive": _substantive_source_text(
                        raw_unlabeled_outcome
                    ),
                }
                if source_code is not None:
                    unlabeled["source_code"] = source_code
                unlabeled_candidates.append(unlabeled)
                if block.get("code_unreadable"):
                    warnings.append(
                        make_warning(
                            "clo_code_unreadable",
                            "the published CLO code cell could not be read reliably; no code was inferred",
                            source_page=page_number,
                        )
                    )
                else:
                    warnings.append(
                        make_warning(
                            "clo_code_missing_in_source",
                            "a substantive CLO row has an ellipsis in its published code cell; no code was inferred",
                            source_page=page_number,
                        )
                    )

    # A published, hash-matched edit manifest can also recover a row whose
    # code cell itself has no usable ToUnicode map.  This is deliberately
    # limited to manifest edits; no code or text is inferred from a title.
    for code, edit in aux_edits.items():
        if candidates.get(code):
            continue
        page_number = int(edit.get("page_1_based") or 0)
        text = _clean_outcome(_sidecar_text(edit), code)
        text_issue = _candidate_issue(text)
        if page_number <= 0 or text_issue:
            warnings.append(
                make_warning(
                    "manifest_row_unusable",
                    "a hash-matched manifest row lacked a usable page or passed no CLO text safety gate",
                    clo_code=code,
                )
            )
            continue
        mappings = _build_plo_mappings(
            scopes,
            source_page=page_number,
            scalar_plos=[],
            parsed_program_map={},
            auxiliary=auxiliary,
            edit=edit,
        )
        candidates[code].append(
            {
                "code": code,
                "text": text,
                "source_status": "present",
                "assessment": None,
                "plo_mappings": mappings,
                "document_plo_codes": [],
                "confidence": "high",
                "source_page": page_number,
                "extraction_method": "hash_matched_manifest",
                "_document_order": _document_order_key(page_number, len(pages), 0, []),
                "_physical_anchor": f"{page_number}:manifest:{code}",
                "_code_unique_in_bundle": True,
                "_synthetic_word_baseline": False,
                "_source_substantive": True,
            }
        )
        warnings.append(
            make_warning(
                "clo_row_recovered_from_manifest",
                "the PDF code cell was unreadable; the row was recovered from its hash-matched edit manifest",
                source_page=page_number,
                clo_code=code,
            )
        )

    records: list[dict[str, Any]] = []
    rank = {"high": 2, "medium": 1, "low": 0}

    def candidate_rank(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item["text"] is not None,
            rank[item["confidence"]],
            not bool(item.get("_synthetic_word_baseline")),
            sum(
                mapping["status"] not in {"missing", "conflict", "ambiguous_for_scope"}
                for mapping in item["plo_mappings"]
            ),
            len(item["text"] or ""),
            item["assessment"] is not None,
            -item["source_page"],
        )

    physical_candidates: dict[str, list[dict[str, Any]]] = {}
    for code, options in candidates.items():
        by_physical_row: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for option in options:
            by_physical_row[str(option["_physical_anchor"])].append(option)
        physical_candidates[code] = [
            max(grouped, key=candidate_rank) for grouped in by_physical_row.values()
        ]

    # Some word-baseline PDFs publish a compact CLO summary before a second
    # table that repeats only a subset of those codes alongside direct
    # assessments.  Detect that relationship from two or more shared, unique
    # printed codes.  The summary remains the row source and establishes
    # document order; the later table is an assessment donor only.  Requiring
    # both bundles to be synthetic keeps this rule away from ordinary
    # geometric tables and from genuinely duplicated source rows.
    bundle_profiles: dict[tuple[int, int], dict[str, Any]] = {}

    def candidate_bundle_key(item: Mapping[str, Any]) -> tuple[int, int] | None:
        order = item.get("_document_order")
        if (
            not item.get("_synthetic_word_baseline")
            or not isinstance(order, (list, tuple))
            or len(order) < 4
        ):
            return None
        return int(item["source_page"]), int(order[3])

    for code, options in physical_candidates.items():
        for option in options:
            bundle_key = candidate_bundle_key(option)
            if bundle_key is None:
                continue
            profile = bundle_profiles.setdefault(
                bundle_key,
                {
                    "codes": set(),
                    "assessment_codes": set(),
                    "order": option["_document_order"],
                },
            )
            profile["codes"].add(code)
            if option.get("assessment"):
                profile["assessment_codes"].add(code)
            profile["order"] = min(profile["order"], option["_document_order"])

    summary_bundle_keys: set[tuple[int, int]] = set()
    assessment_donor_bundle_keys: set[tuple[int, int]] = set()
    for summary_key, summary in bundle_profiles.items():
        if summary["assessment_codes"]:
            continue
        for donor_key, donor in bundle_profiles.items():
            if summary_key == donor_key or len(donor["assessment_codes"]) < 2:
                continue
            shared_codes = summary["codes"] & donor["assessment_codes"]
            if len(shared_codes) < 2 or summary["order"] >= donor["order"]:
                continue
            summary_bundle_keys.add(summary_key)
            assessment_donor_bundle_keys.add(donor_key)

    for code, physical_options in physical_candidates.items():
        summary_options = [
            option
            for option in physical_options
            if candidate_bundle_key(option) in summary_bundle_keys
        ]
        donor_options = [
            option
            for option in physical_options
            if candidate_bundle_key(option) in assessment_donor_bundle_keys
        ]
        if summary_options:
            authoritative_options = summary_options
            chosen = max(authoritative_options, key=candidate_rank)
        elif donor_options:
            # A repeated direct-assessment table cannot establish a CLO row
            # that was not independently present in the preceding summary.
            continue
        else:
            authoritative_options = physical_options
            chosen = max(authoritative_options, key=candidate_rank)

        # A direct-assessment cell may be present only in the teaching/assessment
        # table while the summary table supplies the better CLO text.  Merge this
        # one field only when the printed code occurs exactly once in each table
        # and every eligible donor agrees on the value.
        if chosen["assessment"] is None and chosen.get("_code_unique_in_bundle"):
            donors = donor_options
            if not donors and chosen.get("text"):
                # Outside an independently detected summary/direct relation,
                # an equal printed code is insufficient: require exact text
                # identity before borrowing only the assessment field.
                donors = [
                    option
                    for option in physical_options
                    if option.get("text")
                    and normalized(option["text"]) == normalized(chosen["text"])
                ]
            donors = [
                option
                for option in donors
                if option.get("_code_unique_in_bundle") and option["assessment"]
            ]
            donor_values = {normalized(option["assessment"]) for option in donors}
            if len(donor_values) == 1:
                donor = min(donors, key=lambda item: item["_document_order"])
                chosen = dict(chosen)
                chosen["assessment"] = donor["assessment"]
                chosen["assessment_source_page"] = donor["source_page"]

        if (
            chosen.get("_code_unique_in_bundle")
            and _same_document_competency_match(
                code, chosen.get("text"), competency_summary
            )
            and all(
                mapping["status"] == "missing" for mapping in chosen["plo_mappings"]
            )
        ):
            chosen = dict(chosen)
            chosen["plo_mappings"] = [
                {
                    **mapping,
                    "plo_codes": [code],
                    "status": "mapped",
                    "confidence": "medium",
                    "evidence": (
                        "same-document exact direct-table CLO code matched a "
                        "duplicated-code summary mapping row by outcome text"
                    ),
                }
                for mapping in chosen["plo_mappings"]
            ]
            warnings.append(
                make_warning(
                    "plo_mapping_recovered_from_same_document_direct_table",
                    "recovered a corrupt competency mapping only after exact direct-table code and summary outcome text agreed",
                    source_page=chosen["source_page"],
                    clo_code=code,
                )
            )

        # A table can continue on the next page after already publishing the
        # same numeric code in two distinct rows.  Once disjoint rows in one
        # physical bundle prove that the duplicate is real, retain adjacent-
        # page occurrences too.  This is deliberately narrower than treating
        # every repeated code as a new CLO: assessment-donor tables have
        # already been excluded above, rows must have distinct substantive
        # texts, and the extra rows must be on the duplicate page or an
        # immediately adjacent continuation page.
        repeated_pages = {
            int(option["source_page"])
            for option in authoritative_options
            if not option.get("_code_unique_in_bundle")
            and option.get("_source_substantive")
            and option["text"] is not None
        }
        nearby_substantive = [
            option
            for option in authoritative_options
            if repeated_pages
            and min(
                abs(int(option["source_page"]) - repeated_page)
                for repeated_page in repeated_pages
            )
            <= 1
            and option.get("_source_substantive")
            and option["text"] is not None
        ]
        by_distinct_text: dict[str, dict[str, Any]] = {}
        for option in nearby_substantive:
            identity = normalized(option["text"])
            incumbent = by_distinct_text.get(identity)
            if identity and (
                incumbent is None or candidate_rank(option) > candidate_rank(incumbent)
            ):
                by_distinct_text[identity] = option
        distinct_options = list(by_distinct_text.values())
        if (
            re.fullmatch(r"[123]\.[1-9][0-9]?", code)
            and sum(
                not option.get("_code_unique_in_bundle")
                for option in nearby_substantive
            )
            >= 2
            and len(distinct_options) >= 2
        ):
            for option in sorted(
                distinct_options, key=lambda item: item["_document_order"]
            ):
                records.append(option)
            warnings.extend(
                make_warning(
                    "duplicate_source_clo_code",
                    "the published source contains a distinct substantive row with the same printed CLO code",
                    source_page=option["source_page"],
                    clo_code=code,
                )
                for option in distinct_options
            )
            continue
        # A printed CLO code establishes a source row even when the document's
        # own outcome cell is blank.  Keep the row and report that source state;
        # never fill it from a different or inherited list.
        records.append(chosen)
    unique_unlabeled: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in unlabeled_candidates:
        if "source_code" not in item:
            key = (
                item["source_page"],
                item["_document_order"],
                normalized(item["text"]),
                canonical_json(item["plo_mappings"]),
            )
        else:
            key = (
                item["source_page"],
                item["source_code"],
                normalized(item["text"]),
                canonical_json(item["plo_mappings"]),
            )
        unique_unlabeled.setdefault(key, item)
    records.extend(unique_unlabeled.values())
    records.sort(key=lambda item: item["_document_order"])
    for item in records:
        item.setdefault(
            "assessment_source_page",
            item["source_page"] if item["assessment"] is not None else None,
        )
        item.pop("_document_order", None)
        item.pop("_physical_anchor", None)
        item.pop("_code_unique_in_bundle", None)
        item.pop("_synthetic_word_baseline", None)
        item.pop("_source_substantive", None)
    if capture_audit is not None:
        # Only the page-local physical audit may certify completeness.  The
        # number of records built below is deliberately not used as a fallback:
        # equality with a count derived from those same records would be true by
        # construction and could recreate a false ``complete`` label.  If the
        # physical pass sees fewer rows than were retained, its count is not
        # reliable enough to publish and the variant remains unverified.
        capture_audit["source_clo_row_count"] = _verified_source_clo_row_count(
            geometric_source_clo_row_count, len(records)
        )
        capture_audit["captured_clo_row_count"] = len(records)
    return records, _deduplicate_warnings(warnings)


PERCENT_RE = re.compile(
    r"(?:([0-9]{1,3}(?:[.,]\d+)?)\s*[%٪]|[%٪]\s*([0-9]{1,3}(?:[.,]\d+)?))"
)


def _weight_values(value: Any, *, exact_number: bool = False) -> list[float]:
    text = clean_text(value).replace("٫", ".")
    values = [
        float((match.group(1) or match.group(2)).replace(",", "."))
        for match in PERCENT_RE.finditer(text)
    ]
    if exact_number and not values:
        match = re.fullmatch(r"\s*([0-9]{1,3}(?:[.,]\d+)?)\s*", text)
        if match:
            values.append(float(match.group(1).replace(",", ".")))
    return [value for value in values if 0 < value <= 100]


def parse_assessment_plan(
    pages: Sequence[Sequence[Mapping[str, Any]]],
    document: Any,
    *,
    numeric_text_untrusted: bool = False,
) -> tuple[list[dict[str, Any]], bool, float | None, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for page_index, bundles in enumerate(pages):
        page_number = page_index + 1
        for bundle_index, bundle in enumerate(bundles):
            if bundle.get("suppress_assessment_plan"):
                continue
            rows = bundle["rows"]
            flat = " ".join(cell for row in rows for cell in row if cell)
            if not contains_alias(flat, ASSESSMENT_PLAN_ALIASES):
                continue
            label_column = _header_column(rows, ASSESSMENT_PLAN_ALIASES)
            weight_column = _header_column(rows, WEIGHT_ALIASES)
            if label_column is None or weight_column is None:
                warnings.append(
                    make_warning(
                        "assessment_plan_headers_unresolved",
                        "assessment-plan label or weight column could not be located",
                        source_page=page_number,
                    )
                )
                continue
            label_band = _header_band(bundle, ASSESSMENT_PLAN_ALIASES)
            weight_band = _header_band(bundle, WEIGHT_ALIASES)
            weight_rows: list[dict[str, Any]] = []
            for row_index, row in enumerate(rows):
                values: list[float] = []
                boxes: list[tuple[float, float, float, float]] = []
                value_sources: list[str] = []
                for column, raw in enumerate(row):
                    bbox = bundle["boxes"][row_index][column]
                    in_band = (
                        _horizontal_overlap(bbox, weight_band) >= 0.55
                        if weight_band
                        else column == weight_column
                    )
                    if not in_band:
                        continue
                    parsed = _weight_values(raw, exact_number=True)
                    values.extend(parsed)
                    if parsed:
                        value_sources.append(
                            _bundle_cell_provenance(bundle, row_index, column)
                        )
                    if bbox and parsed:
                        boxes.append(bbox)
                values = list(dict.fromkeys(values))
                if values:
                    weight_rows.append(
                        {
                            "row": row_index,
                            "weight": values[0],
                            "weight_bbox": _bbox_union(boxes),
                            "plain_weight": bool(value_sources)
                            and any(
                                _is_plain_provenance(source) for source in value_sources
                            ),
                        }
                    )
            for position, weight_row in enumerate(weight_rows):
                start = weight_row["row"]
                end = (
                    weight_rows[position + 1]["row"]
                    if position + 1 < len(weight_rows)
                    else len(rows)
                )
                label_parts: list[str] = []
                label_boxes: list[tuple[float, float, float, float]] = []
                label_sources: list[str] = []
                identities: set[str] = set()
                for row_index in range(start, end):
                    row = rows[row_index]
                    for column, raw in enumerate(row):
                        bbox = bundle["boxes"][row_index][column]
                        in_band = (
                            _horizontal_overlap(bbox, label_band) >= 0.55
                            if label_band
                            else column == label_column
                        )
                        if not in_band:
                            continue
                        value, cid_count = logical_cell(
                            raw, geometric=bundle["geometric"]
                        )
                        if cid_count:
                            warnings.append(
                                make_warning(
                                    "cid_removed",
                                    f"removed {cid_count} unmapped CID token(s) from an assessment label",
                                    source_page=page_number,
                                )
                            )
                        identity = normalized(value)
                        if (
                            len(identity) >= 3
                            and identity not in identities
                            and not contains_alias(value, ASSESSMENT_PLAN_ALIASES)
                            and not contains_alias(value, WEIGHT_ALIASES)
                            and not re.fullmatch(r"[0-9 ./%٪\-–—]+", value)
                        ):
                            identities.add(identity)
                            label_parts.append(value.strip(" .،؛:-"))
                            label_sources.append(
                                _bundle_cell_provenance(bundle, row_index, column)
                            )
                            if bbox:
                                label_boxes.append(bbox)
                label = (
                    _remove_orphan_combining_marks(clean_text(" ".join(label_parts)))
                    or None
                )
                label_bbox = _bbox_union(label_boxes)
                label_extension_filtered = any(
                    _is_extension_filtered_provenance(source)
                    for source in label_sources
                )
                if label and (_text_artifact_issue(label) or label_extension_filtered):
                    ocr_label = clean_text(
                        ocr_region(
                            document,
                            page_index,
                            label_bbox,
                            prefer_outcome=False,
                        )
                    ).strip(" .،؛:-")
                    if (
                        ocr_label
                        and not _text_artifact_issue(ocr_label)
                        and (
                            not label_extension_filtered
                            or _ocr_agrees_with_vector(ocr_label, label)
                        )
                    ):
                        if not label_extension_filtered:
                            label = ocr_label
                        label_confidence = "medium"
                    else:
                        label = None
                        label_confidence = "low"
                        warnings.append(
                            make_warning(
                                "assessment_label_unreadable",
                                "assessment label was left null after vector and targeted OCR checks",
                                source_page=page_number,
                            )
                        )
                else:
                    label_confidence = "high" if label else "low"
                if label_confidence != "low" and (
                    weight_row["plain_weight"]
                    or (
                        label_sources
                        and any(
                            _is_plain_provenance(source) for source in label_sources
                        )
                    )
                ):
                    label_confidence = "medium"
                if label is None:
                    warnings.append(
                        make_warning(
                            "assessment_label_unresolved",
                            "a printed assessment weight had no unambiguous activity label",
                            source_page=page_number,
                        )
                    )
                records.append(
                    {
                        "label": label,
                        "weight": weight_row["weight"],
                        "source_page": page_number,
                        "confidence": label_confidence,
                        "_weight_bbox": weight_row["weight_bbox"],
                        "_physical_identity": (
                            page_number,
                            tuple(
                                round(float(coordinate), 2)
                                for coordinate in weight_row["weight_bbox"]
                            ),
                        )
                        if weight_row["weight_bbox"]
                        else (page_number, bundle_index, weight_row["row"]),
                        "_order": (
                            page_number,
                            bundle_index,
                            weight_row["row"],
                        ),
                    }
                )

    records.sort(key=lambda item: item["_order"])
    vector_total = sum(float(item["weight"]) for item in records) if records else None
    needs_numeric_ocr = bool(records) and (
        numeric_text_untrusted
        or vector_total is None
        or abs(vector_total - 100.0) > 0.01
    )
    if needs_numeric_ocr:
        ocr_weights: list[float | None] = []
        for item in records:
            raw = ocr_numeric_region(
                document,
                int(item["source_page"]) - 1,
                item.get("_weight_bbox"),
            )
            parsed = list(dict.fromkeys(_weight_values(raw, exact_number=True)))
            ocr_weights.append(parsed[0] if len(parsed) == 1 else None)
        if all(value is not None for value in ocr_weights):
            recovered = [float(value) for value in ocr_weights if value is not None]
            if numeric_text_untrusted or abs(sum(recovered) - 100.0) <= 0.01:
                for item, value in zip(records, recovered):
                    item["weight"] = int(value) if value.is_integer() else value
                    item["confidence"] = "medium"
                warnings.append(
                    make_warning(
                        "assessment_weights_recovered_by_ocr",
                        "numeric weights were read with targeted OCR because vector digits were untrusted or incoherent",
                    )
                )
            elif vector_total is not None and all(
                abs(float(item["weight"]) - value) <= 0.01
                for item, value in zip(records, recovered)
            ):
                warnings.append(
                    make_warning(
                        "assessment_plan_source_total_not_100",
                        "targeted OCR confirmed that the published assessment plan itself does not total 100",
                    )
                )
            else:
                for item in records:
                    item["weight"] = None
                    item["confidence"] = "low"
                warnings.append(
                    make_warning(
                        "assessment_weight_conflict",
                        "vector and OCR weights conflicted, so weights were left null",
                    )
                )
        elif numeric_text_untrusted:
            for item in records:
                item["weight"] = None
                item["confidence"] = "low"
            warnings.append(
                make_warning(
                    "assessment_weight_ocr_failed",
                    "the vector digit map is known to be corrupt and targeted OCR did not recover every weight",
                )
            )

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in records:
        key = (
            normalized(item["label"]),
            float(item["weight"]) if item["weight"] is not None else None,
            item["_physical_identity"],
        )
        unique.setdefault(key, item)
    records = list(unique.values())
    records.sort(key=lambda item: item["_order"])
    for item in records:
        item.pop("_weight_bbox", None)
        item.pop("_physical_identity", None)
        item.pop("_order", None)
        if isinstance(item.get("weight"), float) and item["weight"].is_integer():
            item["weight"] = int(item["weight"])
    all_weights = bool(records) and all(item["weight"] is not None for item in records)
    total = sum(float(item["weight"]) for item in records) if all_weights else None
    complete = (
        bool(records)
        and all(item["label"] is not None for item in records)
        and total is not None
        and abs(total - 100.0) <= 0.01
    )
    if not records:
        warnings.append(
            make_warning(
                "assessment_plan_missing",
                "no assessment-plan row with both a label and a printed weight was recovered",
            )
        )
    elif total is not None and abs(total - 100.0) > 0.01:
        warnings.append(
            make_warning(
                "assessment_plan_total_not_100",
                f"recovered assessment weights total {total:g}, not 100",
            )
        )
    return records, complete, total, _deduplicate_warnings(warnings)


def _deduplicate_warnings(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique = {canonical_json(dict(item)): dict(item) for item in items}
    return sorted(unique.values(), key=warning_sort_key)


def _reconcile_outcome_warnings(
    clos: Sequence[Mapping[str, Any]],
    items: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach final CLO indexes and discard superseded candidate diagnostics."""
    targeted_codes = {
        "clo_text_blank_in_source",
        "clo_text_unreadable",
        "plo_mapping_unresolved",
        "direct_assessment_unresolved",
        "clo_code_missing_in_source",
        "clo_code_unreadable",
        "duplicate_source_clo_code",
        "plo_numeric_text_untrusted",
    }
    output: list[dict[str, Any]] = []
    for raw in items:
        warning = dict(raw)
        if warning.get("code") not in targeted_codes:
            output.append(warning)
            continue
        matches: list[int] = []
        for index, clo in enumerate(clos):
            if warning.get("source_page") not in {None, clo.get("source_page")}:
                continue
            if "clo_code" in warning and warning.get("clo_code") != clo.get("code"):
                continue
            if "clo_code" not in warning and clo.get("code") is not None:
                continue
            code = warning.get("code")
            mappings = clo.get("plo_mappings") or []
            condition = {
                "clo_text_blank_in_source": clo.get("text") is None
                and clo.get("source_status") == "source_blank",
                "clo_text_unreadable": clo.get("text") is None
                and clo.get("source_status") == "unreadable",
                "plo_mapping_unresolved": any(
                    mapping.get("status")
                    in {"missing", "conflict", "ambiguous_for_scope"}
                    for mapping in mappings
                    if isinstance(mapping, Mapping)
                ),
                "direct_assessment_unresolved": clo.get("assessment") is None,
                "clo_code_missing_in_source": clo.get("code") is None
                and bool(clean_text(clo.get("source_code"))),
                "clo_code_unreadable": clo.get("code") is None
                and not clean_text(clo.get("source_code")),
                "duplicate_source_clo_code": clo.get("code") is not None,
                "plo_numeric_text_untrusted": any(
                    mapping.get("status")
                    in {"missing", "conflict", "ambiguous_for_scope"}
                    for mapping in mappings
                    if isinstance(mapping, Mapping)
                ),
            }[str(code)]
            if condition:
                matches.append(index)
        for index in matches:
            resolved = dict(warning)
            resolved["clo_index"] = index
            if clos[index].get("code") is None:
                resolved.pop("clo_code", None)
            output.append(resolved)
    return _deduplicate_warnings(output)


def extract_variant_worker(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    repo_root = Path(payload["repo_root"])
    source_pdf = str(payload["source_pdf"])
    source = repo_root / source_pdf
    variant_id = str(payload["variant_id"])
    scopes = payload["scopes"]
    catalog = payload["catalog"]
    course_code = str(payload["course_code"])
    auxiliary = payload.get("auxiliary")
    expected_source_sha256 = str(payload["source_sha256"])
    if sha256_file(source) != expected_source_sha256:
        raise SourceChangedError(f"source changed before extraction: {source_pdf}")
    warnings: list[dict[str, Any]] = []
    if not scopes:
        warnings.append(
            make_warning(
                "variant_scope_unresolved",
                "data.json supplied no program scope for this logical variant",
            )
        )
    try:
        if auxiliary and auxiliary.get("course_code") != course_code:
            raise ValueError(
                f"auxiliary course key {auxiliary.get('course_code')!r} does not match {course_code!r}"
            )
        with pdfplumber.open(source) as pdf, pymupdf.open(source) as document:
            pages: list[list[dict[str, Any]]] = []
            logical_page_texts: list[str] = []
            for page_index, page in enumerate(pdf.pages):
                try:
                    raw_text = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    raw_text = ""
                cid_count = len(CID_RE.findall(raw_text))
                if cid_count:
                    warnings.append(
                        make_warning(
                            "cid_removed",
                            f"removed {cid_count} unmapped CID token(s); they were treated as whitespace",
                            source_page=page_index + 1,
                        )
                    )
                logical_page_texts.append(
                    "\n".join(
                        logical_cell(line, geometric=False)[0]
                        for line in raw_text.splitlines()
                        if clean_text(line)
                    )
                )
                page_bundles = extract_page_tables(page)
                pages.append(page_bundles)
                fallback_cells = sum(
                    int(bundle.get("plain_fallback_cells") or 0)
                    for bundle in page_bundles
                )
                if fallback_cells:
                    used_plain_table = any(
                        bundle.get("extraction_provenance") == "plain_table"
                        for bundle in page_bundles
                    )
                    warnings.append(
                        make_warning(
                            (
                                "plain_table_fallback"
                                if used_plain_table
                                else "plain_cell_fallback"
                            ),
                            f"used the plain pdfplumber reading for {fallback_cells} cell(s) whose geometric reading was empty or structurally unusable",
                            source_page=page_index + 1,
                        )
                    )

            vector_title = clean_text(
                title_from_text(logical_page_texts[0] if logical_page_texts else "")
                or title_from_tables(pages)
            ).strip(" .،؛:-")
            expected_title = clean_text(catalog.get("title"))
            title = ""
            title_confidence = "low"
            title_method = "unresolved"
            if (
                vector_title
                and expected_title
                and normalized(vector_title) == normalized(expected_title)
            ):
                title = vector_title
                title_confidence = "high"
                title_method = "geometric_pdf"
            else:
                ocr_title = clean_text(title_from_ocr(ocr_page(document, 0))).strip(
                    " .،؛:-"
                )
                if (
                    ocr_title
                    and expected_title
                    and normalized(ocr_title) == normalized(expected_title)
                ):
                    title = ocr_title
                    title_confidence = "medium"
                    title_method = "page_ocr"
                elif (
                    not expected_title
                    and vector_title
                    and ocr_title
                    and normalized(vector_title) == normalized(ocr_title)
                ):
                    title = vector_title
                    title_confidence = "medium"
                    title_method = "geometric_pdf_ocr_consensus"
            if not title:
                warnings.append(
                    make_warning(
                        "course_name_unresolved",
                        "course name was left null because neither the text layer nor OCR produced a value consistent with the catalog",
                        source_page=1,
                    )
                )

            capture_audit: dict[str, Any] = {}
            clos, clo_warnings = parse_outcome_tables(
                pages,
                scopes,
                auxiliary,
                document,
                pdf.pages,
                numeric_text_untrusted=(
                    "/quranic-studies-master-1445-approved/" in source_pdf
                ),
                capture_audit=capture_audit,
                allow_split_table_stitch=(
                    expected_source_sha256 in SPLIT_OUTCOME_TABLE_SOURCE_SHA256
                ),
                allow_embedded_font_recovery=(
                    expected_source_sha256 in EMBEDDED_FONT_CMAP_SOURCE_SHA256
                ),
            )
            warnings.extend(clo_warnings)
            plan, plan_complete, plan_total, plan_warnings = parse_assessment_plan(
                pages,
                document,
                numeric_text_untrusted=(
                    "/quranic-studies-master-1445-approved/" in source_pdf
                ),
            )
            warnings.extend(plan_warnings)
            if not clos:
                warnings.append(
                    make_warning(
                        "course_outcomes_missing",
                        "no published CLO row could be captured",
                    )
                )
            for clo in clos:
                if clo["assessment"] is None:
                    warnings.append(
                        make_warning(
                            "direct_assessment_unresolved",
                            "no unambiguous direct-assessment text was recovered for this CLO",
                            source_page=clo["source_page"],
                            clo_code=clo["code"],
                        )
                    )

            source_clo_row_count = capture_audit.get("source_clo_row_count")
            captured_clo_row_count = len(clos)
            if captured_clo_row_count == 0:
                extraction_status = "failed"
            elif (
                isinstance(source_clo_row_count, int)
                and source_clo_row_count > 0
                and source_clo_row_count == captured_clo_row_count
            ):
                extraction_status = "complete"
            else:
                extraction_status = "partial"
                warnings.append(
                    make_warning(
                        (
                            "clo_row_count_unverified"
                            if source_clo_row_count is None
                            else "clo_row_count_mismatch"
                        ),
                        (
                            "the number of published CLO rows could not be verified independently"
                            if source_clo_row_count is None
                            else "the number of captured CLO rows does not equal the number of published rows"
                        ),
                    )
                )

            row_source_statuses = {item.get("source_status") for item in clos}
            if not clos or "unreadable" in row_source_statuses:
                source_status = "unreadable"
            elif "source_blank" in row_source_statuses:
                source_status = "source_blank"
            else:
                source_status = "present"

            warnings = _reconcile_outcome_warnings(clos, warnings)
            extracted = {
                "course_name": title or None,
                "course_name_metadata": {
                    "confidence": title_confidence,
                    "source_page": 1,
                    "extraction_method": title_method,
                },
                "clos": clos,
                "assessment_plan": plan,
                "assessment_plan_total": (
                    int(plan_total)
                    if plan_total is not None and float(plan_total).is_integer()
                    else plan_total
                ),
                "assessment_plan_complete": plan_complete,
                "source_status": source_status,
                "source_clo_row_count": source_clo_row_count,
                "captured_clo_row_count": captured_clo_row_count,
                "warnings": warnings,
                "extraction_status": extraction_status,
                "page_count": len(pdf.pages),
            }
        if sha256_file(source) != expected_source_sha256:
            raise SourceChangedError(f"source changed during extraction: {source_pdf}")
    except SourceChangedError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the failed source explicitly
        detail = clean_text(exc).replace(str(repo_root), "<repo>")
        extracted = {
            "course_name": None,
            "course_name_metadata": {
                "confidence": "low",
                "source_page": 1,
                "extraction_method": "unresolved",
            },
            "clos": [],
            "assessment_plan": [],
            "assessment_plan_total": None,
            "assessment_plan_complete": False,
            "source_status": "unreadable",
            "source_clo_row_count": None,
            "captured_clo_row_count": 0,
            "warnings": [
                make_warning(
                    "pdf_extraction_failed",
                    f"{type(exc).__name__}: {detail}",
                )
            ],
            "extraction_status": "failed",
            "page_count": None,
        }
    return variant_id, extracted


def _source_generated_at(repo_root: Path, explicit: str | None) -> str:
    if explicit:
        return clean_text(explicit)
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return (
            datetime.fromtimestamp(int(epoch), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    process = subprocess.run(
        [
            "git",
            "log",
            "-1",
            "--format=%cI",
            "--",
            "data.json",
            "assets/course-specifications",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    value = clean_text(process.stdout)
    return value or "1970-01-01T00:00:00Z"


def _tesseract_version() -> str | None:
    executable = shutil.which("tesseract")
    if not executable:
        return None
    process = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    first = process.stdout.splitlines()[0] if process.stdout.splitlines() else ""
    return clean_text(first).removeprefix("tesseract ") or None


def _excluded_reason(path: str) -> str:
    name = Path(path).name
    if "/quarantine/" in path:
        return "quarantined_not_published_in_data_json"
    if name == "program-specification.pdf":
        return "program_specification_not_a_course_specification"
    if name.startswith("pending-code-"):
        return "pending_course_code_not_published_in_data_json"
    if "/clo-plo-corrections-20260901/" in path:
        return "corrected_scope_copy_not_selected_by_data_json"
    return "superseded_or_unpublished_pdf_not_referenced_by_data_json"


OVERRIDE_FIELD_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*)*"
)
OVERRIDE_TOKEN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def _existing_output(output: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not output.exists():
        return None, None
    before = sha256_file(output)
    try:
        old = json.loads(
            output.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except FileNotFoundError as exc:
        raise SourceChangedError(
            "course-outcomes.json changed while reading overrides"
        ) from exc
    if sha256_file(output) != before:
        raise SourceChangedError("course-outcomes.json changed while reading overrides")
    if old.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"cannot preserve overrides from unsupported schema {old.get('schema_version')!r}"
        )
    return old, before


def _resolve_override_field(root: Any, field: str) -> Any:
    if not OVERRIDE_FIELD_RE.fullmatch(field):
        raise ValueError(f"invalid override field path: {field!r}")
    current = root
    for match in OVERRIDE_TOKEN_RE.finditer(field):
        key, raw_index = match.groups()
        if key is not None:
            if key == "append" and match.end() == len(field) and isinstance(current, list):
                # ``append`` is an override operation on a list rather than a
                # member of the immutable extracted layer.
                return None
            if (
                key == "deleted"
                and match.end() == len(field)
                and isinstance(current, Mapping)
                and key not in current
            ):
                # ``deleted`` is an override-only tombstone.  It intentionally
                # does not exist in the immutable extracted layer.
                return False
            if not isinstance(current, Mapping) or key not in current:
                raise ValueError(f"override field does not resolve: {field!r}")
            current = current[key]
        else:
            index = int(raw_index)
            if not isinstance(current, list) or index >= len(current):
                raise ValueError(f"override field does not resolve: {field!r}")
            current = current[index]
    return current


def _override_anchor(
    extracted: Mapping[str, Any], field: str
) -> tuple[str, str] | None:
    clo_match = re.match(r"^clos\[(\d+)\](?:\.|$)", field)
    if clo_match:
        index = int(clo_match.group(1))
        clos = extracted.get("clos")
        if not isinstance(clos, list) or index >= len(clos):
            raise ValueError(f"override CLO anchor does not resolve: {field!r}")
        record = clos[index]
        assessment_suffix = (
            f":assessment_source_page={record.get('assessment_source_page')!r}"
            if re.fullmatch(r"clos\[\d+\]\.assessment", field)
            else ""
        )
        code = clean_text(record.get("code"))
        if code:
            duplicate_count = sum(
                clean_text(item.get("code")) == code
                for item in clos
                if isinstance(item, Mapping)
            )
            if duplicate_count == 1:
                return "clo", f"{code}{assessment_suffix}"
            source_page = record.get("source_page")
            text_identity = hashlib.sha256(
                normalized(record.get("text")).encode("utf-8")
            ).hexdigest()[:16]
            return (
                "clo_duplicate",
                f"{code}:{source_page}:{text_identity}{assessment_suffix}",
            )
        source_code = clean_text(record.get("source_code"))
        source_page = record.get("source_page")
        text_identity = hashlib.sha256(
            normalized(record.get("text")).encode("utf-8")
        ).hexdigest()[:16]
        return (
            "clo_source",
            f"{source_page}:{source_code}:{text_identity}{assessment_suffix}",
        )
    plan_match = re.match(r"^assessment_plan\[(\d+)\](?:\.|$)", field)
    if plan_match:
        index = int(plan_match.group(1))
        plan = extracted.get("assessment_plan")
        if not isinstance(plan, list) or index >= len(plan):
            raise ValueError(f"override assessment anchor does not resolve: {field!r}")
        record = plan[index]
        return "assessment", canonical_json(
            {
                "source_page": record.get("source_page"),
                "label": normalized(record.get("label")),
                "weight": record.get("weight"),
            }
        )
    return None


def _carry_overrides(output: Path, courses: Mapping[str, Any]) -> str | None:
    old, fingerprint = _existing_output(output)
    current = {
        variant["variant_id"]: variant
        for course in courses.values()
        for variant in course["variants"]
    }
    for variant in current.values():
        variant["overrides"] = []
    if old is None:
        return fingerprint
    old_variants: dict[str, Mapping[str, Any]] = {}
    for course in (old.get("courses") or {}).values():
        for variant in course.get("variants") or []:
            variant_id = variant.get("variant_id")
            overrides = variant.get("overrides")
            if not isinstance(variant_id, str) or not isinstance(overrides, list):
                raise ValueError("existing output has an invalid override layer")
            if variant_id in old_variants:
                raise ValueError(
                    f"duplicate variant_id in existing output: {variant_id}"
                )
            old_variants[variant_id] = variant
    for variant_id, old_variant in old_variants.items():
        overrides = old_variant["overrides"]
        if not overrides:
            continue
        new_variant = current.get(variant_id)
        if new_variant is None:
            raise ValueError(
                f"refusing to discard non-empty overrides for removed variant {variant_id}"
            )
        if old_variant.get("source_sha256") != new_variant.get("source_sha256"):
            raise ValueError(
                "refusing to carry overrides across a changed source PDF for variant "
                f"{variant_id}; review or remove the overrides explicitly"
            )
        old_extracted = old_variant.get("extracted")
        new_extracted = new_variant["extracted"]
        if not isinstance(old_extracted, Mapping):
            raise ValueError(f"existing extracted layer is invalid for {variant_id}")
        for override in overrides:
            if not isinstance(override, Mapping) or not isinstance(
                override.get("field"), str
            ):
                raise ValueError(f"invalid override record for {variant_id}")
            field = override["field"]
            _resolve_override_field(old_extracted, field)
            _resolve_override_field(new_extracted, field)
            old_anchor = _override_anchor(old_extracted, field)
            new_anchor = _override_anchor(new_extracted, field)
            if old_anchor != new_anchor or (
                old_anchor is not None and not old_anchor[1]
            ):
                raise ValueError(
                    f"override anchor changed for {variant_id} {field!r}: "
                    f"{old_anchor!r} -> {new_anchor!r}"
                )
        new_variant["overrides"] = [dict(item) for item in overrides]
    return fingerprint


def _statistics(courses: Mapping[str, Any], excluded_count: int) -> dict[str, Any]:
    variants = [
        variant
        for course in courses.values()
        for variant in course.get("variants") or []
    ]
    statuses = defaultdict(int)
    variant_source_status_counts = {
        "present": 0,
        "source_blank": 0,
        "unreadable": 0,
    }
    clo_source_status_counts = {
        "present": 0,
        "source_blank": 0,
        "unreadable": 0,
    }
    clo_count = 0
    mappings = 0
    mapped = 0
    plo_code_assignments = 0
    multi_plo_mappings = 0
    explicitly_unmapped = 0
    plo_status_counts: dict[str, int] = {
        "mapped": 0,
        "explicitly_unmapped": 0,
        "not_present_for_scope": 0,
        "missing": 0,
        "conflict": 0,
        "ambiguous_for_scope": 0,
    }
    for variant in variants:
        extracted = variant["extracted"]
        statuses[extracted["extraction_status"]] += 1
        variant_source_status_counts[extracted["source_status"]] += 1
        clos = extracted["clos"]
        clo_count += len(clos)
        for clo in clos:
            clo_source_status_counts[clo["source_status"]] += 1
            for mapping in clo["plo_mappings"]:
                mappings += 1
                plo_status_counts[mapping["status"]] += 1
                if mapping["plo_codes"]:
                    mapped += 1
                    plo_code_assignments += len(mapping["plo_codes"])
                    if len(mapping["plo_codes"]) > 1:
                        multi_plo_mappings += 1
                elif mapping["status"] == "explicitly_unmapped":
                    explicitly_unmapped += 1
    applicable = sum(
        plo_status_counts[status]
        for status in ("mapped", "missing", "conflict", "ambiguous_for_scope")
    )
    resolved_or_explicit_denominator = applicable + explicitly_unmapped
    return {
        "course_codes": len(courses),
        "variants": len(variants),
        "active_pdf_sources": len({variant["source_pdf"] for variant in variants}),
        "excluded_pdf_sources": excluded_count,
        "complete_variants": statuses["complete"],
        "partial_variants": statuses["partial"],
        "failed_variants": statuses["failed"],
        "variant_source_status_counts": variant_source_status_counts,
        "clos": clo_count,
        "clo_source_status_counts": clo_source_status_counts,
        "scoped_clo_mappings": mappings,
        "plo_mappings_with_codes": mapped,
        "plo_code_assignments": plo_code_assignments,
        "multi_plo_mappings": multi_plo_mappings,
        "plo_non_null": mapped,
        "plo_explicitly_unmapped": explicitly_unmapped,
        "plo_non_null_rate": round(mapped / mappings, 6) if mappings else None,
        "plo_status_counts": plo_status_counts,
        "plo_applicable_mappings": applicable,
        "plo_applicable_non_null_rate": (
            round(mapped / applicable, 6) if applicable else None
        ),
        "plo_applicable_with_codes_rate": (
            round(mapped / applicable, 6) if applicable else None
        ),
        "plo_resolved_or_explicit_rate": (
            round(
                (mapped + explicitly_unmapped) / resolved_or_explicit_denominator,
                6,
            )
            if resolved_or_explicit_denominator
            else None
        ),
    }


def build_output(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = args.repo_root.resolve()
    extractor_script = Path(__file__).resolve()
    extractor_script_sha256 = sha256_file(extractor_script)
    font_recovery_script = extractor_script.with_name("pdf_font_recovery.py")
    font_recovery_script_sha256 = sha256_file(font_recovery_script)
    data_path = (
        (repo_root / args.data).resolve() if not args.data.is_absolute() else args.data
    )
    specifications_dir = (
        (repo_root / args.specifications_dir).resolve()
        if not args.specifications_dir.is_absolute()
        else args.specifications_dir
    )
    data_sha256 = sha256_file(data_path)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    if sha256_file(data_path) != data_sha256:
        raise SourceChangedError("data.json changed while reading")
    variants = logical_variants(data)
    auxiliary_lookup, auxiliary_ledger = load_auxiliary_sources(repo_root)

    all_pdfs = {
        path.relative_to(repo_root).as_posix()
        for path in specifications_dir.rglob("*.pdf")
    }
    active_paths = {variant["source_pdf"] for variant in variants}
    unknown_active = sorted(active_paths - all_pdfs)
    if unknown_active:
        raise FileNotFoundError(
            "active sources are outside or absent from specifications directory: "
            + ", ".join(unknown_active)
        )
    inventory_sha256 = {
        relative: sha256_file(repo_root / relative) for relative in sorted(all_pdfs)
    }

    jobs = max(1, args.jobs)
    extracted_by_id: dict[str, dict[str, Any]] = {}
    payloads: list[dict[str, Any]] = []
    for variant in variants:
        source = repo_root / variant["source_pdf"]
        if not source.is_file():
            raise FileNotFoundError(
                f"active source PDF is missing: {variant['source_pdf']}"
            )
        auxiliary = auxiliary_lookup.get(variant["source_pdf"])
        payloads.append(
            {
                **variant,
                "repo_root": str(repo_root),
                "auxiliary": auxiliary,
                "source_sha256": inventory_sha256[variant["source_pdf"]],
            }
        )
    if jobs == 1:
        for payload in payloads:
            variant_id, extracted = extract_variant_worker(payload)
            extracted_by_id[variant_id] = extracted
    else:
        with ProcessPoolExecutor(max_workers=jobs) as executor:
            futures = [
                executor.submit(extract_variant_worker, payload) for payload in payloads
            ]
            for future in as_completed(futures):
                variant_id, extracted = future.result()
                extracted_by_id[variant_id] = extracted

    courses: dict[str, dict[str, Any]] = {}
    for item in variants:
        course_code = item["course_code"]
        variant = {
            "variant_id": item["variant_id"],
            "scopes": item["scopes"],
            "catalog": item["catalog"],
            "source_pdf": item["source_pdf"],
            "source_sha256": inventory_sha256[item["source_pdf"]],
            "extracted": extracted_by_id[item["variant_id"]],
            "overrides": [],
        }
        courses.setdefault(course_code, {"variants": []})["variants"].append(variant)
    courses = {
        code: {
            "variants": sorted(
                record["variants"], key=lambda variant: variant["variant_id"]
            )
        }
        for code, record in sorted(courses.items())
    }
    current_pdfs = {
        path.relative_to(repo_root).as_posix()
        for path in specifications_dir.rglob("*.pdf")
    }
    if current_pdfs != all_pdfs:
        raise SourceChangedError(
            "course-specification PDF inventory changed during extraction"
        )
    changed_sources = [
        relative
        for relative, expected in inventory_sha256.items()
        if sha256_file(repo_root / relative) != expected
    ]
    if changed_sources:
        raise SourceChangedError(
            "course-specification PDFs changed during extraction: "
            + ", ".join(changed_sources)
        )
    if sha256_file(data_path) != data_sha256:
        raise SourceChangedError("data.json changed during extraction")
    for record in auxiliary_ledger:
        if sha256_file(repo_root / record["path"]) != record["sha256"]:
            raise SourceChangedError(
                f"auxiliary manifest changed during extraction: {record['path']}"
            )
    excluded = [
        {
            "source_pdf": path,
            "source_sha256": inventory_sha256[path],
            "reason": _excluded_reason(path),
        }
        for path in sorted(all_pdfs - active_paths)
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _source_generated_at(repo_root, args.generated_at),
        "extractor": {
            "name": EXTRACTOR_NAME,
            "version": EXTRACTOR_VERSION,
            "script": extractor_script.relative_to(repo_root).as_posix(),
            "script_sha256": extractor_script_sha256,
            "font_recovery_script": font_recovery_script.relative_to(
                repo_root
            ).as_posix(),
            "font_recovery_script_sha256": font_recovery_script_sha256,
            "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            "pymupdf": getattr(pymupdf, "VersionBind", "unknown"),
            "tesseract": _tesseract_version(),
        },
        "source_data": {
            "path": data_path.relative_to(repo_root).as_posix(),
            "sha256": data_sha256,
        },
        "auxiliary_sources": auxiliary_ledger,
        "courses": courses,
        "excluded_sources": excluded,
    }
    result["statistics"] = _statistics(courses, len(excluded))
    return result


def _assert_payload_sources_current(
    payload: Mapping[str, Any],
    repo_root: Path,
    specifications_dir: Path,
) -> None:
    extractor = payload["extractor"]
    extractor_script = repo_root / extractor["script"]
    if sha256_file(extractor_script) != extractor["script_sha256"]:
        raise SourceChangedError("extractor script changed during extraction")
    font_recovery_script = repo_root / extractor["font_recovery_script"]
    if sha256_file(font_recovery_script) != extractor["font_recovery_script_sha256"]:
        raise SourceChangedError("font-recovery script changed during extraction")
    source_data = payload["source_data"]
    if sha256_file(repo_root / source_data["path"]) != source_data["sha256"]:
        raise SourceChangedError("data.json changed after output verification")
    expected_pdfs: dict[str, str] = {
        variant["source_pdf"]: variant["source_sha256"]
        for course in payload["courses"].values()
        for variant in course["variants"]
    }
    expected_pdfs.update(
        {
            item["source_pdf"]: item["source_sha256"]
            for item in payload["excluded_sources"]
        }
    )
    current_paths = {
        path.relative_to(repo_root).as_posix()
        for path in specifications_dir.rglob("*.pdf")
    }
    if current_paths != set(expected_pdfs):
        raise SourceChangedError(
            "course-specification PDF inventory changed after output verification"
        )
    changed = [
        path
        for path, expected in sorted(expected_pdfs.items())
        if sha256_file(repo_root / path) != expected
    ]
    if changed:
        raise SourceChangedError(
            "course-specification PDFs changed after output verification: "
            + ", ".join(changed)
        )
    for item in payload["auxiliary_sources"]:
        if sha256_file(repo_root / item["path"]) != item["sha256"]:
            raise SourceChangedError(
                f"auxiliary manifest changed after output verification: {item['path']}"
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument(
        "--data", type=Path, default=Path("data.json"), help="site data JSON"
    )
    parser.add_argument(
        "--specifications-dir",
        type=Path,
        default=Path("assets/course-specifications"),
        help="directory whose PDF inventory must be fully accounted for",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("course-outcomes.json"),
        help="deterministic output JSON; existing overrides are preserved",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel PDF workers (default: min(8, CPU count))",
    )
    parser.add_argument(
        "--generated-at",
        help="fixed ISO-8601 source timestamp; defaults to the latest source-data Git commit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    output_path = (
        (args.repo_root.resolve() / args.output).resolve()
        if not args.output.is_absolute()
        else args.output.resolve()
    )
    payload = build_output(args)
    output_fingerprint = _carry_overrides(output_path, payload["courses"])
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        repo_root = args.repo_root.resolve()
        verifier = repo_root / "scripts" / "verify_course_outcomes.py"
        try:
            temporary_relative = Path(temporary_name).resolve().relative_to(repo_root)
        except ValueError as exc:
            raise ValueError("--output must be inside --repo-root") from exc
        verified = subprocess.run(
            [
                sys.executable,
                str(verifier),
                "--root",
                str(repo_root),
                "--data",
                str(payload["source_data"]["path"]),
                "--outcomes",
                temporary_relative.as_posix(),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        if verified.returncode:
            raise RuntimeError(
                "refusing to replace course-outcomes.json because verification failed:\n"
                + clean_text(verified.stderr or verified.stdout)
            )
        specifications_dir = (
            (repo_root / args.specifications_dir).resolve()
            if not args.specifications_dir.is_absolute()
            else args.specifications_dir.resolve()
        )
        _assert_payload_sources_current(payload, repo_root, specifications_dir)
        current_fingerprint = sha256_file(output_path) if output_path.exists() else None
        if current_fingerprint != output_fingerprint:
            raise SourceChangedError(
                "course-outcomes.json changed after overrides were loaded; retry extraction"
            )
        os.replace(temporary_name, output_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(canonical_json(payload["statistics"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
