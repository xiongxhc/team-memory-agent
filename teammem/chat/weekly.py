"""Bounded week sampling of existing, authorized person evidence."""

import time
from dataclasses import replace
from datetime import datetime, timedelta

from .retrieval import RetrievalTimeoutError, _query


def sample_person_weeks(query, limit, vault_search, record_search):
    """Return samples, or None when ordinary search should handle this query.

    Each callback retains the caller's project policy and authorization. No
    weekly narrative is generated, and missing evidence never implies no work.
    """
    text, start, end, person, _ = _query(query)
    if text.strip() or not person or not start or not end:
        return None
    beginning = datetime.fromisoformat(start.replace('Z', '+00:00'))
    ending = datetime.fromisoformat(end.replace('Z', '+00:00')).astimezone(beginning.tzinfo)
    if not timedelta(days=7) < ending - beginning <= timedelta(days=35):
        return None
    deadline = time.monotonic() + 8
    buckets = []
    cursor = beginning

    def checked_search(search, bounds):
        if time.monotonic() >= deadline:
            raise RetrievalTimeoutError('Weekly evidence search exceeded its time budget.')
        found = search(bounds, min(2, limit))
        if time.monotonic() >= deadline:
            raise RetrievalTimeoutError('Weekly evidence search exceeded its time budget.')
        return found

    while cursor < ending:
        monday = cursor.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=cursor.weekday())
        boundary = min(monday + timedelta(days=7), ending)
        bounds = dict(query, start=cursor.isoformat(), end=boundary.isoformat())
        # Daily prose cannot establish a partial day's activity. Keep only full
        # date-prefix days; exact timestamp filtering remains the raw fallback.
        day_start = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        if cursor != day_start:
            day_start += timedelta(days=1)
        day_end = boundary.replace(hour=0, minute=0, second=0, microsecond=0)
        found = []
        if vault_search is not None and day_start < day_end:
            found = checked_search(vault_search, dict(bounds, start=day_start.isoformat(), end=day_end.isoformat()))
        if not found:
            found = checked_search(record_search, bounds)
        notice = (f'Bounded weekly sample for {cursor.isoformat()} to {boundary.isoformat()} '
                  '(end exclusive); excerpts only, not a complete weekly account. ')
        buckets.append([replace(item, text=notice + item.text,
                                coverage=notice + (item.coverage or '')) for item in found])
        cursor = boundary
    # One sample from every eligible week before adding a second from any week.
    return [bucket[index] for index in range(2) for bucket in buckets if len(bucket) > index][:limit]
