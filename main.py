from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI

OPENF1_BASE_URL = os.getenv("OPENF1_BASE_URL", "https://api.openf1.org/v1")
OPENF1_SESSIONS_ENDPOINT = f"{OPENF1_BASE_URL}/sessions"
REFRESH_ACTIVE_SECONDS = 5
REFRESH_IDLE_SECONDS = 7200


@dataclass
class ScheduleCache:
    year: int | None = None
    fetched_at: datetime | None = None
    sessions: list[dict[str, Any]] | None = None


cache = ScheduleCache()
app = FastAPI(title="F1 KWGT Schedule API", version="1.0.0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_openf1_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_active_session(session: dict[str, Any], now: datetime) -> bool:
    start = parse_openf1_datetime(session.get("date_start"))
    end = parse_openf1_datetime(session.get("date_end"))
    if not start or not end:
        return False
    return start <= now <= end


def infer_refresh_rate(sessions: list[dict[str, Any]], now: datetime) -> int:
    return REFRESH_ACTIVE_SECONDS if any(is_active_session(session, now) for session in sessions) else REFRESH_IDLE_SECONDS


def build_round_map(sessions: list[dict[str, Any]]) -> dict[Any, int]:
    meetings: list[tuple[datetime, Any]] = []
    seen_meetings: set[Any] = set()

    for session in sessions:
        meeting_key = session.get("meeting_key")
        start = parse_openf1_datetime(session.get("date_start"))
        if meeting_key is None or not start or meeting_key in seen_meetings:
            continue
        seen_meetings.add(meeting_key)
        meetings.append((start, meeting_key))

    meetings.sort(key=lambda item: item[0])
    return {meeting_key: index for index, (_, meeting_key) in enumerate(meetings, start=1)}


def flatten_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    round_by_meeting = build_round_map(sessions)
    flattened: list[dict[str, Any]] = []

    for session in sessions:
        meeting_key = session.get("meeting_key")
        flattened.append(
            {
                "name": session.get("session_name") or session.get("session_type"),
                "session_type": session.get("session_type"),
                "date_start": session.get("date_start"),
                "date_end": session.get("date_end"),
                "round": session.get("round_number") or round_by_meeting.get(meeting_key),
                "circuit": session.get("circuit_short_name") or session.get("location"),
                "country": session.get("country_name"),
            }
        )

    flattened.sort(key=lambda item: parse_openf1_datetime(item.get("date_start")) or datetime.max.replace(tzinfo=timezone.utc))
    return flattened


async def fetch_current_year_sessions(year: int) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(OPENF1_SESSIONS_ENDPOINT, params={"year": year})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"OpenF1 request failed with status {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("OpenF1 request failed due to a network error") from exc

    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected OpenF1 sessions response shape")

    return data


async def get_schedule_with_adaptive_cache() -> tuple[list[dict[str, Any]], int, datetime]:
    now = utc_now()
    current_year = now.year

    if cache.year == current_year and cache.fetched_at and cache.sessions is not None:
        cached_refresh_rate = infer_refresh_rate(cache.sessions, now)
        age_seconds = (now - cache.fetched_at).total_seconds()
        if age_seconds < cached_refresh_rate:
            return cache.sessions, cached_refresh_rate, cache.fetched_at

    raw_sessions = await fetch_current_year_sessions(current_year)
    flattened_sessions = flatten_sessions(raw_sessions)
    refresh_rate = infer_refresh_rate(flattened_sessions, now)

    cache.year = current_year
    cache.fetched_at = now
    cache.sessions = flattened_sessions

    return flattened_sessions, refresh_rate, now


@app.get("/")
@app.get("/schedule")
async def get_schedule() -> dict[str, Any]:
    try:
        sessions, refresh_rate, fetched_at = await get_schedule_with_adaptive_cache()
        return {
            "refresh_rate": int(refresh_rate),
            "fetched_at": fetched_at.isoformat(),
            "sessions": sessions,
        }
    except RuntimeError:
        return {
            "refresh_rate": REFRESH_IDLE_SECONDS,
            "fetched_at": utc_now().isoformat(),
            "sessions": [],
            "error": "Failed to fetch OpenF1 sessions",
        }
    except Exception:
        return {
            "refresh_rate": REFRESH_IDLE_SECONDS,
            "fetched_at": utc_now().isoformat(),
            "sessions": [],
            "error": "Unexpected server error while building schedule",
        }
