"""Same-deployment owner attestation for an existing occurrence, never a calendar grant.

Only the authenticated management action issues a five-minute, boot-bound
request. The target socket consumes it in the actual SQLite claim transaction.
No token, meeting URL, global ledger or external authority is involved.
"""
import json
import re
import secrets
import time

TARGET_CONSENT_TTL = 300
TARGET_CALENDAR_CLAIM_EVENT = 'target_calendar_consent_claimed'


class TargetConsentMixin:
    def issue_target_consent(self, occurrence_id, revision, *, present, media):
        from .assistant_store import AssistantStoreError
        if present is not True or media is not True or type(revision) is not int:
            raise AssistantStoreError('target_owner_confirmation_required')
        with self._locked() as db,db:
            occurrence=self._occurrence(self._row(db,'calendar_occurrences','id',occurrence_id))
            paused=json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
            if (occurrence['revision']!=revision or occurrence['state']!='pending'
                    or occurrence.get('local_preparation') is not None
                    or occurrence.get('target_speaking_revoked') is True
                    or paused.get('scheduler_paused') is not True or self._capture_interlocks_db(db)
                    or (self.root / '.deployment-maintenance').exists()
                    or db.execute('SELECT 1 FROM assistant_runs WHERE occurrence_id=?',(occurrence_id,)).fetchone()
                    or db.execute("SELECT 1 FROM execution_leases WHERE kind='capture' AND state='active'").fetchone()):
                raise AssistantStoreError('target_occurrence_not_authorized')
            calendar_revision = None
            if occurrence['calendar_ref']:
                calendar=self._row(db,'assistant_calendars','id',occurrence['calendar_ref'])
                if json.loads(calendar['value']).get('enabled') is not True:
                    raise AssistantStoreError('target_occurrence_not_authorized')
                from .calendar_client import validate_teams_url
                validate_teams_url(occurrence.get('join_url'))
                calendar_revision = calendar['revision']
            elif not (occurrence.get('recording_authorized') and occurrence.get('speaking_authorized')):
                raise AssistantStoreError('target_occurrence_not_authorized')
            from .occurrence_window import in_join_window
            wall = time.time()
            if not in_join_window(occurrence,wall):
                raise AssistantStoreError('occurrence_outside_join_window')
            boot,now=self._lease_clock()
            consent=dict(request_sha256=secrets.token_hex(32),revision=revision+1,
                boot_id=boot,issued_boottime=now,expires_boottime=now+TARGET_CONSENT_TTL,
                issued_at=wall,expires_at=wall+TARGET_CONSENT_TTL)
            if calendar_revision is not None:
                consent['calendar_revision'] = calendar_revision
            row=db.execute('SELECT value FROM calendar_occurrences WHERE id=?',(occurrence_id,)).fetchone()
            value=json.loads(row[0]);value['target_control_consent']=consent
            db.execute('UPDATE calendar_occurrences SET value=?,revision=revision+1 WHERE id=?',
                (json.dumps(value),occurrence_id))
            self._audit(db,'admin','target_owner_consent',occurrence_id)
            return dict(occurrence_id=occurrence_id,revision=revision+1,
                request_sha256=consent['request_sha256'],expires_in_seconds=TARGET_CONSENT_TTL,
                issued_at=consent['issued_at'],expires_at=consent['expires_at'])

    def validate_target_consent(self, occurrence_id, acceptance):
        with self._locked() as db:
            occurrence=self._occurrence(self._row(db,'calendar_occurrences','id',occurrence_id))
            self._validate_target_consent_db(db,occurrence,acceptance)

    def _validate_target_consent_db(self, db, occurrence, acceptance):
        from .assistant_store import AssistantStoreError
        from .occurrence_window import in_join_window
        value=occurrence.get('target_control_consent')
        boot,now=self._lease_clock();wall=time.time()
        if (not isinstance(value,dict) or not acceptance
                or value.get('request_sha256')!=acceptance['request_sha256']
                or value.get('revision')!=acceptance['occurrence_revision']
                or occurrence['revision']!=acceptance['occurrence_revision']
                or value.get('boot_id')!=boot
                or not value.get('issued_boottime',float('inf'))<=now<value.get('expires_boottime',0)
                or not value.get('issued_at',float('inf'))<=wall<value.get('expires_at',0)
                or occurrence.get('local_preparation') is not None
                or occurrence.get('target_speaking_revoked') is True):
            raise AssistantStoreError('target_fresh_consent_required')
        if not in_join_window(occurrence,wall):
            raise AssistantStoreError('occurrence_outside_join_window')
        if 'calendar_revision' in value:
            if not self._target_calendar_current(db,occurrence,value):
                raise AssistantStoreError('calendar_not_authorized')
            return value
        # Existing manual and pre-upgrade bindings keep their original gates.
        return None

    def _target_calendar_current(self, db, occurrence, consent):
        calendar = self._row(db,'assistant_calendars','id',occurrence['calendar_ref'])
        return (calendar['revision'] == consent['calendar_revision']
            and json.loads(calendar['value']).get('enabled') is True)

    def target_calendar_media(self, lease):
        """Live authority for the exact claimed grant; expiry bounds dispatch, not the run."""
        consent = lease['target_calendar_consent']
        with self._locked() as db:
            self._verify_lease(db,lease,'capture')
            run = self._row(db,'assistant_runs','session_id',lease['session_id'])
            occurrence = self._occurrence(self._row(db,'calendar_occurrences','id',run['occurrence_id']))
            # An unchanged reread preserves the consent; cancellation or an edit
            # drops it. Never fall back to broad Calendar media flags for this run.
            recording = (run['state']=='running' and occurrence['state']=='running'
                and occurrence.get('target_control_consent') == consent
                and self._target_calendar_current(db,occurrence,consent))
            return recording, recording and occurrence.get('target_speaking_revoked') is not True

    def revoke_target_output(self, session_id, request_sha):
        """Persist denial before output-disable; never permits grant-speaking."""
        from .assistant_store import AssistantStoreError
        from .teams_acceptance import policy_from_db
        if not isinstance(request_sha,str) or not re.fullmatch('[0-9a-f]{64}',request_sha):
            raise AssistantStoreError('target_binding_required')
        with self._locked() as db,db:
            run=self._row(db,'assistant_runs','session_id',session_id)
            policy=policy_from_db(db,session_id)
            if not policy or policy['request_sha256']!=request_sha:
                raise AssistantStoreError('target_binding_required')
            occurrence=self._row(db,'calendar_occurrences','id',run['occurrence_id'])
            value=json.loads(occurrence['value'])
            if value.get('target_speaking_revoked') is not True:
                value.update(speaking_authorized=False,target_speaking_revoked=True)
                db.execute('UPDATE calendar_occurrences SET value=?,revision=revision+1 WHERE id=?',
                    (json.dumps(value),run['occurrence_id']))
                self._audit(db,'local-teams-control','target_speaking_revoked',session_id)
