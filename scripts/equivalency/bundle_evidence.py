"""Whole original-target evidence against the full source curriculum.

Parts are literal audit anchors, never topics, weights or source capacities.
Support labels describe evidence, not part-level percentages. Strict concerns
the original target as a whole: minor disclosed gaps can be non-decisive when
quoted core alignment and the whole-scope rationale support the 80% threshold.
This module checks structural evidence contracts; the independent readers must
justify the academic materiality of a gap, without weighting or counting parts.
A lenient band establishes the overlapping >=60 threshold, not a strict <80
ceiling. Unresolved strict agreement cannot erase established lenient support.
The caller owns full-curriculum prompt/input-hash binding and model execution.
This module performs no I/O or inference and never changes a supplied evidence band.
"""
from __future__ import annotations
import copy
import math
import re

VERSION = 'curriculum-bundle-evidence-v2-bands'
SUPPORTS = ('aligned','related','limited','absent','unknown')
TIERS = ('strict','lenient')
FACET_SCHEMA = {'type':'object','properties':{
    'part_id':{'type':'string'},'support':{'type':'string','enum':list(SUPPORTS)},
    'source_quotes':{'type':'array','maxItems':100,'items':{'type':'object',
        'properties':{'source_id':{'type':'string'},'quote':{'type':'string','minLength':1,'maxLength':4000}},
        'required':['source_id','quote'],'additionalProperties':False}},
    'reason':{'type':'string','minLength':1,'maxLength':2000}},
    'required':['part_id','support','source_quotes','reason'],'additionalProperties':False}
def _row_schema(classification):
    fields = {'target_id':{'type':'string'},
        'facets':{'type':'array','minItems':1,'items':FACET_SCHEMA},
        'reason':{'type':'string','minLength':1,'maxLength':2000},
        classification:{'type':'string','enum':['strict','lenient','weak','uncertain']},
        'possible_tiers':{'type':'array','uniqueItems':True,'items':{'type':'string','enum':list(TIERS)}},
        'uncertainty':{'type':'string','enum':['none','non_decisive','decisive']}}
    return {'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}


PROPOSAL_ROW_SCHEMA = _row_schema('band')
REVIEW_ROW_SCHEMA = _row_schema('verdict')
PROPOSAL_SCHEMA = {'type':'object','properties':{'evaluations':{'type':'array','maxItems':100,'items':PROPOSAL_ROW_SCHEMA}},'required':['evaluations'],'additionalProperties':False}
REVIEW_SCHEMA = {'type':'object','properties':{'reviews':{'type':'array','maxItems':100,'items':REVIEW_ROW_SCHEMA}},'required':['reviews'],'additionalProperties':False}


def _text(topic):
    text=topic.get('corrected_text') or topic.get('text')
    if not isinstance(text,str) or not text.strip():raise ValueError('bundle_topic_text_missing')
    return text


def partition_parts(text):
    """Partition literal punctuation spans without interpreting academic units.

    Delimiters stay in the preceding anchor; whitespace/heading markers remain
    present. Numeric decimals/ranges and intra-word hyphens are not split.
    """
    if not isinstance(text,str) or not text.strip():raise ValueError('bundle_target_text_missing')
    boundaries=[]
    for i,c in enumerate(text):
        previous=text[i-1] if i else '';following=text[i+1] if i+1<len(text) else ''
        numeric=previous.isdigit() and following.isdigit()
        if c in '-–—−':numeric=text[:i].rstrip()[-1:].isdigit() and text[i+1:].lstrip()[:1].isdigit()
        punctuation=c in '،؛;,' and not numeric
        sentence=c in '.。!؟?' and not numeric
        dash=c in '-–—−' and not numeric and (not previous or previous.isspace()) and (not following or following.isspace())
        if punctuation or sentence or dash:boundaries.append(i+1)
    ends=sorted(set(boundaries+[len(text)]));spans=[];start=0
    for end in ends:
        quote=text[start:end]
        if any(c.isalpha() for c in quote):spans.append([start,end])
        elif spans:spans[-1][1]=end
        # Leading numbering/punctuation belongs to the first substantive span.
        else:continue
        start=end
    if start<len(text):
        if spans:spans[-1][1]=len(text)
        else:spans=[[0,len(text)]]
    if not spans:spans=[[0,len(text)]]
    return [{'part_id':f'p{i+1}','start':a,'end':b,'quote':text[a:b]} for i,(a,b) in enumerate(spans)]


def _topics(rows):
    if not isinstance(rows,list):raise ValueError('bundle_topics_not_list')
    result={}
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in result:raise ValueError('bundle_topic_identity_invalid')
        _text(row);result[row['id']]=row
    return result


def _reason(value):
    if not isinstance(value,str) or not value.strip() or len(value)>2000:raise ValueError('bundle_reason_missing')


def _source_ids(facets):
    return sorted({q['source_id'] for facet in facets for q in facet['source_quotes']})


def _bound(targets,sources,target_id,source_ids):
    from .curriculum_exact import scope_upper_bound
    value=scope_upper_bound(targets,sources,target_id,source_ids)
    if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=100):raise ValueError('bundle_scope_bound_invalid')
    return value


def _check_positive_bound(targets,sources,row,threshold):
    limit=_bound(targets,sources,row['target_id'],_source_ids(row['facets']))
    if limit is not None and limit<threshold:raise ValueError('bundle_positive_exceeds_proven_scope')


def _facets(rows,target,sources):
    parts={part['part_id'] for part in partition_parts(_text(target))}
    if not isinstance(rows,list) or len(rows)!=len(parts):raise ValueError('bundle_missing_target_parts')
    seen=set()
    for facet in rows:
        if not isinstance(facet,dict) or set(facet)!={'part_id','support','source_quotes','reason'}:raise ValueError('bundle_facet_fields_invalid')
        part=facet['part_id'];support=facet['support']
        if not isinstance(part,str) or part not in parts or part in seen:raise ValueError('bundle_part_identity_invalid')
        seen.add(part);_reason(facet['reason'])
        if support not in SUPPORTS:raise ValueError('bundle_support_invalid')
        quotes=facet['source_quotes']
        if not isinstance(quotes,list) or len(quotes)>100:raise ValueError('bundle_quotes_not_list')
        seen_quotes=set()
        for proof in quotes:
            if not isinstance(proof,dict) or set(proof)!={'source_id','quote'}:raise ValueError('bundle_quote_fields_invalid')
            sid,quote=proof['source_id'],proof['quote']
            if not isinstance(sid,str) or sid not in sources:raise ValueError('bundle_foreign_source_identity')
            if not isinstance(quote,str) or not quote.strip() or quote not in _text(sources[sid]):raise ValueError('bundle_nonliteral_source_quote')
            if (sid,quote) in seen_quotes:raise ValueError('bundle_duplicate_source_quote')
            seen_quotes.add((sid,quote))
        if support in ('aligned','related','limited') and not quotes:raise ValueError('bundle_positive_support_without_quote')
    return {f['support'] for f in rows}


def _batch(rows,targets,target_ids):
    wanted=list(targets) if target_ids is None else target_ids
    if not isinstance(wanted,(list,tuple,set)) or any(not isinstance(t,str) or t not in targets for t in wanted) or len(set(wanted))!=len(wanted):raise ValueError('bundle_requested_targets_invalid')
    if not isinstance(rows,list) or len(rows)!=len(wanted):raise ValueError('bundle_missing_target_evaluations')
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('target_id'),str) or row['target_id'] not in wanted or row['target_id'] in seen:raise ValueError('bundle_target_identity_invalid')
        seen.add(row['target_id'])
    return rows


def _validate_classifications(rows,targets,sources,classification,*,target_ids=None):
    ts,ss=_topics(targets),_topics(sources);_batch(rows,ts,target_ids)
    for row in rows:
        if set(row)!={'target_id','facets','reason',classification,'possible_tiers','uncertainty'}:
            raise ValueError('bundle_classification_fields_invalid_or_score_leaked')
        _reason(row['reason']);supports=_facets(row['facets'],ts[row['target_id']],ss)
        band,possible,uncertainty=row[classification],row['possible_tiers'],row['uncertainty']
        if band not in ('strict','lenient','weak','uncertain') or uncertainty not in ('none','non_decisive','decisive'):
            raise ValueError('bundle_classification_invalid')
        if not isinstance(possible,list) or any(t not in TIERS for t in possible) or len(set(possible))!=len(possible) or ('strict' in possible and 'lenient' not in possible):
            raise ValueError('bundle_possible_tiers_invalid')
        if (uncertainty=='decisive') != (band=='uncertain'):
            raise ValueError('bundle_decisive_uncertainty_requires_uncertain_band')
        if 'unknown' in supports and uncertainty=='none':raise ValueError('bundle_unknown_not_disclosed')
        if supports=={'aligned'} and band!='strict':raise ValueError('bundle_band_contradicts_complete_alignment')
        if band=='strict':
            complete=supports=={'aligned'} and uncertainty=='none'
            disclosed_minor_gap='aligned' in supports and supports!={'aligned'} and uncertainty=='non_decisive'
            if not (complete or disclosed_minor_gap):raise ValueError('bundle_strict_without_supported_whole_scope')
        if band=='lenient' and not supports&{'aligned','related'}:raise ValueError('bundle_lenient_without_substantial_support')
        if band=='weak' and not supports&{'limited','absent'}:raise ValueError('bundle_weak_without_negative_scope_evidence')
        if band=='weak' and 'unknown' in supports and not any(f['support'] in ('limited','absent') and f['source_quotes'] for f in row['facets']):
            raise ValueError('bundle_weak_unknown_without_core_negative_evidence')
        expected={'strict':{'strict','lenient'},'weak':set()}
        if band in expected and set(possible)!=expected[band]:raise ValueError('bundle_possible_tiers_contradict_band')
        if band=='lenient':
            if set(possible) not in ({'lenient'},{'strict','lenient'}):raise ValueError('bundle_possible_tiers_contradict_band')
            if 'strict' in possible and uncertainty!='non_decisive':raise ValueError('bundle_unresolved_strict_not_disclosed')
        # A possible upper bound concerns every source row, not a selected subset.
        full_bound=_bound(targets,sources,row['target_id'],list(ss))
        if band=='uncertain' and not possible and (full_bound is None or full_bound>=60):
            raise ValueError('bundle_uncertain_without_positive_bound_or_hard_exclusion')
        for tier,threshold in [('strict',80),('lenient',60)]:
            if tier in possible and full_bound is not None and full_bound<threshold:
                raise ValueError('bundle_possible_tier_exceeds_full_scope')
        if band in ('strict','lenient'):_check_positive_bound(targets,sources,row,80 if band=='strict' else 60)
    return copy.deepcopy(rows)


def validate_proposals(rows,targets,sources,*,target_ids=None):
    return _validate_classifications(rows,targets,sources,'band',target_ids=target_ids)


def validate_reviews(rows,targets,sources,*,target_ids=None):
    return _validate_classifications(rows,targets,sources,'verdict',target_ids=target_ids)


def adjudicate(proposals,reviews,targets,sources):
    proposals=validate_proposals(proposals,targets,sources);reviews=validate_reviews(reviews,targets,sources)
    by_target={row['target_id']:row for row in reviews};result=[]
    established={'strict':{'strict','lenient'},'lenient':{'lenient'},'weak':set(),'uncertain':set()}
    for proposal in proposals:
        review=by_target[proposal['target_id']];original=proposal['band'];verdict=review['verdict']
        possible=set(proposal['possible_tiers'])|set(review['possible_tiers'])
        confirmed=established[original]&established[verdict]
        if original==verdict and original!='uncertain':state='confirmed'
        elif {original,verdict}=={'strict','lenient'}:state='partial'
        else:state='uncertain'
        result.append({**proposal,'adjudication':{'version':VERSION,
            'state':state,'confirmed_tiers':sorted(confirmed),
            'possible_tiers':sorted(possible),'review':review}})
    return result


def validate_evaluations(rows,targets,sources):
    _batch(rows,_topics(targets),None);proposals=[];reviews=[]
    for row in rows:
        if set(row)!={'target_id','facets','reason','band','possible_tiers','uncertainty','adjudication'}:raise ValueError('bundle_evaluation_fields_invalid')
        a=row['adjudication']
        if not isinstance(a,dict) or set(a)!={'version','state','confirmed_tiers','possible_tiers','review'}:raise ValueError('bundle_adjudication_fields_invalid')
        proposals.append({k:v for k,v in row.items() if k!='adjudication'});reviews.append(a['review'])
    rebuilt=adjudicate(proposals,reviews,targets,sources)
    if rebuilt!=rows:raise ValueError('bundle_adjudication_changed')
    return copy.deepcopy(rows)


def validate_evidence(evidence,targets,sources):
    if not isinstance(evidence,dict) or evidence.get('provider')!='curriculum_evidence':raise ValueError('bundle_provider_invalid')
    return validate_evaluations(evidence.get('pairs'),targets,sources)


def calculate(targets,sources,evaluations,*,reviewed=True):
    total=len(targets)
    result={'total_topics':total,'coverage_threshold':80,'strict_topic_threshold':80,'lenient_topic_threshold':60,
            'weighting':'topic_count','accounting':'original_target_topics','strict_count':0,'lenient_count':0,'strict_coverage':None,'lenient_coverage':None,
            'strict_matches':{},'lenient_matches':{},'strict_target_ids':[],'lenient_target_ids':[],
            'possible_strict_count':0,'possible_lenient_count':0,'decision':'insufficient','label':'المعلومات لا تكفي لإصدار حكم'}
    if not reviewed or not targets or not sources:return result
    rows=validate_evaluations(evaluations,targets,sources)
    confirmed={tier:[r for r in rows if tier in r['adjudication']['confirmed_tiers']] for tier in TIERS}
    possible={tier:[r['target_id'] for r in rows if tier in r['adjudication']['possible_tiers']] for tier in TIERS}
    result.update(possible_strict_count=len(possible['strict']),possible_lenient_count=len(possible['lenient']))
    if len(confirmed['strict'])*100>=80*total:decision='equivalent'
    elif len(confirmed['lenient'])*100>=80*total:decision='lenient'
    elif len(possible['lenient'])*100<80*total:decision='not_equivalent'
    else:
        result.update(reason='لم تثبت أدلة كافية لحسم توافق بعض مفردات المقررين.',adjudication_review={
            'confirmed_strict_count':len(confirmed['strict']),'possible_strict_count':len(possible['strict']),
            'confirmed_lenient_count':len(confirmed['lenient']),'possible_lenient_count':len(possible['lenient']),
            'pending_target_ids':sorted(r['target_id'] for r in rows if r['adjudication']['state']!='confirmed')})
        return result
    for tier in TIERS:
        chosen=confirmed[tier];result[tier+'_count']=len(chosen);result[tier+'_coverage']=100*len(chosen)/total
        result[tier+'_target_ids']=[r['target_id'] for r in chosen]
        result[tier+'_matches']={r['target_id']:_source_ids(r['adjudication']['review']['facets']) for r in chosen}
    result.update(decision=decision,label={'equivalent':'يمكن معادلته','lenient':'يمكن مع التسامح','not_equivalent':'لا يمكن معادلته'}[decision])
    return result
