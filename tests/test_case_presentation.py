import copy
import unittest

from trace_hunter.case_presentation import case_presentation, first_sentence


class CasePresentationTests(unittest.TestCase):
    def test_description_wins_without_changing_source_identity(self):
        case = {'query_id': 'source-case', 'title': '原标题', 'description': '  建立进货台账，统计库存。 ',
                'conversation': {'mode': 'unknown', 'turns': []}}
        original = copy.deepcopy(case)
        result = case_presentation(case, [{'query': '原始第一句。第二句。'}], 2)
        self.assertEqual(result, {'display_id': 'category 002', 'display_description': '建立进货台账，统计库存。'})
        self.assertEqual(case, original)

    def test_first_input_sentence_fallback_and_no_invented_summary(self):
        case = {'title': '原标题', 'description': '', 'conversation': {'turns': [{'prompt': '请创建台账。再添加统计。'}]}}
        self.assertEqual(case_presentation(case, [{'query': '执行后续请求'}], 1001)['display_description'], '请创建台账。')
        self.assertEqual(case_presentation(case, [], 1001)['display_id'], 'category 1001')
        self.assertEqual(case_presentation({'title': '标题'}, [{'query': '第一句\n第二行'}], 1)['display_description'], '第一句')
        self.assertEqual(case_presentation({'title': '标题'}, [], 1)['display_description'], '标题')
        self.assertEqual(first_sentence('Read https://example.com/data at 3.14. Then continue.'), 'Read https://example.com/data at 3.14.')

    def test_unpunctuated_long_input_is_bounded(self):
        result = case_presentation({'description': '长' * 5000}, [], 1)
        self.assertEqual(len(result['display_description']), 240)
        self.assertTrue(result['display_description'].endswith('…'))

    def test_collector_preamble_stays_in_source_but_not_list_subtitle(self):
        case = {'description': '【来源摘要，完整 query 未提供】创建进货台账'}
        self.assertEqual(case_presentation(case, [], 1)['display_description'], '创建进货台账')
        self.assertTrue(case['description'].startswith('【来源摘要'))
