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
        return None  # Uploaded/manual inputs use the existing student review gate.
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
