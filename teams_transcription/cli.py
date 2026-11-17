"""Standalone calendar daemon and private management UI; no meeting URLs in argv."""
import argparse
import asyncio
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

from .assistant_config import doctor,load_assistant_settings


def output(value):
    print(json.dumps(value,ensure_ascii=False,indent=2))


def parser():
    value = argparse.ArgumentParser(description='Teams 會議助理：Calendar、SQLite、雙 Live 與登入管理頁面')
    commands = value.add_subparsers(dest='command',required=True)
    for name,help_text in (
        ('serve','Start the standalone daemon and authenticated management UI'),
        ('init-admin','Create private admin credentials once; never overwrite'),
        ('auth-preflight','Offline software/mount/auth metadata; no login or account request'),
        ('auth-login','Explicit operator-only interactive Codex ChatGPT device authorization; never CI'),
        ('auth-check','Explicit readonly ChatGPT account admission; no model or audio'),
        ('doctor','Offline readiness only; no browser, meeting, model or token request')):
        command = commands.add_parser(name,help=help_text)
        command.add_argument('--config',help='Standalone assistant YAML; ASSISTANT_ environment overrides')
        if name=='serve':
            command.add_argument('--port',type=int)
            command.add_argument('--local-teams-control',action='store_true',
                help='Explicitly enable same-UID Unix control for the installed single-meeting helper')
        if name=='init-admin':
            command.add_argument('--password-stdin',action='store_true',help='Read one password line from stdin, not argv or environment')
        if name=='auth-login':
            command.add_argument('--confirm-account-authorization',action='store_true')
            command.add_argument('--confirm-private-terminal',action='store_true',help='Operator confirms unrecorded private TTY and disabled container logging')
            command.add_argument('--reauthorize',action='store_true',help='Explicitly replace authorization in the same private deployment home')
    command = commands.add_parser('healthcheck',help='Loopback-only HTTP health check; ignores proxy environment')
    command.add_argument('--port',type=int,default=8080)
    command = commands.add_parser('local-teams-control',help='One bounded JSON request on stdin to the opt-in same-UID Unix socket')
    command.add_argument('--config',required=True)
    command = commands.add_parser('probe-live',help='Explicit local synthetic speech → real GPT Live probe; no meeting')
    command.add_argument('--root',default='.state/probes',help='Private SQLite root for explicit synthetic probes')
    command.add_argument('--codex-bin',default='codex')
    command.add_argument('--chromium')
    command.add_argument('--ice-servers-file')
    command.add_argument('--output',default='evidence/live-probe.json')
    command = commands.add_parser('probe-media',help='Explicit TURN/ICE diagnostics; no meeting or audio')
    command.add_argument('--relay-host',required=True)
    command.add_argument('--relay-port',type=int,default=443)
    command.add_argument('--chromium')
    command.add_argument('--output',default='evidence/media-probe.json')
    command = commands.add_parser('probe-teams',help='Explicit Teams page/proxy probe; does not join')
    command.add_argument('--output',default='evidence/teams-probe.json')
    return value


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        if args.command=='healthcheck':
            if not 1024<=args.port<=65535:
                raise ValueError('invalid_health_port')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{args.port}/healthz',timeout=2) as response:
                return 0 if response.status==200 else 1
        if args.command=='probe-live':
            from .probe import probe
            return asyncio.run(probe(args))
        if args.command=='probe-media':
            from .media import probe_media
            return asyncio.run(probe_media(args))
        if args.command=='probe-teams':
            from .probe import probe_teams
            return probe_teams(args)
        settings = load_assistant_settings(args.config,{'port':getattr(args,'port',None)})
        if args.command=='local-teams-control':
            from .teams_local_control import request, decode_message, MAX_REQUEST
            raw=sys.stdin.buffer.readline(MAX_REQUEST+1)
            if not raw.endswith(b'\n') or len(raw)>MAX_REQUEST:
                raise ValueError('teams_local_control_rejected')
            output(asyncio.run(request(settings.data_root,decode_message(raw))))
            return 0
        if args.command=='doctor':
            result = doctor(settings)
            output(result)
            return 0 if result['local_ready'] else 1
        if args.command in ('auth-preflight','auth-login','auth-check'):
            from .codex_auth import preflight,device_login,check_account
            if args.command=='auth-preflight':
                result=preflight(settings);success=result['login_ready']
            elif args.command=='auth-login':
                result=device_login(settings,confirmed=args.confirm_account_authorization,
                    private_terminal=args.confirm_private_terminal,reauthorize=args.reauthorize);success=True
            else:
                result=asyncio.run(check_account(settings));success=result['account_admitted']
            output(result)
            return 0 if success else 1
        if args.command=='init-admin':
            from .admin_web import initialize_admin_credentials
            if args.password_stdin:
                password = sys.stdin.readline(4097).rstrip('\r\n')
            else:
                password = getpass.getpass('管理密碼（至少 12 字元）：')
                if password!=getpass.getpass('再次輸入管理密碼：'):
                    output({'error':'admin_password_mismatch'})
                    return 1
            initialize_admin_credentials(settings,password)
            output({'admin_initialized':True})
            return 0
        import uvicorn
        from .admin_web import create_admin_app
        from .daemon import AssistantDaemon
        if settings.codex_home is not None:
            os.environ['CODEX_HOME'] = settings.codex_home
        daemon = AssistantDaemon(settings)
        # OAuth callbacks contain codes. Never place URL query strings in access logs.
        uvicorn.run(create_admin_app(daemon,settings,local_teams_control=args.local_teams_control),host=settings.host,port=settings.port,
            workers=1,access_log=False,proxy_headers=False,log_level=settings.log_level,
            timeout_graceful_shutdown=settings.shutdown_seconds+5)
        return 0
    except (ValueError,OSError,RuntimeError,urllib.error.URLError) as exc:
        from .admin_web import AdminError
        from .assistant_store import AssistantStoreError
        from .codex_auth import AuthError
        from .teams_local_control import LocalControlError
        if isinstance(exc,(AdminError,AssistantStoreError,AuthError,LocalControlError)):
            code = exc.code
        elif isinstance(exc,ValueError):
            code = 'invalid_assistant_configuration'
        else:
            code = 'assistant_runtime_failed'
        output({'error':code})
        return 1


if __name__=='__main__':
    raise SystemExit(main())
