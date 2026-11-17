"""Validate the full pinned repo-local skill inventory, without executing it."""
import hashlib
import json
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parent.parent
EXPECTED={
    'engineering':('ask-matt','code-review','codebase-design','diagnosing-bugs','domain-modeling',
        'grill-with-docs','implement','implement-spec','improve-codebase-architecture','pr','prototype',
        'research','retro','setup-matt-pocock-skills','tdd','to-spec','to-tickets','triage','wayfinder','wizard'),
    'productivity':('grill-me','grilling','handoff','teach','to-questionnaire','wait-what','writing-for-agents'),
}


def check():
    directory=ROOT/'.agents/skills'
    manifest=json.loads((directory/'UPSTREAM.json').read_text())
    assert manifest['commit']=='59db609eb1e18aaf59b63284d0e52255aff0c63c'
    assert manifest['source']=='https://github.com/mattpocock/skills'
    names={name for group in EXPECTED.values() for name in group}
    assert {p.parent.name for p in directory.glob('*/SKILL.md')}==names
    assert {skill['name'] for skill in manifest['skills']}==names
    files=0
    assets=[]
    references=[]
    scripts=[]
    for skill in manifest['skills']:
        name=skill['name'];folder=directory/name
        group=next(group for group,values in EXPECTED.items() if name in values)
        assert skill['source']==f'skills/{group}/{name}'
        recorded={entry['path'] for entry in skill['files']}
        actual={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file()}
        assert recorded==actual
        text=(folder/'SKILL.md').read_text()
        header=yaml.safe_load(text.split('---',2)[1])
        assert header['name']==name and header['description']
        assert yaml.safe_load((folder/'agents/openai.yaml').read_text())['policy']['allow_implicit_invocation'] is False
        for entry in skill['files']:
            path=folder/entry['path']
            assert not path.is_symlink() and path.resolve().is_relative_to(folder.resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest()==entry['sha256']
            files+=1
            if entry['path'].startswith('assets/'):assets.append(str(path.relative_to(ROOT)))
            elif entry['path'] not in ('SKILL.md','agents/openai.yaml'):
                references.append(str(path.relative_to(ROOT)))
            if path.suffix in ('.sh','.py','.js','.mjs','.ps1'):scripts.append(str(path.relative_to(ROOT)))
    assert files==79
    assert 'MIT License' in (directory/'LICENSE.mattpocock').read_text()
    agents=(ROOT/'AGENTS.md').read_text()
    for route in ('grill-with-docs','to-spec','to-tickets','tdd','implement','diagnosing-bugs','code-review','pr'):
        assert '`'+route+'`' in agents
    assert 'Draft PR' in agents and 'docs/agents/issue-tracker.md' in agents
    adapter=(ROOT/'docs/agents/issue-tracker.md').read_text()
    assert 'PR 合併' in adapter and '驗收' in adapter and 'Related' in adapter
    return {'success':True,'upstream':manifest['source'],'commit':manifest['commit'],
        'skills':{group:list(names) for group,names in EXPECTED.items()},'skill_count':27,'file_count':files,
        'assets_files':assets,'other_reference_files':references,'scripts_inventoried_not_executed':scripts,
        'implicit_invocation_disabled':True,'AGENTS_routes_verified':True,'next_run_discovery_verified':False}


if __name__=='__main__':print(json.dumps(check(),ensure_ascii=False,indent=2))
