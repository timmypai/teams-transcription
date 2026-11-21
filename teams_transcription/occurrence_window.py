"""Shared scheduled admission window; owner-immediate keeps its own contract."""
from datetime import datetime

from .teams_acceptance import is_owner_immediate


def in_join_window(occurrence, now):
    if occurrence['state']!='pending':
        return False
    if is_owner_immediate(occurrence):
        return True
    start=datetime.fromisoformat(occurrence['start_at']).timestamp() if occurrence.get('start_at') else None
    end=datetime.fromisoformat(occurrence['end_at']).timestamp() if occurrence.get('end_at') else None
    return (start is not None and end is not None and end>now
        and start-occurrence['join_early_seconds']<=now<=start+occurrence['max_lateness_seconds'])
