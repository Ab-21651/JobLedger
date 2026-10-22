"""
Tool D: Profile and Preference Reader
Reads active candidate profile, CV version, and preferences from SQLite.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from jobledger.db import queries as q


@dataclass
class CandidateProfile:
    profile_id: int
    name: str
    email: str
    location: Optional[str]
    work_authorization: Optional[str]
    preferred_arrangements: list[str]
    preferred_types: list[str]
    excluded_companies: list[str]
    excluded_keywords: list[str]
    min_salary: Optional[float]
    currency: str
    notes: Optional[str]


@dataclass
class CVInfo:
    cv_id: int
    version_label: str
    content_text: str
    skills: list[str]
    experience_years: Optional[float]
    education: Optional[str]


@dataclass
class FullProfile:
    profile: CandidateProfile
    cv: Optional[CVInfo]
    preferences_history: list[dict]


def _split_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [s.strip() for s in value.split(",") if s.strip()]


def read_full_profile(
    conn: sqlite3.Connection, profile_id: int
) -> Optional[FullProfile]:
    """Read profile + active CV + last 20 preference changes."""
    row = q.get_profile(conn, profile_id)
    if row is None:
        return None

    profile = CandidateProfile(
        profile_id=row["id"],
        name=row["name"],
        email=row["email"],
        location=row["location"],
        work_authorization=row["work_authorization"],
        preferred_arrangements=_split_csv(row["preferred_arrangements"]),
        preferred_types=_split_csv(row["preferred_types"]),
        excluded_companies=_split_csv(row["excluded_companies"]),
        excluded_keywords=_split_csv(row["excluded_keywords"]),
        min_salary=row["min_salary"],
        currency=row["currency"] or "USD",
        notes=row["notes"],
    )

    cv_row = q.get_active_cv(conn, profile_id)
    cv = None
    if cv_row:
        cv = CVInfo(
            cv_id=cv_row["id"],
            version_label=cv_row["version_label"],
            content_text=cv_row["content_text"],
            skills=_split_csv(cv_row["skills"]),
            experience_years=cv_row["experience_years"],
            education=cv_row["education"],
        )

    pref_rows = conn.execute(
        "SELECT * FROM preferences_history WHERE profile_id=? ORDER BY changed_at DESC LIMIT 20",
        (profile_id,),
    ).fetchall()
    pref_history = [dict(r) for r in pref_rows]

    return FullProfile(profile=profile, cv=cv, preferences_history=pref_history)


def list_all_profiles(conn: sqlite3.Connection) -> list[dict]:
    """List all active profiles with id, name, email."""
    rows = q.list_profiles(conn)
    return [{"id": r["id"], "name": r["name"], "email": r["email"]} for r in rows]


def read_preferences_history(
    conn: sqlite3.Connection, profile_id: int, limit: int = 50
) -> list[dict]:
    """Return preference change history ordered by changed_at DESC."""
    rows = conn.execute(
        "SELECT * FROM preferences_history WHERE profile_id=? ORDER BY changed_at DESC LIMIT ?",
        (profile_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
