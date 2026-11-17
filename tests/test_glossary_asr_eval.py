"""Explicitly synthetic identities; never a user's glossary or real ASR run."""
import hashlib
import json
import copy
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest

from tools.glossary_asr_eval import evaluate_manifest


class GlossaryAsrEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def file(self, name, text):
        path = self.root / name
        raw = text.encode('utf-8')
        path.write_bytes(raw)
        path.chmod(0o600)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def manifest(self, reference, without, with_glossary, annotations):
        audio = self.file('synthetic-audio.bin', 'SYNTHETIC_NOT_ACTUAL_AUDIO')
        glossary = self.file('synthetic-glossary.json', '{"synthetic_term":"星砂協定"}')
        def output(name, text):
            return {**self.file(name, text), 'audio_sha256': audio['sha256'],
                'delivery_verified': True, 'model_error': False,
                'representation': 'provider_original', 'provider': 'synthetic-fixture',
                'model': 'synthetic-not-a-real-model', 'glossary_applied': name == 'with.txt',
                **({'glossary_sha256': glossary['sha256'], 'glossary_source_version': 'synthetic-v1'}
                    if name == 'with.txt' else {})}
        return {'schema_version': 1, 'normalization': 'nfkc-v1',
            'dataset_kind': 'synthetic', 'audio': audio,
            'glossary': {**glossary, 'source_version': 'synthetic-v1'},
            'cases': [{'truth_ref': {**self.file('reference.txt', reference),
                'human_reviewed': True}, 'annotations': annotations,
                'without_glossary': output('without.txt', without),
                'with_glossary': output('with.txt', with_glossary)}]}

    def test_same_audio_original_outputs_yield_separate_counts_and_cer(self):
        reference = '星砂協定已通過。'
        value = self.manifest(reference, '星沙協定已通過。', reference,
            [{'category': 'term', 'start': 0, 'end': 4}])
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['status'], 'scored')
        pair = report['cases'][0]['outputs']
        self.assertEqual(pair['without_glossary']['counts']['wrong'], 1)
        self.assertEqual(pair['without_glossary']['cer']['errors'], 1)
        self.assertEqual(pair['with_glossary']['counts']['hit'], 1)
        self.assertEqual(pair['with_glossary']['cer']['errors'], 0)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('星砂', serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn('pass', serialized)

    def test_ambiguous_homophone_never_uses_a_canonical_spelling_as_reference(self):
        # Deliberately beyond the alignment budget: ambiguous spelling should
        # not be aligned/scored in the first place.
        reference = '林零' * 1500
        value = self.manifest(reference, '林靈' * 1500, reference,
            [{'category': 'homophone_ambiguous', 'start': 0, 'end': len(reference)}])
        report = evaluate_manifest(value, base_dir=self.root)
        for output in report['cases'][0]['outputs'].values():
            self.assertEqual(output['status'], 'ambiguous')
            self.assertIsNone(output['cer'])
            self.assertEqual(output['counts']['ambiguous'], 1)
            self.assertEqual(output['counts']['hit'], 0)

    def test_categories_corrections_and_ordinary_negative_are_not_canonicalized(self):
        reference = '星砂API林零budget普通詞十萬更正十二萬'
        parts = [('term', '星砂'), ('abbreviation', 'API'), ('person', '林零'),
            ('mixed_language', 'budget'), ('ordinary_negative', '普通詞'),
            ('self_correction', '十萬更正十二萬')]
        annotations = []
        at = 0
        for category, text in parts:
            annotations.append({'category': category, 'start': at, 'end': at + len(text),
                **({'forbidden_replacements': ['星砂詞']} if category == 'ordinary_negative' else {})})
            at += len(text)
        value = self.manifest(reference, reference,
            '星砂API林零budget星砂詞十二萬', annotations)
        report = evaluate_manifest(value, base_dir=self.root)
        out = report['cases'][0]['outputs']['with_glossary']
        self.assertEqual(out['categories']['ordinary_negative']['ordinary_false_substitutions'], 1)
        self.assertEqual(out['categories']['self_correction']['wrong'], 1)
        self.assertGreater(out['cer']['errors'], 0)
        self.assertEqual((self.root / 'with.txt').read_text(), '星砂API林零budget星砂詞十二萬')

    def test_substring_containment_does_not_hide_extra_information(self):
        value = self.manifest('星砂協定', '星砂協定', '錯誤前綴星砂協定錯誤後綴',
            [{'category': 'term', 'start': 0, 'end': 4}])
        report = evaluate_manifest(value, base_dir=self.root)
        result = report['cases'][0]['outputs']['with_glossary']
        self.assertGreater(result['cer']['errors'], 0)
        self.assertEqual(report['semantic_assessment'], 'not_performed')
        self.assertNotIn('semantic_pass', result)

    def test_nfkc_version_does_not_fold_case_punctuation_script_or_homophones(self):
        reference = 'ＡＰＩ預算林零。'
        value = self.manifest(reference, 'API預算林零。', 'api预算林靈',
            [{'category': 'mixed_language', 'start': 0, 'end': len(reference)}])
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['normalization_version'], 'nfkc-v1')
        pair = report['cases'][0]['outputs']
        self.assertEqual(pair['without_glossary']['cer']['errors'], 0)
        self.assertGreater(pair['with_glossary']['cer']['errors'], 4)

    def test_missing_truth_glossary_and_failed_delivery_are_unknown(self):
        base = self.manifest('林零', '林零', '林零',
            [{'category': 'person', 'start': 0, 'end': 2}])
        for key, value in [('source_version', None), ('sha256', '0' * 64)]:
            candidate = copy.deepcopy(base)
            candidate['glossary'][key] = value
            self.assertEqual(evaluate_manifest(candidate, base_dir=self.root)['status'], 'unknown')
        for key, value in [('delivery_verified', False), ('model_error', True),
                ('audio_sha256', '0' * 64), ('representation', 'corrected_transcript'),
                ('glossary_sha256', '0' * 64)]:
            candidate = copy.deepcopy(base)
            candidate['cases'][0]['with_glossary'][key] = value
            report = evaluate_manifest(candidate, base_dir=self.root)
            self.assertEqual(report['cases'][0]['outputs']['with_glossary']['status'], 'unknown')
            self.assertNotEqual(report['status'], 'scored')
        base['cases'][0]['truth_ref']['human_reviewed'] = False
        self.assertEqual(evaluate_manifest(base, base_dir=self.root)['cases'][0]['status'], 'unknown')

    def test_silence_requires_empty_reference_and_reports_hallucination(self):
        value = self.manifest('', '', '星砂協定', [{'category': 'silence', 'start': 0, 'end': 0}])
        report = evaluate_manifest(value, base_dir=self.root)
        pair = report['cases'][0]['outputs']
        self.assertEqual(pair['without_glossary']['counts']['hit'], 1)
        self.assertEqual(pair['with_glossary']['counts']['wrong'], 1)
        self.assertEqual(pair['with_glossary']['cer']['errors'], 4)
        self.assertIsNone(pair['with_glossary']['cer']['rate'])

    def test_disambiguated_homophone_requires_reviewed_context(self):
        value = self.manifest('主持人林零', '主持人林靈', '主持人林零',
            [{'category': 'homophone_disambiguated', 'start': 3, 'end': 5}])
        before = evaluate_manifest(value, base_dir=self.root)
        self.assertNotEqual(before['status'], 'scored')
        value['cases'][0]['annotations'][0]['context_reviewed'] = True
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['cases'][0]['outputs']['without_glossary']['counts']['wrong'], 1)
        self.assertEqual(report['cases'][0]['outputs']['with_glossary']['counts']['hit'], 1)

    def test_private_raw_hash_and_alignment_resource_bound_fail_closed(self):
        value = self.manifest('字' * 2100, '字' * 2100, '字' * 2100,
            [{'category': 'term', 'start': 0, 'end': 2100}])
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['cases'][0]['outputs']['with_glossary']['reason'], 'alignment_resource_limit')
        (self.root / 'with.txt').chmod(0o644)
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['cases'][0]['outputs']['with_glossary']['reason'], 'file_not_private')

    def test_cli_preserves_raws_and_writes_only_new_private_redacted_report(self):
        value = self.manifest('林零', '', '林零', [{'category': 'person', 'start': 0, 'end': 2}])
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps(value))
        manifest.chmod(0o600)
        target = self.root / 'report.json'
        operation = [sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/glossary_asr_eval.py'),
            '--manifest', str(manifest), '--output', str(target)]
        result = subprocess.run(operation, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'with.txt').read_text(), '林零')
        self.assertNotIn('林零', target.read_text())
        self.assertNotIn(str(self.root), target.read_text())
        self.assertNotEqual(subprocess.run(operation, capture_output=True, timeout=5).returncode, 0)

    def test_deleted_term_is_a_miss_without_replacing_the_provider_original(self):
        value = self.manifest('星砂協定', '', '星砂協定',
            [{'category': 'term', 'start': 0, 'end': 4}])
        pair = evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']
        self.assertEqual(pair['without_glossary']['counts']['miss'], 1)
        self.assertEqual(pair['without_glossary']['cer']['errors'], 4)
        self.assertEqual((self.root / 'without.txt').read_text(), '')

    def test_longer_false_substitution_is_counted_with_exact_outer_context(self):
        value = self.manifest('甲普乙', '甲普乙', '甲星砂乙',
            [{'category': 'ordinary_negative', 'start': 1, 'end': 2,
              'forbidden_replacements': ['星砂']}])
        result = evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']['with_glossary']
        self.assertEqual(result['categories']['ordinary_negative']['ordinary_false_substitutions'], 1)
        self.assertEqual(result['counts']['wrong'], 1)

    def test_tied_alignment_cannot_attribute_forbidden_ordinary_substitution(self):
        value = self.manifest('甲普普', '甲普普', '星普',
            [{'category': 'ordinary_negative', 'start': 1, 'end': 2,
              'forbidden_replacements': ['星']}])
        result = evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']['with_glossary']
        self.assertEqual(result['categories']['ordinary_negative']['ordinary_false_substitutions'], 0)
        self.assertEqual(result['categories']['ordinary_negative']['ordinary_false_substitution_unknown'], 1)
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['cer']['errors'], 2)
        self.assertEqual((self.root / 'with.txt').read_text(), '星普')

    def test_alignment_ties_outside_ordinary_span_do_not_hide_a_definite_substitution(self):
        value = self.manifest('甲普乙丙丙', '甲普乙丙丙', '甲星乙丙',
            [{'category': 'ordinary_negative', 'start': 1, 'end': 2,
              'forbidden_replacements': ['星']}])
        result = evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']['with_glossary']
        self.assertEqual(result['categories']['ordinary_negative']['ordinary_false_substitutions'], 1)
        self.assertEqual(result['counts']['unknown'], 0)
        self.assertEqual(result['cer']['errors'], 2)

    def test_surrogate_version_and_symlink_or_fifo_are_unknown(self):
        value = self.manifest('合成詞', '', '合成詞', [{'category': 'term', 'start': 0, 'end': 3}])
        invalid = copy.deepcopy(value)
        invalid['glossary']['source_version'] = '\ud800'
        self.assertEqual(evaluate_manifest(invalid, base_dir=self.root)['status'], 'unknown')
        raw = self.root / 'with.txt'
        raw.unlink()
        raw.symlink_to(self.root / 'reference.txt')
        self.assertEqual(evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']
            ['with_glossary']['status'], 'unknown')
        raw.unlink()
        os.mkfifo(raw, 0o600)
        result = evaluate_manifest(value, base_dir=self.root)['cases'][0]['outputs']['with_glossary']
        self.assertEqual(result, {'status': 'unknown', 'reason': 'file_not_regular'})

    def test_reference_hash_provider_pair_and_manifest_version_are_bound(self):
        base = self.manifest('林零', '林零', '林零',
            [{'category': 'person', 'start': 0, 'end': 2}])
        candidate = copy.deepcopy(base)
        candidate['cases'][0]['truth_ref']['sha256'] = '0' * 64
        self.assertEqual(evaluate_manifest(candidate, base_dir=self.root)['status'], 'unknown')
        candidate = copy.deepcopy(base)
        candidate['cases'][0]['without_glossary']['model'] = 'another-synthetic-model'
        report = evaluate_manifest(candidate, base_dir=self.root)
        self.assertEqual(report['cases'][0]['reason'], 'paired_provider_mismatch')
        self.assertEqual(evaluate_manifest({**base, 'schema_version': True},
            base_dir=self.root)['reason'], 'manifest_version_invalid')

    def test_annotation_cannot_split_a_normalization_composition(self):
        value = self.manifest('e\u0301', 'é', 'é',
            [{'category': 'term', 'start': 1, 'end': 2}])
        report = evaluate_manifest(value, base_dir=self.root)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(report['cases'][0]['reason'], 'annotation_normalization_boundary')

    def test_known_raw_output_hash_mismatch_is_unknown_not_a_corrected_result(self):
        value = self.manifest('林零', '林零', '林零',
            [{'category': 'person', 'start': 0, 'end': 2}])
        (self.root / 'with.txt').write_text('PRIVATE_CORRECTED_AFTER_CAPTURE')
        result = evaluate_manifest(value, base_dir=self.root)
        output = result['cases'][0]['outputs']['with_glossary']
        self.assertEqual(output, {'status': 'unknown', 'reason': 'file_hash_mismatch'})
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
