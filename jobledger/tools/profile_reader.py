"""Tool D - Profile and Preference Reader.

Every read is scoped by profile_id; nothing here can return another profile's rows.
CVs and preferences are versioned (cv_versions rows are never overwritten; every
preference change is a preferences_history row with old/new value, reason, timestamp).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from jobledger.db import queries as q


@dataclass
class CVInfo:
    id: int
    version_label: str
    skills: list
    experience_years: Optional[float]
    education: Optional[str]
    content_text: str


@dataclass
class FullProfile:
    id: int
    name: str
    email: str
    location: Optional[str]
    work_authorization: Optional[str]
    preferred_arrangements: list
    preferred_types: list
    excluded_companies: list
    excluded_keywords: list
    min_salary: Optional[float]
    currency: Optional[str]
    cv: Optional[CVInfo]
    prefs_version: int = 0
    notes: Optional[str] = None

    def preference_snapshot(self) -> dict:
        """Everything an evaluation depends on from the profile (used for input hashing)."""
        return {
            "location": (self.location or "").lower(), "work_authorization": (self.work_authorization or "").lower(),
            "arrangements": sorted(self.preferred_arrangements), "types": sorted(self.preferred_types),
            "excluded_companies": sorted(self.excluded_companies), "excluded_keywords": sorted(self.excluded_keywords),
            "min_salary": self.min_salary, "currency": (self.currency or "").upper(),
        }


def split_list(value: Optional[str]) -> list:
    return [x.strip().lower() for x in (value or "").split(",") if x.strip()]


def read_full_profile(conn: sqlite3.Connection, profile_id: int) -> Optional[FullProfile]:
    row = q.get_profile(conn, profile_id)
    if row is None:
        return None
    cv_row = q.get_active_cv(conn, profile_id)
    cv = None
    if cv_row is not None:
        cv = CVInfo(cv_row["id"], cv_row["version_label"],
                    [s.strip() for s in (cv_row["skills"] or "").split(",") if s.strip()],
                    cv_row["experience_years"], cv_row["education"], cv_row["content_text"] or "")
    return FullProfile(
        id=row["id"], name=row["name"], email=row["email"], location=row["location"],
        work_authorization=row["work_authorization"],
        preferred_arrangements=split_list(row["preferred_arrangements"]),
        preferred_types=split_list(row["preferred_types"]),
        excluded_companies=split_list(row["excluded_companies"]),
        excluded_keywords=split_list(row["excluded_keywords"]),
        min_salary=row["min_salary"], currency=row["currency"], cv=cv,
        prefs_version=q.prefs_version(conn, profile_id), notes=row["notes"])


def list_all_profiles(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in q.list_profiles(conn)]


def read_preferences_history(conn: sqlite3.Connection, profile_id: int, limit: int = 100) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM preferences_history WHERE profile_id = ? ORDER BY id DESC LIMIT ?",
        (profile_id, limit)).fetchall()]
