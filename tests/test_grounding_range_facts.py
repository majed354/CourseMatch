"""CPU-only blinded numeric-fact contract; no production data or inference."""
import copy,json,tempfile,unittest
from scripts.equivalency.grounding import scope_facts,review_pairs,decode_reviews,validate_reviews,VERSION

def topic(key,text):return {'id':key,'text':text}
def answer(rows):return {'done_reason':'stop','message':{'content':json.dumps({'reviews':rows},ensure_ascii=False)}}
def row(t,s,verdict,support,possible):
 return {'target_id':t['id'],'source_id':s['id'],'verdict':verdict,'possible_tiers':possible,'reason':'النطاق والموضوع كما وردا في النص.', 'facets':[{'target_start':1,'target_end':len(t['text'].split()),'source_quote':s['text'] if support!='unknown' else '', 'support':support}]}

class GroundingRangeFactsTests(unittest.TestCase):
 def setUp(self):
  self.target=topic('t','مقدمة عن سورة البقرة – تفسير ص:2 البقرة ( 5-1 ) تفسيرا تحليليا')
  self.source=topic('s','مقدمة عن سورة البقرة، وتفسير سورة البقرة الآيات1-2 تفسيرًا تحليليًا.')
 def test_compound_page_metadata_does_not_change_two_of_five(self):
  f=scope_facts([self.target],[self.source],{('t','s')})[0]
  self.assertEqual((f['target']['chapter'],f['target']['start'],f['target']['end']),(2,1,5))
  self.assertEqual((f['intersection'],f['target_count'],f['covered_percent']),(2,5,40.0))
 def test_three_of_five_allows_sixty_but_not_strict(self):
  s=topic('s','مقدمة عن سورة البقرة وتفسير الآيات1-3 تفسيرا تحليليا')
  f=scope_facts([self.target],[s],{('t','s')})[0];self.assertEqual(f['covered_percent'],60.0)
  good=row(self.target,s,'lenient','related',['lenient'])
  validate_reviews(decode_reviews([good],[self.target]),[self.target],[s],{('t','s')})
  bad=row(self.target,s,'strict','aligned',['strict','lenient'])
  with self.assertRaisesRegex(ValueError,'نطاق الآيات'):validate_reviews(decode_reviews([bad],[self.target]),[self.target],[s],{('t','s')})
 def test_different_named_chapters_have_zero_intersection(self):
  s=topic('s','تفسير سورة آل عمران الآيات1-5')
  f=scope_facts([self.target],[s],{('t','s')})[0]
  self.assertEqual((f['intersection'],f['covered_percent']),(0,0.0))
 def test_unresolved_source_scope_remains_unknown_not_zero(self):
  s=topic('s','تفاصيل الوحدة لم تتوفر')
  f=scope_facts([self.target],[s],{('t','s')})[0]
  self.assertIsNone(f['source']);self.assertIsNone(f['intersection']);self.assertIsNone(f['covered_percent'])
  good=row(self.target,s,'uncertain','unknown',['strict','lenient'])
  validate_reviews(decode_reviews([good],[self.target]),[self.target],[s],{('t','s')})
  good['possible_tiers']=[]
  with self.assertRaises(ValueError):validate_reviews(decode_reviews([good],[self.target]),[self.target],[s],{('t','s')})
 def test_payload_is_identical_when_generator_scores_and_reasons_change(self):
  captured=[];valid=row(self.target,self.source,'weak','limited',[])
  def caller(endpoint,payload):captured.append(copy.deepcopy(payload));return answer([valid])
  for score in [0,99]:
   with tempfile.TemporaryDirectory() as folder:
    review_pairs([self.target],[self.source],[{'target_id':'t','source_id':'s','score':score,'reason':'PRIVATE_GENERATOR_'+str(score)}],model='test',model_digest='digest',cache_dir=folder,caller=caller)
  self.assertEqual(captured[0],captured[1])
  body=json.loads(captured[0]['messages'][1]['content']);self.assertEqual(body['scope_facts'][0]['covered_percent'],40.0)
  self.assertNotIn('PRIVATE_GENERATOR',json.dumps(captured));self.assertEqual(VERSION,'blinded-facet-review-v3-explicit-range-facts')
 def test_range_error_feedback_contains_pair_and_computed_facts_then_repair(self):
  bad=row(self.target,self.source,'lenient','related',['lenient']);good=row(self.target,self.source,'weak','limited',[]);calls=[]
  def caller(endpoint,payload):calls.append(copy.deepcopy(payload));return answer([bad if len(calls)==1 else good])
  with tempfile.TemporaryDirectory() as folder:
   r=review_pairs([self.target],[self.source],[{'target_id':'t','source_id':'s','score':40}],model='test',model_digest='digest',cache_dir=folder,caller=caller)
  self.assertEqual(r['validation_attempts'],2);feedback=calls[1]['messages'][-1]['content']
  self.assertIn('"target_id": "t"',feedback);self.assertIn('"source_id": "s"',feedback)
  self.assertIn('"intersection": 2',feedback);self.assertIn('"target_count": 5',feedback);self.assertIn('"covered_percent": 40.0',feedback)
  self.assertEqual(r['reviews'][0]['verdict'],'weak')
 def test_unrepaired_wrong_positive_still_fails_instead_of_being_capped(self):
  bad=row(self.target,self.source,'lenient','related',['lenient'])
  with tempfile.TemporaryDirectory() as folder,self.assertRaisesRegex(ValueError,'نطاق الآيات'):
   review_pairs([self.target],[self.source],[{'target_id':'t','source_id':'s','score':40}],model='test',model_digest='digest',cache_dir=folder,caller=lambda *args:answer([bad]))
