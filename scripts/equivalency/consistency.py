"""Reject a strict grade that contradicts the model's own explicit rationale.

This checks internal coherence only. It neither judges semantic equivalence
from keywords nor assigns replacement scores; rejected answers need review.
"""
import re
from .ranges import normalized

PARTIAL = re.compile(r'(?:التغطية|تغطية|المطابقة|مطابقة|التوافق|توافق)\s+(?:الموضوعية\s+)?جزئي(?:ة|ا|\b)')
ONLY_PART = re.compile(r'(?:المصدر|النص|المفردة المصدرية)\s+(?:يغطي|يشمل|تغطي|تشمل)[^.!؟؛;]{0,160}فقط[^.!؟؛;]{0,45}(?:بينما الهدف|والهدف|لكن الهدف)')
ASSUMED = re.compile(r'(?:مع افتراض|بافتراض ان|يشمل عادة|تشمل عادة)')
NEGATED_PREFIX = re.compile(r'(?:ليست?|وليست?|لا|دون|بدون|عدم|غير|لم تكن|لا توجد|لا تعد|لا تعتبر|ليس صحيحا ان|غير صحيح ان)\s*$')

def affirmed(pattern, text):
    # Interpret only short, directly attached negation. Do not let a negation
    # in an earlier clause erase a later affirmative admission of partiality.
    for match in pattern.finditer(text):
        prefix = re.split(r'[.!؟؛;]', text[max(0,match.start()-60):match.start()])[-1]
        if not NEGATED_PREFIX.search(prefix):
            return True
    return False

def rationale_issues(pairs):
    issues = []
    for pair in pairs:
        if type(pair.get('score')) is not int or pair['score'] < 80:
            continue
        reason = normalized(pair.get('reason', ''))
        pattern = 'partial_coverage' if affirmed(PARTIAL,reason) or affirmed(ONLY_PART,reason) else 'assumed_details' if affirmed(ASSUMED,reason) else None
        if pattern:
            issues.append({'field':'strict_rationale_consistency','target_id':pair.get('target_id'),
                           'source_id':pair.get('source_id'),'score':pair['score'],'conflict':pattern,
                           'reason':pair.get('reason','')})
    return issues
