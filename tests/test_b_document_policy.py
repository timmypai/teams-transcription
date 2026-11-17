"""Passive policy metadata with synthetic Playwright objects; no requests."""
import asyncio
import copy
import json
import unittest

from teams_transcription.b_document_policy import (
    DocumentPolicyCollector, sanitize_document_policy_snapshot,
)


class Page:
    def __init__(self):self.listeners=[]
    def on(self,event,callback):
        assert event=='response'
        self.listeners.append(callback)
    def remove_listener(self,event,callback):self.listeners.remove(callback)
    def emit(self,response):
        for callback in list(self.listeners):callback(response)


class Frame:
    def __init__(self,page,url='https://private.invalid/meeting?token=PRIVATE'):
        self.page,self.url=page,url
        self.detached=False
    def is_detached(self):return self.detached


class Response:
    def __init__(self,frame,headers,*,kind='document',wait=None):
        self.frame,self.url,self.headers,self.wait=frame,frame.url,headers,wait
        self.request=type('Request',(),dict(resource_type=kind))()
        self.calls=[]
        self.started=asyncio.Event()
        self.finished=asyncio.Event()
    async def header_values(self,name):
        self.calls.append(name);self.started.set()
        try:
            if self.wait is not None:await self.wait.wait()
            return self.headers
        finally:self.finished.set()


class DocumentPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page=Page();self.frame=Frame(self.page)
        self.collector=DocumentPolicyCollector(self.page)
        self.addAsyncCleanup(self.collector.close)

    async def wait_snapshot(self,frame):
        async with asyncio.timeout(2):
            while not self.collector.snapshot(frame)['available']:
                await asyncio.sleep(0)
        return self.collector.snapshot(frame)

    async def test_only_document_csp_is_read_and_private_values_are_redacted(self):
        response=Response(self.frame,["default-src 'none'; script-src 'nonce-PRIVATE' 'sha256-PRIVATE' 'unsafe-inline' blob: 'strict-dynamic' https://private.invalid; trusted-types 'none'; require-trusted-types-for 'script'"])
        self.page.emit(response)
        result=await self.wait_snapshot(self.frame)
        self.assertEqual(response.calls,['content-security-policy'])
        flags=result['policies'][0]['directives']['script-src']
        self.assertTrue(flags['blob'] and flags['nonce_present'] and flags['hash_present'])
        self.assertTrue(flags['strict_dynamic'] and flags['unsafe_inline'])
        self.assertFalse(flags['self'])
        self.assertTrue(result['policies'][0]['trusted_types_none'])
        self.assertTrue(result['policies'][0]['trusted_types_script_required'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('private.invalid',json.dumps(result))
        self.assertNotIn('url',result);self.assertNotIn('hash',result)
        self.assertEqual(sanitize_document_policy_snapshot(result),result)

    async def test_non_document_or_other_page_is_not_read(self):
        for frame,kind in ((self.frame,'script'),(Frame(Page()),'document')):
            response=Response(frame,["script-src blob:"],kind=kind)
            self.page.emit(response)
            await asyncio.sleep(0)
            self.assertEqual(response.calls,[])
        self.assertEqual(self.collector.snapshot(self.frame),{'available':False})

    async def test_stale_url_and_superseded_read_do_not_restore_cache(self):
        gate=asyncio.Event()
        old=Response(self.frame,["script-src blob:"],wait=gate)
        self.page.emit(old);await old.started.wait()
        self.frame.url='https://private.invalid/new'
        new=Response(self.frame,["default-src 'none'"])
        self.page.emit(new)
        result=await self.wait_snapshot(self.frame)
        gate.set();await old.finished.wait();await asyncio.sleep(0)
        self.assertEqual(self.collector.snapshot(self.frame),result)
        self.frame.url='https://private.invalid/changed'
        self.assertEqual(self.collector.snapshot(self.frame),{'available':False})

    async def test_close_cancels_only_owned_reads_and_drops_stale_callback(self):
        response=Response(self.frame,["script-src blob:"],wait=asyncio.Event())
        callback=self.page.listeners[0]
        self.page.emit(response);await response.started.wait()
        self.assertTrue(await self.collector.close())
        self.assertTrue(response.finished.is_set());self.assertEqual(self.page.listeners,[])
        callback(Response(self.frame,["script-src blob:"]))
        await asyncio.sleep(0)
        self.assertEqual(self.collector.snapshot(self.frame),{'available':False})

    def test_untrusted_snapshot_is_strictly_revalidated(self):
        for value in (None,{},dict(available=True,policies=['PRIVATE'],policy_count=1),
                dict(available=True,policies=[],policy_count=0,url='PRIVATE'),dict(available=1)):
            self.assertEqual(sanitize_document_policy_snapshot(value),{'available':False})

    async def test_four_read_cap_and_eight_frame_cache_do_not_grow(self):
        gate=asyncio.Event()
        responses=[Response(Frame(self.page,'https://private.invalid/'+str(i)),[],wait=gate) for i in range(5)]
        for response in responses:self.page.emit(response)
        await asyncio.gather(*(r.started.wait() for r in responses[:4]))
        self.assertEqual(responses[4].calls,[])
        self.assertEqual(self.collector.snapshot(responses[4].frame),{'available':False})
        gate.set()
        await asyncio.gather(*tuple(self.collector.tasks))
        frames=[Frame(self.page,'https://private.invalid/cache/'+str(i)) for i in range(9)]
        for frame in frames:
            self.page.emit(Response(frame,[]))
            self.assertEqual((await self.wait_snapshot(frame))['policy_count'],0)
        self.assertEqual(self.collector.snapshot(frames[0]),{'available':False})
        self.assertTrue(all(self.collector.snapshot(frame)['available'] for frame in frames[1:]))

    async def test_policy_count_size_and_read_timeout_are_unknown_not_partial(self):
        for values in (["script-src blob:"]*5,["script-src "+'X'*16384],
                [','.join(["script-src blob:"]*5)]):
            response=Response(self.frame,values)
            self.page.emit(response)
            await asyncio.gather(*tuple(self.collector.tasks))
            self.assertEqual(self.collector.snapshot(self.frame),{'available':False})
        response=Response(self.frame,["script-src blob:"],wait=asyncio.Event())
        self.page.emit(response)
        await asyncio.wait_for(response.finished.wait(),2)
        await asyncio.gather(*tuple(self.collector.tasks))
        self.assertEqual(self.collector.snapshot(self.frame),{'available':False})

    async def test_multiple_policies_and_first_duplicate_directive_remain_separate(self):
        self.page.emit(Response(self.frame,["script-src 'none'; script-src blob:, script-src-elem 'self'", "default-src blob:"]))
        result=await self.wait_snapshot(self.frame)
        self.assertEqual(result['policy_count'],3)
        first=result['policies'][0]['directives']['script-src']
        self.assertTrue(first['none']);self.assertFalse(first['blob'])
        self.assertTrue(result['policies'][1]['directives']['script-src-elem']['self'])
        copy_of_result=copy.deepcopy(result)
        result['policies'][0]['directives']['script-src']['blob']=True
        self.assertEqual(self.collector.snapshot(self.frame),copy_of_result)
        self.frame.detached=True
        self.assertEqual(self.collector.snapshot(self.frame),{'available':False})
