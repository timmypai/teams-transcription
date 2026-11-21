"""Fixed public local-control rejection vocabulary; no exception text."""
# Fixed public codes only: never project arbitrary exception messages.
SAFE_CONTROL_FAILURE_CODES=frozenset({
    'occurrence_outside_join_window','capture_unavailable','meeting_slot_busy',
    'deployment_maintenance','codex_auth_operation_in_progress','provider_account_not_admissible',
    'teams_owner_stop_policy_required','teams_owner_stop_configuration_required',
    'teams_preparation_acceptance_required','target_fresh_consent_required',
    'target_binding_required','record_not_found','revision_conflict',
    'occurrence_not_pending','occurrence_not_ready','occurrence_already_executed',
    'calendar_not_authorized','recording_authorization_required','speaking_authorization_required',
    'teams_acceptance_revision_changed','teams_acceptance_scheduler_must_pause',
    'teams_acceptance_already_claimed','capture_cleanup_review_required',
    'teams_local_control_rejected','teams_local_control_binding_rejected',
    'teams_local_control_path_rejected','meeting_not_running','meeting_output_unavailable',
    'meeting_stopping','speaking_authorization_revoked'})
