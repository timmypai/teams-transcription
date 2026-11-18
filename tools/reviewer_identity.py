"""Target-only fixed identity, read from trusted main policy, never caller/env."""
import json
from pathlib import Path


def designated(user):
    policy=json.loads((Path(__file__).resolve().parents[1]/'policy/pr-policy.json').read_text())
    expected={'login':'paulpai0412','id':273007156,'type':'User'}
    return (policy.get('repository')=='timmypai/teams-transcription'
        and policy.get('designated_reviewer')==expected
        and isinstance(user,dict) and type(user.get('id')) is int
        and all(user.get(k)==v for k,v in expected.items()))
