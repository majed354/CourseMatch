import copy
import unittest

from jsonschema import Draft202012Validator

from scripts.equivalency.bundle_evidence import PROPOSAL_SCHEMA, REVIEW_SCHEMA, validate_proposals
from scripts.equivalency.bundle_wire import build_schema, decode_rows, source_view


SOURCES = [
    {'id':'s1','text':'تعريف العِلم، ونشأته؛ لا يدخل فيه التفسير.\nحدوده 1-3.'},
    {'id':'s2','text':'أدلة الحُكم ومناطه — تطبيقات عملية.'},
    {'id':'s3','text':'نص خام مختلف','corrected_text':'  الحُجِّيَّةُ، وليست مجردَ ارتباط.  '},
]


def row(refs, *, band='strict'):
    return {'target_id':'t1','facets':[{'part_id':'p1','support':'aligned',
            'source_quotes':refs,'reason':'قراءة النص كما طبع دون حذف النفي.'}],
            'reason':'نطاق مفردة الهدف كاملًا.','band':band,
            'possible_tiers':{'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],'uncertain':['lenient']}[band],
            'uncertainty':'decisive' if band=='uncertain' else 'none'}


class BundleWireTests(unittest.TestCase):
    def test_all_sources_are_ordered_lossless_and_whole_text_is_not_repeated(self):
        before=copy.deepcopy(SOURCES);view=source_view(SOURCES)
        self.assertEqual([s['source_id'] for s in view],['s1','s2','s3'])
        for original, exposed in zip(SOURCES,view):
            text=original.get('corrected_text') or original['text']
            self.assertEqual(''.join(a['quote'] for a in exposed['anchors']),text)
            self.assertEqual(exposed['anchors'][0]['start'],0)
            self.assertEqual(exposed['anchors'][-1]['end'],len(text))
            self.assertEqual(set(exposed),{'source_id','whole_ref','anchors'})
            for anchor in exposed['anchors']:
                self.assertEqual(anchor['quote'],text[anchor['start']:anchor['end']])
        self.assertEqual(SOURCES,before)

    def test_reverse_mapping_is_exact_for_each_anchor_and_whole(self):
        for original, exposed in zip(SOURCES,source_view(SOURCES)):
            for ref, text in [(a['ref'],a['quote']) for a in exposed['anchors']]+[(exposed['whole_ref'],original.get('corrected_text') or original['text'])]:
                decoded=decode_rows([row([ref])],SOURCES)[0]['facets'][0]['source_quotes']
                self.assertEqual(decoded,[{'source_id':original['id'],'quote':text}])

    def test_negation_diacritics_whitespace_and_numeric_range_survive_whole_reference(self):
        decoded=decode_rows([row(['s1:whole','s3:whole'])],SOURCES)
        quotes=decoded[0]['facets'][0]['source_quotes']
        self.assertEqual(quotes[0]['quote'],SOURCES[0]['text'])
        self.assertIn('لا يدخل',quotes[0]['quote']);self.assertIn('1-3',quotes[0]['quote'])
        self.assertEqual(quotes[1]['quote'],SOURCES[2]['corrected_text'])

    def test_compound_quotes_can_select_several_parts_or_the_entire_source_without_band_changes(self):
        for band in ('strict','lenient','weak','uncertain'):
            refs=[a['ref'] for a in source_view(SOURCES)[0]['anchors']]
            wire=row(refs,band=band);before=copy.deepcopy(wire)
            decoded=decode_rows([wire],SOURCES)[0]
            self.assertEqual(decoded['band'],band)
            self.assertEqual(decoded['possible_tiers'],wire['possible_tiers'])
            self.assertEqual(decoded['uncertainty'],wire['uncertainty'])
            self.assertEqual(decoded['reason'],wire['reason'])
            self.assertEqual(decoded['facets'][0]['support'],'aligned')
            self.assertEqual(decoded['facets'][0]['reason'],wire['facets'][0]['reason'])
            self.assertEqual(''.join(q['quote'] for q in decoded['facets'][0]['source_quotes']),SOURCES[0]['text'])
            self.assertEqual(wire,before)

    def test_same_ref_can_support_distinct_targets_or_facets_without_capacity_rule(self):
        first=row(['s1:whole']);first['facets'].append({**copy.deepcopy(first['facets'][0]),'part_id':'p2'})
        second=row(['s1:whole']);second['target_id']='t2'
        decoded=decode_rows([first,second],SOURCES)
        self.assertEqual(len(decoded),2)
        self.assertEqual(decoded[0]['facets'][0]['source_quotes'],decoded[1]['facets'][0]['source_quotes'])

    def test_foreign_malformed_legacy_and_duplicate_refs_are_rejected(self):
        for refs in (['s99:whole'],['s1:p999'],['s1'],['s1:WHOLE'],[None],[42],
                     [{'source_id':'s1','quote':SOURCES[0]['text']}],['s1:p1','s1:p1']):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                decode_rows([row(refs)],SOURCES)
        with self.assertRaisesRegex(ValueError,'duplicate_ref'):
            decode_rows([row(['single:p1','single:whole'])],[{'id':'single','text':'نص واحد'}])

    def test_schema_allows_all_source_refs_only_and_retains_strict_semantics(self):
        before_p,before_r=copy.deepcopy(PROPOSAL_SCHEMA),copy.deepcopy(REVIEW_SCHEMA)
        refs=[a['ref'] for s in source_view(SOURCES) for a in s['anchors']]+[s['whole_ref'] for s in source_view(SOURCES)]
        for stage,base,key in [('proposal',PROPOSAL_SCHEMA,'evaluations'),('review',REVIEW_SCHEMA,'reviews')]:
            schema=build_schema(stage,base,SOURCES);Draft202012Validator.check_schema(schema)
            strict=schema['properties'][key]['items']['anyOf'][0]
            facet=strict['properties']['facets']['items']['anyOf'][0]
            enum=facet['properties']['source_quotes']['items']['enum']
            self.assertEqual(set(enum),set(refs))
            self.assertEqual(facet['properties']['support']['enum'],['aligned'])
        schema=build_schema('proposal',PROPOSAL_SCHEMA,SOURCES);validator=Draft202012Validator(schema)
        for ref in refs:self.assertTrue(validator.is_valid({'evaluations':[row([ref])]}))
        self.assertFalse(validator.is_valid({'evaluations':[row(['foreign:whole'])]}))
        self.assertFalse(validator.is_valid({'evaluations':[row([{'source_id':'s1','quote':'forged'}])]}))
        wrong=row(['s1:whole']);wrong['facets'][0]['support']='related'
        self.assertFalse(validator.is_valid({'evaluations':[wrong]}))
        self.assertEqual(PROPOSAL_SCHEMA,before_p);self.assertEqual(REVIEW_SCHEMA,before_r)

    def test_decoding_does_not_repair_an_academically_invalid_band(self):
        wire=row(['s1:whole'],band='strict');wire['facets'][0]['support']='related'
        decoded=decode_rows([wire],SOURCES)
        self.assertEqual(decoded[0]['band'],'strict');self.assertEqual(decoded[0]['facets'][0]['support'],'related')
        with self.assertRaisesRegex(ValueError,'strict_without_supported_whole_scope'):
            validate_proposals(decoded,[{'id':'t1','text':'تعريف العلم'}],SOURCES)

    def test_both_wire_grammars_reject_actual_limited_empty_shape_and_keep_literal_ref_enums(self):
        refs={a['ref']for s in source_view(SOURCES)for a in s['anchors']}|{s['whole_ref']for s in source_view(SOURCES)}
        for stage,base,key in [('proposal',PROPOSAL_SCHEMA,'evaluations'),('review',REVIEW_SCHEMA,'reviews')]:
            schema=build_schema(stage,base,SOURCES);validator=Draft202012Validator(schema)
            bad=row([],band='weak');bad['target_id']='t13';bad['facets']=[{'part_id':f'p{i}','source_quotes':[],
                'reason':'دليل الجزء كما ورد.','support':'absent'if i<4 else'limited'}for i in range(1,5)]
            if stage=='review':bad['verdict']=bad.pop('band')
            before=copy.deepcopy(bad);self.assertFalse(validator.is_valid({key:[bad]}));self.assertEqual(bad,before)
            bad['facets'][3]['source_quotes']=['s1:p1'];self.assertTrue(validator.is_valid({key:[bad]}))
            bad['facets'][3]['source_quotes']=['FOREIGN:p1'];self.assertFalse(validator.is_valid({key:[bad]}))
            def visit(value):
                if isinstance(value,dict):
                    if value.get('type')=='object'and set(value.get('properties',{}))=={'part_id','source_quotes','reason','support'}:
                        self.assertEqual(set(value['properties']['source_quotes']['items']['enum']),refs)
                        self.assertEqual(list(value['properties']),['part_id','source_quotes','reason','support'])
                    for child in value.values():visit(child)
                elif isinstance(value,list):
                    for child in value:visit(child)
            visit(schema)

    def test_unknown_without_source_evidence_is_preserved_instead_of_inventing_absence(self):
        wire=row([],band='uncertain');wire['facets'][0]['support']='unknown'
        self.assertEqual(decode_rows([wire],SOURCES),[wire])

    def test_identity_and_text_failures_are_explicit_and_no_source_is_silently_omitted(self):
        for sources in ([{'id':'s1','text':'نص'},{'id':'s1','text':'نص آخر'}],
                        [{'id':'missing','text':''}], [{'text':'نص'}], [{'id':'x','text':42}]):
            with self.subTest(sources=sources),self.assertRaises(ValueError):source_view(sources)
        with self.assertRaises(ValueError):source_view({})
        with self.assertRaises(ValueError):decode_rows({},SOURCES)
        with self.assertRaises(ValueError):decode_rows([{'facets':[{}]}],SOURCES)
        self.assertEqual(source_view([]),[])
        self.assertEqual(decode_rows([],[]),[])
        with self.assertRaisesRegex(ValueError,'schema_without_sources'):
            build_schema('proposal',PROPOSAL_SCHEMA,[])

    def test_ids_are_resolved_by_exact_registry_not_split_or_normalized(self):
        sources=[{'id':'s:1','text':'نص المصدر المركب الاسم'}]
        self.assertEqual(decode_rows([row(['s:1:whole'])],sources)[0]['facets'][0]['source_quotes'][0]['source_id'],'s:1')


if __name__=='__main__':
    unittest.main()
