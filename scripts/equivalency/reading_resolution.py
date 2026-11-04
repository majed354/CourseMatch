"""One image-only enlarged reread, corroborated at every disputed span by OCR.

No text alternatives are shown to the model. Never changes numerals, source
files, topic identities, or grades. Evidence is replayable without inference.
"""
from __future__ import annotations
import copy
from difflib import SequenceMatcher
import io
import json
import os
from pathlib import Path
import subprocess
import time
import unicodedata
from PIL import Image

VERSION = 'focused-reading-v1-independent-context-witnesses'


def enlarged(images):
    from . import image_input as m
    output=[]
    for content in images:
        with Image.open(io.BytesIO(content)) as im:
            w,h=im.size
            if w*2>m.MAX_DIMENSION or h*2>m.MAX_DIMENSION or w*h*4>m.MAX_PIXELS:
                raise ValueError('focused_image_limit')
            result=im.convert('RGB').resize((w*2,h*2),Image.Resampling.LANCZOS)
            stream=io.BytesIO();result.save(stream,format='PNG');output.append(stream.getvalue())
    return output


def _numeric(text):
    return any(c.isnumeric() or unicodedata.category(c).startswith('S') for c in text)


def contexts(candidate, readings, ocr):
    """Witness every changed span, retaining source offsets and word order.

    A single-word substitution requires a unique literal word and two nearby
    alignment anchors with equal offsets; longer edits also require context.
    The reread and independent OCR see the same complete, located source cell.
    """
    from . import image_input as m
    def words(text):
        raw=[(x.start(),x.end(),m._native_comparison_key(x.group(),punctuation=False))
             for x in m._NATIVE_WORDS.finditer(text)]
        result=[];i=0
        while i<len(raw):
            item=raw[i]
            # Orthographic clitic spacing only: retain the explicit waw and all
            # following letters. Never remove arbitrary spaces or invent waw.
            if item[2]=='و' and i+1<len(raw) and text[item[1]:raw[i+1][0]].isspace():
                item=(item[0],raw[i+1][1],'و'+raw[i+1][2]);i+=1
            result.append(item);i+=1
        return result
    target=words(candidate);keys=[w[2]for w in target];ocr_words=words(ocr);other=[w[2]for w in ocr_words]
    if not target or m.UNKNOWN in candidate or _numeric(candidate) or any(_numeric(r)for r in readings):return None
    def occurrences(haystack,needle):
        return sum(haystack[i:i+len(needle)]==needle for i in range(len(haystack)-len(needle)+1))
    def aligned_word(point):
        if occurrences(other,[keys[point]])!=1:return None
        pos=other.index(keys[point])
        if point==0:
            if pos!=0 or len(keys)<3 or other[:3]!=keys[:3]:return None
            pairs=[(1,1),(2,2)]
        elif point==len(keys)-1:
            if pos!=len(other)-1 or len(keys)<3 or other[-3:]!=keys[-3:]:return None
            pairs=[(point-2,pos-2),(point-1,pos-1)]
        else:
            if pos==0 or pos==len(other)-1 or keys[point-1:point+2]!=other[pos-1:pos+2]:return None
            pairs=[(point-1,pos-1),(point+1,pos+1)]
        return {'ocr_start':ocr_words[pos][0],'ocr_end':ocr_words[pos][1],
                'alignment_anchors':[{'candidate_index':ci,'ocr_index':oi,'word':keys[ci]}for ci,oi in pairs]}
    witnesses=[]
    for reader_index,reading in enumerate(readings):
        original=[w[2]for w in words(reading)]
        for tag,a,b,c,d in SequenceMatcher(a=original,b=keys,autojunk=False).get_opcodes():
            if tag=='equal':continue
            # A lost leading/trailing clause cannot be supported by an interior
            # phrase which merely occurs elsewhere in the independently read cell.
            if tag=='delete' and ((c==0 and other[:min(3,len(keys))]!=keys[:min(3,len(keys))]) or
                                  (c==len(keys) and other[-min(3,len(keys)):]!=keys[-min(3,len(keys)):])):
                return None
            points=range(c,d) if d>c else [min(c,len(keys)-1)]
            for point in points:
                selected=None
                minimum=1 if tag=='replace' and b-a==d-c==1 else 3
                for width in range(minimum,min(7,len(keys))+1):
                    for lo in range(max(0,point-width+1),min(point,len(keys)-width)+1):
                        hi=lo+width
                        if tag=='delete' and c>0 and c<len(keys) and not(lo<c<hi):continue
                        phrase=keys[lo:hi]
                        alignment=aligned_word(point)
                        if occurrences(keys,phrase)==1 and occurrences(other,phrase)==1 and alignment:
                            selected={'reader_index':reader_index,'opcode':[tag,a,b,c,d],
                                      'start':target[lo][0],'end':target[hi-1][1],
                                      'quote':candidate[target[lo][0]:target[hi-1][1]],'keys':phrase}
                            if alignment:selected.update(alignment)
                            break
                    if selected:break
                if selected is None:return None
                if selected not in witnesses:witnesses.append(selected)
    return witnesses or None


def _read(directory, record):
    from . import image_input as m
    name=record['file']
    if not isinstance(name,str)or Path(name).name!=name:raise ValueError('focused_evidence_path')
    data=(directory/name).read_bytes()
    if m._sha(data)!=record['sha256']:raise ValueError('focused_evidence_hash')
    return data


def resolve(directory, images, anchor, first, second, baseline, caller, model):
    from . import image_input as m
    if (anchor.get('unlocated_pages')or not anchor.get('regions')or not first or not second or
        _numeric(anchor['text']+first+second) or m._native_pdf_marks_conflict(anchor,first)):
        return None
    proof={'version':VERSION,'status':'unresolved','anchor_sha256':m._json_sha(anchor),
           'baseline_rows':copy.deepcopy(baseline),'inputs':[],'zooms':[],'responses':[],
           'ocr_raws':[],'ocr_identity':None,'identities':[],'witnesses':None}
    try:
        # No extra model wait when the required independent witness is absent.
        exe,tessdata_root,ocr_identity=m._tesseract_identity();proof['ocr_identity']=ocr_identity
        zooms=enlarged(images)
        for i,(content,zoom)in enumerate(zip(images,zooms)):
            for key,asset_bytes in [('inputs',content),('zooms',zoom)]:
                name=f'focused-{key}-{i+1}.png';m._save(directory/name,asset_bytes,binary=True)
                proof[key].append({'file':name,'sha256':m._sha(asset_bytes)})
        identity=m._identity(model,caller);proof['identities'].append(identity)
        # Only spatial IDs and actual pixels reach this reread, never source text
        # or the competing readings. Keep every continuation page in its order.
        focus_anchor={'id':anchor['id'],'pages':anchor['pages'],
                      'regions':[{'page':page,'bbox':[0,0,1000,1000]}for page in anchor['pages']]}
        rows,_=m._chat(caller,directory,identity,zooms,'native',[focus_anchor],120,proof['responses'])
        candidate=rows[0]['text'];proof['candidate']=candidate
        if m.UNKNOWN in candidate or _numeric(candidate):raise ValueError('focused_incomplete_or_numeric')
        if m._native_pdf_marks_conflict(anchor,candidate):raise ValueError('focused_pdf_marks_conflict')
        texts=[]
        for i,record in enumerate(proof['zooms']):
            result=subprocess.run([exe,str(directory/record['file']),'stdout','--tessdata-dir',tessdata_root,'-l','ara','--psm','6'],
                                  capture_output=True,check=False,timeout=15,
                                  env={**os.environ,'OMP_THREAD_LIMIT':'1','OMP_NUM_THREADS':'1'})
            raw=result.stdout;name=f'focused-ocr-{i+1}-{time.time_ns()}.txt';m._save(directory/name,raw,binary=True)
            proof['ocr_raws'].append({'file':name,'sha256':m._sha(raw)})
            if result.returncode or len(raw)>1024*1024:raise ValueError('focused_ocr_failed')
            texts.append(raw.decode('utf-8'))
        proof['witnesses']=contexts(candidate,[first,second],'\n'.join(texts))
        if not proof['witnesses']:raise ValueError('focused_context_not_corroborated')
        proof['status']='resolved'
    except Exception as error:
        proof['failure']=str(error) if isinstance(error,ValueError)else type(error).__name__
    m._save(directory/'focused-attempt.json',proof)
    return proof


def validate(directory, proof, images, anchor, first, second, rows, caller):
    from . import image_input as m
    if proof['version']!=VERSION or proof['anchor_sha256']!=m._json_sha(anchor):raise ValueError('stale_focused_anchor')
    if anchor.get('unlocated_pages')or not anchor.get('regions'):raise ValueError('unlocated_focused_proof')
    if proof['status']!='resolved':
        if rows!=proof['baseline_rows']:raise ValueError('unresolved_focused_rows_changed')
        return
    if _numeric(anchor['text']+first+second):raise ValueError('numeric_focused_proof')
    if len(proof['inputs'])!=len(images)or len(proof['zooms'])!=len(images)or len(proof['ocr_raws'])!=len(images):raise ValueError('focused_image_count')
    for original,zoom,irec,zrec in zip(images,enlarged(images),proof['inputs'],proof['zooms']):
        if _read(directory,irec)!=original or _read(directory,zrec)!=zoom:raise ValueError('stale_focused_pixels')
    if len(proof['responses'])!=1 or len(proof['identities'])!=1:raise ValueError('focused_reader_count')
    identity=proof['identities'][0]
    if m._identity(identity['model'],caller)!=identity:raise ValueError('stale_focused_model')
    response=proof['responses'][0]
    if response['model']!=identity['model']or response['digest']!=identity['digest']:raise ValueError('focused_model_binding')
    candidate=m._rows(json.loads(_read(directory,response)),'native',[anchor['id']])[0]['text']
    if candidate!=proof['candidate'] or m.UNKNOWN in candidate or _numeric(candidate)or m._native_pdf_marks_conflict(anchor,candidate):raise ValueError('stale_focused_candidate')
    if m._tesseract_identity()[2]!=proof['ocr_identity']:raise ValueError('stale_focused_ocr')
    ocr='\n'.join(_read(directory,r).decode('utf-8')for r in proof['ocr_raws'])
    witnesses=contexts(candidate,[first,second],ocr)
    if not witnesses or witnesses!=proof['witnesses']or rows!=[{'id':anchor['id'],'text':candidate}]:raise ValueError('stale_focused_derivation')
