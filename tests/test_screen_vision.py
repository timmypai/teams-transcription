"""ScreenVisionAnalyzer seam tests (read-only Codex localImage, schema, injection & cancel)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from teams_transcription.screen_vision import (
    SCREEN_OBSERVATION_SCHEMA,
    ScreenVisionAnalyzer,
    ScreenVisionError,
)


class FakeVisionRPC:
    def __init__(self, responses=None, *, account_type='chatgpt', forbidden_item=None, hang=False):
        self.notifications = asyncio.Queue()
        self.state_dir = Path('/dev/shm/fake-vision')
        self.responses = list(responses or [])
        self.account_type = account_type
        self.forbidden_item = forbidden_item
        self.hang = hang
        self.requests = []
        self.closed = False
        self.interrupted = False
        self._turn_seq = 0

    async def start(self):
        return self

    async def close(self):
        self.closed = True

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == 'account/read':
            return {'account': {'type': self.account_type}}
        if method == 'thread/start':
            self._turn_seq += 1
            return {'thread': {'id': f'vision-thread-{self._turn_seq}'},
                    'model': params.get('model') or 'gpt-5.6-sol', 'modelProvider': 'openai'}
        if method == 'turn/interrupt':
            self.interrupted = True
            return {}
        if method == 'turn/start':
            tid = params['threadId']
            turn_id = f'turn-{self._turn_seq}'
            if not self.hang:
                # Inject foreign thread noise that must be ignored
                await self.notifications.put({'method': 'item/completed', 'params': {
                    'threadId': 'foreign-thread', 'turnId': turn_id,
                    'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': '{"hacked":true}'}}})
                if self.forbidden_item:
                    await self.notifications.put({'method': 'item/completed', 'params': {
                        'threadId': tid, 'turnId': turn_id,
                        'item': {'type': self.forbidden_item, 'id': 'tool-1'}}})
                elif self.responses:
                    payload = self.responses.pop(0)
                    await self.notifications.put({'method': 'item/completed', 'params': {
                        'threadId': tid, 'turnId': turn_id,
                        'item': {'type': 'agentMessage', 'phase': 'final_answer',
                                 'text': payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)}}})
                await self.notifications.put({'method': 'turn/completed', 'params': {
                    'threadId': tid, 'turn': {'id': turn_id, 'status': 'completed'}}})
            return {'turn': {'id': turn_id}}
        return {}


def _valid_obs():
    return {
        'screen_kind': 'slide',
        'title': '架構總覽',
        'summary_text': '顯示 A/B 雙通道與 SQLite 儲存架構。',
        'key_items': ['A 通道保存逐字稿', 'B 通道負責問答'],
        'metrics_or_numbers': ['24 kHz', '500 bytes'],
        'visible_text_excerpt': 'Architecture: A -> SQLite -> B',
        'uncertain_areas': [],
    }


class ScreenVisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_frame_uses_readonly_sandbox_shm_local_image_and_reuses_rpc(self):
        rpc = FakeVisionRPC([_valid_obs(), {**_valid_obs(), 'title': '第二頁'}])
        with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='test-vis-') as tmp:
            img = Path(tmp) / 'frame.jpg'
            img.write_bytes(b'\xff\xd8\xff\xd9')
            analyzer = ScreenVisionAnalyzer(Path(tmp) / 'state', rpc_factory=lambda *a, **kw: rpc)
            try:
                r1 = await analyzer.analyze_frame(str(img))
                r2 = await analyzer.analyze_frame(str(img))
            finally:
                await analyzer.close()

        self.assertEqual(r1['title'], '架構總覽')
        self.assertEqual(r1['vision_model'], 'gpt-5.6-sol')
        self.assertEqual(r2['title'], '第二頁')
        self.assertTrue(rpc.closed)
        # account/read called once (RPC reused), thread/start called twice (ephemeral isolation per frame)
        self.assertEqual(sum(1 for m, _ in rpc.requests if m == 'account/read'), 1)
        thread_starts = [p for m, p in rpc.requests if m == 'thread/start']
        self.assertEqual(len(thread_starts), 2)
        for cfg in thread_starts:
            self.assertTrue(cfg['ephemeral'])
            self.assertEqual(cfg['sandbox'], 'read-only')
            self.assertEqual(cfg['approvalPolicy'], 'never')
            self.assertEqual(cfg['config']['mcp_servers'], {})
            self.assertFalse(cfg['config']['features']['shell_tool'])
            self.assertEqual(cfg['config']['web_search'], 'disabled')
        turn_starts = [p for m, p in rpc.requests if m == 'turn/start']
        self.assertEqual(turn_starts[0]['outputSchema'], SCREEN_OBSERVATION_SCHEMA)
        self.assertEqual(turn_starts[0]['input'][1]['type'], 'localImage')
        self.assertEqual(turn_starts[0]['input'][1]['detail'], 'high')

    async def test_rejects_non_shm_image_non_chatgpt_auth_and_forbidden_tool_items(self):
        with tempfile.TemporaryDirectory() as disk_tmp:
            disk_img = Path(disk_tmp) / 'disk.jpg'
            disk_img.write_bytes(b'\xff\xd8\xff\xd9')
            rpc = FakeVisionRPC([_valid_obs()])
            analyzer = ScreenVisionAnalyzer(Path(disk_tmp) / 'state', rpc_factory=lambda *a, **kw: rpc)
            with self.assertRaises(ScreenVisionError) as raised:
                await analyzer.analyze_frame(str(disk_img))
            self.assertEqual(raised.exception.code, 'vision_image_invalid')
            await analyzer.close()

        with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='test-vis-') as shm_tmp:
            img = Path(shm_tmp) / 'frame.jpg'
            img.write_bytes(b'\xff\xd8\xff\xd9')

            bad_auth_rpc = FakeVisionRPC([_valid_obs()], account_type='api_key')
            analyzer = ScreenVisionAnalyzer(Path(shm_tmp) / 's1', rpc_factory=lambda *a, **kw: bad_auth_rpc)
            with self.assertRaises(ScreenVisionError) as raised:
                await analyzer.analyze_frame(str(img))
            self.assertEqual(raised.exception.code, 'vision_account_denied')
            await analyzer.close()

            tool_rpc = FakeVisionRPC(forbidden_item='commandExecution')
            analyzer2 = ScreenVisionAnalyzer(Path(shm_tmp) / 's2', rpc_factory=lambda *a, **kw: tool_rpc)
            with self.assertRaises(ScreenVisionError) as raised:
                await analyzer2.analyze_frame(str(img))
            self.assertEqual(raised.exception.code, 'vision_tool_forbidden')
            await analyzer2.close()

    async def test_cancellation_sends_turn_interrupt_and_question_analysis_validates_schema(self):
        with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='test-vis-') as shm_tmp:
            img = Path(shm_tmp) / 'frame.jpg'
            img.write_bytes(b'\xff\xd8\xff\xd9')

            hang_rpc = FakeVisionRPC(hang=True)
            analyzer = ScreenVisionAnalyzer(Path(shm_tmp) / 's1', rpc_factory=lambda *a, **kw: hang_rpc, timeout=5)
            task = asyncio.create_task(analyzer.analyze_frame(str(img)))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(hang_rpc.interrupted)
            await analyzer.close()

            q_payload = {
                'answer_relevant_findings': '表格第二列 AI Research 為 85,500 USD，狀態為 Pending CFO。',
                'visible_evidence': ['AI Research | 85,500 | Pending CFO'],
                'uncertain_areas': [],
            }
            q_rpc = FakeVisionRPC([q_payload])
            q_analyzer = ScreenVisionAnalyzer(Path(shm_tmp) / 's2', rpc_factory=lambda *a, **kw: q_rpc)
            result = await q_analyzer.analyze_question(str(img), 'AI Research 金額是多少？')
            self.assertIn('85,500', result['answer_relevant_findings'])
            await q_analyzer.close()


if __name__ == '__main__':
    unittest.main()
