"""
Tool I: Approval Manager
CLI approval gate for consequential actions.
The system NEVER auto-applies for jobs or sends emails automatically.
All approvals are recorded with timestamps.
"""
from __future__ import annotations

import sys
from typing import Optional
import sqlite3

from jobledger.db import queries as q
from jobledger.logging_setup import get_logger

logger = get_logger("approval_manager")

AUTO_APPROVE_TYPES = {"export_excel", "list_jobs", "show_job", "view_profile"}


class ApprovalGate:
    """Manages approval requests and decisions."""

    def __init__(self, conn: sqlite3.Connection, interactive: bool = True):
        self.conn = conn
        self.interactive = interactive

    def request_approval(
        self,
        action_type: str,
        description: str,
        evidence: str = "",
        run_id: str = None,
        profile_id: int = None,
        job_id: int = None,
        auto_approve_types: list = None,
    ) -> tuple[int, str]:
        """
        Create approval request. Returns (approval_id, status).
        Status: approved, rejected, pending.
        """
        effective_auto = auto_approve_types or list(AUTO_APPROVE_TYPES)

        # Auto-approve safe read operations
        if action_type in effective_auto:
            approval_id = q.create_approval_request(
                self.conn, action_type, description, run_id=run_id,
                profile_id=profile_id, job_id=job_id, evidence=evidence,
            )
            q.update_approval_status(self.conn, approval_id, "approved", decided_by="system")
            logger.debug(f"Auto-approved: {action_type}")
            return approval_id, "approved"

        # Record in DB first
        approval_id = q.create_approval_request(
            self.conn, action_type, description, run_id=run_id,
            profile_id=profile_id, job_id=job_id, evidence=evidence,
        )

        if not self.interactive:
            logger.info(f"Approval request {approval_id} pending (non-interactive): {action_type}")
            return approval_id, "pending"

        # Interactive prompt
        print("\n" + "=" * 60)
        print(f"⚠  APPROVAL REQUIRED")
        print(f"   Action: {action_type}")
        print(f"   {description}")
        if evidence:
            print(f"   Evidence: {evidence[:300]}")
        print("=" * 60)

        while True:
            try:
                resp = input("  Approve? [y=yes / n=no / s=skip for now]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                resp = "n"

            if resp in ("y", "yes"):
                q.update_approval_status(self.conn, approval_id, "approved", decided_by="user")
                logger.info(f"Approval {approval_id} APPROVED by user: {action_type}")
                return approval_id, "approved"
            elif resp in ("n", "no", ""):
                q.update_approval_status(self.conn, approval_id, "rejected", decided_by="user")
                logger.info(f"Approval {approval_id} REJECTED by user: {action_type}")
                return approval_id, "rejected"
            elif resp in ("s", "skip"):
                logger.info(f"Approval {approval_id} skipped (pending): {action_type}")
                return approval_id, "pending"
            else:
                print("  Please enter y, n, or s")

    def show_pending_approvals(self, profile_id: int = None) -> None:
        """Print all pending approval requests."""
        pending = q.get_pending_approvals(self.conn, profile_id)
        if not pending:
            print("No pending approvals.")
            return
        print(f"\n{'ID':<5} {'Action':<20} {'Description':<40} {'Created':<20}")
        print("-" * 90)
        for ap in pending:
            print(
                f"{ap['id']:<5} {ap['action_type']:<20} "
                f"{(ap['description'] or '')[:40]:<40} {(ap['created_at'] or '')[:19]}"
            )

    def decide_approval(
        self, approval_id: int, decision: str, decided_by: str = "user"
    ) -> bool:
        """Decide on a pending approval. decision: 'approved' or 'rejected'."""
        if decision not in ("approved", "rejected"):
            logger.error(f"Invalid decision '{decision}' for approval {approval_id}")
            return False
        q.update_approval_status(self.conn, approval_id, decision, decided_by=decided_by)
        logger.info(f"Approval {approval_id} -> {decision} by {decided_by}")
        return True

    def list_pending(self, profile_id: int = None) -> list:
        """Return list of pending approval request dicts."""
        return q.get_pending_approvals(self.conn, profile_id)
