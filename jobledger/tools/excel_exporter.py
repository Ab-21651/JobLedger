"""Tool H - Excel Register. A VIEW generated from SQLite; nothing is ever read back from it.

Safety: rebuilt from the DB every time (repeat exports cannot duplicate rows); written to a
temp file then os.replace()d; if the destination is locked (open in Excel on Windows) the
export lands in a timestamped sibling file and says so; spreadsheet formula injection is
neutralised; invalid URLs are shown as text, never as links.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from jobledger.db import queries as q
from jobledger.logging_setup import get_logger
from jobledger.timeutil import now_iso
from jobledger.tools.stage_manager import check_follow_up_eligibility, recommended_next_action

logger = get_logger("excel")

COLUMNS = [
    ("Rank", 6), ("Score", 8), ("Status", 12), ("Title", 34), ("Company", 24), ("Location", 20),
    ("Arrangement", 12), ("Type", 11), ("Salary", 18), ("Skill", 7), ("Exp", 7), ("Arr", 7), ("Type fit", 8),
    ("Loc", 7), ("Match rationale", 70), ("Missing info", 34), ("JD status", 14), ("Stage", 12),
    ("Applied", 12), ("Last update", 18), ("Flags", 34), ("Recommended next action", 44), ("Apply link", 50),
    ("Eval", 6), ("Scoring mode", 16), ("Job ID", 7),
]
_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")
_ORDER = {"scored": 0, "provisional": 0, "needs_jd": 1, "excluded": 2, None: 3}


def safe_cell(value):
    """Neutralise spreadsheet formula injection in any string that will be written to a cell."""
    if isinstance(value, str) and value.startswith(_FORMULA_CHARS):
        return "'" + value
    return value


def build_rows(conn, profile_id: int, wait_days: int = 7) -> list:
    rows = conn.execute(
        """SELECT j.*, e.id AS ev_id, e.status AS ev_status, e.total_score, e.skill_score, e.experience_score,
                  e.arrangement_score, e.type_score, e.location_score, e.match_rationale, e.missing_info,
                  e.eval_version, e.scoring_mode, e.evaluated_at,
                  a.id AS app_id, a.stage, a.applied_at, a.updated_at AS app_updated
           FROM jobs j
           LEFT JOIN evaluations e ON e.id = (SELECT MAX(id) FROM evaluations WHERE job_id = j.id AND profile_id = j.profile_id)
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.profile_id = ?""", (profile_id,)).fetchall()
    out = []
    for r in rows:
        flags = []
        if r["uncertain_fields"]:
            flags += [f"open_conflict:{f}" for f in json.loads(r["uncertain_fields"])]
        snap = q.get_latest_jd_snapshot(conn, r["id"])
        if snap is not None and snap["injection_flags"]:
            flags.append("injection_text_in_jd")
        email_flagged = conn.execute("SELECT 1 FROM action_logs WHERE job_id=? AND action_type='injection_detected' LIMIT 1", (r["id"],)).fetchone()
        if email_flagged:
            flags.append("injection_text_in_email")
        fu = check_follow_up_eligibility(conn, r["app_id"], wait_days=wait_days, persist=False) if r["app_id"] else None
        nxt = recommended_next_action(r["stage"], r["ev_status"], r["jd_status"], fu, flags)
        out.append({"row": r, "flags": flags, "next": nxt, "snap": snap})
    out.sort(key=lambda d: (_ORDER.get(d["row"]["ev_status"], 3), -(d["row"]["total_score"] or 0), d["row"]["id"]))
    return out


def export_excel(conn, profile_id: int, export_dir: str = "exports", filename: str = "jobledger_register.xlsx",
                 wait_days: int = 7) -> dict:
    """Write the register. Returns {path, rows, locked_fallback, warnings}."""
    prof = q.get_profile(conn, profile_id)
    if prof is None:
        raise q.ProfileIsolationError(f"profile {profile_id} not found")
    data = build_rows(conn, profile_id, wait_days)
    wb = Workbook()
    ws = wb.active
    ws.title = "Register"
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    for c, (name, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.freeze_panes = "E2"
    warnings: list = []
    rank = 0
    for i, d in enumerate(data, 2):
        r = d["row"]
        ranked = r["ev_status"] in ("scored", "provisional")
        rank += 1 if ranked else 0
        url = r["apply_url"]
        url_ok = bool(q.canonicalize_url(url))
        if url and not url_ok:
            warnings.append(f"job {r['id']}: invalid apply URL not linked")
        salary = r["salary_raw"] or (f"{r['salary_min']:g}-{r['salary_max']:g}" if r["salary_min"] and r["salary_max"] else None)
        missing = ", ".join(json.loads(r["missing_info"])) if r["missing_info"] else None
        vals = [rank if ranked else None, r["total_score"], (r["ev_status"] or "not evaluated").upper(),
                r["title"], r["company"], r["location"], r["arrangement"], r["employment_type"], salary,
                r["skill_score"], r["experience_score"], r["arrangement_score"], r["type_score"], r["location_score"],
                r["match_rationale"], missing, r["jd_status"], r["stage"] or "-", (r["applied_at"] or "")[:10] or None,
                r["app_updated"] or r["updated_at"], ", ".join(d["flags"]) or None, d["next"],
                (url if url_ok else (f"[invalid] {url}" if url else None)), r["eval_version"], r["scoring_mode"], r["id"]]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=safe_cell(v))
            if c in (15, 16, 21, 22):
                cell.alignment = Alignment(wrap_text=True, vertical="top")
            if c in (10, 11, 12, 13, 14) and isinstance(v, (int, float)):
                cell.number_format = "0.00"
        if url_ok:
            link_cell = ws.cell(row=i, column=23)
            link_cell.hyperlink = url
            link_cell.font = Font(color="0563C1", underline="single")
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(len(data) + 1, 1)}"

    cs = wb.create_sheet("Conflicts")
    for c, name in enumerate(["Job ID", "Title", "Field", "Value A", "Source A", "Value B", "Source B", "Detected", "Status"], 1):
        cs.cell(row=1, column=c, value=name).font = Font(bold=True)
    n = 2
    for d in data:
        for cf in q.list_job_conflicts(conn, d["row"]["id"], only_open=False):
            for c, v in enumerate([cf["job_id"], d["row"]["title"], cf["field_name"], cf["value_a"], cf["source_a"],
                                   cf["value_b"], cf["source_b"], cf["detected_at"], cf["status"]], 1):
                cs.cell(row=n, column=c, value=safe_cell(v))
            n += 1

    lg = wb.create_sheet("About")
    for line in ["JobLedger register - a generated VIEW of the SQLite database (the source of truth).",
                 "Edits made here are NOT imported back. Re-run `export` to regenerate.",
                 f"Profile: {prof['name']} (id {profile_id})   Generated: {now_iso()}",
                 "Score is 0-100, computed in Python from documented weights. PROVISIONAL = material unknowns. "
                 "NEEDS_JD = no verified job description (not a rejection). EXCLUDED = a confirmed hard filter.",
                 "Unknown component scores count as 0.5 (neutral) and are listed under 'Missing info'."]:
        lg.append([safe_cell(line)])
    lg.column_dimensions["A"].width = 120

    out_dir = Path(export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / filename
    tmp = out_dir / f".{filename}.{os.getpid()}.tmp"
    fallback = False
    try:
        wb.save(tmp)
        try:
            os.replace(tmp, dest)
        except PermissionError:
            alt = out_dir / f"{dest.stem}_{now_iso().replace(':', '').replace('-', '')}{dest.suffix}"
            os.replace(tmp, alt)
            dest, fallback = alt, True
            warnings.append(f"{filename} is locked (open in Excel?); wrote {alt.name} instead")
            logger.warning(warnings[-1])
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return {"path": str(dest), "rows": len(data), "locked_fallback": fallback, "warnings": warnings}
