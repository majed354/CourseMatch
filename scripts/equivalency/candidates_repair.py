"""Bounded repair grammar: retain valid scans; allow declining an invalid group.

Withdrawing a proposed retrieval group is not an academic grade or evidence
that its subject is absent. Every source pair and full curriculum remain.
"""
from __future__ import annotations
import copy

VERSION='candidate-repair-v1-explicit-group-withdrawal'


def schema(base, requested, required_groups, retained_pairs=None, allowed_targets=None):
    value=copy.deepcopy(base);by_target={}
    for target,source in sorted(requested):by_target.setdefault(target,[]).append(source)
    scan=value['properties']['scan']
    if not by_target:scan['maxItems']=0
    else:
        variants=[]
        for tid,sids in by_target.items():
            row=copy.deepcopy(scan['items']);row['properties']['target_id']={'type':'string','enum':[tid]}
            def bind(node):
                if isinstance(node,dict):
                    props=node.get('properties',{})
                    if 'source_id'in props:props['source_id']={'type':'string','enum':sids}
                    for child in node.values():bind(child)
                elif isinstance(node,list):
                    for child in node:bind(child)
            bind(row['properties']['sources']);variants.append(row)
        scan['items']={'anyOf':variants}
    if retained_pairs is not None:
        eligible={}
        for (tid,sid),pair in retained_pairs.items():
            if pair['status']!='unrelated' and (allowed_targets is None or tid in allowed_targets):eligible.setdefault(tid,set()).add(sid)
        for tid,sid in requested:eligible.setdefault(tid,set()).add(sid)
        variants=[]
        for tid,sids in sorted(eligible.items()):
            if len(sids)<2:continue
            item=copy.deepcopy(value['properties']['groups']['items'])
            item['properties']['target_id']={'type':'string','enum':[tid]}
            item['properties']['source_ids']['items']={'type':'string','enum':sorted(sids)}
            item['properties']['evidence']['items']['properties']['source_id']={'type':'string','enum':sorted(sids)}
            variants.append(item)
        if variants:value['properties']['groups']['items']={'anyOf':variants}
        else:value['properties']['groups']['maxItems']=0
    choices=[]
    for tid,sids in sorted(required_groups):
        choices.append({'type':'object','properties':{'target_id':{'type':'string','enum':[tid]},
                        'source_ids':{'type':'array','const':list(sids)},
                        'reason':{'type':'string','minLength':1,'maxLength':1600}},
                        'required':['target_id','source_ids','reason'],'additionalProperties':False})
    withdrawn={'type':'array','items':{'anyOf':choices}}if choices else {'type':'array','maxItems':0,'items':{'type':'object'}}
    value['properties']['withdrawn_groups']=withdrawn
    value['required']=list(value['required'])+['withdrawn_groups']
    return value


def response(value, required_groups, retained_groups):
    if not isinstance(value,dict)or not {'scan','groups'}<=set(value)or not set(value)<={'scan','groups','withdrawn_groups'}:
        raise ValueError('invalid_candidate_repair_fields')
    withdrawals=value.get('withdrawn_groups',[])
    if not isinstance(withdrawals,list):raise ValueError('invalid_group_withdrawals')
    declined=set()
    for item in withdrawals:
        if (not isinstance(item,dict)or set(item)!={'target_id','source_ids','reason'}or
            not isinstance(item['target_id'],str)or not isinstance(item['source_ids'],list)or
            not 2<=len(item['source_ids'])<=4 or any(not isinstance(s,str)for s in item['source_ids'])or
            len(set(item['source_ids']))!=len(item['source_ids'])or
            not isinstance(item['reason'],str)or not item['reason'].strip()or len(item['reason'])>1600):
            raise ValueError('invalid_group_withdrawal')
        key=(item['target_id'],tuple(sorted(item['source_ids'])))
        if key not in required_groups or key in retained_groups or key in declined:raise ValueError('foreign_or_valid_retained_group_withdrawal')
        declined.add(key)
    if not isinstance(value['groups'],list):raise ValueError('invalid_repair_groups')
    for item in value['groups']:
        if isinstance(item,dict)and isinstance(item.get('source_ids'),list)and all(isinstance(s,str)for s in item['source_ids']):
            if (item.get('target_id'),tuple(sorted(item['source_ids'])))in declined:raise ValueError('group_both_kept_and_withdrawn')
    return {'scan':copy.deepcopy(value['scan']),'groups':copy.deepcopy(value['groups'])},declined,copy.deepcopy(withdrawals)
