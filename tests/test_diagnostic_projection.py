"""Private log bodies never leave the diagnostic boundary, including unknown formats."""
import json
import unittest
import importlib.util
import sys
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from deploy import diagnostic_projection as projection


class DiagnosticProjectionTests(unittest.TestCase):
    def test_recent_safe_events_survive_but_sensitive_lines_never_do(self):
        secrets=['ya29.synthetic-google-access-secret','1//synthetic-refresh-token',
            'ghp_'+'a'*36,'sk-proj-'+'b'*40,'alice.private@example.invalid',
            '王小明的私人逐字稿','https://teams.microsoft.com/l/meetup-join/PRIVATE-MEETING',
            'v=0 SDP PRIVATE','encoded-audio-PRIVATE','-----BEGIN PRIVATE KEY-----',
            '{"authorization":"Bearer PRIVATE","refresh_token":"PRIVATE"}',
            'INFO:     Application startup complete. PRIVATE-TRAILER']
        lines=['2026-10-29T07:00:00.123456789Z INFO:     Application startup complete.']
        lines.extend('2026-10-29T07:00:01Z '+value for value in secrets)
        result=projection.recent_logs('\n'.join(lines).encode())
        rendered=json.dumps(result,ensure_ascii=False)
        for secret in secrets:self.assertNotIn(secret,rendered)
        self.assertEqual([r['event'] for r in result['events']],['server_started'])
        self.assertEqual(result['omitted_lines'],len(secrets))

    def test_fixed_line_and_byte_bounds_discard_unknown_oversized_content(self):
        line=b'2026-10-29T07:00:00Z INFO:     Application startup complete.\n'
        result=projection.recent_logs(line*150)
        self.assertEqual(len(result['events']),100)
        self.assertTrue(result['line_limit_reached'])
        result=projection.recent_logs(b'PRIVATE'*20000)
        self.assertTrue(result['byte_limit_reached'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        malformed=projection.recent_logs(b'2026-10-29T07:00:00Z \xffPRIVATE\n'
            b'2026-99-99T07:00:00Z INFO:     Application startup complete.\n')
        self.assertEqual(malformed['events'],[])
        self.assertEqual(malformed['omitted_lines'],2)

    def test_resource_summary_accepts_only_numeric_usage_and_limits(self):
        value=projection.resource_usage({'CPUPerc':'12.50%','MemUsage':'32MiB / 4GiB','PIDs':'7'})
        self.assertEqual(value,{'cpu_percent':12.5,'memory_used_bytes':33554432,
            'memory_limit_bytes':4294967296,'pids':7})
        for bad in ('PRIVATE alice@example.invalid','nan%','-1%'):
            with self.assertRaises(ValueError):
                projection.resource_usage({'CPUPerc':bad,'MemUsage':'32MiB / 4GiB','PIDs':'7'})

    def test_metadata_drops_unknown_private_fields_and_links_known_source_run(self):
        flags=projection.database_flags({'database_present':True,'quiescent':True,'schema':'a'*64,
            'transcript':'PRIVATE','oauth':'PRIVATE','full_config':{'secret':'PRIVATE'}})
        self.assertNotIn('PRIVATE',json.dumps(flags))
        image='ghcr.io/paulpai0412/teams-transcription@sha256:'+'a'*64
        link=projection.release_link({'repository':'paulpai0412/teams-transcription','image':image,
            'source_sha':'b'*40,'build_run_id':123,'build_run_attempt':1,'token':'PRIVATE'},image)
        self.assertEqual(link['source_sha'],'b'*40)
        self.assertEqual(link['build_run_url'],'https://github.com/paulpai0412/teams-transcription/actions/runs/123')
        self.assertNotIn('PRIVATE',json.dumps(link))
        for bad in ('PRIVATE','https://evil.invalid'):
            with self.assertRaises(ValueError):projection.database_flags({'schema':bad})

    def test_complete_controller_report_has_only_projected_logs_flags_resources_and_run_links(self):
        report=self.controller_report()
        output=json.dumps(report,ensure_ascii=False)
        for private in ('PRIVATE','王小明','alice.private@example.invalid','Bearer','refresh_token','PRIVATE-TRANSCRIPT'):
            self.assertNotIn(private,output)
        self.assertEqual(report['diagnostic_run_id'],987)
        self.assertEqual(report['deployment_run_id'],456)
        self.assertEqual(report['release']['build_run_id'],123)
        self.assertEqual(report['resources']['usage']['pids'],7)
        self.assertEqual(report['recent_logs']['events'][0]['event'],'server_started')

    def test_complete_controller_rejects_private_string_in_persisted_run_identity(self):
        with self.assertRaises(ValueError):self.controller_report(deployment='PRIVATE')

    def controller_report(self,deployment=456):
        spec=importlib.util.spec_from_file_location('diagnostic_test_controller',
            Path(__file__).resolve().parent.parent/'deploy/stagingctl.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        image='ghcr.io/paulpai0412/teams-transcription@sha256:'+'a'*64
        class ControlledDocker(module.Controller):
            def __init__(self):self.env={}
            def compose(self,*args,**kwargs):return 'a'*64
            def health(self):return {'status':'running','health':'healthy','image':image,'oom':False,'exit_code':0}
            def probe(self,*args):return {'database_present':True,'quiescent':True,'schema':'a'*64,'transcript':'PRIVATE'}
            def docker(self,command,*args,**kwargs):
                if command=='inspect':return json.dumps({'memory_limit_bytes':4294967296,'cpu_limit_nano':2000000000,'pids_limit':512})
                if command=='stats':return json.dumps({'CPUPerc':'1.25%','MemUsage':'32MiB / 4GiB','PIDs':'7','PRIVATE':'PRIVATE'})
                raise AssertionError('unexpected Docker operation')
        state={'current':image,'phase':'healthy','deployment_run_id':deployment,'release':{'repository':'paulpai0412/teams-transcription',
            'image':image,'source_sha':'b'*40,'build_run_id':123,'build_run_attempt':1,'secret':'PRIVATE'}}
        def logs(command,**kwargs):
            self.assertEqual(command[1:7],['logs','--since','15m','--tail','100','--timestamps'])
            kwargs['stdout'].write(b'2026-10-29T07:00:00Z INFO:     Application startup complete.\n'
                + '2026-10-29T07:00:01Z PRIVATE 王小明 alice.private@example.invalid Bearer refresh_token transcript\n'.encode())
            return SimpleNamespace(returncode=0)
        with patch.dict(sys.modules,{'diagnostic_projection':projection}), \
                patch.object(module.subprocess,'run',side_effect=logs), \
                patch.dict(os.environ,{'GITHUB_RUN_ID':'987','GITHUB_SHA':'c'*40}):
            try:return ControlledDocker().diagnostics(state)
            except module.Refused:raise ValueError('diagnostic_report_rejected') from None


if __name__=='__main__':unittest.main()
