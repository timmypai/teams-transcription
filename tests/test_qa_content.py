import unittest

from teams_transcription.qa_content import assess_content, CRITERION_VERSION


class ContentCriterionTests(unittest.TestCase):
    def assess(self, answer, source='預算十二萬元。', delivered=3):
        return assess_content([answer], source, expected_version=3, delivered_version=delivered)

    def test_neutral_prefix_and_equivalent_complete_body_pass_without_exact_quote(self):
        for answer in ('好的，預算十二萬元。', '根據目前提供的會議紀錄，答案是：預算為120000元。',
                '我先只逐字摘錄原文：「預算十二萬元。」'):
            with self.subTest(answer=answer):
                result=self.assess(answer)
                self.assertEqual(result['status'],'pass')
                self.assertEqual(result['criterion_version'],CRITERION_VERSION)

    def test_wrong_stale_multiple_values_and_new_claims_do_not_pass_by_substring(self):
        for answer in ('預算十萬元。','預算十二萬元，另有十萬元。',
                '預算十二萬元，明天已核准。','預算十二萬元。支出已完成。','預算不是十二萬元。'):
            with self.subTest(answer=answer):
                self.assertNotEqual(self.assess(answer)['status'],'pass')

    def test_unknown_forms_and_source_revision_mismatch_are_explicit(self):
        self.assertEqual(self.assess('The budget is120000.')['status'],'unknown')
        self.assertEqual(self.assess('預算十二萬元。',delivered=2)['reason'],'source_version_mismatch')
        self.assertEqual(self.assess('方案已通過。',source='方案已通過。')['status'],'unknown')
        self.assertNotEqual(self.assess('預算一二萬元。',source='預算兩萬元。')['status'],'pass')

    def test_all_utterances_are_checked_not_only_last(self):
        result=assess_content(['預算十萬元。','預算十二萬元。'],'預算十二萬元。',expected_version=3,delivered_version=3)
        self.assertNotEqual(result['status'],'pass')

    def test_supported_time_fact_preserves_time_and_rejects_added_fact(self):
        source='會議時間是週三下午三點。'
        self.assertEqual(self.assess('好的，開會時間為週三下午三點。',source)['status'],'pass')
        self.assertNotEqual(self.assess('會議時間是週三下午四點。',source)['status'],'pass')
        self.assertNotEqual(self.assess(source+'預算十二萬元。',source)['status'],'pass')

    def test_nearby_wrong_decimal_and_inferred_currency_cannot_pass(self):
        for answer in ('預算119999.999999999999999999999999999999元。',
                '預算120000.000000000000000000000000000001元。','預算新台幣十二萬元。'):
            with self.subTest(answer=answer):
                self.assertNotEqual(self.assess(answer)['status'],'pass')

    def test_normalization_expansion_stays_bounded_and_unknown(self):
        self.assertEqual(self.assess('預算'+'㊿'*2200+'元。')['status'],'unknown')
