"""Read-only, fail-closed GitHub Actions evidence verification for one package.

Install next to stagingctl, root-owned. No shell, credentials on disk, arbitrary
URL or artifact extraction. A cached receipt is only for a previously admitted
healthy rollback image in the controller's private deployment history.
"""
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'paulpai0412/teams-transcription'
IMAGE = 'ghcr.io/' + REPOSITORY
WORKFLOW = '.github/workflows/arm64-image.yml'
API = 'https://api.github.com/repos/' + REPOSITORY
MAX_ARCHIVE = 8 * 1024 * 1024
VALIDATION = {'offline_contracts':True,'compose_config':True,'compose_smoke':True,'image_content_scan':True}


class Rejected(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_host(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
        and parsed.port in (None,443) and not parsed.fragment
        and (parsed.hostname or '').endswith(('.blob.core.windows.net','.actions.githubusercontent.com')))


def github_request(token):
    if not isinstance(token,str) or not token or len(token)>16384 or any(c.isspace() for c in token):
        raise Rejected('job_github_token_required')
    opener = urllib.request.build_opener(NoRedirect())

    def read(response, maximum):
        data = response.read(maximum+1)
        if len(data)>maximum:
            raise Rejected('github_evidence_size_exceeded')
        return data

    def request(path):
        if not re.fullmatch(r'/actions/[A-Za-z0-9_./?=+-]+',path) or '..' in path:
            raise Rejected('github_evidence_path_rejected')
        archive = path.endswith('/zip')
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
            'X-GitHub-Api-Version':'2022-11-28','User-Agent':'teams-staging-release-gate'}
        try:
            try:
                with opener.open(urllib.request.Request(API+path,headers=headers),timeout=25) as response:
                    data=read(response,MAX_ARCHIVE if archive else 1024*1024)
            except urllib.error.HTTPError as error:
                if not archive or error.code!=302:
                    raise Rejected('github_access_denied' if error.code in (401,403) else 'github_evidence_unavailable') from None
                location=error.headers.get('Location','')
                if not download_host(location):
                    raise Rejected('artifact_download_host_rejected') from None
                # A new request carries NO GitHub Authorization to signed storage.
                with opener.open(urllib.request.Request(location,headers={'User-Agent':'teams-staging-release-gate'}),timeout=25) as response:
                    data=read(response,MAX_ARCHIVE)
            return data if archive else json.loads(data)
        except (urllib.error.URLError,OSError,ValueError):
            raise Rejected('github_evidence_request_failed') from None
    return request


def identity(digest, source_sha, run_id):
    if (not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest)
            or not isinstance(source_sha,str) or not re.fullmatch(r'[0-9a-f]{40}',source_sha)
            or not isinstance(run_id,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',run_id)):
        raise Rejected('release_identity_invalid')
    return IMAGE+'@'+digest, int(run_id)


def verify_release(digest, source_sha, run_id, *, token=None, request=None, cached=None):
    image, number = identity(digest,source_sha,run_id)
    request = request or github_request(token)
    try:
        workflow=request('/actions/workflows/arm64-image.yml')
        if workflow['path']!=WORKFLOW or type(workflow['id']) is not int:
            raise Rejected('release_workflow_rejected')
        if cached is None:
            run=request('/actions/runs/'+run_id)
        else:
            attempt=cached['build_run_attempt']
            if type(attempt) is not int or not 0<attempt<1000000:
                raise Rejected('cached_release_rejected')
            run=request(f'/actions/runs/{run_id}/attempts/{attempt}')
        if (run['id']!=number or type(run['run_attempt']) is not int or not 0<run['run_attempt']<1000000
                or run['workflow_id']!=workflow['id'] or run['path']!=WORKFLOW
                or run['repository']['full_name']!=REPOSITORY or run['head_repository']['full_name']!=REPOSITORY
                or run['head_sha']!=source_sha or run['head_branch']!='main'
                or run['event']!='workflow_dispatch' or run['status']!='completed' or run['conclusion']!='success'):
            raise Rejected('release_run_not_successful_trusted_main')
        attempt=run['run_attempt']
        if cached is not None:
            proof=dict(cached)
        else:
            artifacts=request(f'/actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
            matches=[a for a in artifacts if a.get('name')==f'arm64-image-{number}-{attempt}']
            if len(matches)!=1:
                raise Rejected('release_artifact_missing_or_ambiguous')
            artifact=matches[0]
            if (artifact['expired'] is not False or type(artifact['id']) is not int or artifact['id']<=0
                    or artifact['workflow_run']['id']!=number or artifact['workflow_run']['head_sha']!=source_sha
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}',artifact['digest'])):
                raise Rejected('release_artifact_rejected')
            archive=request(f'/actions/artifacts/{artifact["id"]}/zip')
            if not isinstance(archive,bytes) or len(archive)>MAX_ARCHIVE or 'sha256:'+hashlib.sha256(archive).hexdigest()!=artifact['digest']:
                raise Rejected('release_artifact_integrity_failed')
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                entries=bundle.infolist()
                proofs=[entry for entry in entries if entry.filename=='published-image.json']
                if len(entries)>32 or len(proofs)!=1 or proofs[0].file_size>16384:
                    raise Rejected('release_proof_bounds_rejected')
                proof=json.loads(bundle.read(proofs[0]))
            proof['artifact_sha256']=artifact['digest']
        if (proof['schema_version']!=1 or proof['repository']!=REPOSITORY or proof['source_sha']!=source_sha
                or proof['image']!=image or proof['platform']!='linux/arm64' or proof['build_run_id']!=number
                or proof['build_run_attempt']!=attempt or proof['validation']!=VALIDATION
                or any(type(v) is not bool for v in proof['validation'].values())
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',proof['artifact_sha256'])):
            raise Rejected('release_proof_does_not_match_candidate')
        return {'schema_version':1,'repository':REPOSITORY,'source_sha':source_sha,'image':image,
            'platform':'linux/arm64','build_run_id':number,'build_run_attempt':attempt,
            'validation':dict(VALIDATION),'artifact_sha256':proof['artifact_sha256']}
    except (KeyError,TypeError,ValueError,zipfile.BadZipFile,RuntimeError):
        raise Rejected('release_evidence_invalid') from None
