"""Selective, evidence-bound recovery from an embedded font's own Unicode cmap.

No OCR, linguistic substitutions, or neighbouring-course text is used. A font
glyph may fix a pdfplumber character only when its font resource and origin are
unique. A topic is updated only after its original cell text reconstructs the
caller's effective text exactly. The raw topic and all other fields survive.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
import unicodedata

import pdfplumber
import pymupdf

from scripts.extract_course_outcomes import logical_region_text
from scripts.pdf_font_recovery import _font_key, _is_non_rendering_placeholder
from .extract import norm

VERSION = 'embedded-font-topic-recovery-v1'
ORIGIN_TOLERANCE = 0.1  # PDF points, not pixels.
MAX_PDF_BYTES = 32 * 1024 * 1024
MAX_TOPICS = 100
MAX_SELECTED_PAGES = 24
MAX_FONT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_FONT_BYTES = 64 * 1024 * 1024
MAX_FONTS = 64
MAX_CODEPOINTS = 65536
MAX_PAGE_CHARS = 100000


class _Abstain(ValueError):
    """Stable reason codes only; library exceptions must not leak into hashes."""


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def topics_sha256(value):
    """Same canonical JSON convention as the visual-intake cache binding."""
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def _canonical(codepoint):
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                   for c in unicodedata.normalize('NFKC', chr(codepoint)))


def _eligible(text):
    # Bidi shaping can mirror punctuation glyphs independently of logical
    # punctuation. It is not safe to reinterpret those from cmap alone.
    return bool(text) and len(text) <= 32 and all(
        c == ' ' or c.isdecimal() or
        (unicodedata.category(c)[0] in 'LM' and not c.isnumeric()) for c in text)


def _invert_cmap(font):
    """Consider ALL aliases before eligibility, including Latin/punctuation.

    Superscripts and other nondecimal numeric codepoints must not become plain
    digits through NFKC. Decimal aliases alone can share their decimal value.
    """
    codes = sorted(set(font.valid_codepoints()))
    if len(codes) > MAX_CODEPOINTS:
        raise _Abstain('font_codepoint_limit')
    aliases = defaultdict(lambda: {'values': set(), 'codepoints': [], 'nondecimal': False})
    for code in codes:
        if type(code) is not int or not 0 <= code <= 0x10FFFF:
            raise _Abstain('font_codepoint_invalid')
        gid = int(font.has_glyph(code, fallback=False) or 0)
        if gid <= 0:
            continue
        item = aliases[gid]
        item['values'].add(_canonical(code))
        item['codepoints'].append(code)
        item['nondecimal'] |= chr(code).isnumeric() and not chr(code).isdecimal()
    result = {}
    for gid, item in aliases.items():
        values = sorted(item['values'])
        value = values[0] if len(values) == 1 else None
        result[gid] = {'text': value if value is not None and not item['nondecimal'] and _eligible(value) else None,
                       'aliases': values, 'codepoints': item['codepoints'],
                       'reason': 'ambiguous_cmap' if len(values) != 1 else 'ineligible_cmap'}
    return result


def _regions(topic):
    candidates = [(topic.get('page'), topic.get('bbox'))]
    fragments = topic.get('fragments', [])
    if not isinstance(fragments, list):
        raise _Abstain('invalid_fragments')
    candidates += [(f.get('page'), f.get('bbox')) for f in fragments if isinstance(f, dict)]
    if len(fragments) != sum(isinstance(f, dict) for f in fragments):
        raise _Abstain('invalid_fragments')
    regions = []
    for page, bbox in candidates:
        if (type(page) is not int or page < 1 or not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in bbox)):
            raise _Abstain('unlocated_region')
        rect = tuple(float(v) for v in bbox)
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise _Abstain('invalid_region')
        pair = (page, rect)
        if pair not in regions:
            regions.append(pair)
    if any(a[0] > b[0] for a, b in zip(regions, regions[1:])):
        raise _Abstain('unordered_fragments')
    last = topic.get('end_page', topic.get('page'))
    if type(last) is not int or last < topic['page']:
        raise _Abstain('invalid_continuation')
    covered = {p for p, _ in regions}
    if not set(range(min(covered), max(last, max(covered))+1)) <= covered:
        raise _Abstain('unlocated_continuation')
    return regions


def _inside(character, regions):
    x = (float(character['x0']) + float(character['x1'])) / 2
    y = (float(character['top']) + float(character['bottom'])) / 2
    return any(a <= x < c and b <= y < d for a, b, c, d in regions)


def recover_topic_texts(pdf, topics):
    """Return ``(deepcopied_topics, deterministic_provenance)``.

    ``pdf`` is bytes or a filesystem path. The path is read exactly once; both
    PDF libraries consume that same byte string. No readiness/quality status is
    conferred. Partial proven glyph fixes may coexist with untouched ambiguous
    glyphs; the subsequent independent visual verification remains necessary.
    """
    output = copy.deepcopy(topics)
    input_sha = topics_sha256(topics)
    raw = pdf if isinstance(pdf, bytes) else Path(pdf).read_bytes()
    proof = {'version': VERSION, 'source_sha256': _sha(raw), 'input_topics_sha256': input_sha,
             'output_topics_sha256': input_sha, 'origin_tolerance_points': ORIGIN_TOLERANCE,
             'status': 'unchanged', 'fonts': [], 'topics': [], 'changes': []}
    if (not isinstance(topics, list) or len(topics) > MAX_TOPICS or
            any(not isinstance(t, dict) or not isinstance(t.get('id'), str) or not t['id'] for t in topics) or
            len({t['id'] for t in topics}) != len(topics) or len(raw) > MAX_PDF_BYTES):
        proof.update(status='abstained', reason='input_limits_or_identity')
        return output, proof
    if not topics:
        return output, proof
    regions = {}
    reasons = {}
    for i, topic in enumerate(topics):
        try:
            effective = topic.get('corrected_text') or topic['text']
            if not isinstance(effective, str) or not effective or len(effective) > 4000:
                raise _Abstain('invalid_effective_text')
            regions[i] = _regions(topic)
        except (ValueError, KeyError, TypeError) as error:
            reasons[i] = str(error) if isinstance(error, _Abstain) else 'invalid_topic'
    pages = sorted({p for r in regions.values() for p, _ in r})
    if len(pages) > MAX_SELECTED_PAGES:
        proof.update(status='abstained', reason='selected_page_limit')
        return output, proof
    font_cache = {}
    resource_names = defaultdict(set)
    total_font_bytes = 0
    views = {}

    def load_font(document, xref):
        nonlocal total_font_bytes
        if xref in font_cache:
            return font_cache[xref]
        info = {'xref': xref, 'status': 'abstained'}
        entry = {'info': info, 'glyphs': {}}
        font_cache[xref] = entry
        if len(font_cache) > MAX_FONTS:
            info['reason'] = 'font_count_limit'
            return entry
        try:
            data = document.extract_font(xref)[3]
            if not data:
                raise _Abstain('nonembedded_font')
            info.update(font_sha256=_sha(data), bytes=len(data))
            total_font_bytes += len(data)
            if len(data) > MAX_FONT_BYTES or total_font_bytes > MAX_TOTAL_FONT_BYTES:
                raise _Abstain('font_size_limit')
            font = pymupdf.Font(fontbuffer=data)
            entry['glyphs'] = _invert_cmap(font)
            info.update(status='embedded', glyphs=len(entry['glyphs']),
                        ambiguous_glyphs=sum(g['reason'] == 'ambiguous_cmap' for g in entry['glyphs'].values()))
        except Exception as error:
            info['reason'] = str(error) if isinstance(error, _Abstain) else 'font_program_unavailable'
        return entry

    try:
        with pymupdf.open(stream=raw, filetype='pdf') as mu, pdfplumber.open(io.BytesIO(raw)) as pl:
            if mu.needs_pass or any(p > len(mu) or p > len(pl.pages) for p in pages):
                raise _Abstain('pdf_pages_unavailable')
            for number in pages:
                page, pp = mu[number-1], pl.pages[number-1]
                if (page.rotation or abs(pp.width-page.rect.width) > ORIGIN_TOLERANCE or
                        abs(pp.height-page.rect.height) > ORIGIN_TOLERANCE or pp.bbox[:2] != (0, 0)):
                    views[number] = {'reason': 'page_geometry_mismatch'}
                    continue
                chars = pp.chars
                traces = page.get_texttrace()
                if len(chars) > MAX_PAGE_CHARS or sum(len(s['chars']) for s in traces) > MAX_PAGE_CHARS:
                    views[number] = {'reason': 'page_character_limit'}
                    continue
                resources = defaultdict(set)
                for resource in page.get_fonts(full=True):
                    # Only Identity-H Type0 resources expose the glyph-id
                    # contract used here (same restriction as the older strict
                    # recovery helper). A simple WinAnsi font's character code
                    # must never be treated as this embedded CID font's gid.
                    if resource[2] == 'Type0' and resource[5] == 'Identity-H':
                        resources[_font_key(resource[3])].add(resource[0])
                        resource_names[resource[0]].add(resource[3])
                positions = defaultdict(list)
                for span in traces:
                    key = _font_key(span['font'])
                    refs = resources.get(key, set())
                    xref = next(iter(refs)) if key and len(refs) == 1 else None
                    for character in span['chars']:
                        if _is_non_rendering_placeholder(character):
                            continue
                        x, y = map(float, character[2])
                        if not math.isfinite(x+y):
                            continue
                        bucket = (key, math.floor(x/ORIGIN_TOLERANCE), math.floor(y/ORIGIN_TOLERANCE))
                        positions[bucket].append((x, y, character[1], xref, character[0]))
                corrected, changes, blocked = [], [], []
                for index, original in enumerate(chars):
                    item = dict(original)
                    issue = None
                    matrix = original.get('matrix')
                    match = None
                    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
                        issue = 'missing_origin'
                    else:
                        x, y = float(matrix[4]), pp.height-float(matrix[5])
                        key = _font_key(original.get('fontname'))
                        candidates = []
                        if math.isfinite(x+y):
                            bx, by = math.floor(x/ORIGIN_TOLERANCE), math.floor(y/ORIGIN_TOLERANCE)
                            for dx in (-1, 0, 1):
                                for dy in (-1, 0, 1):
                                    candidates.extend(t for t in positions[(key, bx+dx, by+dy)]
                                                      if abs(t[0]-x) < ORIGIN_TOLERANCE and abs(t[1]-y) < ORIGIN_TOLERANCE)
                        if len(candidates) != 1:
                            issue = 'origin_not_unique_or_unmatched'
                        else:
                            match = candidates[0]
                            if match[3] is None:
                                issue = 'font_resource_not_unique_or_unmatched'
                    if issue is None:
                        font = load_font(mu, match[3])
                        glyph = font['glyphs'].get(match[2])
                        if not glyph or glyph['text'] is None:
                            issue = glyph['reason'] if glyph else font['info'].get('reason', 'unmapped_glyph')
                        elif _canonical_text(original['text']) != glyph['text']:
                            item['text'] = glyph['text']
                            changes.append({'character_index': index, 'page': number, 'font_xref': match[3],
                                            'font_sha256': font['info']['font_sha256'], 'gid': match[2],
                                            'font_type': 'Type0', 'font_encoding': 'Identity-H',
                                            'trace_gid': match[2], 'trace_unicode': match[4],
                                            'origin': [match[0], match[1]], 'old': original['text'], 'new': glyph['text'],
                                            'old_codepoints': _codepoints(original['text']),
                                            'new_codepoints': _codepoints(glyph['text']),
                                            'cmap_codepoints': [f'U+{cp:04X}' for cp in glyph['codepoints']],
                                            'canonical_aliases': glyph['aliases']})
                    if issue:
                        blocked.append((index, issue))
                    corrected.append(item)
                views[number] = {'original': pp, 'corrected': SimpleNamespace(bbox=pp.bbox, chars=corrected),
                                 'changes': changes, 'blocked': blocked, 'chars': chars}
            applied = {}
            for i, topic in enumerate(topics):
                effective = topic.get('corrected_text') or topic.get('text', '')
                record = {'id': topic['id'], 'status': 'abstained', 'before': effective, 'after': effective, 'changes': []}
                proof['topics'].append(record)
                if i in reasons:
                    record['reason'] = reasons[i]
                    continue
                rr = regions[i]
                bad = next((views[p].get('reason') for p, _ in rr if views[p].get('reason')), None)
                if bad:
                    record['reason'] = bad
                    continue
                # Bounds are checked before calling the region helper, which otherwise clips.
                if any(not mu[p-1].rect.contains(pymupdf.Rect(box)) for p, box in rr):
                    record['reason'] = 'region_outside_page'
                    continue
                before = norm(' '.join(logical_region_text(views[p]['original'], box) for p, box in rr))
                record['reconstructed_original'] = before
                if before != effective:
                    record['reason'] = 'effective_text_does_not_match_original_cell'
                    continue
                after = norm(' '.join(logical_region_text(views[p]['corrected'], box) for p, box in rr))
                if not after or len(after) > 4000:
                    record['reason'] = 'recovered_text_limits'
                    continue
                selected, unresolved = [], Counter()
                for page in sorted({p for p, _ in rr}):
                    boxes = [box for p, box in rr if p == page]
                    view = views[page]
                    for change in view['changes']:
                        if _inside(view['chars'][change['character_index']], boxes):
                            selected.append(change)
                    for index, reason in view['blocked']:
                        if _inside(view['chars'][index], boxes):
                            unresolved[reason] += 1
                record['unresolved_glyphs'] = dict(sorted(unresolved.items()))
                if after == before:
                    record.update(status='abstained' if unresolved else 'unchanged', reason='no_proven_text_change')
                    continue
                if not selected:
                    record['reason'] = 'no_proven_glyph_change'
                    continue
                output[i]['corrected_text'] = after
                record.update(status='recovered', after=after, changes=selected)
                for change in selected:
                    applied[(change['page'], change['character_index'])] = change
            proof['changes'] = [applied[k] for k in sorted(applied)]
    except Exception as error:
        # No partial changes survive a document-level failure.
        output = copy.deepcopy(topics)
        proof.update(status='abstained', reason=str(error) if isinstance(error, _Abstain) else 'pdf_recovery_unavailable',
                     changes=[], topics=[])
    proof['fonts'] = [{**font_cache[x]['info'], 'font_type': 'Type0', 'encoding': 'Identity-H',
                       'base_names': sorted(resource_names[x])} for x in sorted(font_cache)]
    proof['output_topics_sha256'] = topics_sha256(output)
    proof['status'] = 'recovered' if proof['changes'] else 'abstained' if proof.get('reason') or any(t['status'] == 'abstained' for t in proof['topics']) else 'unchanged'
    return output, proof


def _canonical_text(text):
    return ''.join(str(unicodedata.decimal(c)) if c.isdecimal() else c
                   for c in unicodedata.normalize('NFKC', text))


def _codepoints(text):
    return [f'U+{ord(c):04X}' for c in text]
