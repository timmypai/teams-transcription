"""Success means recognized synthetic speech, not merely an HTTP connection."""
import json
import time
import urllib.request
import urllib.error
import concurrent.futures
from pathlib import Path
from .cli import output
from .storage import Session, atomic_json, stamp
from .worker import run_session
from .rpc import safe_error


async def probe(args):
    from .media import load_ice_servers
    load_ice_servers(args.ice_servers_file)
    session = Session(args.root).create({
        "source": "synthetic", "provider": "codex", "duration": 10,
        "codex_bin": args.codex_bin, "chromium": args.chromium,
        "ice_servers_file": str(Path(args.ice_servers_file).resolve()) if args.ice_servers_file else None,
        "drain_seconds": 8, "summary_model": None})
    started = time.monotonic()
    code = await run_session(session)
    text = " ".join(e["text"] for e in session.entries()).lower()
    speech_verified = "meeting" in text and ("ten" in text or "10" in text) and "report" in text
    with session.store._locked() as db:
        events = [{'type':row['kind'],'data':json.loads(row['payload_json'])} for row in db.execute(
            'SELECT kind,payload_json FROM session_events WHERE meeting_id=? ORDER BY rowid',(session.ident,))]
    result = {"tested_at": stamp(), "model": "gpt-live-1-codex", "version": "v3", "transport": "webrtc",
        "session": session.ident, "status": session.status(), "elapsed_seconds": round(time.monotonic() - started, 2),
        "source_transcript_received": bool(text), "synthetic_speech_verified": speech_verified,
        "connection_events": [event for event in events if event["type"] in {
            "live_sdp_received", "live_connected", "live_connection_failed", "live_error", "webrtc_stats"}],
        "success": code == 0 and speech_verified}
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, result)
    output(result)
    return 0 if result["success"] else 1


def probe_teams(args):
    def check(host):
        try:
            with urllib.request.urlopen("https://" + host + "/", timeout=15) as response:
                return {"host": host, "http_status": response.status, "reachable": True}
        except urllib.error.HTTPError as exc:
            return {"host": host, "http_status": exc.code, "reachable": False, "error": safe_error(exc)}
        except Exception as exc:
            return {"host": host, "reachable": False, "error": safe_error(exc)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        hosts = list(pool.map(check, ["teams.microsoft.com", "teams.cloud.microsoft"]))
    result = {"tested_at": stamp(), "joined_meeting": False, "hosts": hosts,
              "success": all(host["reachable"] for host in hosts)}
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, result)
    output(result)
    return 0 if result["success"] else 1
