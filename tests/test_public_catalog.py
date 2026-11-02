"""JS builder/Python guard parity and the public data boundary."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

from scripts.public_catalog import project_public_catalog, matches_public_catalog

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return {'catalog_id': 'canonical-id', 'contexts': [{'id': 'plan', 'program': 'الأنظمة', 'degree': 'بكالوريوس', 'version': '47', 'private': 'secret'}],
            'courses': [{'id': 'course', 'code': '2003243-3', 'name': 'القانون الإداري', 'hours': 3.0,
                         'context': {'program': 'الأنظمة', 'version': '47'}, 'context_id': 'plan', 'source_pdf': 'assets/source.pdf', 'source_sha256': 'a' * 64,
                         'topics': [{'id': 't1', 'text': 'خام\nمحفوظ للعرض', 'corrected_text': 'المفردة المصححة', 'page': 6, 'end_page': 7, 'origin': 'reviewed_visual_transcription', 'unresolved': False,
                                     'bbox': [0, 0, 50, 50], 'raw_text': 'PRIVATE RAW', 'native_rows': [{'secret': True}], 'fragments': [{'private': 'raw'}]}],
                         'reference_quality': {'state': 'ready_visual', 'reason': 'مكتمل', 'source_sha256': 'a' * 64, 'topics_sha256': 'b' * 64, 'manifest': 'private/audit.json'},
                         'excluded_rows': [{'text': 'PRIVATE EXAM RAW', 'bbox': [1, 2, 3, 4]}], 'warnings': ['ملاحظة'],
                         'original_extracted_topics': [{'secret': 'OLD'}], 'reference_review': {'private': True}}],
            'library_sources': [{'id': 'library', 'library_only': True, 'context': None, 'topics': []}], 'source_hashes': {'private/source': 'hash'}, 'new_private_field': {'secret': True}}


def javascript_projection(value):
    program = "import fs from 'node:fs';import {projectPublicCatalog} from './scripts/public_catalog.mjs';process.stdout.write(JSON.stringify(projectPublicCatalog(JSON.parse(fs.readFileSync(0,'utf8')))));"
    completed = subprocess.run(['node', '--input-type=module', '-e', program], cwd=ROOT, input=json.dumps(value, ensure_ascii=False), text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


class PublicCatalogProjectionTests(unittest.TestCase):
    def test_projection_preserves_ui_identity_scope_reading_readiness_and_excluded_count(self):
        value = fixture(); before = copy.deepcopy(value); result = project_public_catalog(value)
        self.assertEqual(value, before)
        self.assertEqual(result['catalog_id'], value['catalog_id'])
        course = result['courses'][0]
        for key in ('id', 'code', 'name', 'hours', 'context_id', 'source_pdf', 'source_sha256'):
            self.assertEqual(course[key], value['courses'][0][key])
        self.assertEqual(course['topics'][0]['corrected_text'], 'المفردة المصححة')
        self.assertEqual(course['topics'][0]['text'], 'خام\nمحفوظ للعرض')
        self.assertEqual(course['topics'][0]['end_page'], 7)
        self.assertEqual(course['reference_quality']['state'], 'ready_visual')
        self.assertEqual(course['excluded_rows'], [{}])
        self.assertIsNone(result['library_sources'][0]['context'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('original_extracted_topics', course)
        self.assertNotIn('reference_review', course)
        self.assertNotIn('source_hashes', result)
        self.assertNotIn('bbox', course['topics'][0])

    def test_javascript_python_equivalence_on_boundary_fixture_and_complete_catalog(self):
        for value in (fixture(), json.loads((ROOT / 'equivalency-pilot/catalog.json').read_text())):
            with self.subTest(full='policy' in value):
                self.assertTrue(matches_public_catalog(value, javascript_projection(value)))

    def test_missing_null_false_and_empty_remain_distinct(self):
        value = fixture(); result = project_public_catalog(value)
        self.assertNotIn('reviewed', result['courses'][0])
        self.assertEqual(result['library_sources'][0]['topics'], [])
        result['courses'][0]['topics'][0]['unresolved'] = 0
        self.assertFalse(matches_public_catalog(value, result))

    def test_unknown_public_fields_or_audit_payload_are_not_allowed(self):
        value = fixture(); result = project_public_catalog(value)
        result['courses'][0]['reference_review'] = {'private': True}
        self.assertFalse(matches_public_catalog(value, result))

    def test_scalar_fields_cannot_smuggle_nested_audit_objects(self):
        value = fixture(); value['courses'][0]['name'] = {'secret': 'private'}
        with self.assertRaisesRegex(ValueError, 'scalar'):
            project_public_catalog(value)
        with self.assertRaises(subprocess.CalledProcessError):
            javascript_projection(value)
