import copy,json,tempfile,unittest
from jsonschema import Draft202012Validator
from scripts.equivalency import candidates as c,candidates_repair as repair,candidates_wire as wire
from tests.test_candidate_discovery import Caller,answer,response,group

class CandidateGroupRepairTests(unittest.TestCase):
    def setUp(self):
        self.targets=[{'id':'t1','text':'تعريف الموظف وشروط شغل الوظيفة'}]
        self.sources=[{'id':'s1','text':'تعريف الموظف وأحكامه'},{'id':'s2','text':'تأديب الموظف والقرارات الإدارية'}]
        self.key=('t1',('s1','s2'));self.base=wire.build_schema(c.SCHEMA,self.targets,self.sources)
    def withdrawal(self):return {'target_id':'t1','source_ids':['s1','s2'],'reason':'لا يثبت المصدر الثاني تكاملًا مع مطلب شروط الوظيفة؛ يسحب اقتراح المجموعة فقط.'}
    def test_repair_scan_cannot_rewrite_retained_pairs_and_base_is_unchanged(self):
        before=copy.deepcopy(self.base);schema=repair.schema(self.base,set(),{self.key});Draft202012Validator.check_schema(schema)
        value={'scan':[],'groups':[],'withdrawn_groups':[self.withdrawal()]};self.assertTrue(Draft202012Validator(schema).is_valid(value))
        value['scan']=[{'target_id':'t1','sources':[{'source_id':'s1','status':'unrelated'}]}];self.assertFalse(Draft202012Validator(schema).is_valid(value));self.assertEqual(self.base,before)
        schema=repair.schema(self.base,{('t1','s2')},set());value={'scan':[{'target_id':'t1','sources':[{'source_id':'s1','status':'unrelated'}]}],'groups':[],'withdrawn_groups':[]}
        self.assertFalse(Draft202012Validator(schema).is_valid(value));value['scan'][0]['sources'][0]['source_id']='s2';self.assertTrue(Draft202012Validator(schema).is_valid(value))
    def test_withdrawal_only_for_invalid_outstanding_groups_never_valid_ones(self):
        value={'scan':[],'groups':[],'withdrawn_groups':[self.withdrawal()]};clean,declined,notes=repair.response(value,{self.key},{})
        self.assertEqual(clean,{'scan':[],'groups':[]});self.assertEqual(declined,{self.key});self.assertEqual(notes,[self.withdrawal()])
        for required,retained in [(set(),{}),({self.key},{self.key:{}})]:
            with self.assertRaises(ValueError):repair.response(value,required,retained)
        for change in [lambda v:v['withdrawn_groups'].append(self.withdrawal()),lambda v:v['withdrawn_groups'][0].update(reason=''),
                       lambda v:v['withdrawn_groups'][0].update(source_ids=['s1','s1']),lambda v:v['groups'].append({'target_id':'t1','source_ids':['s2','s1']})]:
            bad=copy.deepcopy(value);change(bad)
            with self.assertRaises(ValueError):repair.response(bad,{self.key},{})
    def test_actual_deadlock_shape_can_be_declined_without_changing_any_scan_status(self):
        def make(payload,n):
            if n==1:
                value=answer(payload);value['scan'][0]['sources'][1]={'source_id':'s2','status':'unrelated'}
                value['groups']=[group('t1',['s1','s2'],self.targets,self.sources)];return value
            self.assertTrue(payload['repair_only']);self.assertEqual(payload['requested_pairs'],[])
            self.assertEqual(len(payload['retained_pairs']),2)
            return {'scan':[],'groups':[],'withdrawn_groups':[self.withdrawal()]}
        caller=Caller(make)
        with tempfile.TemporaryDirectory()as path:
            result=c.discover_candidates(self.targets,self.sources,model='local:test',model_digest='digest',caller=caller,cache_dir=path)
            self.assertEqual(len(caller.calls),2);self.assertEqual(result['coverage']['pairs'],2);self.assertEqual(result['groups'],[])
            self.assertEqual(result['scan'][0]['sources'][1],{'source_id':'s2','status':'unrelated'})
            self.assertEqual(caller.calls[1]['format']['properties']['scan']['maxItems'],0)
            again=c.discover_candidates(self.targets,self.sources,model='local:test',model_digest='digest',caller=lambda *x:self.fail('warm inference'),cache_dir=path)
            self.assertTrue(again['cache_hit']);self.assertEqual(result['scan'],again['scan'])

if __name__=='__main__':unittest.main()
