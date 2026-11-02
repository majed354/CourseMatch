"""External PDF regressions run when the private university test corpus exists.

The PDFs remain original user test sources outside Git. Repository extraction
fixtures are also exercised by test_course_equivalency in every environment.
"""
from pathlib import Path
import unittest

from scripts.equivalency.extract import EXTRACTOR_VERSION, extract_pdf


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'output/equivalency-external-sources-20261102'
QURAN_INTRO = SOURCES / 'جامعة_أم_القرى/QS1101_مدخل_لعلوم_القرآن_الكريم.pdf'
LEGAL_EVIDENCE = SOURCES / 'جامعة_الملك_سعود/361_سلم_أدلة_الأحكام.pdf'
MARRIAGE = SOURCES / 'جامعة_الملك_سعود/383_سلم_فقه_النكاح.pdf'


class ExternalExtractionTests(unittest.TestCase):
    @unittest.skipUnless(QURAN_INTRO.is_file(), 'private external university PDF corpus unavailable')
    def test_right_hand_row_number_recovers_topic_with_broken_hour_cell_borders(self):
        result = extract_pdf(QURAN_INTRO)
        self.assertNotEqual(EXTRACTOR_VERSION, 'topic-grid-v7')
        self.assertEqual(len(result['topics']), 12)
        last = result['topics'][-1]
        self.assertIn('أنواع التفسير', last['text'])
        self.assertEqual((last['page'], last['hours_raw']), (6, '4'))
        self.assertEqual(sum(int(row['hours_raw']) for row in result['topics']), 30)
        self.assertFalse(any('الاختبار النهائي' in row['text'] for row in result['topics']))

    @unittest.skipUnless(LEGAL_EVIDENCE.is_file(), 'private external university PDF corpus unavailable')
    def test_shifted_continuation_column_preserves_new_unit_without_inventing_hours(self):
        result = extract_pdf(LEGAL_EVIDENCE)
        self.assertEqual(len(result['topics']), 12)
        last = result['topics'][-1]
        self.assertTrue(last['text'].startswith('الوحدة الثانية عشرة'))
        self.assertIn('المصلحة المرسلة', last['text'])
        self.assertIn('سد الذرائع', last['text'])
        self.assertNotIn('المصلحة المرسلة', result['topics'][-2]['text'])
        self.assertEqual((last['page'], last['hours_raw'], last['hours_status']), (6, '', 'missing_in_source'))
        self.assertTrue(any('بلا ساعات' in warning for warning in result['warnings']))
        self.assertEqual(result['status'], 'needs_review')

    @unittest.skipUnless(MARRIAGE.is_file(), 'private external university PDF corpus unavailable')
    def test_shifted_continuation_column_stitches_unheaded_fragment_into_previous_topic(self):
        result = extract_pdf(MARRIAGE)
        self.assertEqual(len(result['topics']), 12)
        last = result['topics'][-1]
        self.assertTrue(last['text'].startswith('أحكام الزينة'))
        self.assertIn('السن من ذهب أو فضة', last['text'])
        self.assertIn('الرموش الصناعية', last['text'])
        self.assertIn('عمليات التجميل', last['text'])
        self.assertEqual((last['page'], last['end_page']), (5, 6))
        self.assertEqual([fragment['page'] for fragment in last['fragments']], [5, 6])
        self.assertEqual(last['hours_raw'], '2')
        self.assertFalse(any('المراجع المساندة' in row['text'] for row in result['topics']))



class ExtendedExtractionTests(unittest.TestCase):
    def test_subject_separator_without_an_exam_is_not_a_mixed_assessment(self):
        from scripts.equivalency.extract import mixed_assessment_reading
        self.assertIsNone(mixed_assessment_reading('تعريف بالسورة + المقطع الأول.'))
        self.assertIsNone(mixed_assessment_reading('الاختبار النصفي + مراجعة عامة'))
        self.assertEqual(mixed_assessment_reading('الاختبارالنصفي + المقطع السادس من السورة.'), 'المقطع السادس من السورة')

    @unittest.skipUnless((SOURCES/'جامعة_الملك_سعود/356_سلم_فقه_المعاملات_1.pdf').is_file(), 'private external university PDF corpus unavailable')
    def test_fragmented_table_lines_are_twelve_units_not_zero_or_sub_bullets(self):
        result=extract_pdf(SOURCES/'جامعة_الملك_سعود/356_سلم_فقه_المعاملات_1.pdf')
        self.assertEqual(len(result['topics']),12)
        self.assertEqual(result['extraction_method'],'explicit_unit_headings')
        self.assertTrue(all(t['text'].startswith('الوحدة') for t in result['topics']))
        self.assertIn('المال: تعريفه',result['topics'][0]['text'])
        self.assertFalse(any('طرق التقييم' in t['text'] for t in result['topics']))

    @unittest.skipUnless((SOURCES/'جامعة_أم_القرى/QS2106_التفسير_التحليلي_1.pdf').is_file(), 'private external university PDF corpus unavailable')
    def test_mixed_exam_and_actual_verse_content_keeps_the_subject(self):
        result=extract_pdf(SOURCES/'جامعة_أم_القرى/QS2106_التفسير_التحليلي_1.pdf')
        self.assertEqual(len(result['topics']),15)
        mixed=[t for t in result['topics'] if t.get('mixed_assessment_row')]
        self.assertEqual(len(mixed),1)
        self.assertIn('الاختبارالنصفي',mixed[0]['text'])
        self.assertIn('المقطع السادس',mixed[0]['corrected_text'])
        self.assertNotIn('الاختبار',mixed[0]['corrected_text'])

    @unittest.skipUnless((SOURCES/'جامعة_أم_القرى/QS4117_الانتصار_للقرآن.pdf').is_file(), 'private external university PDF corpus unavailable')
    def test_mixed_exam_and_quran_defense_subject_is_not_discarded(self):
        result=extract_pdf(SOURCES/'جامعة_أم_القرى/QS4117_الانتصار_للقرآن.pdf')
        self.assertEqual(len(result['topics']),15)
        mixed=next(t for t in result['topics'] if t.get('mixed_assessment_row'))
        self.assertIn('الانتصار لجمع القرآن',mixed['corrected_text'])
        self.assertNotIn('الاختبار',mixed['corrected_text'])
        self.assertEqual(mixed['page'],5)

if __name__ == '__main__':
    unittest.main()

class RecoveryPriorityTests(unittest.TestCase):
    @unittest.skipUnless((SOURCES/'جامعة_الملك_سعود/150_سلم_فقه_الطهارة_وشروط_الصلاة.pdf').is_file(), 'private external university PDF corpus unavailable')
    def test_existing_strict_recovery_preserves_all_units_and_page_fragments(self):
        from scripts.equivalency.extract import extract_cached
        import tempfile
        with tempfile.TemporaryDirectory() as cache:
            result=extract_cached(SOURCES/'جامعة_الملك_سعود/150_سلم_فقه_الطهارة_وشروط_الصلاة.pdf',cache)
        self.assertEqual(len(result['topics']),12)
        self.assertEqual(result['extraction_method'],'strict_vector_grid')
        self.assertTrue(any(t.get('end_page') for t in result['topics']))
        self.assertIn('الوحدة الثانية',result['topics'][1]['text'])
