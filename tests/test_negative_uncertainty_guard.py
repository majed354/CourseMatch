"""A finite candidate graph is not proof that an unknown syllabus is negative.

CPU-only structural regressions; no model, API, private report or fixture file is
read. The 15/13 case reproduces the Najran-shaped missing-alternative risk with
independent evidence. It does not prescribe numeric academic scores.
"""
import copy
import unittest

from scripts.equivalency.engine import calculate, RUBRIC_VERSION
from scripts.equivalency.grounding import adjudicate, validate_evidence


def topic(key, text):
    return {'id':key, 'text':text}


def evidence(targets, sources, rows):
    proposed, reviews = [], []
    for tid, sid, score, verdict in rows:
        target = next(t for t in targets if t['id'] == tid)
        source = next(s for s in sources if s['id'] == sid)
        supports = {'strict':'aligned', 'lenient':'related', 'weak':'absent', 'uncertain':'unknown'}
        possible = {'strict':['strict','lenient'], 'lenient':['lenient'], 'weak':[], 'uncertain':['lenient']}
        proposed.append({'target_id':tid, 'source_id':sid, 'score':score,
                         'target_quote':target['text'], 'source_quote':source['text'],
                         'reason':'تقدير مستقل محفوظ للمقابلة.'})
        reviews.append({'target_id':tid,'source_id':sid,'verdict':verdict,
                        'possible_tiers':possible[verdict], 'reason':'وصف الأدلة كما وردت في النص.',
                        'facets':[{'target_quote':target['text'],
                                   'source_quote':source['text'] if verdict in ('strict','lenient') else '',
                                   'support':supports[verdict]}]})
    result={'provider':'ollama_local','rubric':RUBRIC_VERSION,
            'pairs':adjudicate(proposed,reviews,targets,sources)}
    validate_evidence(result,targets,sources)
    return result


def ordinary(count):
    return ([topic('t'+str(i),'المادة المطلوبة رقم '+str(i)) for i in range(1,count+1)],
            [topic('s'+str(i),'المادة المصدرية رقم '+str(i)) for i in range(1,count+1)])


class NegativeUncertaintyGuardTests(unittest.TestCase):
    def test_najran_shaped_15_by_13_does_not_reject_on_an_incomplete_possible_graph(self):
        targets=[topic('t'+str(i),'متطلبات المقرر المفصلة رقم '+str(i)) for i in range(1,14)]
        targets[4]['text']='الصحة والفساد والعزيمة والرخصة والأداء والإعادة والقضاء'
        headings=['التعريف بعلم أصول الفقه ونشأته','وأهميته وغايته','طرق التأليف فيه',
                  'الحكم الشرعي التكليفي','أقسام الحكم التكليفي','الحكم الشرعي الوضعي',
                  'أقسام الحكم الشرعي الوضعي','الحاكم والمحكوم فيه والمحكوم عليه',
                  'الأهلية وعوارضها','الأدلة الشرعية المتفق عليها والقرآن الكريم',
                  'السنة','أنواع السنة','الإجماع','حجية الإجماع','القياس']
        sources=[topic('s'+str(i),text) for i,text in enumerate(headings,1)]
        edges=[(1,1,'uncertain'),(2,4,'uncertain'),(3,5,'lenient'),(4,6,'uncertain'),
               (5,4,'weak'),(6,9,'uncertain'),(7,10,'uncertain'),(8,11,'lenient'),
               (9,12,'lenient'),(10,13,'uncertain'),(11,15,'uncertain'),(12,10,'weak'),(13,15,'weak')]
        rows=[('t'+str(t),'s'+str(s),65 if verdict=='lenient' else 40 if verdict=='uncertain' else 10,verdict)
              for t,s,verdict in edges]
        audited=evidence(targets,sources,rows);before=copy.deepcopy(audited)
        self.assertEqual((len(targets),len(sources)),(13,15))
        self.assertNotIn(('t5','s7'),{(p['target_id'],p['source_id']) for p in audited['pairs']})
        self.assertEqual(sum(p['adjudication']['state']=='uncertain' for p in audited['pairs']),7)
        result=calculate(targets,sources,audited['pairs'])
        self.assertEqual(result['decision'],'insufficient')
        self.assertIsNone(result['strict_coverage']);self.assertIsNone(result['lenient_coverage'])
        self.assertEqual(audited,before,'guard must not inject the unrepresented alternative or change any score')

    def test_all_confirmed_weak_pairs_still_support_a_definitive_negative(self):
        targets,sources=ordinary(5)
        audited=evidence(targets,sources,[(t['id'],s['id'],10,'weak') for t,s in zip(targets,sources)])
        result=calculate(targets,sources,audited['pairs'])
        self.assertEqual(result['decision'],'not_equivalent')
        self.assertEqual((result['strict_count'],result['lenient_count']),(0,0))
        self.assertEqual((result['strict_coverage'],result['lenient_coverage']),(0,0))

    def test_hard_verse_exclusion_can_reject_even_with_uncertain_empty_bounds(self):
        targets=[topic('t','تفسير سورة البقرة الآيات1-10')]
        sources=[topic('s','تفسير سورة البقرة الآيات1-2')]
        raw={'target_id':'t','source_id':'s','score':20,'target_quote':targets[0]['text'],
             'source_quote':sources[0]['text'],'reason':'النطاق المثبت في النصين.'}
        review={'target_id':'t','source_id':'s','verdict':'uncertain','possible_tiers':[],
                'reason':'لا يوجد حد إيجابي ممكن مع استبعاد نطاق الآيات المثبت.',
                'facets':[{'target_quote':targets[0]['text'],'source_quote':'','support':'unknown'}]}
        pairs=adjudicate([raw],[review],targets,sources)
        validate_evidence({'provider':'ollama_local','rubric':RUBRIC_VERSION,'pairs':pairs},targets,sources)
        self.assertEqual(pairs[0]['adjudication']['state'],'uncertain')
        self.assertEqual(pairs[0]['adjudication']['possible_tiers'],[])
        result=calculate(targets,sources,pairs)
        self.assertEqual(result['decision'],'not_equivalent')
        self.assertEqual(result['lenient_coverage'],0)

    def test_confirmed_strict_exactly_eighty_remains_accepted_with_irrelevant_uncertainty(self):
        targets,sources=ordinary(5)
        rows=[(t['id'],s['id'],87 if i<4 else 40,'strict' if i<4 else 'uncertain')
              for i,(t,s) in enumerate(zip(targets,sources))]
        audited=evidence(targets,sources,rows);before=copy.deepcopy(audited)
        result=calculate(targets,sources,audited['pairs'])
        self.assertEqual(result['decision'],'equivalent');self.assertEqual(result['strict_count'],4)
        self.assertEqual(result['strict_coverage'],80);self.assertEqual(audited,before)

    def test_confirmed_lenient_exactly_eighty_remains_lenient_with_irrelevant_uncertainty(self):
        targets,sources=ordinary(5)
        rows=[(t['id'],s['id'],65 if i<4 else 40,'lenient' if i<4 else 'uncertain')
              for i,(t,s) in enumerate(zip(targets,sources))]
        audited=evidence(targets,sources,rows);before=copy.deepcopy(audited)
        result=calculate(targets,sources,audited['pairs'])
        self.assertEqual(result['decision'],'lenient');self.assertEqual(result['lenient_count'],4)
        self.assertEqual(result['lenient_coverage'],80);self.assertEqual(audited,before)

    def test_possible_eighty_is_pending_but_confirmed_below_eighty_without_unknown_still_rejects(self):
        targets,sources=ordinary(5)
        base=[(t['id'],s['id'],65 if i<3 else 10,'lenient' if i<3 else 'weak')
              for i,(t,s) in enumerate(zip(targets,sources))]
        negative=evidence(targets,sources,base)
        self.assertEqual(calculate(targets,sources,negative['pairs'])['decision'],'not_equivalent')
        pending=list(base);pending[3]=('t4','s4',40,'uncertain')
        audited=evidence(targets,sources,pending)
        result=calculate(targets,sources,audited['pairs'])
        self.assertEqual(result['decision'],'insufficient')
        self.assertIsNone(result['lenient_coverage'])
        self.assertEqual(sum(bool(p['adjudication']['possible_tiers']) for p in audited['pairs']),4)


if __name__=='__main__':
    unittest.main()
