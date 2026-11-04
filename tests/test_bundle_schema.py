"""Categorical generation contracts; synthetic evidence, no numeric regrading."""
import copy
import unittest

from jsonschema import Draft202012Validator

from scripts.equivalency.bundle_evidence import (
    PROPOSAL_SCHEMA, REVIEW_SCHEMA, validate_proposals, validate_reviews,
)
from scripts.equivalency.bundle_schema import model_schema


def facet(support='aligned', part_id='p1'):
    return {'part_id':part_id, 'support':support,
            'source_quotes': [] if support in ('absent','unknown') else [{'source_id':'s1','quote':'تعريف العلم'}],
            'reason':'دليل جزء الهدف كما ورد في المنهج.'}


def reading(stage, band='strict', supports=('aligned',), uncertainty='none', tiers=None):
    if tiers is None:
        tiers={'strict':['strict','lenient'],'lenient':['lenient'],'weak':[],'uncertain':['lenient']}[band]
    return {'target_id':'t1','facets':[facet(s,f'p{i+1}') for i,s in enumerate(supports)],
            'reason':'تقييم مستقل لنطاق المفردة الأصلية من المنهج الكامل.',
            'band' if stage=='proposal' else 'verdict':band,
            'possible_tiers':tiers,'uncertainty':uncertainty}


class BundleSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schemas={stage:model_schema(stage,base) for stage,base in [('proposal',PROPOSAL_SCHEMA),('review',REVIEW_SCHEMA)]}
        self.validators={stage:Draft202012Validator(schema) for stage,schema in self.schemas.items()}

    def valid(self,stage,row):
        return self.validators[stage].is_valid({'evaluations' if stage=='proposal' else 'reviews':[row]})

    def test_grammars_are_valid_standard_json_schema_and_do_not_mutate_base(self):
        for stage,base,key in [('proposal',PROPOSAL_SCHEMA,'evaluations'),('review',REVIEW_SCHEMA,'reviews')]:
            before=copy.deepcopy(base);schema=model_schema(stage,base)
            Draft202012Validator.check_schema(schema)
            schema['properties'][key]['items']['anyOf'][0]['properties']['facets']['items']['properties']['support']['enum'].append('MUTATED_COPY')
            self.assertEqual(base,before)

    def test_both_strict_readers_require_complete_or_disclosed_nondecisive_core_scope(self):
        for stage in self.schemas:
            self.assertTrue(self.valid(stage,reading(stage,supports=('aligned','aligned'))))
            for support in ('related','limited','absent','unknown'):
                with self.subTest(stage=stage,support=support):
                    self.assertFalse(self.valid(stage,reading(stage,supports=('aligned',support))))
            self.assertFalse(self.valid(stage,reading(stage,uncertainty='non_decisive')))
            self.assertFalse(self.valid(stage,reading(stage,tiers=['lenient'])))

    def test_strict_mixed_evidence_has_nondecisive_branch_but_not_none_or_decisive(self):
        for stage in self.schemas:
            for support in ('related','limited','absent','unknown'):
                row=reading(stage,'strict',('aligned',support),'non_decisive')
                self.assertTrue(self.valid(stage,row))
                row['uncertainty']='none';self.assertFalse(self.valid(stage,row))
                row['uncertainty']='decisive';self.assertFalse(self.valid(stage,row))
            # Portable grammar does not simulate contains. The domain checks
            # that mixed multi-part evidence actually has quoted core alignment.
            row=reading(stage,'strict',('related','related'),'non_decisive')
            self.assertTrue(self.valid(stage,row))
            validate=validate_proposals if stage=='proposal' else validate_reviews
            with self.assertRaises(ValueError):
                validate([row],[{'id':'t1','text':'تعريف العلم، أهميته'}],[{'id':'s1','text':'تعريف العلم'}])

    def test_both_lenient_readers_preserve_nondecisive_unknown_but_reject_decisive(self):
        for stage in self.schemas:
            self.assertTrue(self.valid(stage,reading(stage,'lenient',('related',))))
            row=reading(stage,'lenient',('related','unknown'),'non_decisive');before=copy.deepcopy(row)
            self.assertTrue(self.valid(stage,row));self.assertEqual(row,before)
            row['uncertainty']='none';self.assertFalse(self.valid(stage,row))
            row['uncertainty']='decisive';self.assertFalse(self.valid(stage,row))
            row['uncertainty']='non_decisive';row['possible_tiers']=[];self.assertFalse(self.valid(stage,row))
            row['possible_tiers']=['strict','lenient'];self.assertTrue(self.valid(stage,row))
            row['uncertainty']='none';self.assertFalse(self.valid(stage,row))
            self.assertFalse(self.valid(stage,reading(stage,'lenient',('aligned',))))

    def test_both_weak_readers_can_have_nondecisive_unknown_with_negative_evidence_and_no_positive_tier(self):
        for stage in self.schemas:
            self.assertTrue(self.valid(stage,reading(stage,'weak',('limited','unknown'),'non_decisive')))
            self.assertTrue(self.valid(stage,reading(stage,'weak',('absent',))))
            self.assertFalse(self.valid(stage,reading(stage,'weak',('limited','unknown'),'none')))
            self.assertFalse(self.valid(stage,reading(stage,'weak',('unknown',),'non_decisive')))
            self.assertFalse(self.valid(stage,reading(stage,'weak',('related',))))
            self.assertFalse(self.valid(stage,reading(stage,'weak',('limited',),'none',['lenient'])))

    def test_both_uncertain_readers_keep_decisive_and_all_valid_upper_bound_shapes(self):
        for stage in self.schemas:
            for tiers in ([],['lenient'],['strict','lenient'],['lenient','strict']):
                self.assertTrue(self.valid(stage,reading(stage,'uncertain',('unknown',),'decisive',tiers)))
            self.assertFalse(self.valid(stage,reading(stage,'uncertain',('unknown',),'none')))
            self.assertFalse(self.valid(stage,reading(stage,'uncertain',('unknown',),'non_decisive')))
            self.assertFalse(self.valid(stage,reading(stage,'uncertain',('unknown',),'decisive',['strict'])))

    def test_empty_uncertain_bound_still_requires_independent_scope_proof_for_both_readers(self):
        for stage,validate in [('proposal',validate_proposals),('review',validate_reviews)]:
            row=reading(stage,'uncertain',('unknown',),'decisive',[])
            self.assertTrue(self.valid(stage,row))
            with self.assertRaisesRegex(ValueError,'without_positive_bound_or_hard_exclusion'):
                validate([row],[{'id':'t1','text':'تعريف العلم'}],[{'id':'s1','text':'تعريف العلم'}])

    def test_no_numeric_grade_can_enter_either_generation_schema(self):
        for stage in self.schemas:
            key='band' if stage=='proposal' else 'verdict'
            for score in (0,8,59,60,79,80,85,95,100):
                row=reading(stage);row['score']=score
                self.assertFalse(self.valid(stage,row))
                row=reading(stage);row[key]=score
                self.assertFalse(self.valid(stage,row))
            row=reading(stage);del row[key];row['score']=85
            self.assertFalse(self.valid(stage,row))

    def test_evidence_precedes_category_and_no_nonportable_conditionals_are_introduced(self):
        for stage,schema in self.schemas.items():
            key='evaluations' if stage=='proposal' else 'reviews'
            category='band' if stage=='proposal' else 'verdict'
            for branch in schema['properties'][key]['items']['anyOf']:
                props=list(branch['properties'])
                self.assertLess(props.index('facets'),props.index('reason'))
                self.assertLess(props.index('reason'),props.index(category))
                self.assertNotIn('score',props)
            def visit(value):
                if isinstance(value,dict):
                    self.assertFalse(set(value)&{'contains','if','then','else','dependentSchemas','not','oneOf','allOf'})
                    for v in value.values():visit(v)
                elif isinstance(value,list):
                    for v in value:visit(v)
            visit(schema)

    def test_invalid_stage_and_old_score_schema_fail_instead_of_being_implicitly_converted(self):
        with self.assertRaisesRegex(ValueError,'stage_invalid'):model_schema('grade',PROPOSAL_SCHEMA)
        with self.assertRaisesRegex(ValueError,'shape_invalid'):model_schema('proposal',REVIEW_SCHEMA)
        old=copy.deepcopy(PROPOSAL_SCHEMA);props=old['properties']['evaluations']['items']['properties']
        del props['band'];props['score']={'type':'integer','minimum':0,'maximum':100}
        with self.assertRaisesRegex(ValueError,'shape_invalid'):model_schema('proposal',old)
        for stage in self.schemas:
            row=reading(stage);row['private_field']='not allowed'
            self.assertFalse(self.valid(stage,row))


    def test_every_model_facet_is_evidence_first_with_unchanged_schema_meaning(self):
        # Restore only the old ordering to compare complete schema semantics.
        def normalized(value,key=None):
            if isinstance(value,dict):return {k:normalized(v,k)for k,v in value.items()}
            if isinstance(value,list):
                values=[normalized(v)for v in value]
                return sorted(values)if key=='required'else values
            return value
        for schema in self.schemas.values():
            previous=copy.deepcopy(schema);seen=[]
            def visit(value):
                if isinstance(value,dict):
                    if value.get('type')=='object'and set(value.get('properties',{}))=={'part_id','source_quotes','reason','support'}:
                        self.assertEqual(list(value['properties']),['part_id','source_quotes','reason','support'])
                        self.assertEqual(value['required'],['part_id','source_quotes','reason','support']);seen.append(value)
                        order=['part_id','support','source_quotes','reason']
                        value['properties']={k:value['properties'][k]for k in order};value['required']=order
                    for v in value.values():visit(v)
                elif isinstance(value,list):
                    for v in value:visit(v)
            visit(previous);self.assertGreater(len(seen),0)
            self.assertEqual(normalized(schema),normalized(previous))


if __name__=='__main__':
    unittest.main()
