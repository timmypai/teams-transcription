"""Fixed manual source/main gate; never dispatches a workflow."""
from pathlib import Path
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]

class ReadinessWorkflowTests(unittest.TestCase):
    def test_no_caller_target_or_trust_inputs_and_secret_only_after_admission(self):
        w=yaml.load((ROOT/'.github/workflows/readiness.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(w['on']),{'workflow_dispatch'})
        self.assertIn(w['on']['workflow_dispatch'],(None,''))
        self.assertEqual(w['permissions'],dict(contents='read',actions='read',**{'pull-requests':'read','checks':'read'}))
        self.assertEqual(w['concurrency'],dict(group='manual-target-teams-transcription',**{'cancel-in-progress':'false'}))
        j=w['jobs']['verify'];self.assertEqual(j['runs-on'],['self-hosted','devflow'])
        self.assertIn("github.ref == 'refs/heads/main'",j['if'])
        steps=j['steps'];self.assertEqual(steps[0]['with']['ref'],'${{ github.sha }}')
        self.assertEqual(steps[0]['with']['persist-credentials'],'false')
        self.assertIn('target_admission.py',steps[1]['run']);self.assertNotIn('DEVFLOW_APP_ENV',str(steps[1]))
        self.assertEqual(steps[2]['run'],'python3 deploy/target_deploy.py verify')
        self.assertEqual(steps[2]['env']['DEVFLOW_APP_ENV'],'${{ secrets.DEVFLOW_APP_ENV }}')
        self.assertNotIn('inputs.',str(w))
        self.assertEqual(steps[3]['with']['path'],'.devflow/evidence-deploy-${{ github.run_id }}-${{ github.run_attempt }}/')
