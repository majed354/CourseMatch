"""Independently evidenced target coverage, preserving original syllabus rows.

This module only proves literal topic equality or identical explicitly named
analytical methods with verse ranges. It never derives semantic grades from
facet counts, sums overlapping passages, or consumes a source row once.
"""
from __future__ import annotations
import re
import unicodedata

VERSION = 'curriculum-exact-v1-source-union'


def text(row):
    return row.get('corrected_text') or row['text']


def _validate_topics(rows):
    if not isinstance(rows,list):
        raise ValueError('invalid_curriculum_topic_list')
    seen=set()
    for row in rows:
        if (not isinstance(row,dict) or not isinstance(row.get('id'),str)
                or not row['id'].strip() or row['id'] in seen):
            raise ValueError('invalid_curriculum_topic_identity')
        value=row.get('corrected_text') or row.get('text')
        if not isinstance(value,str) or not value.strip():
            raise ValueError('invalid_curriculum_topic_text')
        seen.add(row['id'])


def literal_text(value):
    # Vocalisation can distinguish an actor from the person acted upon.
    # Equality shortcuts preserve it; semantic differences need actual review.
    value=unicodedata.normalize('NFKC',value)
    value=re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069ـ]', '', value)
    return ' '.join(value.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789')).split())


def scope_upper_bound(targets, sources, target_id, source_ids):
    """Percent of a known target range in a union of known source ranges.

    None means the requested bound cannot be proved, rather than zero. The
    whole tables are parsed so headings carried into later rows are retained.
    Calling with a subset bounds a positive witness only; a negative bound
    must include every source row in the curriculum.
    """
    from .ranges import topic_scopes
    _validate_topics(targets);_validate_topics(sources)
    ts, ss = topic_scopes(targets), topic_scopes(sources)
    target = ts.get(target_id)
    if target is None:
        return None
    ids = list(source_ids)
    if len(set(ids)) != len(ids) or any(s not in {row['id'] for row in sources} for s in ids):
        raise ValueError('invalid_source_scope_ids')
    if not ids or any(ss.get(sid) is None for sid in ids):
        return None
    required = set(range(target.start, target.end + 1))
    covered = set()
    for sid in ids:
        source = ss[sid]
        if source.chapter == target.chapter:
            covered.update(required.intersection(range(source.start, source.end + 1)))
    return 100 * len(covered) / len(required)


def proven_alignment(targets, sources, rubric):
    from .atomic_scope import analytical_units
    from .ranges import invalid_scope_topics, range_issues
    _validate_topics(targets);_validate_topics(sources)
    if not targets or not sources or invalid_scope_topics(targets) or invalid_scope_topics(sources):
        return None
    exact = []
    for target in targets:
        found = []
        for source in sources:
            if literal_text(text(target)) != literal_text(text(source)):
                continue
            # Identical wording can inherit different chapter headings.
            if not range_issues([{'target_id':target['id'], 'source_id':source['id'], 'score':100}], targets, sources):
                found.append(source['id'])
        if not found:
            break
        exact.append({'target_id':target['id'], 'source_ids':sorted(found), 'score':100,
                      'reason':'يتطابق نص المفردة ونطاقها مع المحتوى الموثق في المقرر المصدر.'})
    if len(exact) == len(targets):
        return {'provider':'curriculum_exact', 'version':VERSION, 'kind':'literal', 'rubric':rubric, 'pairs':exact}
    ts, ss = analytical_units(targets), analytical_units(sources)
    if ts is None or ss is None:
        return None
    pairs = []
    for row in targets:
        target = ts[row['id']]
        required = set(range(target.start, target.end + 1))
        covered, contributors = set(), []
        for source in sources:
            scope = ss[source['id']]
            part = (required.intersection(range(scope.start, scope.end + 1))
                    if scope.chapter == target.chapter else set())
            if part:
                covered.update(part)
                contributors.append(source['id'])
        score = 100 * len(covered) // len(required)
        pairs.append({'target_id':row['id'], 'source_ids':sorted(contributors), 'score':score,
                      'reason':f'يشترك المقرران في التفسير التحليلي المحدد؛ يغطي محتوى المصدر {len(covered)} من {len(required)} آية من نطاق الهدف ({score}%).',
                      'scope_proof':{'target':target.evidence(), 'covered_verses':sorted(covered),
                                     'target_verse_count':len(required)}})
    return {'provider':'curriculum_exact', 'version':VERSION, 'kind':'analytical_scope', 'rubric':rubric, 'pairs':pairs}


def validate_evidence(evidence, targets, sources, rubric):
    expected = proven_alignment(targets, sources, rubric)
    if expected is None or evidence != expected:
        raise ValueError('invalid_curriculum_exact_proof')
    return evidence


def calculate(targets, sources, evidence, rubric):
    from .engine import LABELS
    validate_evidence(evidence, targets, sources, rubric)
    strict = {p['target_id']:p['source_ids'] for p in evidence['pairs'] if p['score'] >= 80}
    lenient = {p['target_id']:p['source_ids'] for p in evidence['pairs'] if p['score'] >= 60}
    total = len(targets)
    decision = 'equivalent' if len(strict)*100 >= 80*total else 'lenient' if len(lenient)*100 >= 80*total else 'not_equivalent'
    return {'total_topics':total, 'coverage_threshold':80, 'strict_topic_threshold':80,
            'lenient_topic_threshold':60, 'weighting':'topic_count', 'accounting':'original_target_topics',
            'strict_count':len(strict), 'lenient_count':len(lenient),
            'strict_coverage':100*len(strict)/total, 'lenient_coverage':100*len(lenient)/total,
            'strict_matches':strict, 'lenient_matches':lenient,
            'decision':decision, 'label':LABELS[decision]}
