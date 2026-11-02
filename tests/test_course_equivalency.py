"""User's two-level thresholds, source evidence, and scoped source selection."""
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.equivalency.engine import calculate, compare, maximum_matching, validate_pairs, search
from scripts.equivalency.extract import extract_pdf, select_details, context_id
from scripts.equivalency.server import Pilot, clean_beneficiary, clean_topics


def fixture(scores):
    target = [{"id": f"t{i}", "text": f"الموضوع الهدف رقم {i}"} for i in range(len(scores))]
    source = [{"id": f"s{i}", "text": f"الموضوع المصدر رقم {i}"} for i in range(len(scores))]
    pairs = [{"target_id": t["id"], "source_id": s["id"], "score": score, "reason": "اختبار حدود السياسة",
              "target_quote": t["text"], "source_quote": s["text"]} for t, s, score in zip(target, source, scores)]
    return target, source, pairs


class DecisionTests(unittest.TestCase):
    def test_eighty_percent_strict(self):
        r = calculate(*fixture([80] * 8 + [59] * 2))
        self.assertEqual((r["decision"], r["strict_count"]), ("equivalent", 8))

    def test_sixty_is_per_topic_not_course_coverage(self):
        r = calculate(*fixture([80] * 6 + [60] * 2 + [59] * 2))
        self.assertEqual((r["decision"], r["strict_coverage"], r["lenient_coverage"]), ("lenient", 60, 80))

    def test_lenient_still_requires_eighty_course_coverage(self):
        self.assertEqual(calculate(*fixture([79] * 7 + [59] * 3))["decision"], "not_equivalent")

    def test_do_not_round_79_5_up_to_80(self):
        self.assertEqual(calculate(*fixture([100] * 159 + [0] * 41))["decision"], "not_equivalent")

    def test_empty_or_unreviewed_is_not_rejection(self):
        self.assertEqual(calculate([], [], [])["decision"], "insufficient")
        self.assertEqual(calculate(*fixture([100]), reviewed=False)["decision"], "insufficient")

    def test_unspecified_other_topic_never_becomes_rejection_or_acceptance(self):
        t, s, p = fixture([100])
        t[0]["text"] = "أخرى"
        self.assertEqual(calculate(t, s, p)["decision"], "insufficient")
        with patch("scripts.equivalency.engine.local_model", side_effect=AssertionError("no inference for unknown content")):
            self.assertEqual(compare({"topics": t}, {"topics": s})["provider"], "unresolved_source")

    def test_unreadable_cid_requires_a_corrected_reading(self):
        t, s, pairs = fixture([100])
        t[0]['text'] = 'الموضوع (cid:24) غير المقروء'
        self.assertEqual(calculate(t, s, pairs)['decision'], 'insufficient')

    def test_project_without_a_fixed_subject_cannot_be_scored(self):
        t, s, pairs = fixture([100])
        t[0]['text'] = 'لا يوجد موضوع بعينه؛ يختار الطالب موضوع مشروعه'
        self.assertEqual(calculate(t, s, pairs)['decision'], 'insufficient')

    def test_missing_judgment_is_not_zero(self):
        t, s, p = fixture([100, 100])
        with self.assertRaises(ValueError):
            calculate(t, s, p[:1])

    def test_source_topic_cannot_count_twice(self):
        t, s, p = fixture([100, 100])
        p[1].update(source_id=s[0]["id"], source_quote=s[0]["text"])
        r = calculate(t, s, p)
        self.assertEqual(r["decision"], "insufficient")
        self.assertEqual(r["segmentation_review"]["distinct_source_matching"],1)

    def test_augmenting_path_prevents_greedy_loss(self):
        pairs = [{"target_id": "a", "source_id": "1", "score": 100}, {"target_id": "a", "source_id": "2", "score": 80}, {"target_id": "b", "source_id": "1", "score": 90}]
        self.assertEqual(len(maximum_matching(pairs, 80)), 2)

    def test_direction_uses_target_denominator(self):
        t, s, p = fixture([100, 100])
        s.extend([{"id": "extra", "text": "موضوع إضافي في المصدر"}])
        self.assertEqual(calculate(t, s, p)["strict_coverage"], 100)

    def test_fabricated_quote_rejected(self):
        t, s, p = fixture([100])
        p[0]["source_quote"] = "نص غير وارد في المصدر"
        with self.assertRaises(ValueError):
            validate_pairs(p, t, s)

    def test_nan_fractional_and_out_of_range_scores_rejected(self):
        for score in [float("nan"), 79.9, True, -1, 101]:
            with self.subTest(score=score), self.assertRaises(ValueError):
                validate_pairs(fixture([score])[2], *fixture([score])[:2])

    def test_exact_rows_need_no_model(self):
        topics = [{"id": "1", "text": "تعريف علم القراءات ونشأته"}]
        with patch("scripts.equivalency.engine.local_model", side_effect=AssertionError("must not call model")):
            result = compare({"topics": topics}, {"topics": topics})
        self.assertEqual(result["provider"], "exact_text")


class SourceTests(unittest.TestCase):
    def test_large_upload_is_rejected_before_any_recovery(self):
        import base64
        import pymupdf
        from scripts.equivalency.server import Pilot
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as document:
            for _ in range(41):
                document.new_page()
            pilot = object.__new__(Pilot)
            pilot.root = Path(directory)
            pilot.runtime = Path(directory)
            raw = base64.b64encode(document.tobytes()).decode()
            with patch('scripts.equivalency.server.extract_cached', side_effect=AssertionError('no recovery')):
                with self.assertRaisesRegex(ValueError, '40'):
                    pilot.create_source({'pdf_base64':raw})

    def test_visual_transcription_excludes_later_administrative_fields(self):
        from scripts.equivalency.recovery import sanitize_vision
        data={'topics':[{'id':'1','page':5,'text':'موضوع مقرر حقيقي'},
                        {'id':'2','page':5,'text':'المرافق النوعية'},
                        {'id':'3','page':5,'text':'حقول تجهيزات لاحقة'},
                        {'id':'4','page':6,'text':'صفحة خارج جدول الموضوعات'}], 'page_proof':[{'page':5},{'page':6}]}
        with patch('scripts.equivalency.recovery.ocr_pages',return_value=[{'page':5}]):
            cleaned=sanitize_vision(data,'unused','unused')
        self.assertEqual([r['text'] for r in cleaned['topics']], ['موضوع مقرر حقيقي'])
        self.assertEqual(len(cleaned['excluded_transcription_rows']),3)

    def test_alternate_grid_recovers_the_law_template(self):
        from scripts.equivalency.recovery import strict_grid
        from scripts.equivalency.extract import apply_readings
        from scripts.equivalency.engine import topic_text
        root=Path(__file__).resolve().parents[1]
        data=apply_readings(strict_grid(root/'assets/course-specifications/systems-source-2022/2003225-2.pdf'))
        self.assertEqual(len(data['topics']),15)
        self.assertIn('المنظمات الدولية',topic_text(data['topics'][0]))
        self.assertIn('المنظمات الدولة',data['topics'][0]['text'])

    def test_search_keeps_program_degree_and_plan_separate(self):
        contexts = [{"program": p, "degree": d, "plan_type": "جديدة", "version": "1"}
                    for p, d in [('القراءات', 'ماجستير'), ('القراءات', 'دكتوراه'), ('الفقه', 'دكتوراه')]]
        for context in contexts:
            context['id'] = context_id(context)
        courses = [{"id": str(i), "context": {k:v for k,v in ctx.items() if k!='id'}, "context_id": ctx['id'],
                    "code": "shared", "name": "اسم واحد", "topics": [{"id": "t1", "text": "موضوع واحد في سياقات مختلفة"}]} for i, ctx in enumerate(contexts)]
        catalog = {"courses": courses, "contexts": contexts}
        result = search(catalog, courses[0], context_id=contexts[1]['id'], semantic=False)
        self.assertEqual([c['id'] for c in result['candidates']], ['1'])
        with self.assertRaises(ValueError):
            search(catalog, courses[0], context_id='unknown', semantic=False)

    def test_legacy_version_request_remains_quran_only(self):
        base = {"topics": [{"id": "1", "text": "تعريف القراءات"}]}
        courses = [{**base, "id": name, "context": {"program": name, "degree": "بكالوريوس", "version": "47"}} for name in ['القرآن وعلومه', 'القراءات']]
        self.assertEqual([c['id'] for c in search({"courses": courses}, base, version='47', semantic=False)['candidates']], ['القرآن وعلومه'])

    def search_catalog(self):
        contexts = [{'id':'ba47','program':'القراءات','degree':'بكالوريوس','version':'47'},
                    {'id':'ma1','program':'القراءات','degree':'ماجستير','version':'1'},
                    {'id':'phd1','program':'القراءات','degree':'دكتوراه','version':'1'}]
        base = {'code':'2002801-8','name':'القراءات العشر','source_sha256':'same-pdf',
                'topics':[{'id':'t1','text':'تعريف علم القراءات'}]}
        courses = [{**copy.deepcopy(base), 'id':str(i),'context_id':ctx['id'],
                    'context':{k:v for k,v in ctx.items() if k!='id'}} for i,ctx in enumerate(contexts)]
        return {'contexts':contexts, 'courses':courses}

    def test_global_search_groups_identical_sources_without_losing_context_identities(self):
        catalog=self.search_catalog()
        before=copy.deepcopy(catalog)
        # A source-only library entry never becomes a target candidate.
        catalog['library_sources']=[{**copy.deepcopy(catalog['courses'][0]),'id':'library'}]
        result=search(catalog,catalog['courses'][0],all_contexts=True,semantic=False)
        self.assertEqual((result['scope_count'],result['unique_count']), (3,1))
        candidate=result['candidates'][0]
        self.assertEqual(candidate['id'],'0')
        self.assertEqual([ctx['course_id'] for ctx in candidate['available_contexts']],['0','1','2'])
        self.assertEqual([ctx['context']['degree'] for ctx in candidate['available_contexts']],['بكالوريوس','ماجستير','دكتوراه'])
        self.assertEqual(catalog['courses'],before['courses'])

    def test_global_search_keeps_distinct_names_codes_pdfs_and_topic_content(self):
        catalog=self.search_catalog()
        original=catalog['courses'][0]
        for i,change in enumerate([{'code':'other-code'},{'name':'عنوان آخر'}, {'source_sha256':'other-pdf'},
                                   {'topics':[{'id':'t1','text':'قراءة أبي عمرو'}]}],3):
            catalog['courses'].append({**copy.deepcopy(original),'id':str(i),**change})
        catalog['courses'].append({**copy.deepcopy(original),'id':'missing','topics':[]})
        result=search(catalog,original,all_contexts=True,limit=8,semantic=False)
        self.assertEqual((result['scope_count'],result['unique_count']), (7,5))
        self.assertNotIn('missing',[candidate['id'] for candidate in result['candidates']])

    def test_program_context_union_is_explicit_and_does_not_include_other_degrees(self):
        catalog=self.search_catalog()
        result=search(catalog,catalog['courses'][0],context_ids=['ba47','ma1'],semantic=False)
        self.assertEqual(result['scope_count'],2)
        self.assertEqual([ctx['course_id'] for ctx in result['candidates'][0]['available_contexts']],['0','1'])
        for options in [{'all_contexts':'true'}, {'all_contexts':True,'context_id':'ma1'},
                        {'all_contexts':True,'context_ids':['ma1']}, {'context_ids':[]},
                        {'context_ids':['unknown']}, {'context_ids':'ma1'}, {'limit':True}, {'limit':11}]:
            with self.subTest(options=options),self.assertRaises(ValueError):
                search(catalog,catalog['courses'][0],semantic=False,**options)

    def test_source_without_topics_returns_no_arbitrary_candidates(self):
        catalog=self.search_catalog()
        with patch('scripts.equivalency.engine.local_model',side_effect=AssertionError('no model for empty source')):
            result=search(catalog,{'topics':[]},all_contexts=True)
        self.assertEqual(result['candidates'],[])
        self.assertTrue(result['warning'])

    def test_semantic_cache_reuses_unique_content_across_global_and_scoped_searches(self):
        catalog=self.search_catalog()
        # Distinct identities with identical content retain both candidates but
        # compute only one vector for that content.
        other={**copy.deepcopy(catalog['courses'][0]),'id':'different','code':'other-code'}
        catalog['courses'].append(other)
        source={'topics':[{'id':'s1','text':'الأحرف السبعة وعلاقتها بالقراءات'}]}
        calls=[]
        def embed(endpoint,payload):
            self.assertEqual(endpoint,'embed')
            self.assertIsInstance(payload['input'],list)
            calls.extend(payload['input'])
            return {'embeddings':[[len(text),1] for text in payload['input']]}
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.ROOT',Path(directory)),\
             patch('scripts.equivalency.engine.local_model',return_value='model-digest'),\
             patch('scripts.equivalency.engine.post_ollama',side_effect=embed):
            first=search(catalog,source,all_contexts=True)
            second=search(catalog,source,context_id='ma1')
        self.assertEqual(first['method'],'hybrid_qwen3_embedding_0.6b')
        self.assertEqual(second['method'],'hybrid_qwen3_embedding_0.6b')
        self.assertEqual(len(calls),2)
        self.assertEqual(first['unique_count'],2)
        self.assertEqual([candidate['id'] for candidate in second['candidates']],['1'])

    def test_invalid_embedding_response_falls_back_without_truncating_candidate_list(self):
        catalog=self.search_catalog()
        with tempfile.TemporaryDirectory() as directory,patch('scripts.equivalency.engine.ROOT',Path(directory)),\
             patch('scripts.equivalency.engine.local_model',return_value='model-digest'),\
             patch('scripts.equivalency.engine.post_ollama',return_value={'embeddings':[]}):
            result=search(catalog,catalog['courses'][0],all_contexts=True)
        self.assertEqual(result['method'],'lexical')
        self.assertEqual(len(result['candidates']),1)
        self.assertTrue(result['warning'])

    def test_corrected_reading_does_not_replace_original(self):
        original = [{"id": "t1", "text": "وشوطه", "page": 5}]
        before = copy.deepcopy(original)
        result = clean_topics([{"id": "t1", "text": "وشروطه"}], original)
        self.assertEqual(original, before)
        self.assertEqual((result[0]["text"], result[0]["corrected_text"]), ("وشوطه", "وشروطه"))

    def test_context_selects_current_pdf_and_respects_exclusion(self):
        context = {"program": "القرآن وعلومه", "degree": "بكالوريوس", "version": "47", "plan_type": "جديدة"}
        old = {"title": "التجويد", "pdf_url": "old.pdf", "scopes": [context], "excluded_scopes": [context]}
        new = {"title": "التجويد", "pdf_url": "uploaded.pdf", "scopes": [context]}
        data = {"course_details": {"1": {"variants": [old, new]}}}
        self.assertEqual(select_details(data, {"code": "1", "name": "التجويد"}, context)["pdf_url"], "uploaded.pdf")
        self.assertIsNone(select_details(data, {"code": "1", "name": "تفسير"}, context))

    def test_multiline_topics_continue_and_review_is_excluded(self):
        root = Path(__file__).resolve().parents[1]
        result = extract_pdf(root / "assets/course-specifications/quran-old-v39/2002110-2.pdf")
        self.assertEqual(len(result["topics"]), 16)
        self.assertIn("الفرش", result["topics"][0]["text"])
        self.assertEqual(result["topics"][-1]["page"], 5)
        self.assertEqual(len(result["excluded_rows"]), 1)
        self.assertFalse(any("المراجع" in t["text"] for t in result["topics"]))

    def test_old_three_column_template_and_continuation(self):
        root = Path(__file__).resolve().parents[1]
        result = extract_pdf(root / "assets/course-specifications/eight-course-completion-20260915/2001113-2.pdf")
        self.assertEqual(len(result["topics"]), 14)
        self.assertIn("المحكوم فيه", result["topics"][-1]["text"])

    def test_topic_fragment_crossing_pages_is_stitched_before_counting(self):
        root = Path(__file__).resolve().parents[1]
        result = extract_pdf(root / "assets/course-specifications/shared-course-blank-plo-20260905/2001340-2--16914d8743.pdf")
        self.assertEqual(len(result["topics"]), 14)
        self.assertIn("تعريف التخريج", result["topics"][0]["text"])
        self.assertEqual(result["topics"][0]["end_page"], 6)
        self.assertEqual(len(result["excluded_rows"]), 2)

    def test_cid_artifacts_recovered_without_discarding_original(self):
        root = Path(__file__).resolve().parents[1]
        result = extract_pdf(root / "assets/course-specifications/shared-course-blank-plo-20260905/2001101-2--6acb2da09b.pdf")
        self.assertEqual(len(result["topics"]), 14)
        repaired = [t for t in result["topics"] if "(cid:" in t["text"]]
        self.assertTrue(repaired)
        self.assertTrue(all("(cid:" not in t["corrected_text"] for t in repaired))


class BeneficiaryTests(unittest.TestCase):
    def pilot(self, directory):
        pilot = object.__new__(Pilot)
        pilot.runtime = Path(directory)
        pilot.jobs = {}
        return pilot

    def test_optional_metadata_preserves_leading_zeroes_and_arabic_digits(self):
        self.assertEqual(clean_beneficiary(), {"name": "", "number": ""})
        self.assertEqual(clean_beneficiary({"name": "  مستفيد تجريبي  ", "number": "٠٠١٢34"}),
                         {"name": "مستفيد تجريبي", "number": "٠٠١٢34"})
        self.assertEqual(clean_beneficiary({"number": "000014"})["number"], "000014")

    def test_metadata_rejects_wrong_types_excess_length_and_control_characters(self):
        for value in ([], "name", 1, {"name": None}, {"number": 123}, {"name": "x" * 161},
                      {"number": "1" * 61}, {"name": "bad\x00name"}, {"number": "1\n2"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_beneficiary(value)
        self.assertEqual(len(clean_beneficiary({"name": "ن" * 160})["name"]), 160)

    def test_job_saves_beneficiary_and_creation_time_without_affecting_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            pilot = self.pilot(directory)
            topics = [{"id": "t1", "text": "تعريف علم القراءات"}]
            pilot.source = lambda key: {"name": "مصدر تجريبي", "topics": topics}
            pilot.courses = {"target": {"name": "هدف تجريبي", "topics": topics}}
            pilot.verify_course = lambda course: None
            pilot.catalog = {"catalog_id": "fixture"}
            pilot.model = "local-fixture"
            pilot.executor = SimpleNamespace(submit=lambda task: task())
            with patch("scripts.equivalency.engine.local_model", side_effect=AssertionError("exact match needs no model")):
                job = pilot.job({"source_id": "s", "target_id": "target", "reviewed": True,
                                 "beneficiary": {"name": "مستفيد تجريبي", "number": "000٣"}})
            self.assertEqual(job["status"], "done")
            saved = json.loads(pilot.path("reports", job["id"]).read_text())
            self.assertEqual(saved["beneficiary"], {"name": "مستفيد تجريبي", "number": "000٣"})
            self.assertEqual(saved["calculation"]["decision"], "equivalent")
            self.assertIsNotNone(datetime.fromisoformat(saved["created_at"]).tzinfo)

    def test_update_old_saved_report_and_in_memory_report_does_not_repeat_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            pilot = self.pilot(directory)
            key = "a" * 32
            original = {"id": key, "calculation": {"decision": "lenient", "strict_coverage": 60},
                        "evidence": {"pairs": []}, "source": {"topics": []}, "target": {"topics": []}}
            pilot.save_report(original)
            pilot.jobs[key] = {"id": key, "status": "done", "result": original}
            metadata = {"name": '<script>alert("fixture")</script>', "number": "٠٠١23"}
            with patch("scripts.equivalency.server.compare", side_effect=AssertionError("metadata cannot rerun inference")):
                updated = pilot.update_beneficiary(key, {"beneficiary": metadata})
            self.assertEqual(updated, {**original, "beneficiary": metadata})
            self.assertEqual(pilot.jobs[key]["result"], updated)
            self.assertEqual(json.loads(pilot.path("reports", key).read_text()), updated)
            self.assertNotIn("created_at", updated)  # No invented timestamp for a legacy report.
            pilot.jobs = {}  # Simulate reopening after the local service restarts.
            reopened = pilot.update_beneficiary(key, {"beneficiary": {"number": "000045"}})
            self.assertEqual(reopened["beneficiary"], {"name": "", "number": "000045"})
            self.assertEqual(reopened["evidence"], original["evidence"])

    def test_invalid_or_pending_update_does_not_overwrite_report(self):
        with tempfile.TemporaryDirectory() as directory:
            pilot = self.pilot(directory)
            key = "b" * 32
            original = {"id": key, "beneficiary": {"name": "", "number": "001"}}
            pilot.save_report(original)
            for data in ({"beneficiary": {"number": 1}}, {"beneficiary": {"name": "ن" * 161}}):
                with self.assertRaises(ValueError):
                    pilot.update_beneficiary(key, data)
            self.assertEqual(json.loads(pilot.path("reports", key).read_text()), original)
            pilot.jobs[key] = {"id": key, "status": "running"}
            with self.assertRaisesRegex(ValueError, "انتظر"):
                pilot.update_beneficiary(key, {"beneficiary": {"name": "تجربة"}})
            with self.assertRaises(ValueError):
                pilot.update_beneficiary("c" * 32, {})


if __name__ == "__main__":
    unittest.main()
