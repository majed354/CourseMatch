"""Verify the standalone catalog and source bindings, without modifying them."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def verify():
    catalog = json.loads((ROOT/'equivalency-pilot/catalog.json').read_text())
    for name, expected in catalog['source_hashes'].items():
        assert sha(ROOT/name) == expected, f'Changed catalog source: {name}'
    clean = {k:v for k,v in catalog.items() if k != 'catalog_id'}
    assert hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True).encode()).hexdigest() == catalog['catalog_id'], 'Catalog ID mismatch'
    paths = {}
    for course in catalog['courses'] + catalog.get('library_sources', []):
        if not course.get('source_pdf'):
            continue
        path = (ROOT/course['source_pdf']).resolve()
        assert path.is_relative_to((ROOT/'assets').resolve()) and path.is_file(), path
        if path not in paths:
            assert sha(path) == course['source_sha256'], path
            paths[path] = course['source_sha256']
    ids = [c['id'] for c in catalog['courses']]
    assert len(ids) == len(set(ids)), 'Duplicate course identity'
    contexts = {c['id'] for c in catalog['contexts']}
    assert all(c['context_id'] in contexts for c in catalog['courses'])
    assert catalog['policy']['coverage_threshold'] == 80
    assert catalog['policy']['strict_topic_threshold'] == 80
    assert catalog['policy']['lenient_topic_threshold'] == 60
    print(f'CourseMatch verification PASSED: {len(ids)} appearances, {len(contexts)} contexts, {len(paths)} PDF sources.')

if __name__ == '__main__':
    verify()
