"""Revision-aware grace window for marks made in upcoming-order panels."""

from datetime import datetime
import time


class OrderMarkState:
    def __init__(self, ttl_sec: float):
        self.ttl_sec = ttl_sec
        self.pending = {}
        self._latest_rows = {}
        self._clock = time.monotonic

    @staticmethod
    def _revision(row):
        value = row.get("expected_revision", row.get("version"))
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def clear(self):
        self.pending.clear()
        self._latest_rows.clear()

    def begin(self, admin_id: int, mark: str, row=None):
        admin_id = int(admin_id)
        row = dict(row or {})
        revision = self._revision(row) if admin_id > 0 else None
        self.pending[admin_id] = {
            "mark": str(mark or ""),
            "started_mono": self._clock(),
            "actual_time": datetime.now().isoformat(),
            "expected_revision": revision + 1 if revision is not None else None,
            "saved": False,
        }
        if revision is not None:
            self._latest_rows[admin_id] = row

    def get(self, admin_id):
        if admin_id is None:
            return None
        admin_id = int(admin_id)
        pending = self.pending.get(admin_id)
        if pending and pending["saved"] and self._clock() - pending["started_mono"] >= self.ttl_sec:
            self.pending.pop(admin_id, None)
            return None
        return pending

    def confirm(self, admin_id: int):
        admin_id = int(admin_id)
        pending = self.pending.get(admin_id)
        if pending is None:
            return
        pending["saved"] = True
        pending["started_mono"] = self._clock()
        revision = pending["expected_revision"]
        if revision is None:
            return
        latest = self._latest_rows[admin_id]
        if self._revision(latest) > revision:
            self.pending.pop(admin_id, None)
            return
        if self._revision(latest) < revision:
            # The version-checked UPDATE succeeded and increments the revision
            # exactly once. Retain that confirmed mark even if a read started
            # before the write completes after the grace window has expired.
            latest = dict(latest, comment=pending["mark"], actual_time=pending["actual_time"])
            for key in ("version", "expected_revision"):
                if key in latest:
                    latest[key] = revision
            self._latest_rows[admin_id] = latest

    def reconcile(self, rows):
        """Keep confirmed rows monotonic without storing optimistic overlays."""
        result = []
        for source in rows:
            row = dict(source)
            admin_id = row.get("id")
            latest = self._latest_rows.get(admin_id)
            revision = self._revision(row)
            if latest is not None and revision is not None:
                if revision < self._revision(latest):
                    row = dict(latest)
                    revision = self._revision(row)
                else:
                    self._latest_rows[admin_id] = dict(row)
            pending = self.get(admin_id)
            if (pending and pending["saved"] and revision is not None
                    and pending["expected_revision"] is not None
                    and revision > pending["expected_revision"]):
                # A later committed action supersedes the panel's own mark.
                # Keep the revision watermark to reject older replies as well.
                self.pending.pop(admin_id, None)
            result.append(row)
        return result

    def overlay(self, rows):
        for admin_id in list(self.pending):
            self.get(admin_id)
        result = []
        for row in rows:
            pending = self.get(row.get("id"))
            if pending:
                row = dict(row, comment=pending["mark"],
                           actual_time=row.get("actual_time") or pending["actual_time"])
            result.append(row)
        return result

    def next_expiry_ms(self):
        deadlines = [
            pending["started_mono"] + self.ttl_sec
            for pending in self.pending.values() if pending["saved"]
        ]
        if not deadlines:
            return None
        remaining = min(deadlines) - self._clock()
        return max(1, int(remaining * 1000) + 1)
