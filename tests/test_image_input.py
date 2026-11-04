"""Raster-PDF and native visual-check contracts. All model replies are mocked.

These are mechanism tests, not measurements of actual OCR accuracy.
"""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import pymupdf
from jsonschema import Draft202012Validator
from scripts.equivalency import image_input as image
from scripts.equivalency.engine import calculate


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
        self.boundary_identity_function=image._tesseract_identity
        # CPU tests never invoke an installed OCR process unless explicitly
        # replacing it with an independent-reader fixture in the boundary tests.
        patch.object(image,'_tesseract_identity',side_effect=ValueError('not_requested_by_fixture')).start()
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

    def test_native_three_pages_use_one_call_per_original_cell_and_preserve_raw_text(self):
        path=self.native(pages=3);source=self.extracted(3);before=copy.deepcopy(source)
        caller=VisionCaller([answer({t['id']:t['text']}) for t in source['topics']])
        result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        self.assertEqual(source,before);self.assertEqual(result['visual_verification_status'],'completed');self.assertFalse(result['reviewed'])
        self.assertEqual([t['text'] for t in result['topics']],[t['text'] for t in source['topics']]);self.assertEqual(len(caller.chats),3)
        self.assertTrue(all(len(c[1]['messages'][1]['images'])==1 for c in caller.chats));self.assertTrue(all(c[2]==60 for c in caller.chats))
        self.assertEqual(len(result['visual_verification_proof']['pages']),3)

    def test_native_changed_reading_falls_back_only_on_affected_page(self):
        path=self.native(pages=3);source=self.extracted(3);source['topics'][1]['text']='Broken extracted letters 2'
        primary=[{'id':f't{i+1}','text':f'Topic {i+1} and details'} for i in range(3)]
        caller=VisionCaller([answer([primary[0]]),answer([primary[1]]),answer([primary[1]]),answer([primary[2]])]);result=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(result['topics'][1]['text'],'Broken extracted letters 2');self.assertEqual(result['topics'][1]['corrected_text'],'Topic 2 and details')
        self.assertEqual(len(caller.chats),4);self.assertEqual(len(caller.chats[2][1]['messages'][1]['images']),1)
        self.assertEqual([c[2] for c in caller.chats],[60,60,300,60])
        self.assertIn('t2',caller.chats[2][1]['messages'][1]['content']);self.assertNotIn('t1',caller.chats[2][1]['messages'][1]['content'])

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

    def test_native_model_schema_requires_every_id_as_text_without_forcing_readable_content(self):
        schema=image.native_schema(['t1','t2']);validator=Draft202012Validator(schema)
        Draft202012Validator.check_schema(schema)
        for bad in [{'rows':[]},{'rows':{}},{'rows':{'t1':'Topic'}},
                    {'rows':{'t1':'Topic','t2':'Other','invented':'Extra'}},
                    {'rows':{'t1':'Topic','t2':''}},{'rows':{'t1':'Topic','t2':None}}]:
            with self.subTest(bad=bad):self.assertFalse(validator.is_valid(bad))
        self.assertTrue(validator.is_valid({'rows':{'t1':image.UNKNOWN,'t2':image.UNKNOWN}}))
        source=self.extracted();unknown={'t1':image.UNKNOWN}
        caller=VisionCaller([answer(unknown),answer(unknown)])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertEqual(result['topics'][0]['text'],source['topics'][0]['text'])
        self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        self.assertTrue(result['topics'][0]['corrected_text'].startswith(source['topics'][0]['text']))
        self.assertFalse(result['reviewed'])
        self.assertEqual(calculate(source['topics'],result['topics'],[])['decision'],'insufficient')
        self.assertEqual(result['visual_verification_proof']['native_protocol_version'],image.NATIVE_PROTOCOL_VERSION)
        self.assertEqual(caller.chats[0][1]['format']['properties']['rows']['type'],'object')
        self.assertNotIn('أعد rows فارغة',caller.chats[0][1]['messages'][0]['content'])

    def test_native_visual_readers_do_not_receive_native_words_or_each_others_reading(self):
        source=self.extracted()
        source['topics'][0].update(text='المصال المرسلة',bbox=[40,50,200,70],
                                   fragments=[{'page':1,'bbox':[60,70,220,90]}])
        caller=VisionCaller([answer({'t1':'المصالح المرسلة'}),answer({'t1':'المصالح المرسلة'})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(len(caller.chats),2)
        for _,payload,_ in caller.chats:
            prompt=payload['messages'][1]['content']
            self.assertNotIn('المصال',prompt)
            self.assertNotIn('المصالح',prompt)
            anchors=json.loads(prompt.split('\n')[-1])
            self.assertEqual(anchors,[{'id':'t1','pages':[1],'regions':[
                {'page':1,'bbox':[21,83,872,500]},
                {'page':1,'bbox':[128,500,979,917]}]}])
            self.assertEqual(set(anchors[0]),{'id','pages','regions'})
        self.assertEqual(result['topics'][0]['text'],'المصال المرسلة')
        self.assertEqual(result['topics'][0]['corrected_text'],'المصالح المرسلة')
        self.assertEqual(result['visual_verification_status'],'completed')
        self.assertEqual(result['visual_verification_proof']['native_protocol_version'],image.NATIVE_PROTOCOL_VERSION)

    def test_native_spatial_anchor_does_not_hide_continuation_without_a_fragment_box(self):
        source=self.extracted();source['topics'][0]['end_page']=2
        caller=VisionCaller([answer({'t1':source['topics'][0]['text']})])
        result=image.verify_pdf_topics(self.native(pages=2),source,self.cache,caller=caller)
        prompt=caller.chats[0][1]['messages'][1]['content']
        anchor=json.loads(prompt.split('\n')[-1])[0]
        self.assertEqual(anchor['pages'],[1,2])
        self.assertEqual({r['page'] for r in anchor['regions']},{1})
        self.assertEqual(anchor['unlocated_pages'],[2])
        self.assertEqual(len(caller.chats[0][1]['messages'][1]['images']),2)
        self.assertEqual(result['topics'][0]['end_page'],2)

    def test_native_map_order_is_canonical_but_legacy_array_order_remains_strict(self):
        response=answer({'t2':'Topic two','t1':'Topic one'});before=copy.deepcopy(response)
        self.assertEqual(image._rows(response,'native',['t1','t2']),
                         [{'id':'t1','text':'Topic one'},{'id':'t2','text':'Topic two'}])
        self.assertEqual(response,before)
        for rows in [{'t1':'Topic'},{'t1':'Topic','t2':'Other','extra':'No'},
                     [{'id':'t2','text':'Topic two'},{'id':'t1','text':'Topic one'}]]:
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,'native_row_identity_mismatch'):
                image._rows(answer(rows),'native',['t1','t2'])
        legacy=[{'id':'t1','text':'Topic one'},{'id':'t2','text':'Topic two'}]
        self.assertEqual(image._rows(answer(legacy),'native',['t1','t2']),legacy)

    def test_native_fallback_keeps_all_anchors_on_a_partially_affected_page(self):
        native=[{'id':'t1','text':'Unaffected page one','pages':[1]},
                {'id':'t2','text':'Anchor before','pages':[2]},
                {'id':'t3','text':'Broken native','pages':[2]},
                {'id':'t4','text':'Anchor after','pages':[2]},
                {'id':'t5','text':'Unaffected page three','pages':[3]}]
        primary={n['id']:n['text'] for n in native};primary['t3']='Correct visual reading'
        fallback={'t4':'Unrelated fallback drift','t3':'Correct visual reading','t2':image.UNKNOWN}
        directory=image._directory(self.cache/'partial-page')
        caller=VisionCaller([answer(primary),answer(fallback)])
        result=image._transcribe(directory,'partial-page-key',[b'one',b'two',b'three'],
                                 'native',native,caller,'gemma4:e4b',60,page_numbers=[1,2,3],fallback_timeout=300)
        payload=caller.chats[1][1]
        self.assertEqual(payload['format']['properties']['rows']['required'],['t2','t3','t4'])
        self.assertEqual(payload['messages'][1]['images'],[base64.b64encode(b'two').decode()])
        self.assertEqual(result['rows'],[{'id':key,'text':primary[key]} for key in primary])
        self.assertEqual([call[2] for call in caller.chats],[60,300])
        self.assertEqual(result['native_protocol_version'],image.NATIVE_PROTOCOL_VERSION)

    def test_native_page_anchor_closure_includes_continuations_without_replacing_unaffected_primary(self):
        native=[{'id':'a','text':'First anchor','pages':[1]},
                {'id':'b','text':'Broken native','pages':[1]},
                {'id':'c','text':'Continuation one two','pages':[1,2]},
                {'id':'d','text':'Continuation two three','pages':[2,3]},
                {'id':'e','text':'Last anchor','pages':[3]}]
        primary={n['id']:n['text'] for n in native};primary['b']='Correct visual reading'
        fallback={key:('Correct visual reading' if key=='b' else 'Different unrelated anchor') for key in reversed(primary)}
        directory=image._directory(self.cache/'closure')
        caller=VisionCaller([answer(primary),answer(fallback)])
        result=image._transcribe(directory,'closure-key',[b'one',b'two',b'three'],'native',native,caller,
                                 'gemma4:e4b',60,page_numbers=[1,2,3],fallback_timeout=300)
        self.assertEqual(caller.chats[1][1]['format']['properties']['rows']['required'],list(primary))
        self.assertEqual(len(caller.chats[1][1]['messages'][1]['images']),3)
        self.assertEqual(result['rows'],[{'id':key,'text':primary[key]} for key in primary])

    def test_native_older_protocol_cache_is_not_reused_and_raw_map_response_is_retained(self):
        path=self.native();source=self.extracted();reply=answer({'t1':source['topics'][0]['text']})
        caller=VisionCaller([reply]);first=image.verify_pdf_topics(path,source,self.cache,caller=caller)
        receipt=next(self.cache.rglob('cache.json'));saved=json.loads(receipt.read_text())
        self.assertEqual(saved.pop('native_protocol_version'),image.NATIVE_PROTOCOL_VERSION)
        receipt.write_text(json.dumps(saved))
        fresh=VisionCaller([reply]);second=image.verify_pdf_topics(path,source,self.cache,caller=fresh)
        self.assertEqual(len(fresh.chats),1);self.assertEqual(second['visual_verification_status'],'completed')
        raw=list(self.cache.rglob('raw-response-*.json'))
        self.assertEqual(len(raw),2)
        self.assertTrue(all(isinstance(json.loads(json.loads(p.read_text())['message']['content'])['rows'],dict) for p in raw))
        self.assertEqual(first['visual_verification_cache_key'],second['visual_verification_cache_key'])

    def test_native_orthographic_agreement_keeps_primary_text_exactly(self):
        pairs=[('طرق التأليف فيه.','طرق التأليف فيه .'),
               ('حجية الإجماع','حجية الاجماع'),
               ('آثار الإذن، وأحكامه.','اثار الاذن واحكامه'),
               ('استعمال ٱلأدوات','استعمال الادوات')]
        for primary,second in pairs:
            with self.subTest(primary=primary):
                merged=image._merge_rows([{'id':'t1','text':primary}],[{'id':'t1','text':second}],'native')
                self.assertEqual(merged,[{'id':'t1','text':primary}])
                self.assertEqual(image._merge_text(primary,second,native=True),primary)

    def test_native_terminal_fathatan_and_kaf_are_bounded_comparison_variants(self):
        forms=['صداقا','صداقاً','صداقًا']
        for first in forms:
            for second in forms:
                with self.subTest(first=first,second=second):
                    self.assertTrue(image._native_readings_agree(first,second,punctuation=True))
                    self.assertEqual(image._merge_text(first,second,native=True),first)
        for primary,other in [('حكم نکاح الكتابية','حكم نكاح الكتابية'),('نكاح','نکاح')]:
            self.assertTrue(image._native_readings_agree(primary,other,punctuation=False))
            self.assertEqual(image._merge_text(primary,other,native=True),primary)
            self.assertEqual(image._native_three_way_text(other,primary,other),primary)
        negatives=[('المُكرِه','المُكرَه'),('المكره','المُكرِه'),
                   ('أحكام الأيمان','أحكام الإيمان'),('آيات','أيات'),
                   ('مبتدأُ','مبتدأَ'),('مبتدأ','مبتدأً'),('فاعل','فاعلًا'),
                   ('صداق','صداقًا'),('صداقا','صداقٌا'),('صِداقا','صَداقاً'),
                   ('صداقًاهم','صداقاهم'),('صداقاًه','صداقاه'),
                   ('لا يجوز صداقًا','يجوز صداقا'),('صداقا 3','صداقًا 3'),
                   ('البقرة 1-3 صداقا','البقرة 1،3 صداقاً'),
                   ('صداقا','مهرًا'),('بناء','بناءً'),('نكاح','نڪاح'),('في','فی')]
        for first,second in negatives:
            with self.subTest(first=first,second=second):
                self.assertFalse(image._native_readings_agree(first,second,punctuation=False))
                self.assertIn(image.UNKNOWN,image._merge_text(first,second,native=True))
        self.assertFalse(image._native_complete_match('صداقا','صداقًا '+image.UNKNOWN))
        self.assertIn(image.UNKNOWN,image._merge_text('نکاح','نكاح'))
        self.assertEqual(image._native_agreement_text('صداقاً نکاح',punctuation=False),'صداقاً نکاح')

    def test_actual_full_t5_and_t7_readings_keep_raw_primary_and_unrelated_word_boundary_uncertainty(self):
        # Immutable real diagnostic strings. Mock replay tests the mechanism;
        # it is neither a new OCR run nor a correction of that historical source.
        t5='الصداق وأحكامه الصداق: تعريفه، وبيان ما يصلح أن يكون صداقا، وحكمه، وحكم تأجيله، والحالات التي تستحق فيها المرأة الصداق كله أو نصفه. والمفوضة: تعريفها، وبيان أحكامها. وصداق المثل: المراد به، وكيفية تقديره، والحالات التي يطبق فيها. والصداق في النكاح الفاسد'
        p5='الصداق وأحكامه\nالصداق: تعريفه، وبيان ما يصلح أن يكون صداقاً، وحكمه، وحكم تأجيله، والحالات التي تستحق فيها المرأة الصداق كله أو نصفه. والمفوضة: تعريفها، وبيان أحكامها. وصداق المثل: المراد به، وكيفية تقديره، والحالات التي يطبق فيها. والصداق في النكاح الفاسد'
        t7='. أنكحة الكفار حكم نكاح الكتابية، وشروط جوازه. وأنكحة الكفار فيما بينهم، والحكم إذا ترافعوا إلينا، أو إذا أسلم الزوجان معاأو أحدهما.'
        p7='. أنكحة الكفار\nحكم نکاح الكتابية، وشروط جوازه. وأنكحة الكفار فيما بينهم، والحكم إذا ترافعوا إلينا، أو إذا أسلم الزوجان معاً أو أحدهما.'
        q7='حكم نكاح الكتابة، وشروط جوازه. وأنكحة الكفار فيما بينهم، والحكم إذا ترافعا إلينا، أو إذا أسلم الزوجان معاً أو أحدهما.'
        for text in (p5,p5.replace('صداقاً','صداقًا')):
            self.assertTrue(image._native_complete_match(t5,text))
            self.assertEqual(image._merge_text(text,t5,native=True),text)
        self.assertFalse(image._native_complete_match(t7,p7))
        source={'topics':[{'id':'t5','text':t5,'page':1,'bbox':[10,20,300,90]},
                          {'id':'t7','text':t7,'page':1,'bbox':[10,100,300,170]}],'warnings':[],'reviewed':False}
        original=copy.deepcopy(source);replies=[answer({'t5':p5}),answer({'t7':p7}),answer({'t7':q7})]
        before=copy.deepcopy(replies);caller=VisionCaller(replies)
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual([c[2]for c in caller.chats],[60,60,300])
        self.assertEqual(result['topics'][0]['corrected_text'],' '.join(p5.split()))
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertIn(image.UNKNOWN,result['topics'][1]['corrected_text'])
        self.assertEqual(source,original);self.assertEqual(replies,before)
        self.assertEqual([t['text']for t in result['topics']],[t5,t7])
        self.assertCountEqual([json.loads(p.read_text())for p in self.cache.rglob('raw-response-*.json')],before)

    def test_native_numeric_row_keeps_old_strict_spelling_and_requires_fallback(self):
        source=self.extracted();source['topics'][0]['text']='صداقا ونكاح ٣'
        primary=answer({'t1':'صداقاً ونکاح 3'});fallback=answer({'t1':'صداقا ونكاح 3'})
        caller=VisionCaller([primary,fallback]);result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual([c[2]for c in caller.chats],[60,300])
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        self.assertEqual(result['visual_verification_proof']['numeric_disagreements'],[])

    def test_native_current_cache_is_separate_from_v7_and_preserves_prior_raw(self):
        path=self.native();source=self.extracted();source['topics'][0]['text']='حكم نكاح الكتابية'
        reply=answer({'t1':source['topics'][0]['text']})
        with patch.object(image,'NATIVE_PROTOCOL_VERSION','native-cell-v7-unlocated-native-binding'):
            previous=image.verify_pdf_topics(path,source,self.cache,caller=VisionCaller([reply]))
        old={p:p.read_bytes()for p in self.cache.rglob('*')if p.is_file()}
        current=VisionCaller([reply]);fresh=image.verify_pdf_topics(path,source,self.cache,caller=current)
        self.assertNotEqual(previous['visual_verification_cache_key'],fresh['visual_verification_cache_key'])
        self.assertEqual(len(current.chats),1)
        self.assertEqual(fresh['visual_verification_proof']['native_protocol_version'],image.NATIVE_PROTOCOL_VERSION)
        self.assertTrue(all(p.read_bytes()==data for p,data in old.items()))
        warm=VisionCaller([]);image.verify_pdf_topics(path,source,self.cache,caller=warm)
        self.assertEqual(warm.chats,[])

    def test_actual_t7_spacing_needs_unique_independent_phrase_and_keeps_primary_raw(self):
        native='. أنكحة الكفار حكم نكاح الكتابية، وشروط جوازه. وأنكحة الكفار فيما بينهم، والحكم إذا ترافعوا إلينا، أو إذا أسلم الزوجان معاأو أحدهما.'
        primary='. أنكحة الكفار\nحكم نکاح الكتابية، وشروط جوازه. وأنكحة الكفار فيما بينهم، والحكم إذا ترافعوا إلينا، أو إذا أسلم الزوجان معاً أو أحدهما.'
        tess='. أنكحة الكفار\nحكم نكاح الكتابية» وشروط جوازه. وأنكحة الكفار فيما بينهم» والحكم إذا ترافعوا إليناء أو إذا\nأسلم الزوجان معاً أو أحدهما.\n'
        source=self.extracted();source['topics'][0]['text']=native;before=copy.deepcopy(source)
        raw=answer({'t1':primary});raw_before=copy.deepcopy(raw)
        identity={'reader':'tesseract','version':'fixture','executable_sha256':'binary','ara_traineddata_sha256':'language','language':'ara','psm':6,'omp_threads':1}
        with patch.object(image,'_tesseract_identity',return_value=('/fixture/tesseract','/fixture/tessdata',identity)), \
             patch.object(image.subprocess,'run',return_value=Mock(stdout=tess.encode(),returncode=0)) as process:
            caller=VisionCaller([raw]);result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
            self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(len(caller.chats),1)
            self.assertEqual(result['topics'][0]['corrected_text'],' '.join(primary.split()))
            self.assertEqual(result['topics'][0]['text'],native);self.assertEqual(source,before);self.assertEqual(raw,raw_before)
            proofs=result['visual_verification_proof'];boundary=proofs['cells'][0]['boundary_corroborations'][0]
            self.assertEqual(boundary['phrases'][0]['quote'],'الزوجان معاً أو أحدهما')
            self.assertEqual(boundary['identity'],identity)
            directory=self.cache/image.VERSION/result['visual_verification_cache_key']
            for entry in boundary['inputs']+boundary['raws']:
                self.assertEqual(image._sha((directory/entry['file']).read_bytes()),entry['sha256'])
            self.assertEqual((directory/boundary['raws'][0]['file']).read_text(),tess)
            self.assertEqual(process.call_args.kwargs['timeout'],15)
            self.assertEqual(process.call_args.kwargs['env']['OMP_THREAD_LIMIT'],'1')
            self.assertEqual(process.call_args.kwargs['env']['OMP_NUM_THREADS'],'1')
            warm=VisionCaller([]);again=image.verify_pdf_topics(self.root/'native.pdf',source,self.cache,caller=warm)
            self.assertEqual(again['topics'],result['topics']);self.assertEqual(warm.chats,[]);self.assertEqual(process.call_count,1)

    def test_boundary_edit_is_not_agreement_without_unique_primary_context(self):
        native='قال إن ما الحكم ثابت هنا';primary='قال إنما الحكم ثابت هنا'
        phrases=image._boundary_phrases(native,primary)
        self.assertTrue(phrases)
        self.assertFalse(image._native_complete_match(native,primary))
        for independent,expected in [('قال إنما الحكم ثابت هنا',True),
                                     ('قال إن ما الحكم ثابت هنا',False),
                                     ('إنما الحكم ثابت',False),
                                     ('قال إنما الحكم ثابت هنا. قال إنما الحكم ثابت هنا',False),
                                     ('قال إنما العلم ثابت هنا',False)]:
            with self.subTest(independent=independent):self.assertEqual(image._boundary_witnesses(phrases,independent),expected)
        multiple=image._boundary_phrases('قال إن ما الحكم كل ما وضح هنا','قال إنما الحكم كلما وضح هنا')
        self.assertEqual(len(multiple),2)
        self.assertTrue(image._boundary_witnesses(multiple,'قال إنما الحكم كلما وضح هنا'))
        self.assertFalse(image._boundary_witnesses(multiple,'قال إنما الحكم كل ما وضح هنا'))
        repeated_native='قال إن ما الحكم ثابت. قال إن ما الحكم ثابت'
        repeated_primary='قال إنما الحكم ثابت. قال إنما الحكم ثابت'
        single_independent='قال إنما الحكم ثابت'
        self.assertEqual(single_independent.count('قال إنما الحكم'),1)
        self.assertIsNone(image._boundary_phrases(repeated_native,repeated_primary))
        with patch.object(image,'_tesseract_identity')as independent:
            anchor={'id':'t1','text':repeated_native,'regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
            self.assertIsNone(image._boundary_corroboration(image._directory(self.cache/'repeated-primary'),[b'image'],anchor,repeated_primary))
            independent.assert_not_called()
        for native,primary in [('لا يجوز الحكم','يجوز الحكم'),('إنما الحكم','إما الحكم'),
                               ('المُكرِه حاضر','المُكرَه حاضر'),('أحكام الأيمان','أحكام الإيمان'),
                               ('إن ما الحكم 3','إنما الحكم 3'),('إن ما الحكم ١-٣','إنما الحكم 1،3'),
                               ('C ++ ثابت','C++ ثابت'),('إن ما '+image.UNKNOWN,'إنما '+image.UNKNOWN)]:
            with self.subTest(native=native,primary=primary):self.assertIsNone(image._boundary_phrases(native,primary))

    def test_boundary_reader_unavailable_failed_or_unsupported_abstains(self):
        anchor={'id':'t1','text':'قال إن ما الحكم ثابت هنا','regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
        primary='قال إنما الحكم ثابت هنا';directory=image._directory(self.cache/'boundary-failures')
        identity=('/fixture/tesseract','/fixture/tessdata',{'reader':'tesseract'})
        self.assertIsNone(image._boundary_corroboration(directory,[b'image'],anchor,primary))
        for response in [Mock(stdout=b'',returncode=1),Mock(stdout='قال إن ما الحكم ثابت هنا'.encode(),returncode=0),image.subprocess.TimeoutExpired('tesseract',15)]:
            with self.subTest(response=type(response).__name__),patch.object(image,'_tesseract_identity',return_value=identity), \
                 patch.object(image.subprocess,'run',side_effect=response if isinstance(response,Exception) else None,return_value=response):
                self.assertIsNone(image._boundary_corroboration(directory,[b'image'],anchor,primary))
        attempts=[json.loads(p.read_text())for p in directory.glob('boundary-attempt-*.json')]
        self.assertEqual({x['status']for x in attempts},{'unavailable','not_supported'})
        self.assertTrue(all(x['identity']==identity[2] and x['inputs'][0]['sha256']==image._sha(b'image') for x in attempts))

    def test_boundary_witness_cannot_authorize_unlocated_or_numeric_row(self):
        for unlocated,numeric in [(True,False),(False,True)]:
            source=self.extracted();source['topics'][0]['text']='قال إن ما الحكم ثابت'+(' 3'if numeric else '')
            if unlocated:source['topics'][0].pop('bbox')
            primary='قال إنما الحكم ثابت'+(' 3'if numeric else '')
            with self.subTest(unlocated=unlocated,numeric=numeric),patch.object(image,'_tesseract_identity')as independent:
                fallback=primary if unlocated else source['topics'][0]['text']
                caller=VisionCaller([answer({'t1':primary}),answer({'t1':fallback})])
                result=image.verify_pdf_topics(self.native(str(unlocated)+'.pdf'),source,self.cache/str(unlocated),caller=caller)
                self.assertEqual(len(caller.chats),2);independent.assert_not_called()
                self.assertEqual(result['visual_verification_status'],'uncertain')
                self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])

    def test_boundary_cache_rechecks_text_image_raw_uniqueness_and_reader_identity(self):
        directory=image._directory(self.cache/'boundary-cache');original='قال إن ما الحكم ثابت هنا';primary='قال إنما الحكم ثابت هنا'
        anchor={'id':'t1','text':original,'pages':[1],'regions':[{'page':1,'bbox':[0,0,1000,1000]}]}
        identity={'reader':'tesseract','version':'fixture','executable_sha256':'exe','ara_traineddata_sha256':'trained'}
        with patch.object(image,'_tesseract_identity',return_value=('/fixture/tesseract','/fixture/tessdata',identity)), \
             patch.object(image.subprocess,'run',return_value=Mock(stdout=primary.encode(),returncode=0)):
            saved=image._transcribe(directory,'test-key',[b'image'],'native',[anchor],VisionCaller([answer({'t1':primary})]),'gemma4:e4b',60,page_numbers=[1])
            self.assertEqual(saved['rows'][0]['text'],primary)
            self.assertIsNotNone(image._cache_load(directory,'test-key','native',['t1'],VisionCaller([]),native_rows=[anchor],images=[b'image']))
            raw=directory/saved['boundary_corroborations'][0]['raws'][0]['file'];raw_original=raw.read_bytes()
            inp=directory/saved['boundary_corroborations'][0]['inputs'][0]['file'];input_original=inp.read_bytes()
            for corruption in ('raw','duplicate_with_updated_sha','image','identity','missing_proof','quote','traversal'):
                with self.subTest(corruption=corruption):
                    modified=copy.deepcopy(saved);proof=modified['boundary_corroborations'][0]
                    if corruption=='raw':raw.write_bytes(b'changed raw')
                    elif corruption=='duplicate_with_updated_sha':
                        raw.write_bytes((primary+' '+primary).encode());proof['raws'][0]['sha256']=image._sha(raw.read_bytes())
                    elif corruption=='image':inp.write_bytes(b'other-image');proof['inputs'][0]['sha256']=image._sha(inp.read_bytes())
                    elif corruption=='identity':proof['identity']['ara_traineddata_sha256']='changed'
                    elif corruption=='missing_proof':modified.pop('boundary_corroborations')
                    elif corruption=='quote':proof['phrases'][0]['quote']='unrelated quote'
                    else:proof['raws'][0]['file']='../outside.txt'
                    image._save(directory/'cache.json',modified)
                    self.assertIsNone(image._cache_load(directory,'test-key','native',['t1'],VisionCaller([]),native_rows=[anchor],images=[b'image']))
                    raw.write_bytes(raw_original);inp.write_bytes(input_original)

    def test_boundary_identity_binds_language_data_and_finds_homebrew_without_path(self):
        with tempfile.TemporaryDirectory()as temp:
            data=Path(temp);(data/'ara.traineddata').write_bytes(b'fixed-language-data')
            executable=data/'tesseract';executable.write_bytes(b'fixed-executable')
            replies=[Mock(stdout=b'tesseract test-version\n'),Mock(stdout=('List of available languages in "'+str(data)+'" (1):\nara\n').encode())]
            # Call the actual function, bypassing only the default no-process fixture.
            with patch.object(image.shutil,'which',return_value=str(executable)),patch.object(image.subprocess,'run',side_effect=replies):
                identity=self.boundary_identity_function()[2]
            self.assertEqual(identity['ara_traineddata_sha256'],image._sha(b'fixed-language-data'))
            self.assertEqual(identity['executable_sha256'],image._sha(b'fixed-executable'))
            replies=[Mock(stdout=b'tesseract test-version\n'),Mock(stdout=('List of available languages in "'+str(data)+'" (1):\nara\n').encode())]
            original_read=Path.read_bytes
            with patch.object(image.shutil,'which',return_value=None),patch.object(image.os.path,'isfile',side_effect=lambda p:p=='/opt/homebrew/bin/tesseract'), \
                 patch.object(image.os,'access',return_value=True),patch.object(image.subprocess,'run',side_effect=replies)as process, \
                 patch.object(Path,'read_bytes',lambda p:b'homebrew-binary'if str(p)=='/opt/homebrew/bin/tesseract'else original_read(p)):
                resolved=self.boundary_identity_function()
            self.assertEqual(process.call_args_list[0].args[0][0],'/opt/homebrew/bin/tesseract')
            self.assertEqual(resolved[2]['executable_sha256'],image._sha(b'homebrew-binary'))

    def test_native_orthographic_agreement_does_not_hide_words_negation_unknown_or_numeric_ranges(self):
        pairs=[('حجية الإجماع','حجية القياس'),('تجب النفقة','لا تجب النفقة'),
               ('تجب النفقة','تجب '+image.UNKNOWN),
               ('تفسير الآيات 1-3','تفسير الآيات 13'),
               ('تفسير الآيات 1-3','تفسير الآيات 1،3'),
               ('الكفر وأحكامه','القفر وأحكامه'),
               ('الهبة وشروطها','التبة وشروطها')]
        for primary,second in pairs:
            with self.subTest(primary=primary,second=second):
                self.assertIn(image.UNKNOWN,image._merge_text(primary,second,native=True))

    def test_image_text_agreement_stays_strict_despite_native_spelling_tolerance(self):
        for primary,second in [('طرق التأليف فيه.','طرق التأليف فيه .'),('حجية الإجماع','حجية الاجماع')]:
            with self.subTest(primary=primary):
                self.assertIn(image.UNKNOWN,image._merge_text(primary,second))
                merged=image._merge_rows([{'number':1,'text':primary}],[{'number':1,'text':second}],'image')
                self.assertIn(image.UNKNOWN,merged[0]['text'])

    def test_native_two_readers_can_agree_on_punctuation_and_hamza_without_false_uncertainty(self):
        source={'topics':[{'id':'t1','text':'طرق التاليف فيه.','page':1,'bbox':[10,20,300,50]},
                          {'id':'t2','text':'حجية الاجماع','page':1,'bbox':[10,50,300,80]}],
                'warnings':[],'reviewed':False,'extraction_method':'native_grid'}
        primary={'t1':'طرق التأليف فيه.','t2':'حجية الإجماع'}
        second={'t1':'طرق التأليف فيه .','t2':'حجية الاجماع'}
        caller=VisionCaller([answer({'t1':primary['t1']}),answer({'t2':primary['t2']})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed')
        self.assertEqual([t['corrected_text'] for t in result['topics']],list(primary.values()))
        self.assertEqual([t['text'] for t in result['topics']],[t['text'] for t in source['topics']])
        self.assertFalse(result['reviewed']);self.assertEqual(len(caller.chats),2)
        self.assertEqual(result['visual_verification_proof']['numeric_disagreements'],[])
        self.assertEqual(result['visual_verification_proof']['native_protocol_version'],image.NATIVE_PROTOCOL_VERSION)

    def test_native_distinct_explicit_alef_marks_are_real_disagreement_in_both_orders(self):
        pairs=[('أحكام الأيمان','أحكام الإيمان'),
               ('آيات الأيمان','آيات الإيمان'),
               ('أَ','إِ'),('آ','أ'),('ٱ','إ')]
        for a,b in pairs:
            for first,second in [(a,b),(b,a)]:
                with self.subTest(first=first,second=second):
                    self.assertFalse(image._native_readings_agree(first,second,punctuation=True))
                    self.assertIn(image.UNKNOWN,image._merge_text(first,second,native=True))
                    merged=image._merge_rows([{'id':'t1','text':first}],[{'id':'t1','text':second}],'native')
                    self.assertIn(image.UNKNOWN,merged[0]['text'])

    def test_native_one_omitted_alef_mark_is_compatible_without_rewriting_primary(self):
        for a,b in [('حجية الإجماع','حجية الاجماع'),('آيات','ايات'),('أحكام','احكام'),('ٱلأدوات','الادوات')]:
            for first,second in [(a,b),(b,a)]:
                with self.subTest(first=first,second=second):
                    self.assertTrue(image._native_readings_agree(first,second,punctuation=False))
                    self.assertEqual(image._merge_text(first,second,native=True),first)

    def test_native_marked_alef_conflict_remains_uncertain_in_real_transcription_flow(self):
        source=self.extracted();source['topics'][0]['text']='أحكام الإيمان'
        caller=VisionCaller([answer({'t1':'أحكام الأيمان'}),answer({'t1':'أحكام الإيمان'})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        self.assertEqual(result['topics'][0]['text'],source['topics'][0]['text'])
        self.assertEqual(calculate(source['topics'],result['topics'],[])['decision'],'insufficient')
        self.assertFalse(result['reviewed'])


    def test_native_cells_on_same_page_are_separate_scale_three_images_with_private_relative_proofs(self):
        source=self.extracted();source['topics'][0].update(text='تعريف المقاصد',bbox=[40,40,300,70])
        source['topics'].append({'id':'t2','text':'أدلة اعتبارها','page':1,'bbox':[40,80,300,120]})
        before=copy.deepcopy(source)
        caller=VisionCaller([answer({'t1':'تعريف المقاصد'}),answer({'t2':'أدلة اعتبارها'})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(source,before)
        self.assertEqual(len(caller.chats),2)
        self.assertEqual([c[1]['format']['properties']['rows']['required']for c in caller.chats],[['t1'],['t2']])
        self.assertNotEqual(caller.chats[0][1]['messages'][1]['images'],caller.chats[1][1]['messages'][1]['images'])
        proof=result['visual_verification_proof'];self.assertEqual(len(proof['cells']),2)
        self.assertTrue(all(p['render_scale']==3 for p in proof['pages']))
        directory=self.cache/image.VERSION/result['visual_verification_cache_key']
        self.assertEqual(len(list(directory.glob('cells/*/cache.json'))),2)
        for response in proof['responses']:
            self.assertFalse(Path(response['file']).is_absolute());self.assertEqual(Path(response['file']).parts[0],'cells')
            self.assertEqual(image._sha((directory/response['file']).read_bytes()),response['sha256'])
        warm=VisionCaller([]);again=image.verify_pdf_topics(self.root/'native.pdf',source,self.cache,caller=warm)
        self.assertEqual(warm.chats,[]);self.assertEqual(again['topics'],result['topics'])

    def test_native_cell_continuations_all_stay_in_one_request_without_native_text(self):
        source=self.extracted();source['topics'][0].update(text='نص سري لا يظهر للقارئ',end_page=3,
            fragments=[{'page':2,'bbox':[40,20,300,70]}])
        caller=VisionCaller([answer({'t1':'قراءة مستقلة كاملة'}),answer({'t1':'قراءة مستقلة كاملة'})])
        result=image.verify_pdf_topics(self.native(pages=3),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertEqual(result['topics'][0]['corrected_text'],source['topics'][0]['text']+' '+image.UNKNOWN)
        for _,payload,_ in caller.chats:
            content=payload['messages'][1]['content'];self.assertNotIn('نص سري',content)
            anchor=json.loads(content.split('\n')[-1])[0]
            self.assertEqual(anchor['pages'],[1,2,3]);self.assertEqual(anchor['unlocated_pages'],[3])
            self.assertEqual(len(payload['messages'][1]['images']),3)
        self.assertFalse(result['visual_verification_proof']['pages'][2]['cropped_from_topic_bboxes'])
        self.assertEqual(result['topics'][0]['end_page'],3)

    def test_native_three_way_corrects_only_agreed_words_and_keeps_primary_punctuation(self):
        native='وعلاقتها بالعلل والمصالح';primary='وعلاقتها، بالعلل والمصال.';second='وعلاقاتها بالعلل والمصالح'
        self.assertEqual(image._native_three_way_text(native,primary,second),'وعلاقتها، بالعلل والمصالح.')
        self.assertEqual(image._native_three_way_text('لا تجب النفقة','لا، تجب النفقة.','لا تجب النفقه'),'لا، تجب النفقة.')
        self.assertIsNone(image._native_three_way_text('تجب النفقة','لا تجب النفقة','تجب النفقة'))
        self.assertIsNone(image._native_three_way_text('تفسير 1-3','تفسير 1،3','تفسير 13'))
        self.assertEqual(image._native_three_way_text('حكم خاص','حكم '+image.UNKNOWN,'حكم '+image.UNKNOWN),'حكم '+image.UNKNOWN)

    def test_native_bare_alef_cannot_bridge_explicit_alef_or_diacritic_conflicts(self):
        for native,first,second in [('احكام الايمان','أحكام الأيمان','أحكام الإيمان'),
                                   ('المكره','المُكرِه','المُكرَه'),
                                   ('المُكرِه','المُكرِه','المُكرَه')]:
            with self.subTest(native=native):
                self.assertIn(image.UNKNOWN,image._native_three_way_text(native,first,second))
        self.assertFalse(image._native_readings_agree('المُكرِه','المُكرَه',punctuation=True))
        self.assertIn(image.UNKNOWN,image._merge_text('المُكرِه','المُكرَه',native=True))
        # Unmarked native alone does not corroborate the primary's new vowel.
        self.assertIn(image.UNKNOWN,image._native_three_way_text('المكره','المُكرِه',image.UNKNOWN))
        self.assertEqual(image._native_three_way_text('المكره','المُكرِه','المُكرِه'),'المُكرِه')

    def test_native_punctuation_agreement_avoids_redundant_fallback_but_preserves_raw(self):
        source=self.extracted();source['topics'][0]['text']='حجية الاجماع .'
        raw=answer({'t1':'حجية الإجماع.'});before=copy.deepcopy(raw);caller=VisionCaller([raw])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(len(caller.chats),1)
        self.assertEqual(result['topics'][0]['corrected_text'],'حجية الإجماع.');self.assertEqual(raw,before)

    def test_native_diacritic_conflict_is_retained_in_raw_and_uncertain_after_blind_reads(self):
        source=self.extracted();source['topics'][0]['text']='المكره'
        first=answer({'t1':'المُكرِه'});second=answer({'t1':'المُكرَه'})
        caller=VisionCaller([first,second]);result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'uncertain');self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        raw=[json.loads(p.read_text())for p in self.cache.rglob('raw-response-*.json')]
        self.assertCountEqual(raw,[first,second]);self.assertEqual(result['topics'][0]['text'],'المكره')

    def test_native_failed_cell_retains_all_original_rows_and_relative_raw_attempts(self):
        source=self.extracted();source['topics'][0]['text']='قراءة أصلية'
        caller=VisionCaller([answer({'t1':image.UNKNOWN}),answer({})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'unavailable');self.assertEqual(result['topics'],source['topics'])
        proof=result['visual_verification_proof'];self.assertEqual(len(proof['responses']),2)
        self.assertEqual(proof['cells'][0]['status'],'unavailable');self.assertEqual(len(proof['identities']),2)
        self.assertTrue(all((p.parent/response['file']).exists() for p in self.cache.rglob('source.pdf')for response in proof['responses']))


    def test_native_three_way_does_not_smuggle_unconfirmed_symbols_as_primary_punctuation(self):
        self.assertIsNone(image._native_three_way_text('C','C++','C'))
        self.assertIn(image.UNKNOWN,image._merge_text('C++','C',native=True))


    def test_native_numbered_row_requires_fallback_for_unconfirmed_new_diacritics(self):
        source=self.extracted();source['topics'][0]['text']='المكره ٣'
        caller=VisionCaller([answer({'t1':'المُكرِه 3'}),answer({'t1':'المكره 3'})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual([c[2]for c in caller.chats],[60,300])
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertIn(image.UNKNOWN,result['topics'][0]['corrected_text'])
        self.assertNotIn('المُكرِه',result['topics'][0]['corrected_text'])
        self.assertEqual(result['topics'][0]['text'],'المكره ٣')
        self.assertEqual(result['visual_verification_proof']['numeric_disagreements'],[])
        self.assertEqual(image._native_agreement_text('المكره ٣',punctuation=False),'المكره 3')


    def test_native_unlocated_matching_wrong_readers_cannot_replace_native_cell(self):
        source=self.extracted();source['topics'][0].update(text='أحكام البيع وشروطه');source['topics'][0].pop('bbox')
        wrong=answer({'t1':'أحكام الوقف وشروطه'});caller=VisionCaller([wrong,wrong]);before=copy.deepcopy(source)
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'uncertain')
        self.assertEqual(result['topics'][0]['corrected_text'],'أحكام البيع وشروطه '+image.UNKNOWN)
        self.assertEqual(source,before);self.assertEqual(len(caller.chats),2)
        self.assertEqual(json.loads(caller.chats[0][1]['messages'][1]['content'].split('\n')[-1])[0]['unlocated_pages'],[1])
        self.assertTrue(all(json.loads(p.read_text())==wrong for p in self.cache.rglob('raw-response-*.json')))

    def test_native_unlocated_fallback_only_requires_complete_native_agreement(self):
        for agrees in (False,True):
            with self.subTest(agrees=agrees):
                source=self.extracted();source['topics'][0].update(text='أحكام البيع وشروطه');source['topics'][0].pop('bbox')
                visual='أحكام البيع وشروطه'if agrees else'أحكام الوقف وشروطه'
                caller=VisionCaller([TimeoutError(),answer({'t1':visual})])
                result=image.verify_pdf_topics(self.native('fallback-'+str(agrees)+'.pdf'),source,self.cache/str(agrees),caller=caller)
                self.assertEqual(result['visual_verification_status'],'completed'if agrees else'uncertain')
                self.assertEqual(result['topics'][0]['corrected_text'],visual if agrees else source['topics'][0]['text']+' '+image.UNKNOWN)
                self.assertEqual(len(caller.chats),2)

    def test_native_unlocated_early_primary_native_agreement_stays_valid(self):
        source=self.extracted();source['topics'][0].update(text='حجية الاجماع .');source['topics'][0].pop('bbox')
        visual='حجية الإجماع.';caller=VisionCaller([answer({'t1':visual})])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(len(caller.chats),1)
        self.assertEqual(result['topics'][0]['corrected_text'],visual)

    def test_native_located_cell_still_allows_corroborated_visual_correction(self):
        source=self.extracted();source['topics'][0]['text']='أحكام البيع وشروطه'
        visual='أحكام الوقف وشروطه';raw=answer({'t1':visual});caller=VisionCaller([raw,raw])
        result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(result['topics'][0]['corrected_text'],visual)
        self.assertTrue(result['visual_verification_proof']['pages'][0]['cropped_from_topic_bboxes'])
        self.assertEqual(result['topics'][0]['text'],source['topics'][0]['text'])

    def test_native_unlocated_match_respects_diacritics_unknown_numbers_and_ranges(self):
        for original,visual in [('المكره','المُكرِه'),('أحكام الأيمان','أحكام الإيمان'),('نص واضح',image.UNKNOWN),
                                ('تفسير 1-3','تفسير 1،3'),('تفسير 1-3','تفسير 13')]:
            self.assertFalse(image._native_complete_match(original,visual))
        self.assertTrue(image._native_complete_match('تفسير ١-٣','تفسير 1-3'))
        self.assertTrue(image._native_complete_match('طرق التاليف فيه .','طرق التأليف فيه.'))


    def test_native_unlocated_uses_the_actual_matching_fallback_reading_not_a_voted_rewrite(self):
        source=self.extracted();source['topics'][0]['text']='حجية الإجماع.';source['topics'][0].pop('bbox')
        primary=answer({'t1':'حجية القياس.'});fallback=answer({'t1':'حجية الاجماع .'})
        caller=VisionCaller([primary,fallback]);result=image.verify_pdf_topics(self.native(),source,self.cache,caller=caller)
        self.assertEqual(result['visual_verification_status'],'completed');self.assertEqual(len(caller.chats),2)
        self.assertEqual(result['topics'][0]['corrected_text'],'حجية الاجماع .')
        self.assertCountEqual([json.loads(p.read_text())for p in self.cache.rglob('raw-response-*.json')],[primary,fallback])
