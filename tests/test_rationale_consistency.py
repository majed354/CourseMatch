import copy
import unittest
from scripts.equivalency.consistency import rationale_issues
from scripts.equivalency.engine import validate_pairs

class RationaleConsistencyTests(unittest.TestCase):
    def pair(self,score,reason):
        return {'target_id':'t','source_id':'s','score':score,'reason':reason,
                'target_quote':'تعريف العقد وأركانه وآثاره','source_quote':'تعريف العقد'}

    def test_explicit_partial_rationale_cannot_receive_a_strict_grade(self):
        for reason in ['التغطية جزئية لكن مرتبطة بشدة.', 'تغطية جزئية في الموضوع.',
                       'المصدر يغطي التعريف فقط، بينما الهدف يجمع معه الأركان والآثار.',
                       'المصدر يشمل عادة أركان العقد.', 'توافق كامل مع افتراض أن التفاصيل مضمنة.']:
            with self.subTest(reason=reason):
                pairs=[self.pair(90,reason)];before=copy.deepcopy(pairs)
                with self.assertRaisesRegex(ValueError,'تناقض'):
                    validate_pairs(pairs,[{'id':'t','text':'تعريف العقد وأركانه وآثاره'}],[{'id':'s','text':'تعريف العقد'}])
                self.assertEqual(pairs,before)

    def test_partial_reason_is_consistent_with_lenient_or_lower_scores(self):
        for score in [0,59,60,79]:
            self.assertEqual(rationale_issues([self.pair(score,'التغطية جزئية.')]),[])

    def test_minor_differences_and_unrelated_uses_of_partial_do_not_trigger(self):
        for reason in ['تطابق في الموضوع والنطاق مع فروق طفيفة.', 'يغطي المصدر كل الموضوع بما في ذلك المسؤولية الجزئية.',
                       'يتناول جزئية خاصة من موضوع آخر؛ أما هذا الزوج فهو كامل.', 'يشمل المصدر تعريف العقد وأركانه وآثاره صراحة.']:
            self.assertEqual(rationale_issues([self.pair(90,reason)]),[])

    def test_fixture_reproduces_the_three_live_contradictions_without_regrading(self):
        reasons=['المصدر يغطي جمع القرآن، بينما الهدف يجمع بين الجمع والكتابة، مما يجعل التغطية جزئية لكن مرتبطة بشدة.',
                 'المصدر يغطي القراءات فقط، بينما الهدف يجمع بين القراءات ونزول القرآن على سبعة أحرف.',
                 'المصدر يغطي نزول القرآن على سبعة أحرف فقط، بينما الهدف يجمع بينه وبين القراءات.']
        pairs=[self.pair(score,reason) for score,reason in zip([90,80,80],reasons)]
        self.assertEqual(len(rationale_issues(pairs)),3)
        self.assertEqual([p['score']for p in pairs],[90,80,80])

    def test_direct_negation_does_not_become_an_affirmative_partiality_claim(self):
        for reason in ['ليست التغطية جزئية؛ بل يتطابق الموضوع والنطاق صراحة.',
                       'توافق كامل دون تغطية جزئية أو تفاصيل مفترضة.',
                       'المصدر لا يشمل عادة موضوعا أوسع؛ بل يطابق هذا النطاق المحدد صراحة.',
                       'ليس صحيحا أن التغطية جزئية؛ فالمفردتان متطابقتان.']:
            with self.subTest(reason=reason):self.assertEqual(rationale_issues([self.pair(100,reason)]),[])

    def test_negated_early_claim_does_not_erase_later_affirmative_gap(self):
        reason='ليست التغطية جزئية في التعريف؛ لكن التغطية جزئية في الأركان.'
        self.assertEqual(len(rationale_issues([self.pair(90,reason)])),1)

if __name__=='__main__':unittest.main()
