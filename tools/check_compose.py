"""Render standalone Compose with synthetic inputs; no service or registry access."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from teams_transcription.assistant_config import load_assistant_settings
from teams_transcription.storage import atomic_json,stamp


def example_values():
    return dict(line.split('=',1) for line in (ROOT/'.env.example').read_text().splitlines()
        if line and not line.startswith('#'))


def render(file,overrides=()):
    environment=os.environ.copy()
    # Shell values outrank --env-file in Compose. Never let an operator's
    # deployment select the image, private env file, mounts or fixture identity.
    for name in (*example_values(),'DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        environment.pop(name,None)
    command=['docker','--host=unix:///var/run/docker.sock','compose','--env-file',str(file),'-f',str(ROOT/'docker-compose.yml')]
    for name in overrides:
        command.extend(['-f',str(ROOT/name)])
    command.extend(['config','--format','json'])
    result=subprocess.run(command,env=environment,capture_output=True,text=True,timeout=30)
    if result.returncode:
        raise ValueError('compose_configuration_rejected')
    return json.loads(result.stdout)


def write_env(file,values):
    file.write_text(''.join(f'{key}={value}\n' for key,value in values.items()))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    checks={}
    with tempfile.TemporaryDirectory(prefix='assistant-compose-config-') as root:
        root=Path(root)
        file=root/'fixture.env'
        values=example_values()
        values.update(TRANSCRIPTION_IMAGE='example.invalid/standalone-fixture@sha256:'+'a'*64,
            PUID='10001',PGID='10001',APPDATA_HOST=str(root/'appdata'),CONFIG_ENV_FILE=str(file),
            HOST_PORT='18081',ASSISTANT_PORT='18080',ASSISTANT_MAX_DURATION_SECONDS='900',
            CODEX_AUTH_HOME_HOST=str(root/'same-deployment-auth'))
        write_env(file,values)
        # A real shell's deployment values must not override this fixture.
        # The conflicting env file deliberately does not exist: reading it
        # would fail rather than silently consume an operator's configuration.
        with patch.dict(os.environ,{
                'CONFIG_ENV_FILE':str(root/'not-an-operator.env'),
                'APPDATA_HOST':str(root/'not-operator-data'),
                'TRANSCRIPTION_IMAGE':'example.invalid/not-the-fixture:unused',
                'PUID':'0','PGID':'0','ASSISTANT_PORT':'65535'}):
            base=render(file)
        service=base['services']['teams-transcription']
        checks['fixture_ignores_ambient_deployment']=(
            service['image']==values['TRANSCRIPTION_IMAGE']
            and service['environment']['CONFIG_ENV_FILE']==str(file)
            and service['environment']['ASSISTANT_PORT']=='18080'
            and any(value['target']=='/data' and value['source']==values['APPDATA_HOST'] for value in service['volumes']))
        checks['base_has_no_published_ports']=not service.get('ports')
        checks['nonroot_explicit_identity']=service['user']=='10001:10001'
        checks['read_only_drop_caps']=service['read_only'] and service['cap_drop']==['ALL']
        security=service['security_opt']
        checks['sandbox_without_privilege_bypass']=('no-new-privileges:true' in security
            and any(value.startswith('seccomp:') and value.endswith('chromium-seccomp.json') for value in security)
            and not service.get('privileged',False) and service.get('ipc')!='host'
            and service.get('network_mode')!='host')
        checks['bounded_resources']=service['pids_limit']==512 and int(service['mem_limit'])==4294967296 and float(service['cpus'])==2
        checks['background_entrypoint']=service['command']==['serve','--config','/data/config/runtime.yml']
        checks['health_port_matches_runtime']=service['healthcheck']['test']==['CMD','teams-transcription','healthcheck','--port','18080']
        checks['private_data_mount_no_auto_creation']=any(value['target']=='/data' and not value.get('bind',{}).get('create_host_path',False) for value in service['volumes'])
        local=render(file,['compose.local.yml'])['services']['teams-transcription']
        checks['local_management_loopback_only']=local['ports'][0]['host_ip']=='127.0.0.1' and int(local['ports'][0]['target'])==18080
        auth=render(file,['compose.auth.yml'])['services']['teams-transcription']
        checks['codex_auth_opt_in']=not any(value['target']=='/auth/codex' for value in service['volumes']) and auth['environment']['ASSISTANT_CODEX_HOME']=='/auth/codex'
        checks['auth_refresh_same_private_directory']=any(value['target']=='/auth/codex' and not value.get('read_only',False)
            and not value.get('bind',{}).get('create_host_path',False) for value in auth['volumes'])
        settings=load_assistant_settings(ROOT/'runtime.example.yml',environ=values)
        checks['environment_overrides_yaml']=settings.port==18080 and settings.max_duration_seconds==900
        for required in ('TRANSCRIPTION_IMAGE','PUID','PGID','APPDATA_HOST'):
            missing=dict(values)
            missing[required]=''
            write_env(file,missing)
            try:
                render(file)
            except ValueError:
                checks['required_'+required]=True
            else:
                checks['required_'+required]=False
    result={'test_id':'standalone-compose-config','tested_at':stamp(),'input_kind':'synthetic non-secret configuration',
        'service_started':False,'registry_accessed':False,'model_or_meeting_called':False,
        'checks':checks,'success':all(checks.values())}
    if args.output:
        if args.output.exists():
            raise ValueError('evidence_destination_exists')
        atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
