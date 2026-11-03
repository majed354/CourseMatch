"""Canonical reference readiness is curator/audit metadata, never student input."""
from __future__ import annotations
import hashlib
import json


def topics_digest(topics):
    return hashlib.sha256(json.dumps(topics,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def reference_problem(course):
    review=course.get('reference_review',{})
    if review.get('comparison_readiness','ready')!='ready':
        return review.get('readiness_reason') or 'لم يكتمل تحديد نطاق مفردات المرجع.'
    quality=course.get('reference_quality')
    if quality is None:
        return None  # Uploaded inputs use extraction-integrity and unresolved-topic gates.
    if (quality.get('source_sha256')!=course.get('source_sha256')
            or quality.get('topics_sha256')!=topics_digest(course.get('topics',[]))):
        return 'تغيرت قراءة مفردات المرجع بعد مراجعتها؛ يلزم تدقيق النسخة الحالية.'
    if quality.get('state') not in ('ready_visual','ready_native'):
        return quality.get('reason') or 'قراءة مفردات المرجع قيد المراجعة؛ لا تتوافر أدلة كافية للحكم.'
    return None


def reference_pending(target,source,rubric):
    issues={side:problem for side,course in [('target',target),('source',source)] if (problem:=reference_problem(course))}
    if not issues:return None
    return {'provider':'reference_pending','pairs':[],'rubric':rubric,'reference_review':issues}


def evidence_calculation(targets,sources,evidence):
    from .engine import calculate
    result=calculate(targets,sources,evidence['pairs'],reviewed=evidence.get('provider')!='reference_pending')
    if evidence.get('provider')=='reference_pending':
        result.update(reference_review=evidence['reference_review'],reason='لم تكتمل مراجعة نص المرجع أو نطاق مفرداته؛ يلزم استكماله قبل إصدار الحكم.')
    return result


_INTEGRITY_VERSION = 'uploaded-target-integrity-v1'
_INTEGRITY_REASONS = {
    'empty_topic_rows': 'توجد صفوف موضوعات لم يثبت نصها؛ يلزم استكمال قراءة المقرر الهدف.',
    'merged_source_rows': 'لم يثبت تقسيم بعض صفوف موضوعات المقرر الهدف؛ لا يمكن اعتماد عددها الحالي.',
    'ambiguous_topic_sections': 'لم يتحدد جدول الموضوعات المقصود بين أقسام الملف؛ يلزم تثبيت نطاق المقرر الهدف.',
    'no_fixed_topics': 'لا يحدد التوصيف قائمة موضوعات ثابتة تصلح لحساب المعادلة.',
    'incomplete_topic_table': 'توجد علامة صريحة على نقص جدول موضوعات المقرر الهدف أو تتمته.',
}
_INTEGRITY_BINDING_PROBLEM = 'لم تثبت مطابقة دليل اكتمال القراءة مع ملف المقرر الهدف ومفرداته الحالية.'


def _effective_texts(course):
    rows = course.get('topics')
    if not isinstance(rows, list):
        raise ValueError('invalid_effective_topics')
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid_effective_topic')
        text = row.get('corrected_text') or row.get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('invalid_effective_topic_text')
        result.append(text.strip())
    return result


def _effective_signature(course):
    # IDs, page positions and audit fields do not define academic content.
    # Order, duplicate rows and exact effective wording remain significant.
    return hashlib.sha256(json.dumps(_effective_texts(course), ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def extraction_integrity(source):
    """Bind explicit extraction defects to the exact PDF and effective texts.

    Warning prose, general status and hours never establish a missing topic.
    A missing total alone does not establish a truncated table. Extractors may
    mark topic_table_incomplete, or a table's incomplete/truncated/
    missing_continuation flag, when the missing scope is explicitly known.
    """
    issues = set()
    evidence = source.get('table_evidence') or []
    if any(section.get('empty_rows') for section in evidence if isinstance(section, dict)):
        issues.add('empty_topic_rows')
    if any(row.get('segmentation_status') == 'merged_source_rows'
           for row in source.get('topics', []) if isinstance(row, dict)):
        issues.add('merged_source_rows')
    if source.get('additional_topic_sections') and source.get('topic_sections_resolved') is not True:
        issues.add('ambiguous_topic_sections')
    if source.get('fixed_topic_curriculum') is False:
        issues.add('no_fixed_topics')
    if source.get('topic_table_incomplete') is True or any(
            any(section.get(flag) is True for flag in ('incomplete', 'truncated', 'missing_continuation'))
            for section in evidence if isinstance(section, dict)):
        issues.add('incomplete_topic_table')
    return {'version': _INTEGRITY_VERSION, 'source_sha256': source.get('source_sha256'),
            'effective_topics_sha256': _effective_signature(source), 'issues': sorted(issues)}


def _integrity_problem(target):
    if 'extraction_integrity' not in target:
        return None
    summary = target['extraction_integrity']
    keys = {'version', 'source_sha256', 'effective_topics_sha256', 'issues'}
    if not isinstance(summary, dict) or set(summary) != keys:
        return _INTEGRITY_BINDING_PROBLEM
    issues = summary.get('issues')
    if (summary['version'] != _INTEGRITY_VERSION
            or not isinstance(summary['source_sha256'], str) or not summary['source_sha256'].strip()
            or summary['source_sha256'] != target.get('source_sha256')
            or summary['effective_topics_sha256'] != _effective_signature(target)
            or not isinstance(issues, list) or any(not isinstance(issue, str) or issue not in _INTEGRITY_REASONS for issue in issues)
            or len(set(issues)) != len(issues)):
        return _INTEGRITY_BINDING_PROBLEM
    return ' '.join(_INTEGRITY_REASONS[issue] for issue in issues) or None


def uploaded_target_reference(target, catalog_sources):
    """Return a matching ready canonical anchor, or a formal reason to hold.

    Unknown PDFs remain eligible. A known PDF cannot shed a curator hold by
    being uploaded again. This helper performs no I/O and changes no inputs;
    the caller verifies the returned anchor's actual PDF path independently.
    """
    if not isinstance(target, dict):
        return None, _INTEGRITY_BINDING_PROBLEM
    try:
        effective = _effective_texts(target)
        if problem := _integrity_problem(target):
            return None, problem
    except (TypeError, ValueError, KeyError, AttributeError):
        return None, _INTEGRITY_BINDING_PROBLEM
    sha = target.get('source_sha256')
    if not isinstance(sha, str) or not sha.strip():
        return None, None
    courses = catalog_sources.values() if isinstance(catalog_sources, dict) else catalog_sources
    known = [course for course in courses if isinstance(course, dict)
             and isinstance(course.get('source_sha256'), str) and course['source_sha256'].strip()
             and course['source_sha256'] == sha]
    if not known:
        return None, None
    for course in known:
        try:
            if reference_problem(course) is None and _effective_texts(course) == effective:
                return course, None
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return None, 'ملف المقرر الهدف معروف، لكن لم تثبت مطابقة مفرداته الحالية لقراءة مرجعية مكتملة ومعتمدة لهذا الملف.'
