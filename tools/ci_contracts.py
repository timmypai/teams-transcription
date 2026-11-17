"""Credential-free CI subset, with machine-readable counts and real failures.

This deliberately excludes browser/model/meeting probes and the two missing
historical bridge experiments. It is not the full original discovery suite.
"""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'tests'), str(ROOT)]
SUITES = (
    'test_assistant_store', 'test_assistant_daemon', 'test_calendar_client',
    'test_glossary_context', 'test_admin_web', 'test_sqlite_voice_context',
    'test_sqlite_only_store', 'test_b_no_plc_startup', 'test_rx_contract', 'test_prefetch_codex_npm',
    'test_no_plc_setup_failure',
    'test_render_history', 'test_history_output_consumers',
    'test_decoder_continuity_check', 'test_b_no_plc_pacing',
    'test_beta_voice_duration', 'test_glossary_asr_eval',
    'test_glossary_native_pair', 'test_live_cleanup_snapshot',
    'test_question_input_diagnostics',
    'test_response_capture_fanout', 'test_response_policy_failure',
    'test_b_document_policy',
    'test_staging_control', 'test_capture_cleanup_review',
    'test_teams_recovery_reader',
    'test_release_gate', 'test_diagnostic_projection',
    'test_codex_auth', 'test_provider_state', 'test_workflow_triggers',
    'test_pr_policy', 'test_pr_policy_github', 'test_pr_policy_workflow',
    'test_staging_docker_diagnostics',
    'test_staging_compose',
    'test_first_deploy_recovery',
    'test_qa_probe.ProbeTests',
    'test_qa_receiver_projection',
    'test_qa_audio_observer',
    'test_qa_content',
    'test_qa_candidate_versions',
    'test_cloud_qa_semantic.SemanticRuntimeTests',
    'test_cloud_qa_startup.StartupRuntimeTests',
    'test_teams_acceptance', 'test_teams_acceptance_host', 'test_teams_local_control.LocalControlTests',
    'test_teams_target_prepare.TargetPrepareTests',
    'test_teams_preparation_host.HostPreparationTests',
    'test_teams_immediate_recovery.ImmediateRecoveryTests',
    'test_teams_immediate_chain.ImmediateChainTests',
    'test_native_output_attach_diagnostics',
    'test_cloud_qa_contract',
    'test_cloud_qa_bridge.CloudQABridgeTests',
    'test_cloud_qa_isolated.CloudQAIsolatedTests',
    'test_turn_lifecycle_evidence.TurnLifecycleEvidenceTests',
    'test_speech_completion_snapshot.SpeechCompletionSnapshotTests',
    'test_incremental_minutes_context.ContextTests.test_wire_instruction_distinguishes_verbatim_quote_and_normal_answers',
    'test_native_incremental_context.NativeSnapshotContextTests.test_delivery_receipt_hashes_identify_actual_current_payload_without_content',
    'test_b_native.NativeLiveTests.test_live_prompt_distinguishes_verbatim_format_from_normal_current_fact_answers',
    'test_b_native.NativeLiveTests.test_three_physical_questions_in_one_provider_turn_query_current_sqlite_each_time',
)


def main():
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromNames(SUITES)
    with (output / 'contracts.log').open('w') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    receipt = {'scope': 'controlled offline subset', 'suites': list(SUITES),
        'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped),
        'success': result.wasSuccessful(), 'teams_or_model_called': False,
        'audio_e2e_tested': False}
    (output / 'contracts.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f"### Offline contracts\n\nTests: {result.testsRun}; "
                f"failures: {len(result.failures)}; errors: {len(result.errors)}; "
                f"skipped: {len(result.skipped)}.\n\n"
                "Controlled subset only. Real Teams / GPT Live / audio E2E untested.\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
