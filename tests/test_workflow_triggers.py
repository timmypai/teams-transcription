"""Initial Actions rollout must require an explicit dispatch before any runner.

The event declarations are GitHub's public trigger interface. This policy does
not depend on repository variables, which may be unreadable or already enabled.
"""
from pathlib import Path
import hashlib
import unittest

import yaml


class ManualWorkflowTriggerTests(unittest.TestCase):
    def test_all_delivery_workflows_require_explicit_manual_dispatch(self):
        directory = Path(__file__).resolve().parents[1] / '.github' / 'workflows'
        # This task explicitly authorizes the new hosted-only ci.yml PR entry.
        # All seven inherited delivery/VM workflows keep their manual contract.
        workflows = sorted(w for w in [*directory.glob('*.yml'), *directory.glob('*.yaml')]
                           if w.name != 'ci.yml')
        self.assertEqual({w.name for w in workflows}, {
            'arm64-image.yml', 'meeting-functional-validation.yml',
            'oracle-staging.yml', 'oracle-diagnostics.yml', 'oracle-qa.yml',
            'pr-policy-shadow.yml', 'oracle-teams.yml',
        })
        for workflow in workflows:
            with self.subTest(workflow=workflow.name):
                # BaseLoader preserves the YAML 1.2 event name "on" as a string.
                definition = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
                self.assertIsInstance(definition.get('on'), dict)
                self.assertEqual(set(definition['on']), {'workflow_dispatch'},
                    'Push, PR and other automatic runner triggers need separate approval')

    def test_teams_workflow_matches_fixed_candidate_and_restricted_runner_contract(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/oracle-teams.yml'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
            '4d74f0c51a0b3dfae9412d41017c84e1e76f1a63fff1e9ffa3e3430b2b262286')
        workflow=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(workflow['permissions'],{})
        self.assertEqual(workflow['concurrency'],{'group':'oracle-staging','cancel-in-progress':'false'})
        job=workflow['jobs']['acceptance']
        self.assertEqual(job['runs-on'],['self-hosted','Linux','ARM64','teams-transcription-staging'])
        self.assertEqual(job['timeout-minutes'],'5')
        self.assertIn("github.ref == 'refs/heads/main'",job['if'])
        self.assertFalse(any('actions/checkout' in step.get('uses','') for step in job['steps']))
        self.assertIn('args=("$TEAMS_OPERATION")',job['steps'][0]['run'])
        self.assertIn('if [[ -n "$TEAMS_CASE_ID" ]]; then args+=(--case "$TEAMS_CASE_ID"); fi',job['steps'][0]['run'])
        self.assertIn('/opt/teams-staging/bin/teams-acceptance "${args[@]}"',job['steps'][0]['run'])
        inputs=workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs),{'operation','confirm_meeting','participant_confirmed_at','case_id'})
        self.assertEqual(inputs['case_id']['default'],'')
        self.assertEqual(job['steps'][0]['env']['TEAMS_CASE_ID'],'${{ inputs.case_id }}')
        self.assertEqual(inputs['operation']['options'],['prepare','offline','run','stop'])
        self.assertEqual(inputs['operation']['default'],'offline')
        self.assertEqual(inputs['confirm_meeting']['default'],'false')
        self.assertEqual(inputs['participant_confirmed_at']['default'],'')
        self.assertEqual(job['steps'][0]['env']['STAGING_TEAMS_CONFIRMED_AT'],'${{ inputs.participant_confirmed_at }}')
