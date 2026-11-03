"""Local pilot parity for two real uploaded artifacts, without model inference."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from scripts.equivalency.server import Pilot
from scripts.equivalency.extract import digest


class ImmediateExecutor:
    def submit(self,function):
        function()


class UploadedTargetPilotTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.pilot=object.__new__(Pilot);self.pilot.root=Path(temporary.name);self.pilot.runtime=self.pilot.root/'runtime';self.pilot.runtime.mkdir()
        self.pilot.catalog={'catalog_id':'test-only','source_hashes':{}};self.pilot.courses={};self.pilot.sources={};self.pilot.model='unused';self.pilot.jobs={};self.pilot.executor=ImmediateExecutor()
        self.source=self.upload('a'*32,'Original course');self.target=self.upload('b'*32,'Target course')

    def upload(self,key,name):
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((30,40),'Definition of a topic')
            self.pilot.path('sources',key,'pdf').write_bytes(pdf.tobytes())
        value={'id':key,'name':name,'topics':[{'id':'t1','text':'Definition of a topic'}],'source_sha256':digest(self.pilot.path('sources',key,'pdf')),'upload_pdf':True}
        self.pilot.path('sources',key).write_text(json.dumps(value));return value

    def data(self,**updates):
        return {'source_id':self.source['id'],'target_source_id':self.target['id'],'review_mode':'automatic',**updates}

    def test_pair_uses_uploaded_target_and_automatic_exact_report(self):
        with patch('scripts.equivalency.engine.post_ollama',side_effect=AssertionError('literal fixture needs no model')):
            job=self.pilot.job(self.data())
        self.assertEqual(job['status'],'done');report=job['result']
        self.assertEqual(report['target_mode'],'uploaded');self.assertEqual(report['target']['id'],self.target['id'])
        self.assertEqual(report['calculation']['decision'],'equivalent');self.assertFalse(report['human_reviewed'])
        self.assertNotIn('context',report['target'])

    def test_target_choice_scope_and_topic_override_are_rejected(self):
        for data in [self.data(target_id='catalog-id'),{'source_id':self.source['id'],'review_mode':'automatic'},self.data(target_topics=[]),self.data(context_id='foreign-context')]:
            with self.subTest(data=data),self.assertRaises(ValueError):self.pilot.job(data)

    def test_uploaded_target_requires_actual_pdf_and_matching_fingerprint(self):
        manual=copy.deepcopy(self.target);manual['upload_pdf']=False;self.pilot.path('sources',self.target['id']).write_text(json.dumps(manual))
        with self.assertRaises(ValueError):self.pilot.job(self.data())
        self.pilot.path('sources',self.target['id']).write_text(json.dumps(self.target))
        self.pilot.path('sources',self.target['id'],'pdf').write_bytes(b'%PDF-changed')
        with self.assertRaises(ValueError):self.pilot.job(self.data())

    def test_original_catalog_mode_remains_available(self):
        target={**self.target,'id':'catalog-target'};target.pop('upload_pdf');self.pilot.courses[target['id']]=target
        job=self.pilot.job({'source_id':self.source['id'],'target_id':target['id'],'review_mode':'automatic'})
        self.assertEqual(job['status'],'done');self.assertEqual(job['result']['target_mode'],'catalog')

    def test_typed_evidence_failure_is_formal_incomplete_without_private_error_text(self):
        from scripts.equivalency.engine import EvidenceValidationError,RUBRIC_VERSION
        for stage in ['proposal','facet_review']:
            error=EvidenceValidationError('/private/raw-response.json secret model output',stage=stage,target_ids=['t1'])
            before=copy.deepcopy([self.source,self.target])
            with patch('scripts.equivalency.server.compare',side_effect=error):job=self.pilot.job(self.data())
            self.assertEqual(job['status'],'done');report=job['result']
            self.assertEqual(report['evidence'],{'provider':'audit_incomplete','rubric':RUBRIC_VERSION,'pairs':[],
                                                'evidence_review':{'stage':stage,'target_ids':['t1']}})
            self.assertEqual(report['calculation']['decision'],'insufficient')
            self.assertIsNone(report['calculation']['strict_coverage']);self.assertIsNone(report['calculation']['lenient_coverage'])
            self.assertFalse(report['human_reviewed'])
            for key,course in [('source',self.source),('target',self.target)]:
                self.assertEqual(report[key]['source_sha256'],course['source_sha256'])
            self.assertNotIn('secret',json.dumps(report));self.assertNotIn('/private/',json.dumps(report))
            self.assertEqual([self.source,self.target],before)

    def test_generic_operational_failures_remain_job_errors_without_academic_report(self):
        import urllib.error
        for error in [RuntimeError('private programming details'),OSError('/private/path'),urllib.error.URLError('private network details')]:
            with self.subTest(error=type(error).__name__),patch('scripts.equivalency.server.compare',side_effect=error),patch('scripts.equivalency.server.traceback.print_exc'):
                job=self.pilot.job(self.data())
            self.assertEqual(job['status'],'error');self.assertNotIn('result',job)
            self.assertNotIn('private',job['message'])
