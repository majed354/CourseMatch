#!/usr/bin/env python3
"""Bind every reference to audit readiness. Default dry-run; never changes PDFs.

A native acceptance proves literal/structural agreement, not a visual academic
review. Canonical curator holds override all otherwise valid native proofs.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys
from collections import Counter
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from scripts.review_catalog_topics import catalog_sha256,canonical_sha256
from scripts.equivalency.reference_quality import topics_digest
from scripts.equivalency.extract import norm


def prepared_catalog(catalog, records, native, bound_batches, batch_id, overrides=None):
    updated=copy.deepcopy(catalog);native_by_id={}
    for proof in native['proofs']:
        for row in proof['course_proofs']:
            native_by_id[row['course_id']]={**row,'source_sha256':proof['source_sha256']}
    approvals={}
    for relative,batch in bound_batches.items():
        for entry in batch.get('entries',[]):
            for cid in entry.get('course_ids',[]):
                approvals[(batch.get('batch_id'),cid)]={'topics_sha256':topics_digest(entry['topics']),'source_sha256':entry['source_sha256'],'manifest':relative}
    states=Counter();rows=[]
    for course in updated['courses']+updated.get('library_sources',[]):
        sha=course.get('source_sha256');cid=course['id'];digest=topics_digest(course.get('topics',[]))
        record=records.get(sha);review=course.get('reference_review',{});state='needs_review'
        reason='لم تثبت قراءة جميع مفردات المرجع ونطاقها بعد.';proof={}
        if not course.get('source_pdf'):
            state='missing_source';reason='لا يتوافر توصيف للمقرر.'
        elif not record:
            raise ValueError('reference_missing_from_full_audit: '+cid)
        elif (overrides or {}).get(sha):
            override=overrides[sha]
            if isinstance(override,dict):
                if override.get('state') not in ('needs_review','no_fixed_topics'):raise ValueError('invalid_quality_override_state')
                state=override['state'];reason=override['reason']
            else:reason=override
        elif review.get('comparison_readiness','ready')!='ready':
            reason=review.get('readiness_reason') or 'لم يكتمل تحديد نطاق مفردات المرجع.'
        elif review:
            approved=approvals.get((review.get('batch_id'),cid))
            if approved and approved['source_sha256']==sha and approved['topics_sha256']==digest:
                state='ready_visual';reason='مفردات المرجع منقولة من صفحات المصدر المراجعة.';proof={'manifest':approved['manifest']}
            else:reason='تغير نص المرجع أو لم يثبت ربطه بقراءة الصفحات المعتمدة.'
        elif record.get('review_status')=='no_fixed_topics_existing':
            state='no_fixed_topics';reason='لا يتضمن هذا التوصيف قائمة موضوعات ثابتة قابلة للمقارنة.'
        else:
            proof_native=native_by_id.get(cid)
            candidate=record.get('proposed_topics',[])
            same=[norm(t.get('corrected_text') or t['text']) for t in candidate]==[norm(t.get('corrected_text') or t['text']) for t in course.get('topics',[])]
            if (proof_native and proof_native['source_sha256']==sha and proof_native['canonical_topics_sha256']==digest
                and record.get('review_status')=='verified_native' and not record.get('risk_reasons') and same):
                state='ready_native';reason='تطابق بنيوي ونصي مثبت مع جدول المصدر؛ ليس اعتمادًا بصريًا.'
                proof={'kind':'literal_native_structure','human_visual_review':False}
        quality={'state':state,'source_sha256':sha,'topics_sha256':digest,'audit_batch_id':batch_id,'reason':reason,**proof}
        course['reference_quality']=quality;states[state]+=1
        rows.append({'course_id':cid,'source_sha256':sha,'topics_sha256':digest,'state':state,'reason':reason})
    return updated,{'state_counts':dict(states),'appearances':rows}


def apply_quality(root,audit_dir,native_path,manifest_path,*,apply=False,overrides=None):
    root=Path(root).resolve();audit_dir=Path(audit_dir).resolve();native_path=Path(native_path).resolve();manifest_path=Path(manifest_path).resolve()
    if not manifest_path.is_relative_to(root/'review-batches'):raise ValueError('quality_manifest_requires_review_batches')
    if manifest_path.exists():raise ValueError('quality_manifest_is_immutable_use_new_batch_name')
    catalog_path=root/'equivalency-pilot/catalog.json';before=catalog_path.read_bytes();catalog=json.loads(before)
    if catalog_sha256(catalog)!=catalog['catalog_id']:raise ValueError('catalog_id_mismatch')
    batches={}
    for relative,expected in catalog['source_hashes'].items():
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('bound_source_changed')
        if relative.startswith('review-batches/'):
            value=json.loads(path.read_text())
            if isinstance(value,dict) and value.get('entries') and value.get('batch_id'):batches[relative]=value
    index=json.loads((audit_dir/'index.json').read_text());native=json.loads(native_path.read_text());records={}
    for file in (audit_dir/'records').glob('*.json'):
        value=json.loads(file.read_text());records[value['source_sha256']]=value
    if len(records)!=index['unique_sha_count'] or index['unique_sha_count']!=448:raise ValueError('full_reference_audit_incomplete')
    batch_id=manifest_path.stem
    updated,summary=prepared_catalog(catalog,records,native,batches,batch_id,overrides)
    manifest={'batch_id':batch_id,'catalog_base_id':catalog['catalog_id'],'audit_index_sha256':hashlib.sha256((audit_dir/'index.json').read_bytes()).hexdigest(),
              'native_proof_sha256':hashlib.sha256(native_path.read_bytes()).hexdigest(),'audit_counts':index['status_counts'],
              'definitions':{'ready_visual':'Full topic-page transcription approved and bound to unchanged PDF and topics.','ready_native':'Literal existing text/order/count agrees with bounded native numbered table; no visual academic review claimed.','needs_review':'No final equivalency judgment permitted.'},
              'source_statuses':[{'source_sha256':r['source_sha256'],'source_pdfs':r['source_pdfs'],'review_status':r['review_status'],'risk_reasons':r.get('risk_reasons',[]),'review_pages':r.get('review_pages',[])} for r in sorted(records.values(),key=lambda r:r['source_sha256'])],
              'native_acceptance_proofs':native['proofs'],'overrides':overrides or {},**summary}
    data=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    updated['source_hashes'][manifest_path.relative_to(root).as_posix()]=hashlib.sha256(data).hexdigest();updated['catalog_id']=catalog_sha256(updated)
    proof={'mode':'apply' if apply else 'dry_run','catalog_base_id':catalog['catalog_id'],'catalog_id':updated['catalog_id'],'state_counts':summary['state_counts'],'audited_pdf_count':len(records)}
    if apply:
        if catalog_path.read_bytes()!=before:raise ValueError('catalog_changed_during_quality_review')
        manifest_path.write_bytes(data)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=catalog_path.parent,delete=False) as stream:
            json.dump(updated,stream,ensure_ascii=False,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno());tmp=Path(stream.name)
        tmp.chmod(catalog_path.stat().st_mode&0o777);tmp.replace(catalog_path)
    return proof


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);parser.add_argument('--audit',type=Path,required=True);parser.add_argument('--native',type=Path,required=True);parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--overrides',type=Path);parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    print(json.dumps(apply_quality(args.root,args.audit,args.native,args.manifest,apply=args.apply,overrides=json.loads(args.overrides.read_text()) if args.overrides else None),ensure_ascii=False))
