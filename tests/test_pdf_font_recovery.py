"""Public synthetic-PDF tests; no university file, OCR, or model is required."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pdfplumber
import pymupdf

from scripts.equivalency import pdf_font_recovery as recovery
from scripts.equivalency.extract import norm
from scripts.extract_course_outcomes import logical_region_text


class FakeCmap:
    def __init__(self, entries):
        self.entries = entries

    def valid_codepoints(self):
        return list(self.entries)

    def has_glyph(self, codepoint, **kwargs):
        assert kwargs == {'fallback': False}
        return self.entries[codepoint]


class FontRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.font_file = Path(__file__).resolve().parents[1] / 'equivalency-pilot/fonts/IBMPlexSansArabic-Regular.ttf'

    def fixture(self, *, embedded=True, lines=('Topic 3',)):
        """The font still draws 3; only the PDF ToUnicode reports 6."""
        doc = pymupdf.open()
        page = doc.new_page()
        for i, text in enumerate(lines):
            kw = {'fontfile': str(self.font_file), 'fontname': 'IBMPlex'} if embedded else {'fontname': 'helv'}
            page.insert_text((50, 80+i*50), text, fontsize=16, **kw)
        fontref = page.get_fonts()[0][0]
        font = pymupdf.Font(fontbuffer=doc.extract_font(fontref)[3]) if embedded else None
        entries = []
        for character in sorted(set(''.join(lines))):
            cid = font.has_glyph(ord(character)) if embedded else ord(character)
            reported = '6' if character == '3' else character
            entries.append(f'<{cid:04X}> <{ord(reported):04X}>')
        cmap = ('/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n'
                '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                '/CMapName /Synthetic-Test def\n/CMapType 2 def\n'
                '1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n'
                f'{len(entries)} beginbfchar\n'+'\n'.join(entries)+'\nendbfchar\n'
                'endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend').encode()
        kind, value = doc.xref_get_key(fontref, 'ToUnicode')
        if kind == 'xref':
            to_unicode = int(value.split()[0])
        else:
            to_unicode = doc.get_new_xref()
            doc.update_object(to_unicode, '<<>>')
            doc.xref_set_key(fontref, 'ToUnicode', f'{to_unicode} 0 R')
        doc.update_stream(to_unicode, cmap)
        raw = doc.tobytes(garbage=4, deflate=True)
        doc.close()
        path = self.root / ('embedded.pdf' if embedded else 'nonembedded.pdf')
        path.write_bytes(raw)
        topics = []
        with pdfplumber.open(io.BytesIO(raw)) as pl:
            for i in range(len(lines)):
                bbox = [40, 50+i*50, 250, 100+i*50]
                topics.append({'id': f't{i+1}', 'text': norm(logical_region_text(pl.pages[0], bbox)),
                               'page': 1, 'bbox': bbox, 'hours_raw': '2', 'custom': {'keep': [i]}})
        return path, raw, topics

    def test_real_embedded_glyph_repairs_corrupted_tounicode_with_proof_and_no_pdf_mutation(self):
        path, raw, topics = self.fixture()
        self.assertEqual(topics[0]['text'], 'Topic 6')
        with pymupdf.open(stream=raw, filetype='pdf') as doc:
            trace = next(c for span in doc[0].get_texttrace() for c in span['chars'] if c[0] == ord('6'))
            real_font = pymupdf.Font(fontbuffer=doc.extract_font(doc[0].get_fonts()[0][0])[3])
            self.assertEqual(trace[1], real_font.has_glyph(ord('3')))
        before = copy.deepcopy(topics)
        result, proof = recovery.recover_topic_texts(path, topics)
        self.assertEqual(result[0]['corrected_text'], 'Topic 3')
        self.assertEqual(result[0]['text'], 'Topic 6')
        self.assertEqual(topics, before)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(proof['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(proof['input_topics_sha256'], recovery.topics_sha256(topics))
        self.assertEqual(proof['output_topics_sha256'], recovery.topics_sha256(result))
        self.assertEqual(proof['status'], 'recovered')
        self.assertEqual(len(proof['changes']), 1)
        change = proof['changes'][0]
        self.assertEqual((change['old'], change['new']), ('6', '3'))
        self.assertEqual(change['old_codepoints'], ['U+0036'])
        self.assertEqual(change['new_codepoints'], ['U+0033'])
        self.assertEqual(change['gid'], trace[1])
        self.assertEqual(change['trace_gid'], trace[1])
        self.assertEqual(change['font_type'], 'Type0')
        self.assertEqual(change['font_encoding'], 'Identity-H')
        self.assertEqual(len(change['font_sha256']), 64)
        self.assertEqual(len(change['origin']), 2)

    def test_path_read_once_bytes_read_zero_and_provenance_deterministic(self):
        path, raw, topics = self.fixture()
        read = Path.read_bytes
        calls = []
        def tracked(p):
            calls.append(p)
            return read(p)
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=tracked):
            result = recovery.recover_topic_texts(path, topics)
        self.assertEqual(calls, [path])
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('bytes input must not read a path')):
            again = recovery.recover_topic_texts(raw, topics)
        self.assertEqual(result, again)

    def test_decimal_aliases_agree_but_full_latin_or_punctuation_alias_set_abstains(self):
        aliases = recovery._invert_cmap(FakeCmap({0x663: 7, 0x6F3: 7}))
        self.assertEqual(aliases[7]['text'], '3')
        self.assertEqual(aliases[7]['codepoints'], [0x663, 0x6F3])
        for extra in [ord('I'), ord('/')]:
            with self.subTest(extra=extra):
                ambiguous = recovery._invert_cmap(FakeCmap({ord('1'): 7, extra: 7}))[7]
                self.assertIsNone(ambiguous['text'])
                self.assertEqual(ambiguous['reason'], 'ambiguous_cmap')
                self.assertEqual(len(ambiguous['aliases']), 2)

    def test_nondecimal_numeric_alias_cannot_become_an_ordinary_digit(self):
        for entries in [{0xB3: 7}, {ord('3'): 7, 0xB3: 7}, {0x2160: 7}]:
            with self.subTest(entries=entries):
                self.assertIsNone(recovery._invert_cmap(FakeCmap(entries))[7]['text'])

    def test_unique_punctuation_is_not_reinterpreted_from_bidi_glyphs(self):
        self.assertIsNone(recovery._invert_cmap(FakeCmap({ord('('): 7}))[7]['text'])

    def test_mismatched_effective_baseline_never_replaces_existing_review(self):
        _, raw, topics = self.fixture()
        topics[0]['corrected_text'] = 'Existing reviewed wording'
        before = copy.deepcopy(topics)
        result, proof = recovery.recover_topic_texts(raw, topics)
        self.assertEqual(result, before)
        self.assertEqual(topics, before)
        self.assertEqual(proof['topics'][0]['reason'], 'effective_text_does_not_match_original_cell')
        self.assertEqual(proof['changes'], [])

    def test_nonembedded_only_font_abstains(self):
        path, raw, topics = self.fixture(embedded=False)
        before = copy.deepcopy(topics)
        result, proof = recovery.recover_topic_texts(path, topics)
        self.assertEqual(result, before)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(proof['changes'], [])
        self.assertFalse(any(f['status'] == 'embedded' for f in proof['fonts']))

    def test_nonembedded_winansi_shadow_does_not_compete_with_eligible_identity_font(self):
        _, raw, topics = self.fixture()
        with pymupdf.open(stream=raw, filetype='pdf') as doc:
            page = doc[0]
            name = doc.xref_get_key(page.get_fonts()[0][0], 'BaseFont')[1].replace(' ', '#20')
            shadow = page.insert_font(fontname='helv')
            doc.xref_set_key(shadow, 'BaseFont', name)
            descriptor = doc.get_new_xref()
            doc.update_object(descriptor, '<< /Type /FontDescriptor /FontName '+name+
                              ' /Flags 32 /FontBBox [0 -300 1200 1200] /ItalicAngle 0'
                              ' /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>')
            doc.xref_set_key(shadow, 'FontDescriptor', f'{descriptor} 0 R')
            self.assertFalse(doc.extract_font(shadow)[3])
            self.assertEqual(next(f[5] for f in page.get_fonts() if f[0] == shadow), 'WinAnsiEncoding')
            combined = doc.tobytes()
        result, _ = recovery.recover_topic_texts(combined, topics)
        self.assertEqual(result[0]['corrected_text'], 'Topic 3')

    def test_two_eligible_embedded_font_candidates_abstain_even_if_one_would_repair(self):
        _, raw, topics = self.fixture()
        with pymupdf.open(stream=raw, filetype='pdf') as doc:
            page = doc[0]
            first = page.get_fonts()[0][0]
            first_font = pymupdf.Font(fontbuffer=doc.extract_font(first)[3])
            second = page.insert_font(fontname='OtherEmbedded', fontbuffer=pymupdf.Font('helv').buffer)
            second_font = pymupdf.Font(fontbuffer=doc.extract_font(second)[3])
            gid = first_font.has_glyph(ord('3'))
            self.assertNotEqual(second_font.has_glyph(ord('3')), gid)
            self.assertIn(ord('_'), [cp for cp in second_font.valid_codepoints()
                                    if second_font.has_glyph(cp, fallback=False) == gid])
            name = doc.xref_get_key(first, 'BaseFont')[1].replace(' ', '#20')
            doc.xref_set_key(second, 'BaseFont', name)
            self.assertEqual(len([f for f in page.get_fonts() if f[2] == 'Type0' and f[5] == 'Identity-H']), 2)
            combined = doc.tobytes()
        result, proof = recovery.recover_topic_texts(combined, topics)
        self.assertEqual(result, topics)
        self.assertEqual(proof['changes'], [])

    def test_ambiguous_origin_abstains_instead_of_voting_on_equal_values(self):
        _, raw, topics = self.fixture()
        real = pymupdf.Page.get_texttrace
        def duplicate(page):
            traces = copy.deepcopy(real(page))
            for span in traces:
                digits = [c for c in span['chars'] if c[0] == ord('6')]
                if digits:
                    span['chars'] = tuple(span['chars']) + (digits[0],)
            return traces
        with patch.object(pymupdf.Page, 'get_texttrace', autospec=True, side_effect=duplicate):
            result, proof = recovery.recover_topic_texts(raw, topics)
        self.assertEqual(result, topics)
        self.assertEqual(proof['changes'], [])

    def test_geometry_mismatch_retains_raw_text(self):
        _, raw, topics = self.fixture()
        with pdfplumber.open(io.BytesIO(raw)) as doc:
            page = doc.pages[0]
            chars = copy.deepcopy(page.chars)
            for c in chars:
                matrix = list(c['matrix'])
                matrix[4] += 1
                c['matrix'] = tuple(matrix)
            fake_page = SimpleNamespace(chars=chars, bbox=page.bbox, width=page.width, height=page.height)
        class FakePDF:
            pages = [fake_page]
            def __enter__(self): return self
            def __exit__(self, *args): return None
        with patch.object(recovery.pdfplumber, 'open', return_value=FakePDF()):
            result, proof = recovery.recover_topic_texts(raw, topics)
        self.assertEqual(result, topics)
        self.assertEqual(proof['changes'], [])

    def test_fragments_deduplicate_same_region_and_keep_topic_identity_order_raw_fields(self):
        _, raw, topics = self.fixture(lines=('Topic 3', 'Next 3'))
        topic = copy.deepcopy(topics[0])
        topic['text'] += ' ' + topics[1]['text']
        topic['fragments'] = [{'page': 1, 'bbox': topic['bbox']}, {'page': 1, 'bbox': topics[1]['bbox']}]
        original = [topic]
        before = copy.deepcopy(original)
        result, proof = recovery.recover_topic_texts(raw, original)
        self.assertEqual(result[0]['corrected_text'], 'Topic 3 Next 3')
        self.assertEqual(len(result), 1)
        self.assertEqual(len(proof['changes']), 2)
        self.assertEqual({k: v for k, v in result[0].items() if k != 'corrected_text'}, before[0])
        self.assertEqual(original, before)

    def test_invalid_regions_or_duplicate_ids_abstain_without_mutating_topics(self):
        _, raw, topics = self.fixture()
        for modified in [[{**topics[0], 'bbox': [0, 0, 9000, 9000]}], [topics[0], topics[0]],
                         [{**topics[0], 'page': 999}]]:
            with self.subTest(modified=modified):
                before = copy.deepcopy(modified)
                result, proof = recovery.recover_topic_texts(raw, modified)
                self.assertEqual(result, before)
                self.assertEqual(modified, before)
                self.assertEqual(proof['changes'], [])

    def test_declared_but_unlocated_continuation_cannot_be_recovered_from_first_page_only(self):
        _, raw, topics = self.fixture()
        topics[0]['end_page'] = 2
        result, proof = recovery.recover_topic_texts(raw, topics)
        self.assertEqual(result, topics)
        self.assertEqual(proof['topics'][0]['reason'], 'unlocated_continuation')


if __name__ == '__main__':
    unittest.main()
