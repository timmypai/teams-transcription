"""Public workflow permissions/events are part of the activation safety contract."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ShadowWorkflowTests(unittest.TestCase):
    def test_shadow_only_reads_trusted_main_and_has_no_write_permissions(self):
        workflow = yaml.load((ROOT / '.github/workflows/pr-policy-shadow.yml').read_text(),
                             Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['permissions'], {
            'contents': 'read', 'pull-requests': 'read', 'checks': 'read'})
        job = workflow['jobs']['shadow']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertEqual(job['timeout-minutes'], '3')
        self.assertIn("github.ref == 'refs/heads/main'", job['if'])
        self.assertIn("vars.ENABLE_PR_POLICY_SHADOW == 'true'", job['if'])
        checkout = job['steps'][0]
        self.assertEqual(checkout['with']['ref'], '${{ github.workflow_sha }}')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        self.assertNotIn('permissions', job)
        self.assertNotIn('secrets', job)

    def test_proposed_nonprivileged_ci_is_outside_active_workflow_directory(self):
        proposal = ROOT / 'docs/pr-automation/pr-ci.yml.example'
        workflow = yaml.load(proposal.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'pull_request'})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        for job in workflow['jobs'].values():
            self.assertEqual(job['runs-on'], 'ubuntu-24.04')
            self.assertNotIn('secrets', job)
            self.assertNotIn('permissions', job)
        self.assertFalse((ROOT / '.github/workflows/pr-ci.yml').exists())
