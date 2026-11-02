"""Deterministic overlap for narrowly recognised atomic analytical-verse topics.

Full phrase matching is intentional: extra content, mixed methods, unspecified
chapters or multiple ranges return None and stay with semantic review. Printed
page numbers are metadata. No LLM grade is capped or replaced by this module.
"""
from __future__ import annotations
import re
from .ranges import normalized,topic_scopes

VERSION='atomic-analytical-verse-scope-v2-validated-chapter-limits'
DASH=r'[-–—−]'
BOUNDS=rf'[([]?\s*\d{{1,3}}\s*{DASH}\s*\d{{1,3}}\s*[)\]]?'


def analytical_units(topics):
    scopes=topic_scopes(topics);result={}
    for topic in topics:
        scope=scopes.get(topic['id'])
        if scope is None:return None
        name=re.escape(normalized(scope.chapter_name)).replace(r'\ ',r'\s+')
        chapter=rf'(?:ل?سورة\s+)?{name}'
        verse=rf'(?:الايات|ايات|الاية|اية)\s*[:：]?\s*(?:من\s*)?{BOUNDS}'
        # All alternatives fully consume the topic. The analytical method is
        # explicit on both sides; a bare word "تفسير" is insufficient.
        forms=[rf'تفسير\s+(?:ص(?:فحة)?\s*[:：]?\s*\d+\s+)?{chapter}\s*[,،:：]?\s*(?:{verse}|{BOUNDS})\s+تفسيرا\s+تحليليا',
               rf'(?:دراسة\s+تفسيرية\s+تحليلية|تفسير\s+تحليلي)\s+{chapter}\s*[,،:：]?\s*{verse}',
               rf'تفسير\s+تحليلي\s+ل?{verse}\s+من\s+{chapter}']
        text=normalized(topic.get('corrected_text') or topic['text']).strip().rstrip('.،؛ ')
        if not any(re.fullmatch(pattern,text) for pattern in forms):return None
        result[topic['id']]=scope
    return result if result else None


def atomic_pairs(target,source):
    ts,ss=analytical_units(target),analytical_units(source)
    if ts is None or ss is None:return None
    pairs=[]
    for t in target:
        scope=ts[t['id']];denominator=scope.end-scope.start+1;options=[]
        for s in source:
            other=ss[s['id']]
            intersection=max(0,min(scope.end,other.end)-max(scope.start,other.start)+1) if scope.chapter==other.chapter else 0
            score=100*intersection//denominator
            # This grade is the defined numeric overlap of identical, explicit
            # atomic material/method, not a semantic estimate from a model.
            reason=(f'يشترك المقرران في التفسير التحليلي لسورة {scope.chapter_name}؛ يغطي المصدر {intersection} من {denominator} آية من نطاق الهدف ({score}%).'
                    if scope.chapter==other.chapter else 'تختلف السورة المدروسة بين المفردتين؛ لا يتطابق محتوى الآيات.')
            options.append({'target_id':t['id'],'source_id':s['id'],'score':score,'reason':reason,
                            'target_quote':t.get('corrected_text') or t['text'],'source_quote':s.get('corrected_text') or s['text'],
                            'scope_proof':{'version':VERSION,'target':scope.evidence(),'source':other.evidence(),'intersection_verses':intersection,'target_verses':denominator}})
        chosen=[p for p in options if p['score']>=60]
        pairs.extend(chosen or [max(options,key=lambda p:p['score'])])
    return pairs


def atomic_subpairs(target,source):
    """Prove individual edges in mixed curricula; do not infer other edges."""
    pairs=[]
    for t in target:
        if analytical_units([t]) is None:continue
        for s in source:
            if analytical_units([s]) is None:continue
            pairs.extend(atomic_pairs([t],[s]))
    return pairs


def split_atomic_targets(targets,sources):
    """Identify a potentially countable atomic range split across source rows.

    This is a reason to review segmentation before an influential rejection,
    never a combined score or accepted edge. Each contributing unit must pass
    the same full-phrase, chapter and analytical-method proof as atomic_pairs.
    Overlapping repeated ranges are unioned, not added or double-counted.
    """
    candidates=[]
    for target in targets:
        units=analytical_units([target])
        if units is None:continue
        scope=units[target['id']]
        required=set(range(scope.start,scope.end+1));parts=[]
        for source in sources:
            other_units=analytical_units([source])
            if other_units is None:continue
            other=other_units[source['id']]
            if other.chapter!=scope.chapter:continue
            covered=required & set(range(other.start,other.end+1))
            if covered:parts.append((source['id'],covered))
        if len({sid for sid,_ in parts})<2 or any(covered==required for _,covered in parts):continue
        union=set().union(*(covered for _,covered in parts))
        # >=60% could qualify a resegmented unit for the lenient threshold.
        # This is only a possible segmentation issue, not a scored new edge.
        if len(union)*100>=60*len(required):candidates.append(target['id'])
    return candidates


def confirmed_atomic(pair):
    from .grounding import VERSION
    score=pair['score'];tiers=['lenient','strict'] if score>=80 else ['lenient'] if score>=60 else []
    return {**pair,'adjudication':{'version':VERSION,'kind':'deterministic_atomic_scope','state':'confirmed','possible_tiers':tiers}}
