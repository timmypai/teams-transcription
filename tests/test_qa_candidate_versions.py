import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import shutil

from qa_c409_fixture import baseline_bytes, semantic_base_bytes, run_c409


class CandidateVersionTests(unittest.TestCase):
    def test_startup_fixture_restores_its_exact_browser_tx_without_repinning_current_source(self):
        result=run_c409('import hashlib,json;from pathlib import Path;from teams_transcription import b_browser_tx;'
            'from teams_transcription import b_no_plc_startup;'
            'print(json.dumps({"sha":hashlib.sha256(Path(b_browser_tx.__file__).read_bytes()).hexdigest(),'
            '"startup_sha":hashlib.sha256(Path(b_no_plc_startup.__file__).read_bytes()).hexdigest()}))',startup=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['sha'],'f523aad40474ea5499ba8cafa3611d457abdb4408260aac63e4b74618c2dc87f')
        self.assertEqual(json.loads(result.stdout)['startup_sha'],
            '9d2067fb66a7450cb4aac265d67f59f4d6894ee5848f8cdf564b19cef00becbe')

    def test_old_fixture_imports_exact_physical_c409_bytes_in_separate_process(self):
        result=run_c409('import hashlib,json;from pathlib import Path;from teams_transcription import qa_probe;'
            'print(json.dumps({"sha":hashlib.sha256(Path(qa_probe.__file__).read_bytes()).hexdigest()}))')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['sha'],'c718c7e08600b383a9916be3684393dcd531fb86007be8811b60a2431be39197')

    def test_changed_old_fixture_is_rejected_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture';path.write_bytes(b'raise AssertionError("must not execute")')
            with self.assertRaisesRegex(ValueError,'c409_fixture_hash_rejected'):
                baseline_bytes(path)

    def test_changed_semantic_fixture_is_rejected_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fixture';path.write_bytes(b'raise AssertionError("must not execute")')
            with self.assertRaisesRegex(ValueError,'semantic_fixture_hash_rejected'):
                semantic_base_bytes(path)

    def test_old_a_and_b_loaders_still_reject_the_new_semantic_source(self):
        from teams_transcription.qa_audio_probe import verify_base
        from deploy.qa_container_entry import run
        from test_cloud_qa_isolated import CloudQAIsolatedTests
        with self.assertRaisesRegex(ValueError,'observer_requires_reviewed_c4090ed_base'):
            verify_base()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'qa_entry_base_rejected'):
                run(CloudQAIsolatedTests().request(),data_root=Path(directory))
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_semantic_candidate_has_separate_review_pin_and_rejects_source_changes(self):
        from tools.check_qa_semantic_candidate import check, MANIFEST, FILES
        from teams_transcription.qa_content import CRITERION_VERSION
        root=Path(__file__).resolve().parents[1]
        pin=hashlib.sha256((root/MANIFEST).read_bytes()).hexdigest()
        # The current QA gained new cleanup evidence. Old preparation pins must
        # reject it, while the unchanged physical candidate remains verifiable.
        with self.assertRaisesRegex(ValueError,'semantic_candidate_source_rejected'):
            check(root,pin)
        with self.assertRaisesRegex(ValueError,'semantic_manifest_hash_rejected'):
            check(root,'0'*64)
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            for name in FILES|{MANIFEST}:
                path=target/name;path.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(root/name,path)
            (target/'teams_transcription/qa_probe.py').write_bytes(semantic_base_bytes())
            result=check(target,pin)
            self.assertEqual(result['criterion_version'],CRITERION_VERSION)
            self.assertFalse(result['runtime_authorized'])
            self.assertFalse(result['model_started'])
            self.assertFalse(result['legacy_candidates_compatible'])
            (target/'teams_transcription/qa_content.py').write_bytes(b'raise AssertionError("must not execute")')
            with self.assertRaisesRegex(ValueError,'semantic_candidate_source_rejected'):
                check(target,pin)
