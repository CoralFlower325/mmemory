"""真实消息质量入口的合成回归，不读取私人正文。"""

import copy
import unittest

import filter as memory_filter


FIELDS = {'goal': '完成合成功能', 'approach': '实现并实际运行',
          'outcome': '行为已完成', 'success_key': '真实执行并通过验证', 'pitfall': ''}


def messages():
    return [{'role': 'user', 'content': '实现合成功能'},
            {'role': 'assistant', 'content': '实现如下\n```python\nprint(1)\n```'},
            {'role': 'user', 'content': '功能已验证完成'}]


class QualityTests(unittest.TestCase):
    def assess(self, transcript=None, **kwargs):
        return memory_filter.assess_messages(messages() if transcript is None else transcript,
                                             FIELDS, **kwargs)

    def test_real_code_and_dialogue_pass_without_fabricated_complexity(self):
        self.assertTrue(self.assess()[0])

    def test_tool_messages_do_not_inflate_conversation_length(self):
        transcript = messages()[:2] + [{'role': 'tool', 'content': 'done'}] * 30
        self.assertFalse(self.assess(transcript)[0])

    def test_retain_itself_does_not_make_small_talk_reusable_work(self):
        transcript = [{'role': 'user', 'content': '你好'},
                      {'role': 'assistant', 'content': '你好', 'tool_calls': [
                          {'function': {'name': 'mmemory_retain'}}] * 9},
                      {'role': 'user', 'content': '好的'}]
        self.assertFalse(self.assess(transcript)[0])

    def test_five_actual_work_tools_pass(self):
        transcript = messages()
        transcript[1] = {'role': 'assistant', 'content': '已实施', 'tool_calls': [
            {'id': str(i), 'function': {'name': 'terminal'}} for i in range(5)]}
        transcript[2:2] = [{'role': 'tool', 'tool_call_id': str(i), 'content': '{"exit_code": 0}'}
                           for i in range(5)]
        self.assertTrue(self.assess(transcript)[0])
        self.assertFalse(self.assess([m for m in transcript if m['role'] != 'tool'])[0])

    def test_structured_error_with_solution_passes(self):
        transcript = messages()
        transcript[1]['content'] = '已经定位并修正'
        transcript[1]['tool_calls'] = [{'id': 'error', 'function': {'name': 'terminal'}}]
        transcript.insert(2, {'role': 'tool', 'tool_call_id': 'error', 'content': '{"exit_code": 2}'})
        self.assertTrue(memory_filter.assess_messages(
            transcript, {**FIELDS, 'pitfall': '调用报错，改正参数后成功'})[0])

    def test_explicit_preference_does_not_require_code(self):
        transcript = [{'role': 'user', 'content': '以后每版单独目录'},
                      {'role': 'assistant', 'content': '按版本保留'},
                      {'role': 'user', 'content': '对，这个是长期要求'}]
        self.assertTrue(self.assess(transcript, memory_kind='preference')[0])
        self.assertFalse(self.assess(transcript)[0])

    def test_cancel_resume_and_pending(self):
        transcript = messages() + [{'role': 'user', 'content': '不用做了'}]
        self.assertFalse(self.assess(transcript)[0])
        transcript.append({'role': 'user', 'content': '继续完成'})
        self.assertTrue(self.assess(transcript)[0])
        for state in ('in_progress', 'cancelled'):
            self.assertFalse(self.assess(transcript, task_state=state)[0])

    def test_simple_question_and_unknown_kind_rejected(self):
        transcript = messages()
        transcript[0]['content'] = '解释这个概念是什么意思'
        self.assertFalse(self.assess(transcript)[0])
        self.assertFalse(self.assess(memory_kind='test_fixture')[0])

    def test_multimodal_actual_text_is_kept_images_do_not_count(self):
        transcript = messages()
        transcript[0]['content'] = [{'type': 'text', 'text': '实现合成功能'},
                                    {'type': 'image_url', 'image_url': {'url': 'synthetic'}}]
        self.assertTrue(self.assess(transcript)[0])
        transcript[2]['content'] = [{'type': 'image_url', 'image_url': {'url': 'synthetic'}}]
        self.assertFalse(self.assess(transcript)[0])

    def test_missing_context_and_bad_fields_refuse_without_mutation(self):
        self.assertFalse(self.assess([])[0])
        self.assertFalse(memory_filter.assess_messages(None, FIELDS)[0])
        self.assertFalse(memory_filter.assess_messages(messages(), {**FIELDS, 'outcome': ''})[0])
        self.assertFalse(memory_filter.assess_messages(messages(), {**FIELDS, 'goal': None})[0])
        transcript = messages()
        before = copy.deepcopy(transcript)
        self.assess(transcript)
        self.assertEqual(transcript, before)


if __name__ == '__main__':
    unittest.main()
