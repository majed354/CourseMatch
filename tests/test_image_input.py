"""Raster-PDF and native visual-check contracts. All model replies are mocked.

These are mechanism tests, not measurements of actual OCR accuracy.
"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf
from scripts.equivalency import image_input as image


def answer(rows,reason='stop'):
    return {'done':True,'done_reason':reason,'message':{'content':json.dumps({'rows':rows},ensure_ascii=False)}}


class VisionCaller:
    def __init__(self,replies,capabilities=None):
        self.replies=list(replies);self.calls=[];self.capabilities=capabilities or {}
    def __call__(self,endpoint,payload,timeout=None):
        self.calls.append((endpoint,copy.deepcopy(payload),timeout))
        if endpoint=='show':return {'capabilities':self.capabilities.get(payload['model'],['completion','vision'])}
        result=self.replies.pop(0)
        if isinstance(result,Exception):raise result
        return result
    @property
    def chats(self):return [c for c in self.calls if c[0]=='chat']


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.cache=self.root/'private-cache'
        self.digests={'gemma4:e4b':'primary-digest',image.FALLBACK_MODEL:'fallback-digest'}
        self.local=patch.object(image,'local_model',side_effect=lambda m:self.digests[m]).start();self.addCleanup(patch.stopall)
        self.regions=[{'id':'r1','bboxpixel':[20,15,220,160],'header_bbox':[20,15,220,30],'method':'fixture_chromatic_geometry'}]
        self.detector=patch.object(image,'_table_regions',side_effect=lambda _:copy.deepcopy(self.regions)).start()
    def raster(self,name='raster.pdf',jpeg=True,blank=False,pages=1,text=False,images=1):
        pix=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,240,180),False);pix.clear_with(255)
        if not blank:pix.set_rect(pymupdf.IRect(20,20,220,35),(0,0,0))
        content=pix.tobytes('jpeg' if jpeg else 'png');doc=pymupdf.open()
        for _ in range(pages):
            page=doc.new_page(width=240,height=180)
            for _ in range(images):page.insert_image(page.rect,stream=content)
            if text:page.insert_text((10,70),'Native text')
        path=self.root/name;doc.save(path);doc.close();return path
    def native(self,name='native.pdf',pages=1):
        doc=pymupdf.open()
        for i in range(pages):doc.new_page().insert_text((40,60),f'Topic {i+1} and details')
        path=self.root/name;doc.save(path);doc.close();return path
    def extracted(self,pages=1):
        return {'topics':[{'id':f't{i+1}','text':f'Topic {i+1} and details','page':i+1,'bbox':[10,20,300,50]} for i in range(pages)],'warnings':[],'reviewed':False,'extraction_method':'native_grid'}
    def run_image(self,path,replies,**kw):
        caller=VisionCaller(replies);return image.extract_image_pdf(path,self.cache,caller=caller,**kw),caller

    def layout(self,box=None,numbers=None,found=True):
        value={'found':found,'box':box if box is not None else ([100,100,900,900] if found else None)}
        if numbers is not None:value['printed_row_numbers']=numbers
        return {'done':True,'done_reason':'stop','message':{'content':json.dumps(value)}}

    def spatial(self,rows,box=None,numbers=None,path=None):
        return self.run_image(path or self.raster(),[self.layout(box,numbers),answer(rows),answer(rows)])

    def test_layout_then_qwen_crop_without_heading_and_complete_proof(self):
        path=self.raster();original=path.read_bytes()
        result,caller=self.spatial([{'number':1,'text':'تعريف العقد وأركانه وآثاره'}],numbers=[1],path=path)
        self.assertEqual(path.read_bytes(),original);self.assertEqual(len(result['topics']),1)
        self.assertEqual([c[1]['model'] for c in caller.chats],['gemma4:e4b',image.FALLBACK_MODEL,'gemma4:e4b'])
        self.assertEqual([c[2] for c in caller.chats],[120,180,60]);self.assertFalse(result['reviewed'])
        self.assertEqual(result['topics'][0]['source_row_numbers'],'1')
        proof=result['visual_spatial_proof'];self.assertEqual(proof['version'],image.VERSION)
        self.assertEqual(proof['source_sha256'],result['source_sha256']);self.assertEqual(proof['crop']['source_width'],240)
        self.assertLess(proof['crop']['width'],240);self.assertEqual(len(result['visual_response_proof']),3)
        self.assertNotEqual(caller.chats[0][1]['messages'][1]['images'],caller.chats[1][1]['messages'][1]['images'])
        self.assertTrue(list(self.cache.rglob('source.pdf')));self.assertTrue(list(self.cache.rglob('table-crop.png')))

    def test_layout_unknown_invalid_box_or_counter_is_safe_without_reading(self):
        bad=[self.layout(found=False),self.layout([800,100,200,900]),self.layout([0,0,1001,900]),self.layout(numbers=[1,1]),self.layout([0,0,1,1])]
        for index,reply in enumerate(bad):
            with self.subTest(index=index):
                caller=VisionCaller([reply]);result=image.extract_image_pdf(self.raster(str(index)+'.pdf'),self.cache,caller=caller)
                self.assertEqual(result['topics'],[]);self.assertEqual(len(caller.chats),1)
                self.assertEqual(result['status'],'insufficient_source_text')

    def test_counter_mismatch_and_uncertainty_do_not_accept_first_reading(self):
        for rows in [[{'number':2,'text':'تعريف العقد'}],[{'number':1,'text':'تعريف [غير واضح]'}]]:
            result,caller=self.spatial(rows,numbers=[1],path=self.raster(str(rows[0]['number'])+str(len(rows[0]['text']))+'.pdf'))
            self.assertEqual(result['topics'],[]);self.assertEqual(len(caller.chats),2)
            self.assertFalse(any('يدوي' in w or 'راجع' in w for w in result['warnings']))

    def test_primary_and_reading_failure_have_no_retry_loop_and_raw_saved(self):
        for replies,count in [([TimeoutError()],1),([self.layout(),TimeoutError()],2),([self.layout(),answer([{'number':1,'text':'موضوع'}],reason='length')],2)]:
            caller=VisionCaller(replies);result=image.extract_image_pdf(self.raster(str(count)+str(len(replies))+str(type(replies[-1]))+'.pdf'),self.cache,caller=caller)
            self.assertEqual(result['topics'],[]);self.assertEqual(len(caller.chats),count)
        self.assertTrue(list(self.cache.rglob('raw-response-*.json')))

    def test_printed_numbers_strict_types_duplicates_resets_and_partial_null(self):
        bad=[[{'number':n,'text':'الموضوع'}] for n in ['عنوان المقدمة','1',True,0,101,1.5]]
        bad += [[{'number':n,'text':'الموضوع'} for n in numbers] for numbers in [[1,1],[2,1],[1,None],[1,2,1]]]
        for rows in bad:
            with self.subTest(rows=rows),self.assertRaises(ValueError):image._rows(answer(rows),'image')

    def test_unnumbered_table_remains_valid_without_invented_counter(self):
        result,caller=self.spatial([{'number':None,'text':'الموضوع الأول'},{'number':None,'text':'الموضوع الثاني'}])
        self.assertEqual(len(result['topics']),2);self.assertTrue(all(t['source_row_numbers']=='' for t in result['topics']))

    def test_pure_reviews_and_exams_excluded_compounds_specific_reviews_stay(self):
        rows=[{'number':i,'text':text} for i,text in enumerate(['مراجعة عامة','الاختبار النصفي','الاختبار النصفي وأحكام الوكالة','مراجعة باب الوكالة','تعريف العقد وأركانه وآثاره'],1)]
        result,caller=self.spatial(rows,numbers=[1,2,3,4,5]);self.assertEqual(len(result['topics']),3);self.assertEqual(len(result['excluded_rows']),2)
        for text in ['مراجعة عامة','المراجعة العامة','مراجعة شاملة']:self.assertEqual(image._exclusion_reason(text),'pure_general_review_only')
        for text in ['مراجعة عامة وأحكام الوكالة','مراجعة شاملة: موضوع الوقف']:self.assertIsNone(image._exclusion_reason(text))

    def test_fifteen_raw_rows_preserve_two_pure_reviews(self):
        rows=[{'number':i,'text':'مراجعة عامة' if i in (9,15) else f'موضوع فقهي رقم {i}'} for i in range(1,16)]
        result,caller=self.spatial(rows,numbers=list(range(1,16)))
        self.assertEqual(len(result['topics']),13);self.assertEqual([x['source_row_numbers'] for x in result['excluded_rows']],['9','15'])
        raws=[json.loads(p.read_text()) for p in self.cache.rglob('raw-response-*.json')]
        self.assertIn(15,[len(json.loads(r['message']['content']).get('rows',[])) for r in raws])

    def test_only_explicit_null_total_is_excluded_not_book_title(self):
        result,caller=self.spatial([{'number':1,'text':'تعريف العقد'},{'number':None,'text':'المجموع ٣٤ ساعة'}],numbers=[1])
        self.assertEqual(len(result['topics']),1);self.assertEqual(result['excluded_rows'][0]['reason'],'pure_total_only')
        for text in ['المجموع','المجموع: 34 ساعة','مجموع الساعات ٣٤']:self.assertTrue(image._pure_total(text))
        for text in ['كتاب المجموع','المجموع شرح المهذب','المجموع وأثره في الفقه']:self.assertFalse(image._pure_total(text))
        with self.assertRaises(ValueError):image._rows(answer([{'number':None,'text':'المجموع'},{'number':1,'text':'التقييم الدوري'}]),'image')

    def test_cache_reuses_both_stages_and_revalidates_crop_model_and_typed_rows(self):
        path=self.raster();rows=[{'number':1,'text':'تعريف العقد'},{'number':None,'text':'المجموع'}]
        result,caller=self.spatial(rows,numbers=[1],path=path);cached,caller=self.run_image(path,[])
        self.assertEqual(caller.chats,[]);self.assertEqual(cached['topics'],result['topics'])
        receipt=next(self.cache.rglob('cache.json'));saved=json.loads(receipt.read_text())
        self.assertIs(type(saved['rows'][0]['number']),int);self.assertIsNone(saved['rows'][1]['number'])
        crop=next(self.cache.rglob('table-crop.png'));crop.write_bytes(b'tampered')
        _,caller=self.spatial(rows,numbers=[1],path=path);self.assertEqual(len(caller.chats),3)
        self.digests[image.FALLBACK_MODEL]='new-digest'
        _,caller=self.spatial(rows,numbers=[1],path=path);self.assertEqual(len(caller.chats),3)
        saved=json.loads(receipt.read_text());saved['rows'][0]['number']='1';saved['rows_sha256']=image._json_sha(saved['rows']);receipt.write_text(json.dumps(saved))
        _,caller=self.spatial(rows,numbers=[1],path=path);self.assertEqual(len(caller.chats),3)

    def test_current_spatial_proof_skips_second_read_only_for_same_source_sha(self):
        path=self.raster();result,caller=self.spatial([{'number':1,'text':'تعريف العقد'}],numbers=[1],path=path)
        never=VisionCaller([]);verified=image.verify_pdf_topics(path,result,self.cache,caller=never)
        self.assertEqual(verified,result);self.assertEqual(never.calls,[])
        other=self.raster('other.pdf');caller=VisionCaller([TimeoutError(),TimeoutError()])
        checked=image.verify_pdf_topics(other,result,self.cache,caller=caller)
        self.assertEqual(checked['visual_verification_status'],'unavailable');self.assertEqual(len(caller.chats),2)

    def test_blank_structural_limits_and_fullframe_container_validation(self):
        blank,caller=self.run_image(self.raster(blank=True),[]);self.assertEqual(blank['topics'],[]);self.assertEqual(caller.calls,[])
        for name,kwargs in [('text',{'text':True}),('multi',{'pages':2}),('repeated',{'images':2})]:
            with self.assertRaises(ValueError):image.extract_image_pdf(self.raster(name+'.pdf',**kwargs),self.cache,caller=VisionCaller([]))
        original=self.raster()
        with pymupdf.open(original) as doc:content=doc.extract_image(doc[0].get_images()[0][0])['image']
        with pymupdf.open() as doc:
            page=doc.new_page(width=240,height=180);page.insert_image(pymupdf.Rect(-10,0,230,180),stream=content,keep_proportion=False);target=self.root/'offset.pdf';doc.save(target)
        with self.assertRaises(ValueError):image.extract_image_pdf(target,self.cache,caller=VisionCaller([]))
        for width,height in [(12001,100),(8000,4001)]:
            with pymupdf.open(original) as doc:
                doc.xref_set_key(doc[0].get_images()[0][0],'Width',str(width));doc.xref_set_key(doc[0].get_images()[0][0],'Height',str(height));target=self.root/f'large-{width}.pdf';doc.save(target)
            with self.assertRaises(ValueError):image.extract_image_pdf(target,self.cache,caller=VisionCaller([]))

    def test_native_crop_uses_union_of_topic_fragment_boxes_and_fallback_fullpage(self):
        source=self.extracted();source['topics'][0]['bbox']=[40,50,200,70];source['topics'][0]['fragments']=[{'page':1,'bbox':[60,70,220,90]}]
        caller=VisionCaller([answer([{'id':'t1','text':source['topics'][0]['text']}])]);result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        proof=result['visual_verification_proof']['pages'][0];self.assertEqual(proof['bbox'],[36,46,224,94]);self.assertTrue(proof['cropped_from_topic_bboxes'])
        source['topics'][0].pop('bbox');source['topics'][0].pop('fragments')
        caller=VisionCaller([answer([{'id':'t1','text':source['topics'][0]['text']}])]);result=image.verify_pdf_topics(self.native('no-bbox.pdf'),source,self.cache,caller=caller)
        self.assertFalse(result['visual_verification_proof']['pages'][0]['cropped_from_topic_bboxes'])

    def test_nonvision_or_cloud_layout_does_not_call_remote_or_fallback(self):
        path=self.raster();caller=VisionCaller([],{'gemma4:e4b':['completion']})
        result=image.extract_image_pdf(path,self.cache,caller=caller)
        self.assertEqual(result['topics'],[]);self.assertEqual(caller.chats,[])
        caller=VisionCaller([]);result=image.extract_image_pdf(path,self.cache,model='model:cloud',caller=caller)
        self.assertEqual(result['topics'],[]);self.assertEqual(caller.calls,[])

    def test_layout_truncation_and_invalid_json_saved_before_rejection(self):
        for index,response in enumerate([{'done_reason':'length','message':{'content':'{}'}},{'done_reason':'stop','message':{'content':'{broken'}}]):
            caller=VisionCaller([response]);result=image.extract_image_pdf(self.raster(f'bad-layout-{index}.pdf'),self.cache,caller=caller)
            self.assertEqual(result['topics'],[]);self.assertEqual(len(caller.chats),1)
        self.assertEqual(len(list(self.cache.rglob('raw-response-*.json'))),2)

    def test_native_current_proof_does_not_skip_modified_topic_text(self):
        path=self.raster();result,_=self.spatial([{'number':1,'text':'الموضوع الأصلي'}],numbers=[1],path=path)
        result['topics'][0]['text']='الموضوع المحرف';caller=VisionCaller([TimeoutError(),TimeoutError()])
        checked=image.verify_pdf_topics(path,result,self.cache,caller=caller)
        self.assertEqual(checked['visual_verification_status'],'unavailable');self.assertEqual(len(caller.chats),2)

    def test_source_sha_and_raw_response_tampering_invalidate_spatial_cache(self):
        rows=[{'number':1,'text':'الموضوع'}];p1=self.raster('source-one.pdf');first,_=self.spatial(rows,numbers=[1],path=p1)
        p2=self.raster('source-two.pdf');second,caller=self.spatial(rows,numbers=[1],path=p2)
        self.assertNotEqual(first['cache_key'],second['cache_key']);self.assertEqual(len(caller.chats),3)
        folder=self.cache/image.VERSION/second['cache_key'];raw=folder/second['visual_response_proof'][0]['file'];raw.write_text('{}')
        _,caller=self.spatial(rows,numbers=[1],path=p2);self.assertEqual(len(caller.chats),3)

    def test_optional_layout_counters_absent_is_unknown_empty_list_invalid(self):
        self.assertNotIn('printed_row_numbers',image._layout(self.layout()))
        with self.assertRaisesRegex(ValueError,'uncertain_table_counter'):
            image._layout(self.layout(numbers=[]))
        # Absent counters do not claim the table is unnumbered: the reader may
        # independently return strictly increasing printed integer numbers.
        result,caller=self.spatial([{'number':3,'text':'الموضوع الأول'},{'number':4,'text':'الموضوع الثاني'}])
        self.assertEqual([t['source_row_numbers'] for t in result['topics']],['3','4'])
        self.assertEqual(len(caller.chats),3)

    def test_snap_selects_whole_detected_region_not_free_crop(self):
        pixels=pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,240,180),False);pixels.clear_with(255);content=pixels.tobytes('png')
        regions=[{'id':'topic','bboxpixel':[10,10,230,110],'header_bbox':[10,10,230,20],'method':'chromatic_geometry'},{'id':'assessment','bboxpixel':[10,130,230,170],'header_bbox':[10,130,230,140],'method':'chromatic_geometry'}]
        selection=image._snap_region(content,{'box':[100,100,600,850]},regions)
        self.assertEqual(selection['selected_candidate']['id'],'topic');self.assertEqual(selection['selected_candidate']['bboxpixel'],[10,10,230,110])
        self.assertEqual(selection['origin'],'pixel_table_region')

    def test_snap_tie_missing_or_unproven_region_fails_safe_before_reading(self):
        for index,regions in enumerate([[],[self.regions[0],{**self.regions[0],'id':'duplicate-geometry'}],[{**self.regions[0],'method':''}],[{**self.regions[0],'bboxpixel':[0,0,5,5]}]]):
            self.regions=regions;caller=VisionCaller([self.layout()]);result=image.extract_image_pdf(self.raster(f'regions-{index}.pdf'),self.cache,caller=caller)
            self.assertEqual(result['topics'],[]);self.assertLessEqual(len(caller.chats),1)

    def test_spatial_proof_is_saved_before_reader_even_if_reader_times_out(self):
        caller=VisionCaller([self.layout(),TimeoutError()]);result=image.extract_image_pdf(self.raster(),self.cache,caller=caller)
        self.assertEqual(result['topics'],[]);proof=json.loads(next(self.cache.rglob('spatial-proof.json')).read_text())
        self.assertEqual(proof['selection']['selected_candidate']['bboxpixel'],self.regions[0]['bboxpixel'])
        self.assertEqual(proof['crop']['region_bboxpixel'],self.regions[0]['bboxpixel'])
        self.assertEqual(proof['selection']['origin'],'pixel_table_region');self.assertIsNone(proof['crop']['normalized_box'])

    def test_detector_geometry_change_invalidates_spatial_cache(self):
        path=self.raster();rows=[{'number':1,'text':'الموضوع'}];first,_=self.spatial(rows,numbers=[1],path=path)
        self.regions[0]['bboxpixel'][2]=215;self.regions[0]['header_bbox'][2]=215
        second,caller=self.spatial(rows,numbers=[1],path=path)
        self.assertEqual(len(caller.chats),3);self.assertNotEqual(first['visual_spatial_proof']['crop'],second['visual_spatial_proof']['crop'])

    def test_numeric_audit_range_order_uses_counter_without_changing_values(self):
        rows=[{'number':5,'text':'تفسير ص: 3 البقرة (6-16)'}]
        audited=[{'number':5,'tokens':['٣','١٦','٦']}]
        comparison=image._numeric_comparisons(rows,audited)[0]
        self.assertTrue(comparison['agreed']);self.assertEqual(comparison['corrected_text'],rows[0]['text'])
        self.assertEqual(comparison['original_sequence'],['3','6','16']);self.assertEqual(comparison['audited_sequence'],['3','16','6'])

    def test_numeric_audit_replaces_known_page_conflict_only_and_preserves_raw(self):
        rows=[{'number':5,'text':'تفسير ص: 2 البقرة (6-16)'}]
        replies=[self.layout(numbers=[5]),answer(rows),answer([{'number':5,'text':'تفسير ص: 3 البقرة (16-6)'}])]
        result,caller=self.run_image(self.raster(),replies)
        self.assertEqual(result['topics'][0]['text'],rows[0]['text'])
        self.assertEqual(result['topics'][0]['corrected_text'],'تفسير ص: [غير واضح] البقرة (6-16)')
        self.assertEqual(result['status'],'insufficient_source_text');self.assertEqual(len(caller.chats),3)
        proof=result['visual_spatial_proof']['numeric_audit'];self.assertEqual(proof['status'],'uncertain')
        self.assertEqual(proof['comparisons'][0]['disagreements'][0]['original_value'],'2')
        prompt=caller.chats[2][1]['messages'][1]['content'];self.assertNotIn(rows[0]['text'],prompt)
        self.assertEqual(caller.chats[2][2],60);self.assertEqual(len(result['visual_response_proof']),3)

    def test_numeric_audit_missing_extra_duplicate_and_unknown_are_conservative(self):
        cases=[('رقم 6',[{'number':1,'tokens':['6','16']}],'رقم 6 [غير واضح]'),('رقم 6 16',[{'number':1,'tokens':['6']}],'رقم 6 [غير واضح]'),('رقم 6 6',[{'number':1,'tokens':['6']}],'رقم [غير واضح] 6'),('رقم 6',[{'number':1,'tokens':['[غير واضح]']}],'رقم [غير واضح]')]
        for text,audited,expected in cases:
            with self.subTest(text=text):
                comparison=image._numeric_comparisons([{'number':1,'text':text}],audited)[0]
                self.assertFalse(comparison['agreed']);self.assertEqual(comparison['corrected_text'],expected)

    def test_numeric_audit_invalid_independent_row_identity_or_text_is_rejected(self):
        rows=[{'number':1,'text':'الآية 6'}]
        for audited in [[],[{'number':2,'text':'الآية 6'}],[{'number':True,'text':'الآية 6'}],[{'number':1,'text':6}],[{'number':1,'text':'6'}]]:
            with self.subTest(audited=audited),self.assertRaises(ValueError):image._numeric_rows(answer(audited),rows)

    def test_numeric_audit_proof_required_for_cache_and_skip(self):
        path=self.raster();rows=[{'number':1,'text':'الآية 6'}];result,_=self.spatial(rows,numbers=[1],path=path)
        receipt=next(self.cache.rglob('cache.json'));saved=json.loads(receipt.read_text());saved['spatial_proof'].pop('numeric_audit');receipt.write_text(json.dumps(saved))
        _,caller=self.spatial(rows,numbers=[1],path=path);self.assertEqual(len(caller.chats),3)
        result['visual_spatial_proof'].pop('numeric_audit');caller=VisionCaller([TimeoutError(),TimeoutError()])
        checked=image.verify_pdf_topics(path,result,self.cache,caller=caller)
        self.assertEqual(checked['visual_verification_status'],'unavailable');self.assertEqual(len(caller.chats),2)

    def test_numeric_audit_timeout_returns_safe_insufficient_without_retry(self):
        caller=VisionCaller([self.layout(numbers=[1]),answer([{'number':1,'text':'الآية 6'}]),TimeoutError()])
        result=image.extract_image_pdf(self.raster(),self.cache,caller=caller)
        self.assertEqual(len(result['topics']),1);self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text']);self.assertEqual(result['topics'][0]['text'],'الآية 6');self.assertEqual(result['status'],'insufficient_source_text');self.assertEqual(len(caller.chats),3);self.assertEqual(caller.chats[-1][2],60)
        self.assertEqual(result['visual_spatial_proof']['numeric_audit']['status'],'unavailable');self.assertFalse(list(self.cache.rglob('cache.json')))
        self.assertTrue(list(self.cache.rglob('spatial-proof.json')))

    def test_independent_schema_number_alone_cannot_represent_topic_text(self):
        self.assertEqual(image.NUMERIC_SCHEMA,image.IMAGE_SCHEMA)
        with self.assertRaises(ValueError):image._numeric_rows(answer([{'number':1,'text':'1'}]),[{'number':1,'text':'موضوع 6'}])
        with self.assertRaises(ValueError):image._numeric_rows(answer([{'number':1,'tokens':['1']}]),[{'number':1,'text':'موضوع 6'}])

    def test_invalid_numeric_response_preserves_source_with_formal_insufficiency(self):
        rows=[{'number':1,'text':'الآية 6'}];caller=VisionCaller([self.layout(numbers=[1]),answer(rows),answer([{'number':1,'tokens':['الآية 6']}])])
        result=image.extract_image_pdf(self.raster(),self.cache,caller=caller)
        self.assertEqual(result['topics'][0]['text'],'الآية 6');self.assertEqual(result['topics'][0]['corrected_text'],'الآية [غير واضح]')
        self.assertEqual(result['status'],'insufficient_source_text');self.assertEqual(result['visual_spatial_proof']['numeric_audit']['status'],'unavailable')
        self.assertEqual(len(caller.chats),3);self.assertFalse(list(self.cache.rglob('cache.json')))

    def test_numeric_proof_inconsistent_counter_cannot_fast_skip(self):
        path=self.raster();result,_=self.spatial([{'number':1,'text':'الآية 6'}],numbers=[1],path=path)
        result['visual_spatial_proof']['numeric_audit']['comparisons'][0]['audited_sequence']=['999']
        caller=VisionCaller([TimeoutError(),TimeoutError()]);checked=image.verify_pdf_topics(path,result,self.cache,caller=caller)
        self.assertEqual(checked['visual_verification_status'],'unavailable');self.assertEqual(len(caller.chats),2)

    def test_independent_optional_total_excluded_only_when_explicit_unprinted(self):
        primary=[{'number':1,'text':'الآية 6'}]
        response=answer([{'number':1,'text':'الآية 6'},{'number':None,'text':'المجموع 34 ساعة'}])
        aligned=image._numeric_rows(response,primary);self.assertEqual(len(aligned),1);self.assertEqual(aligned[0]['tokens'],['6'])
        with self.assertRaises(ValueError):image._numeric_rows(answer([{'number':1,'text':'الآية 6'},{'number':None,'text':'كتاب المجموع'}]),primary)
        with self.assertRaises(ValueError):image._numeric_rows(answer([{'number':1,'text':'الآية 6'},{'number':2,'text':'المجموع'}]),primary)

    def test_independent_content_conflict_stays_uncertain_even_when_numbers_match(self):
        primary=[{'number':1,'text':'أحكام البيع في 6 مسائل'}]
        independent=image._numeric_rows(answer([{'number':1,'text':'أحكام الوقف في 6 مسائل'}]),primary)
        comparison=image._numeric_comparisons(primary,independent)[0]
        self.assertFalse(comparison['agreed']);self.assertFalse(comparison['content_agreed']);self.assertIn(image.UNKNOWN,comparison['corrected_text'])
        ordered=[{'number':1,'text':'تفسير البقرة (6-16)'}]
        reversed_reading=image._numeric_rows(answer([{'number':1,'text':'تفسير البقرة (١٦-٦).'}]),ordered)
        self.assertTrue(image._numeric_comparisons(ordered,reversed_reading)[0]['agreed'])

    def test_native_numeric_disagreement_preserves_original_and_marks_uncertainty(self):
        source=self.extracted();source['topics'][0]['text']='تفسير البقرة (6-16)';before=copy.deepcopy(source)
        wrong=[{'id':'t1','text':'تفسير البقرة (1-16)'}]
        caller=VisionCaller([answer(wrong),answer(wrong)])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(source,before);self.assertEqual(result['topics'][0]['text'],before['topics'][0]['text'])
        self.assertEqual(result['topics'][0]['corrected_text'],'تفسير البقرة (6-16) '+image.UNKNOWN)
        self.assertEqual(result['visual_verification_status'],'uncertain')
        finding=result['visual_verification_proof']['numeric_disagreements'][0]
        self.assertEqual(finding['native_numbers'],['6','16']);self.assertEqual(finding['visual_numbers'],['1','16'])
        self.assertEqual(finding['visual_text'],'تفسير البقرة (1-16)');self.assertTrue(finding['retained_native_text'])
        warm=VisionCaller([]);cached=image.verify_pdf_topics(self.root/'native.pdf',source,self.cache,caller=warm)
        self.assertEqual(warm.chats,[]);self.assertEqual(cached['visual_verification_status'],'uncertain')

    def test_native_digit_script_change_is_not_numeric_disagreement(self):
        source=self.extracted();source['topics'][0]['text']='تفسير البقرة (٦-١٦)'
        reading=[{'id':'t1','text':'تفسير البقرة (6-16)'}];caller=VisionCaller([answer(reading),answer(reading)])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(result['visual_verification_proof']['numeric_disagreements'],[])
        self.assertEqual(result['topics'][0]['text'],'تفسير البقرة (٦-١٦)')
        self.assertEqual(image._numeric_sequence('آية ۶ إلى ۱۶'),['6','16'])

    def test_native_preexisting_corrected_text_retained_on_numeric_conflict(self):
        source=self.extracted();source['topics'][0].update(text='نص خام 1',corrected_text='النص الأصلي المصحح 6')
        reading=[{'id':'t1','text':'النص الأصلي المصحح 56'}];caller=VisionCaller([answer(reading),answer(reading)])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['topics'][0]['text'],'نص خام 1');self.assertEqual(result['topics'][0]['corrected_text'],'النص الأصلي المصحح 6 '+image.UNKNOWN)
        self.assertEqual(result['visual_verification_status'],'uncertain')

    def test_native_three_pages_use_one_multi_image_call_and_preserve_raw_text(self):
        path=self.native(pages=3);source=self.extracted(3);before=copy.deepcopy(source)
        caller=VisionCaller([answer([{'id':t['id'],'text':t['text']} for t in source['topics']])])
        result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        self.assertEqual(source,before);self.assertEqual(result['visual_verification_status'],'completed');self.assertFalse(result['reviewed'])
        self.assertEqual([t['text'] for t in result['topics']],[t['text'] for t in source['topics']]);self.assertEqual(len(caller.chats),1)
        self.assertEqual(len(caller.chats[0][1]['messages'][1]['images']),3);self.assertEqual(caller.chats[0][2],60)
        self.assertEqual(len(result['visual_verification_proof']['pages']),3)

    def test_native_changed_reading_falls_back_only_on_affected_page(self):
        path=self.native(pages=3);source=self.extracted(3);source['topics'][1]['text']='Broken extracted letters 2'
        primary=[{'id':f't{i+1}','text':f'Topic {i+1} and details'} for i in range(3)]
        caller=VisionCaller([answer(primary),answer([primary[1]])]);result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(result['topics'][1]['text'],'Broken extracted letters 2');self.assertEqual(result['topics'][1]['corrected_text'],'Topic 2 and details')
        self.assertEqual(len(caller.chats),2);self.assertEqual(len(caller.chats[1][1]['messages'][1]['images']),1)
        self.assertEqual([c[2] for c in caller.chats],[60,300])
        self.assertIn('t2',caller.chats[1][1]['messages'][1]['content']);self.assertNotIn('t1',caller.chats[1][1]['messages'][1]['content'])

    def test_native_failure_is_best_effort_without_erasing_rows(self):
        source=self.extracted();path=self.native();before=copy.deepcopy(source);caller=VisionCaller([TimeoutError(),TimeoutError()])
        result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        self.assertEqual(result['topics'],before['topics']);self.assertEqual(result['visual_verification_status'],'unavailable');self.assertEqual(source,before);self.assertEqual(len(caller.chats),2)
        self.assertEqual([c[2] for c in caller.chats],[60,300])
        self.assertTrue(result['visual_verification_proof']['source_sha256'])

    def test_native_disagreement_preserves_raw_and_marks_unclear_corrected_text(self):
        source=self.extracted();source['topics'][0]['text']='Broken native extraction'
        caller=VisionCaller([answer([{'id':'t1','text':'Topic one and details'}]),answer([{'id':'t1','text':'Topic two and details'}])])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['topics'][0]['text'],'Broken native extraction')
        self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        self.assertEqual(result['visual_verification_status'],'uncertain')

    def test_native_row_count_or_id_mismatch_cannot_silently_change_denominator(self):
        source=self.extracted();caller=VisionCaller([answer([{'id':'invented','text':'Another topic'}]),answer([])])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['topics'],source['topics']);self.assertEqual(result['visual_verification_status'],'unavailable');self.assertEqual(len(list(self.cache.rglob('raw-response-*.json'))),2)

    def test_native_page_cap_and_gap_return_original_without_any_model_call(self):
        path=self.native(pages=4)
        for source in [self.extracted(4),{'topics':[{'id':'a','text':'Topic','page':1},{'id':'b','text':'Topic','page':3}]}]:
            caller=VisionCaller([]);result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
            self.assertEqual(result['topics'],source['topics']);self.assertEqual(result['visual_verification_status'],'unavailable');self.assertEqual(caller.calls,[])

    def test_legacy_vision_input_without_current_proof_is_verified(self):
        source={'topics':[{'id':'a','text':'Topic','page':1}],'extraction_method':'local_vision_transcription','reviewed':False};caller=VisionCaller([answer([{'id':'a','text':'Topic'}])])
        result=image.verify_pdf_topics(self.raster(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(len(caller.chats),1)

    def test_native_topics_hash_changes_cache_boundary(self):
        path=self.native();source=self.extracted();caller=VisionCaller([answer([{'id':'t1','text':source['topics'][0]['text']}])]);first=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        warm=VisionCaller([]);second=image.verify_pdf_topics(path,source,self.cache,caller=warm);self.assertEqual(second['visual_verification_status'],'completed');self.assertEqual(warm.chats,[])
        changed=copy.deepcopy(source);changed['topics'][0]['bbox']=[1,2,3,4];new=VisionCaller([answer([{'id':'t1','text':changed['topics'][0]['text']}])]);third=image.verify_pdf_topics(path,changed,self.cache,caller=new)
        self.assertNotEqual(first['visual_verification_cache_key'],third['visual_verification_cache_key']);self.assertEqual(len(new.chats),1)
