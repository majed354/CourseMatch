"""Complete-reference audit and independent row-number geometry regressions."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pymupdf

from scripts.audit_catalog_references import audit_one, classify, inventory, text_quality, complete_page_evidence, refresh_page_coverage
from scripts.equivalency.extract import extract_pdf, mixed_assessment_reading, pure_assessment_text, _prefer_numbered_grid
from scripts.equivalency.recovery import numbered_grid

ROOT = Path(__file__).resolve().parents[1]


class ReferenceAuditTests(unittest.TestCase):
    def coverage_fixture(self, root):
        (root / 'assets').mkdir()
        path = root / 'assets/source.pdf'
        with pymupdf.open() as document:
            for _ in range(3):
                document.new_page()
            path.write_bytes(document.tobytes())
        return {'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'source_pdfs': ['assets/source.pdf'], 'course_ids': ['a'], 'review_status': 'needs_visual', 'risk_priority': 25, 'risk_reasons': ['incomplete_native_proof'], 'review_pages': [1], 'native_candidate': {'topics': [{'id': 't1', 'text': 'نص موضوع', 'page': 1, 'bbox': [100, 100, 400, 150]}], 'table_evidence': []}}

    def test_unclosed_extraction_reviews_following_pages_to_pdf_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.coverage_fixture(root)
            record['review_pages'] = [1, 3]
            complete_page_evidence(root, record)
            self.assertEqual(record['review_pages'], [1, 2, 3])
            self.assertIn('coverage_extension_reason', record)

    def test_blank_numbered_or_clipped_rows_are_unresolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.coverage_fixture(root)
            record['native_candidate']['topics'][0]['bbox'][3] = 900
            record['native_candidate']['table_evidence'] = [{'page': 1, 'topic_column': [100, 400], 'table_bbox': [50, 50, 450, 170], 'total': None, 'empty_rows': [{'label': '2', 'bbox': [100, 150, 400, 170]}]}]
            complete_page_evidence(root, record)
            self.assertEqual(record['review_status'], 'unresolved')
            self.assertEqual(len(record['blank_numbered_rows']), 1)
            self.assertEqual(len(record['clipped_topic_rows']), 1)
            self.assertGreaterEqual(record['risk_priority'], 1000)

    def test_coverage_refresh_preserves_the_original_audit_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = self.coverage_fixture(root)
            source, output = root / 'saved', root / 'refreshed'
            (source / 'records').mkdir(parents=True)
            index = {'records': [{'source_sha256': record['source_sha256']}], 'catalog_id': 'baseline', 'unique_sha_count': 1}
            raw = json.dumps(index).encode()
            (source / 'index.json').write_bytes(raw)
            (source / 'catalog-input-snapshot.json').write_text('{"frozen":true}')
            (source / 'records' / (record['source_sha256'] + '.json')).write_text(json.dumps(record))
            result = refresh_page_coverage(root, source, output, render_contacts=False)
            self.assertEqual((source / 'index.json').read_bytes(), raw)
            self.assertEqual((output / 'catalog-input-snapshot.json').read_bytes(), (source / 'catalog-input-snapshot.json').read_bytes())
            self.assertEqual(result['records'][0]['review_pages'], [1, 2, 3])
            self.assertEqual(result['native_extraction_source_index_sha256'], hashlib.sha256(raw).hexdigest())

    def test_inventory_deduplicates_bytes_without_losing_scope_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'equivalency-pilot').mkdir()
            courses = [{'id': 'a', 'source_sha256': 'same', 'source_pdf': 'assets/one.pdf', 'topics': []},
                       {'id': 'b', 'source_sha256': 'same', 'source_pdf': 'assets/copy.pdf', 'topics': []},
                       {'id': 'unavailable', 'topics': []}]
            (root / 'equivalency-pilot/catalog.json').write_text(json.dumps({'courses': courses}))
            catalog, tasks = inventory(root)
            self.assertEqual(len(catalog['courses']), 3)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0][2], ['assets/copy.pdf', 'assets/one.pdf'])
            self.assertEqual([course['id'] for course in tasks[0][3]], ['a', 'b'])

    def test_automatic_screen_never_calls_a_curated_reading_inferior(self):
        candidate = {'topics': [{'id': 't1', 'text': 'مقدمة'}], 'warnings': ['incomplete']}
        self.assertEqual(classify(candidate, [{'reference_review': {'kind': 'visual_source_transcription'}}])[0], 'verified_visual_existing')
        self.assertEqual(classify(candidate, [{'status': 'no_fixed_topics'}])[0], 'no_fixed_topics_existing')
        self.assertEqual(classify({'topics': []}, [{'status': 'needs_review'}])[0], 'unresolved')
        self.assertEqual(classify(candidate, [{'status': 'extracted_needs_review'}])[0], 'needs_visual')

    def test_native_verification_needs_total_and_no_unreadable_cells(self):
        candidate = {'topics': [{'id': 't1', 'text': 'تعريف المفاهيم الأساسية للمقرر وتطبيقاتها العلمية'}], 'warnings': [], 'table_evidence': [{'page': 1, 'total': {'text': 'المجموع'}, 'numbered_geometry_rows': 1, 'content_rows': [{}], 'excluded_rows': []}]}
        self.assertEqual(classify(candidate, [{}])[0], 'verified_native')
        candidate['table_evidence'][0]['total'] = None
        self.assertEqual(classify(candidate, [{}])[0], 'needs_visual')
        candidate['table_evidence'][0].update(total={'text': 'المجموع'}, empty_rows=[{}])
        self.assertEqual(classify(candidate, [{}])[0], 'needs_visual')

    def test_glyph_and_fragmentation_flags_are_explicit_not_silent_corrections(self):
        topics = [{'id': 'x', 'text': 'نص (cid:12) غير مقروء'}, {'id': 'y', 'text': 'م ف ه م ع ل م البيان'}]
        before = copy.deepcopy(topics)
        flags = text_quality(topics)
        self.assertEqual(topics, before)
        self.assertTrue(any(flag['kind'] == 'unresolved_glyphs' for flag in flags))
        self.assertTrue(any(flag['kind'] == 'possibly_fragmented_arabic' for flag in flags))

    def test_audit_preserves_all_curated_topics_when_native_candidate_is_shorter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'assets').mkdir()
            pdf = root / 'assets/source.pdf'
            with pymupdf.open() as document:
                document.new_page()
                pdf.write_bytes(document.tobytes())
            sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            topics = [{'id': f't{i}', 'text': 'نص موثق من الصورة', 'page': 1} for i in range(3)]
            course = {'id': 'curated', 'topics': topics, 'reference_review': {'source_sha256': sha}, 'status': 'extracted_needs_review'}
            candidate = {'topics': topics[:1], 'warnings': [], 'table_evidence': []}
            with patch('scripts.audit_catalog_references.numbered_grid', return_value=candidate):
                result = audit_one((str(root), sha, ['assets/source.pdf'], [course]))
            self.assertEqual(result['proposed_topics'], topics)
            self.assertEqual(result['proposal_action'], 'keep_existing_curated_reading')
            self.assertEqual(result['risk_priority'], 0)
            self.assertEqual(result['native_candidate_count'], 1)
            ordinary = dict(course, topics=topics[:1])
            ordinary.pop('reference_review')
            with patch('scripts.audit_catalog_references.numbered_grid', return_value=candidate):
                pending = audit_one((str(root), sha, ['assets/source.pdf'], [ordinary]))
            self.assertEqual(pending['review_status'], 'needs_visual')
            self.assertGreater(pending['risk_priority'], 0, 'Even a stable denominator with incomplete proof must remain in the visual queue')


class NumberedGridTests(unittest.TestCase):
    def test_independent_assessment_labels_and_mixed_curriculum_are_distinct(self):
        for label in ('التقويم الأول.', 'التقويم الثاني', 'التقييم الأول', 'التقييم الثاني', 'الاختبار الدوري', 'اختبار دوري', 'الا ختبار النصفي', 'تقييم البحوث', 'مراجعة وتقييم'):
            with self.subTest(label=label):
                self.assertTrue(pure_assessment_text(label))
        self.assertEqual(mixed_assessment_reading('التقويم الأول: انقضاء الشركة وأسبابه.'), 'انقضاء الشركة وأسبابه')
        self.assertEqual(mixed_assessment_reading('الاختبار الدوري. الإعراب الظاهر والمقدر.'), 'الإعراب الظاهر والمقدر')
        self.assertFalse(pure_assessment_text('تسميع سورة نوح – مراجعة.'))
        self.assertFalse(pure_assessment_text('تقويم الحديث والحكم عليه'))
        self.assertFalse(pure_assessment_text('اختبار صحة العقد'))
        self.assertFalse(pure_assessment_text('تقييم البحوث وتحليل مناهجها'))

    def test_mixed_exam_does_not_require_plus_and_keeps_real_subject(self):
        self.assertEqual(mixed_assessment_reading('الاختبار النصفي - الخلع وأحكامه.'), 'الخلع وأحكامه')
        self.assertIsNone(mixed_assessment_reading('الاختبار النصفي'))
        self.assertIsNone(mixed_assessment_reading('الاختبار النصفي + مراجعة عامة'))
        self.assertEqual(mixed_assessment_reading('الاختبار النصف الواجبات السلبية'), 'الواجبات السلبية')
        self.assertTrue(pure_assessment_text('الاختبار النهائ'))
        self.assertTrue(pure_assessment_text('الاختبار النصفي + مراجعة عامة'))
        self.assertFalse(pure_assessment_text('اختبار صحة العقد'))

    def test_closed_grid_removes_assessment_contamination_even_when_it_is_shorter(self):
        old = [{'id': 'a', 'text': 'أحكام العقد'}, {'id': 'b', 'text': 'الاختبار النهائ'}]
        grid = {'topics': old[:1], 'table_evidence': [{'total': {'text': 'المجموع'}}]}
        self.assertTrue(_prefer_numbered_grid(old, grid))
        self.assertFalse(_prefer_numbered_grid([{'id': 'a', 'text': 'أحكام العقد'}, {'id': 'b', 'text': 'آثار العقد'}], grid))
        grid['table_evidence'] = []
        self.assertFalse(_prefer_numbered_grid(old, grid))

    def test_continuation_body_containing_topic_keyword_is_not_a_new_header(self):
        def cell(x0, y0, x1, y1):
            return (x0, y0, x1, y1)
        header = SimpleNamespace(cells=[cell(60, 300, 150, 330), cell(150, 300, 510, 330), cell(510, 300, 540, 330)])
        first = SimpleNamespace(cells=[cell(60, 330, 150, 400), cell(150, 330, 510, 400), cell(510, 330, 540, 400)])
        second = SimpleNamespace(cells=[cell(60, 60, 150, 100), cell(150, 60, 510, 100), cell(510, 60, 540, 100)])
        total = SimpleNamespace(cells=[cell(60, 100, 150, 120), cell(150, 100, 540, 120)])
        tables = [SimpleNamespace(bbox=(60, 300, 540, 400), rows=[header, first]), SimpleNamespace(bbox=(60, 60, 540, 120), rows=[second, total])]
        native_pages = []
        for table in tables:
            native_pages.append(SimpleNamespace(rect=SimpleNamespace(width=600), find_tables=lambda table=table, **kwargs: SimpleNamespace(tables=[table])))
        pp = [SimpleNamespace(index=i, close=lambda: None) for i in range(2)]
        def read(page, bbox):
            x0, y0, x1, y1 = bbox
            center = (y0 + y1) / 2
            if page.index == 0 and center < 330:
                return 'م' if x0 >= 510 else 'قائمة الموضوعات' if x0 >= 150 else 'الساعات'
            if page.index == 1 and center >= 100:
                return 'المجموع' if x0 >= 150 else '4'
            if x0 >= 510:
                return '1' if page.index == 0 else '2'
            if x0 < 150:
                return '2'
            return 'تعريفات أساسية للمقرر' if page.index == 0 else 'موضوعات المفاهيم وموضوع المقرر'
        native = MagicMock()
        native.__enter__.return_value = native_pages
        text = MagicMock()
        text.__enter__.return_value = SimpleNamespace(pages=pp)
        with patch('scripts.equivalency.recovery.pymupdf.open', return_value=native), patch('scripts.equivalency.recovery.pdfplumber.open', return_value=text), patch('scripts.equivalency.recovery.logical_region_text', side_effect=read), patch('scripts.pdf_font_recovery.recover_embedded_font_text', return_value=None), patch('scripts.equivalency.extract.digest', return_value='mock-pdf-sha'):
            result = numbered_grid('fixture.pdf')
        self.assertEqual(len(result['topics']), 2)
        self.assertEqual(result['topics'][1]['source_row_numbers'], '2')
        self.assertTrue(result['table_evidence'][1]['continuation'])
        self.assertIsNotNone(result['table_evidence'][-1]['total'])
        def fragment_read(page, bbox):
            if page.index == 1 and (bbox[1] + bbox[3]) / 2 < 100 and (bbox[0] >= 510 or bbox[0] < 150):
                return ''
            return read(page, bbox)
        with patch('scripts.equivalency.recovery.pymupdf.open', return_value=native), patch('scripts.equivalency.recovery.pdfplumber.open', return_value=text), patch('scripts.equivalency.recovery.logical_region_text', side_effect=fragment_read), patch('scripts.equivalency.recovery.number_cell_has_ink', return_value=False), patch('scripts.pdf_font_recovery.recover_embedded_font_text', return_value='قراءة مستردة من خط المصدر'), patch('scripts.equivalency.extract.digest', return_value='mock-pdf-sha'):
            continued = numbered_grid('fixture.pdf')
        self.assertEqual(len(continued['topics']), 1)
        self.assertEqual(continued['topics'][0]['corrected_text'], 'قراءة مستردة من خط المصدر قراءة مستردة من خط المصدر')
        self.assertEqual([fragment['page'] for fragment in continued['topics'][0]['fragments']], [1, 2])
        def unnumbered_unit_read(page, bbox):
            if page.index == 1 and (bbox[1]+bbox[3])/2 < 100 and bbox[0] >= 510:
                return ''
            return read(page, bbox)
        with patch('scripts.equivalency.recovery.pymupdf.open', return_value=native), patch('scripts.equivalency.recovery.pdfplumber.open', return_value=text), patch('scripts.equivalency.recovery.logical_region_text', side_effect=unnumbered_unit_read), patch('scripts.equivalency.recovery.number_cell_has_ink', return_value=False), patch('scripts.pdf_font_recovery.recover_embedded_font_text', return_value=None), patch('scripts.equivalency.extract.digest', return_value='mock-pdf-sha'):
            independent = numbered_grid('fixture.pdf')
        self.assertEqual(len(independent['topics']), 2)
        self.assertEqual(independent['topics'][1]['row_basis'], 'independent_hours_cell_geometry')
        self.assertEqual(independent['topics'][1]['hours_raw'], '2')

    def test_unnumbered_continuation_with_independent_hours_preserves_fourteen_units(self):
        path = ROOT / 'assets/course-specifications/clo-plo-corrections-20260901/2001115-4--sharia-new-v47.pdf'
        if not path.exists():
            self.skipTest('imported structural regression fixture unavailable')
        result = numbered_grid(path)
        self.assertEqual(len(result['topics']), 14)
        independent = [t for t in result['topics'] if t.get('row_basis') == 'independent_hours_cell_geometry']
        self.assertEqual(len(independent), 7)
        self.assertTrue(all(t['page'] == 6 for t in independent))
        self.assertEqual([t['hours_raw'] for t in independent], ['3', '3', '6', '6', '6', '6', '6'])

    def test_blank_hours_do_not_stop_sixteen_explicit_topic_rows(self):
        path = ROOT / 'assets/course-specifications/quran-old-v39/2002223-2.pdf'
        if not path.exists():
            self.skipTest('imported reference fixture unavailable')
        result = numbered_grid(path)
        self.assertEqual(len(result['topics']), 16)
        third = result['topics'][2]
        self.assertEqual(third['source_row_numbers'], '3')
        self.assertEqual(third['hours_raw'], '')
        self.assertEqual(third['hours_status'], 'missing_in_source')
        self.assertFalse(any('أن يوضح الطالب' in row['text'] for row in result['topics']))
        self.assertEqual([section['page'] for section in result['table_evidence']], [3, 4])

    def test_civil_intro_is_one_full_row_and_continuation_keeps_fifteen_topics(self):
        path = ROOT / 'assets/course-specifications/local-gap-completion-20260822/law-creed-master/2003802-2.pdf'
        if not path.exists():
            self.skipTest('imported reference fixture unavailable')
        result = numbered_grid(path)
        self.assertEqual(len(result['topics']), 15)
        self.assertEqual(len(result['excluded_rows']), 1)
        self.assertTrue(any('النيائي' in row['text'] for row in result['excluded_rows']))
        mixed = [row for row in result['topics'] if row.get('mixed_assessment_row')]
        self.assertEqual(len(mixed), 1)
        self.assertIn('فوات الفرصة', mixed[0]['corrected_text'])
        self.assertNotIn('الاختبار', mixed[0]['corrected_text'])
        self.assertEqual(len(extract_pdf(path)['topics']), 15)

    def test_course_bundles_and_unnumbered_subrows_do_not_inflate_the_denominator(self):
        catalog_path = ROOT / 'equivalency-pilot/catalog.json'
        if not catalog_path.exists():
            self.skipTest('imported reference catalog unavailable')
        courses = json.loads(catalog_path.read_text())['courses']
        for prefix, count, extra_pages in [('aab0e7bb741e', 15, [12, 20, 27]), ('fc2ae80d9c5a', 7, []), ('d30cb41edf0e', 17, [])]:
            course = next((c for c in courses if c.get('source_sha256', '').startswith(prefix)), None)
            if not course or not (ROOT / course['source_pdf']).exists():
                self.skipTest('imported structural regression fixture unavailable')
            with self.subTest(source_sha_prefix=prefix):
                result = numbered_grid(ROOT / course['source_pdf'])
                self.assertEqual(len(result['topics']), count)
                self.assertEqual([section['page'] for section in result['additional_topic_sections']], extra_pages)
                if extra_pages:
                    self.assertEqual(result['status'], 'needs_review')
                    self.assertTrue(any('أقسام موضوعات أخرى' in message for message in result['warnings']))
                else:
                    self.assertTrue(any(section['merged_fragment_rows'] for section in result['table_evidence']))


if __name__ == '__main__':
    unittest.main()
