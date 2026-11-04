import copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from scripts.equivalency import image_input as m
from scripts.equivalency import reading_resolution as f
from tests.test_image_input import VisionCaller,answer


class FocusedReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.anchor={'id':'t1','text':'أحكام التفويض الإداري ومقارنته باللنابة والحلول في النظام',
                     'pages':[1],'regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
        self.good='أحكام التفويض الإداري ومقارنته بالإنابة والحلول في النظام'
        self.native=self.anchor['text'];self.bad='أحكام التفويض الإداري ومقارنته بالإلانة والحلول في النظام';self.baseline=[{'id':'t1','text':'أحكام التفويض الإداري ومقارنته [غير واضح] والحلول في النظام'}]
        im=Image.new('RGB',(100,40),'white');out=io.BytesIO();im.save(out,format='PNG');self.images=[out.getvalue()]
        self.identity={'reader':'fixture','version':'test','psm':6}
        patch.object(m,'local_model',return_value='digest').start();self.addCleanup(patch.stopall)
        self.ocr=patch.object(m,'_tesseract_identity',return_value=('tesseract','data',self.identity)).start()
    def resolve(self,text=None,ocr=None):
        raw=(ocr or self.good).encode()
        run=type('Result',(),{'returncode':0,'stdout':raw})()
        caller=VisionCaller([answer({'t1':text or self.good})])
        def checked_process(argv,**kwargs):
            self.assertEqual(argv[argv.index('--tessdata-dir')+1],'data')
            self.assertTrue(all(isinstance(x,str) for x in argv));return run
        with patch.object(f.subprocess,'run',side_effect=checked_process):
            proof=f.resolve(self.root,self.images,self.anchor,self.good,self.bad,self.baseline,caller,'gemma4:e4b')
        return proof,caller
    def validate(self,proof):
        rows=[{'id':'t1','text':proof['candidate']}]if proof['status']=='resolved'else proof['baseline_rows']
        f.validate(self.root,proof,self.images,self.anchor,self.good,self.bad,rows,VisionCaller([]))
    def test_one_blind_enlarged_read_independent_witness_and_replay(self):
        proof,caller=self.resolve();self.assertEqual(proof['status'],'resolved');self.validate(proof)
        self.assertEqual(len(caller.chats),1);request=caller.chats[0][1]
        self.assertNotIn(self.good,json.dumps(request['messages'],ensure_ascii=False));self.assertNotIn(self.bad,json.dumps(request['messages'],ensure_ascii=False))
        self.assertEqual(self.anchor['text'],self.native);self.assertEqual(self.baseline[0]['text'].count(m.UNKNOWN),1)
        self.assertEqual(proof['witnesses'][0]['quote'],'بالإنابة')
    def test_repeated_same_model_read_without_ocr_witness_stays_uncertain(self):
        proof,_=self.resolve(ocr='كلام مختلف بلا شاهد صحيح');self.assertEqual(proof['status'],'unresolved');self.validate(proof)
    def test_no_extra_model_if_independent_reader_absent(self):
        self.ocr.side_effect=ValueError('unavailable');proof,caller=self.resolve();self.assertEqual(proof['status'],'unresolved');self.assertEqual(caller.chats,[])
    def test_numeric_symbols_and_roman_numbers_never_enter_resolution(self):
        for token in ['1','١','⅓','Ⅳ','²','±']:
            with self.subTest(token=token):
                self.assertIsNone(f.contexts('لفظ '+token,['لفظ مختلف'],'لفظ '+token))
                anchor={**self.anchor,'text':self.bad+' '+token};caller=VisionCaller([])
                self.assertIsNone(f.resolve(self.root,self.images,anchor,self.good,self.bad,self.baseline,caller,'gemma4:e4b'));self.assertEqual(caller.calls,[])
    def test_clitic_spacing_keeps_letters_and_literal_offsets(self):
        new='النظام الإداري و نشأته وتطوره وعلاقته بالقانون'
        old='النظام الإداري نشأته وتطوره وعلاقته بالقانون'
        proofs=f.contexts(new,[new,old],'النظام الإداري ونشأته وتطوره وعلاقته بالقانون')
        self.assertTrue(proofs);self.assertEqual(proofs[0]['quote'],'و نشأته')
        self.assertEqual(new[proofs[0]['start']:proofs[0]['end']],proofs[0]['quote'])
    def test_no_arbitrary_space_removal_or_missing_clauses_or_reordering(self):
        self.assertIsNone(f.contexts('الأصل حملها جميعا',['الأصلح ملها جميعا'],'الأصلح ملها جميعا'))
        for candidate in ['باء جيم دال','ألف باء جيم','ألف جيم دال','جيم باء ألف دال']:
            self.assertIsNone(f.contexts(candidate,['ألف باء جيم دال'],'ألف باء جيم دال'))
        self.assertIsNone(f.contexts('الأصل لم يقبل التأويل عند غياب الدليل',
                                    ['الأصل لا يقبل التأويل عند غياب الدليل'],
                                    'الأصل لا يقبل التأويل عند غياب الدليل ثم لم يثبت الحكم'))
        self.assertIsNone(f.contexts('الأصل معتبر والحكم ثابت',['الأصل ملغى والحكم ثابت'],
                                    'الأصل ملغى وغير معتبر والحكم ثابت'))
        self.assertIsNone(f.contexts('بعد التأويل واجب',['قبل التأويل واجب'],
                                    'قبل الأصل ثم بعد التأويل واجب'))
    def test_cached_candidate_images_response_and_witness_cannot_be_changed(self):
        proof,_=self.resolve();self.validate(proof)
        for mutate in [lambda p:p.update(candidate=self.bad),lambda p:p.update(witnesses=[]),
                       lambda p:p['zooms'][0].update(sha256='0'*64),lambda p:p['responses'][0].update(digest='other'),
                       lambda p:p.update(ocr_identity={'reader':'other'})]:
            changed=copy.deepcopy(proof);mutate(changed)
            with self.assertRaises((ValueError,KeyError)):self.validate(changed)
    def test_cache_stripping_readers_or_focused_proof_never_licenses_clean_rows(self):
        proof,_=self.resolve();rows=[{'id':'t1','text':self.good}];responses=[]
        for text in [self.good,self.bad]:
            name='original-'+str(len(responses))+'.json';m._save(self.root/name,answer({'t1':text}))
            responses.append({'file':name,'sha256':m._sha((self.root/name).read_bytes()),'model':'gemma4:e4b','digest':'digest'})
        saved={'version':m.VERSION,'native_protocol_version':m.NATIVE_PROTOCOL_VERSION,'key':'key','rows':rows,'rows_sha256':m._json_sha(rows),
               'identities':[{'model':'gemma4:e4b','digest':'digest'}],'responses':responses,'focused_resolution':proof}
        m._save(self.root/'cache.json',saved)
        self.assertEqual(m._cache_load(self.root,'key','native',['t1'],VisionCaller([]),native_rows=[self.anchor],images=self.images),saved)
        for count in [0,1,2]:
            changed=copy.deepcopy(saved);changed['responses']=responses[:count];changed['focused_resolution']=None;m._save(self.root/'cache.json',changed)
            self.assertIsNone(m._cache_load(self.root,'key','native',['t1'],VisionCaller([]),native_rows=[self.anchor],images=self.images))

if __name__=='__main__':unittest.main()
