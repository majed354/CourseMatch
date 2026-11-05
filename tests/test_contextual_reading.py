import base64,copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from scripts.equivalency import image_input as m
from scripts.equivalency import contextual_reading as c
from scripts.equivalency import reading_resolution as f
from tests.test_image_input import answer


def reviewed(choices,choice='c0',corrected=None):
    selected=next(x for x in choices if x['id']==choice)
    value={'choice':choice,'coverage':'complete','problems':[],
           'observations':[{'ref':s['ref'],'visible_quote':s['quote'],'image_index':1,'reason':'قرأت الحروف في الصورة.'}for s in selected['spans']]}
    if corrected is not None:value['corrected_text']=corrected
    return {'done':True,'done_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}


class ContextualReadingTests(unittest.TestCase):
    def setUp(self):
        self.anchor={'id':'t1','text':'النظام الإداري وعلاقته بالقانون','pages':[1],'regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
        self.first='النظام الإداري ونشأته وتطوره وعلاقته بالقانون'
        self.second='النظام الإداري نشأته وتطوره وعلاقته بالقانون'
        self.choices=c.plan(self.anchor,self.first,self.second,self.first)
    def test_only_complete_existing_gemma_candidates_are_selectable(self):
        self.assertEqual([x['text']for x in self.choices],[self.first]);self.assertEqual(c.decision(reviewed(self.choices),self.choices,1),self.first)
        self.assertIsNone(c.plan({**self.anchor,'text':'الآيات١٢'},self.first,self.second,self.first))
        self.assertIsNone(c.plan(self.anchor,'[غير واضح]',self.second,'[غير واضح]'))
    def test_every_disputed_span_image_and_literal_quote_are_required(self):
        good=reviewed(self.choices)
        for change in [lambda v:v.update(observations=[]),lambda v:v['observations'][0].update(image_index=2),
                       lambda v:v['observations'][0].update(visible_quote='موضوع آخر'),
                       lambda v:v['observations'].append(copy.deepcopy(v['observations'][0]))]:
            value=json.loads(good['message']['content']);change(value)
            with self.assertRaises(ValueError):c.decision({'done':True,'done_reason':'stop','message':{'content':json.dumps(value)}},self.choices,1)
        bad=copy.deepcopy(good);bad['done_reason']='length'
        with self.assertRaises(ValueError):c.decision(bad,self.choices,1)
    def test_abstention_or_any_remaining_problem_prevents_acceptance(self):
        for value in [{'choice':'none','coverage':'complete','observations':[],'problems':['اختلاف مطبوع']},
                      {'choice':'c0','coverage':'incomplete','observations':[],'problems':[]}]:
            self.assertIsNone(c.decision({'done':True,'done_reason':'stop','message':{'content':json.dumps(value)}},self.choices,1))
    def test_visual_mark_correction_preserves_letters_negations_and_scope(self):
        raw=reviewed(self.choices,corrected=self.first.replace('ونشأته','ونَشأته'))
        value=json.loads(raw['message']['content'])
        for obs in value['observations']:obs['visible_quote']=obs['visible_quote'].replace('ونشأته','ونَشأته')
        raw['message']['content']=json.dumps(value)
        self.assertEqual(c.correction_decision(raw,self.choices,1,self.anchor),self.first.replace('ونشأته','ونَشأته'))
        for changed in [self.first.replace('ونشأته','وحكمه'),self.first+' ولا يصح',self.first.replace('وعلاقته بالقانون',''),self.first+'⅓']:
            with self.assertRaises(ValueError):c.correction_decision(reviewed(self.choices,corrected=changed),self.choices,1,self.anchor)
    def test_explicit_PDF_vowels_cannot_be_reinterpreted(self):
        anchor={**self.anchor,'native_layout_text':'بَيَّنَ حكم العقد','native_layout_proof_sha256':'bound'}
        choices=c.plan(anchor,'بَيَّنَ حكم العقد','حكم العقد','بَيَّنَ حكم العقد')
        with self.assertRaises(ValueError):c.correction_decision(reviewed(choices,corrected='بَيِّنَ حكم العقد'),choices,1,anchor)
    def test_same_digest_is_not_an_independent_reviewer(self):
        with tempfile.TemporaryDirectory()as temp,patch.object(m,'local_model',return_value='same'):
            def caller(endpoint,payload,timeout=None):
                self.assertEqual(endpoint,'show');return {'capabilities':['vision','completion']}
            proof=c.run(Path(temp),[b'original'],self.anchor,self.first,self.second,{'candidate':self.first},caller,'gemma4:e4b')
            self.assertEqual(proof['status'],'unresolved');self.assertEqual(proof['responses'],[])
    def test_real_pipeline_cache_requires_different_model_review_and_its_proof(self):
        anchor={**self.anchor,'text':'النظام الإداري ونشئته وتطوره وعلاقته بالقانون'}
        im=Image.new('RGB',(100,40),'white');stream=io.BytesIO();im.save(stream,format='PNG');images=[stream.getvalue()];calls=[]
        def caller(endpoint,payload,timeout=None):
            if endpoint=='show':return {'capabilities':['vision','completion']}
            calls.append(copy.deepcopy(payload));n=len(calls)
            if n==1:return answer({'t1':self.first})
            if n==2:return answer({'t1':self.second})
            if n==3:return answer({'t1':self.first})
            self.assertEqual(payload['messages'][0]['content'],c.SYSTEM)
            self.assertEqual([base64.b64decode(x)for x in payload['messages'][1]['images']],images)
            return reviewed(json.loads(payload['messages'][1]['content'])['candidate_readings'])
        with tempfile.TemporaryDirectory()as temp,patch.object(m,'local_model',side_effect=lambda model:'G'if model=='gemma4:e4b'else'Q'),patch.object(m,'_tesseract_identity',return_value=('reader','data',{'reader':'fixture'})),patch.object(f.subprocess,'run',return_value=type('Result',(),{'returncode':0,'stdout':b'unrelated'})):
            path=Path(temp);saved=m._transcribe(path,'key',images,'native',[anchor],caller,'gemma4:e4b',60,page_numbers=[1],fallback_timeout=300)
            self.assertEqual(saved['rows'],[{'id':'t1','text':self.first}]);self.assertEqual(len(calls),4)
            self.assertEqual(m._cache_load(path,'key','native',['t1'],caller,native_rows=[anchor],images=images),saved);self.assertEqual(len(calls),4)
            broken=copy.deepcopy(saved);broken['focused_resolution'].pop('contextual_review');m._save(path/'cache.json',broken)
            self.assertIsNone(m._cache_load(path,'key','native',['t1'],caller,native_rows=[anchor],images=images))


class CorrectedContextCacheTests(unittest.TestCase):
    def test_mark_correction_cache_replays_actual_both_reviews_and_rejects_stripping(self):
        anchor={'id':'t1','text':'النظام الإداري ونشئته وتطوره وعلاقته بالقانون','pages':[1],'regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
        first='النظام الإداري ونشأته وتطوره وعلاقته بالقانون';second='النظام الإداري نشأته وتطوره وعلاقته بالقانون'
        im=Image.new('RGB',(100,40),'white');out=io.BytesIO();im.save(out,format='PNG');images=[out.getvalue()];calls=[]
        def caller(endpoint,payload,timeout=None):
            if endpoint=='show':return {'capabilities':['vision','completion']}
            calls.append(copy.deepcopy(payload));n=len(calls)
            if n in [1,3]:return answer({'t1':first})
            if n==2:return answer({'t1':second})
            if n==4:return {'done':True,'done_reason':'stop','message':{'content':json.dumps({'choice':'none','coverage':'complete','problems':['اختلاف تشكيل'],'observations':[]})}}
            choices=json.loads(payload['messages'][1]['content'])['candidate_readings'];raw=reviewed(choices,corrected=first.replace('ونشأته','ونَشأته'));v=json.loads(raw['message']['content'])
            for o in v['observations']:o['visible_quote']=o['visible_quote'].replace('ونشأته','ونَشأته')
            raw['message']['content']=json.dumps(v);return raw
        with tempfile.TemporaryDirectory()as temp,patch.object(m,'local_model',side_effect=lambda model:'G'if model=='gemma4:e4b'else'Q'),patch.object(m,'_tesseract_identity',return_value=('reader','data',{'reader':'fixture'})),patch.object(f.subprocess,'run',return_value=type('Result',(),{'returncode':0,'stdout':b'unrelated'})):
            path=Path(temp);saved=m._transcribe(path,'key',images,'native',[anchor],caller,'gemma4:e4b',60,page_numbers=[1],fallback_timeout=300)
            self.assertEqual(saved['rows'][0]['text'],first.replace('ونشأته','ونَشأته'));self.assertEqual(len(calls),5)
            self.assertEqual(m._cache_load(path,'key','native',['t1'],caller,native_rows=[anchor],images=images),saved);self.assertEqual(len(calls),5)
            broken=copy.deepcopy(saved);broken['focused_resolution']['contextual_review'].pop('correction_review');m._save(path/'cache.json',broken)
            self.assertIsNone(m._cache_load(path,'key','native',['t1'],caller,native_rows=[anchor],images=images))

if __name__=='__main__':unittest.main()
