from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

OPENF1_BASE_URL = os.getenv("OPENF1_BASE_URL", "https://api.openf1.org/v1")
REFRESH_ACTIVE_SECONDS = 5
REFRESH_IDLE_SECONDS = 7200


@dataclass
class ScheduleCache:
    year: int | None = None
    fetched_at: datetime | None = None
    sessions: list[dict[str, Any]] | None = None


cache = ScheduleCache()
app = FastAPI(title="F1 KWGT Adaptive Cache API", version="1.0.0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_openf1_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
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
    for session in sessions:
        if is_active_session(session, now):
            return REFRESH_ACTIVE_SECONDS
    return REFRESH_IDLE_SECONDS


async def fetch_current_year_schedule(year: int) -> list[dict[str, Any]]:
    url = f"{OPENF1_BASE_URL}/sessions"
    params = {"year": year}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params)

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"OpenF1 returned {response.status_code} while fetching sessions",
        )

    data = response.json()
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected OpenF1 schedule response")

    return data


async def get_schedule_with_adaptive_cache() -> tuple[list[dict[str, Any]], int, bool]:
    now = utc_now()
    current_year = now.year

    if cache.year == current_year and cache.sessions is not None and cache.fetched_at is not None:
        dynamic_ttl = infer_refresh_rate(cache.sessions, now)
        age = (now - cache.fetched_at).total_seconds()
        if age < dynamic_ttl:
            return cache.sessions, dynamic_ttl, False

    sessions = await fetch_current_year_schedule(current_year)
    cache.year = current_year
    cache.fetched_at = now
    cache.sessions = sessions

    refresh_rate = infer_refresh_rate(sessions, now)
    return sessions, refresh_rate, True


def flatten_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for session in sessions:
        flattened.append(
            {
                "session_key": session.get("session_key"),
                "meeting_key": session.get("meeting_key"),
                "session_name": session.get("session_name"),
                "session_type": session.get("session_type"),
                "country_name": session.get("country_name"),
                "location": session.get("location"),
                "circuit_short_name": session.get("circuit_short_name"),
                "date_start": session.get("date_start"),
                "date_end": session.get("date_end"),
                "gmt_offset": session.get("gmt_offset"),
            }
        )
    return flattened


def find_next_session(sessions: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    for session in sessions:
        start = parse_openf1_datetime(session.get("date_start"))
        if start and start > now:
            upcoming.append((start, session))

    if not upcoming:
        return None

    upcoming.sort(key=lambda item: item[0])
    return upcoming[0][1]


@app.get("/")
@app.get("/schedule")
async def get_schedule() -> dict[str, Any]:
    now = utc_now()
    sessions, refresh_rate, _ = await get_schedule_with_adaptive_cache()
    flattened = flatten_sessions(sessions)

    active_sessions = [session for session in flattened if is_active_session(session, now)]
    next_session = find_next_session(flattened, now)

    return {
        "refresh_rate": refresh_rate,
        "timestamp_utc": now.isoformat(),
        "year": now.year,
        "active": len(active_sessions) > 0,
        "active_session_count": len(active_sessions),
        "active_sessions": active_sessions,
        "next_session": next_session,
        "session_count": len(flattened),
        "sessions": flattened,
    }
