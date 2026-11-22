"""Fixed client input must follow reviewed main admission and no dynamic args."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class GoogleClientWorkflowTests(unittest.TestCase):
    def test_manual_no_input_separate_secret_after_review_and_no_shell_path(self):
        workflow = yaml.load((ROOT/'.github/workflows/google-client.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        self.assertIn(workflow['on']['workflow_dispatch'],('',None))
        self.assertEqual(workflow['concurrency'],{'group':'manual-target-teams-transcription','cancel-in-progress':'false'})
        job = workflow['jobs']['install']
        self.assertEqual(job['runs-on'],['self-hosted','devflow'])
        self.assertIn("github.repository == 'timmypai/teams-transcription'",job['if'])
        self.assertIn("github.ref == 'refs/heads/main'",job['if'])
        steps = job['steps']
        self.assertEqual(steps[0]['with']['ref'],'${{ github.sha }}')
        self.assertEqual(steps[0]['with']['persist-credentials'],'false')
        self.assertIn('target_admission.py',steps[1]['run'])
        self.assertNotIn('secrets.',str(steps[:2]))
        self.assertEqual(steps[2]['run'],'python3 deploy/target_deploy.py install-google-client')
        self.assertEqual(steps[2]['env']['TEAMS_GOOGLE_CLIENT_JSON'],'${{ secrets.TEAMS_GOOGLE_CLIENT_JSON }}')
        self.assertNotIn('DEVFLOW_OPS_ENV',str(workflow))
        self.assertNotIn('DEVFLOW_APP_ENV',str(workflow))
        self.assertNotIn('inputs.',str(workflow))
        self.assertEqual(steps[3]['with']['path'],'.devflow/evidence-deploy-${{ github.run_id }}-${{ github.run_attempt }}/')
