#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
events.py — detection event log, snapshots, and webhooks.

One row per *track*, not per frame. A cat that sits in front of the camera for
two minutes is one event with a duration, not 3,600 rows.

All database writes go through a single background thread. SQLite connections
aren't safe to share across threads, and the capture loop must never block on
disk I/O — a stalled write would drop frames.
"""

import os
import csv
import json
import time
import queue
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL,
    class       TEXT    NOT NULL,
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    duration_s  REAL,
    max_conf    REAL,
    frames      INTEGER,
    snapshot    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_started ON events(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_class   ON events(class, started_at DESC);
"""


class EventLog:
    """Append-only-ish event store with a background writer thread."""

    def __init__(self, db_path, retention_days=30, enabled=True):
        self.db_path = db_path
        self.retention_days = retention_days
        self.enabled = enabled
        self._q = queue.Queue(maxsize=1000)
        self._stop = threading.Event()
        self._thread = None
        self._id_map = {}            # track_id -> db row id
        self._id_map_lock = threading.Lock()
        self._dropped = 0

        if self.enabled:
            self._init_db()
            self._thread = threading.Thread(
                target=self._writer, daemon=True, name="eventlog")
            self._thread.start()

    # ---------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers (the /events endpoint) from blocking the writer.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------

    def _writer(self):
        conn = self._connect()
        last_prune = 0.0
        try:
            while not self._stop.is_set():
                try:
                    job = self._q.get(timeout=1.0)
                except queue.Empty:
                    job = None

                if job is not None:
                    try:
                        self._apply(conn, job)
                        conn.commit()
                    except Exception as e:
                        print(f"[events] write failed: {e}")

                now = time.time()
                if now - last_prune > 3600:
                    last_prune = now
                    self._prune(conn)
        finally:
            try:
                conn.commit()
            except Exception:
                pass
            conn.close()

    def _apply(self, conn, job):
        kind = job[0]

        if kind == "start":
            _, track_id, class_name, started_at, conf = job
            cur = conn.execute(
                "INSERT INTO events (track_id, class, started_at, max_conf, frames) "
                "VALUES (?, ?, ?, ?, ?)",
                (track_id, class_name, started_at, conf, 1),
            )
            with self._id_map_lock:
                self._id_map[track_id] = cur.lastrowid

        elif kind == "end":
            _, track_id, ended_at, duration, max_conf, frames = job
            with self._id_map_lock:
                row_id = self._id_map.pop(track_id, None)
            if row_id is None:
                return
            conn.execute(
                "UPDATE events SET ended_at=?, duration_s=?, max_conf=?, frames=? "
                "WHERE id=?",
                (ended_at, duration, max_conf, frames, row_id),
            )

        elif kind == "snapshot":
            _, track_id, path = job
            with self._id_map_lock:
                row_id = self._id_map.get(track_id)
            if row_id is None:
                return
            conn.execute("UPDATE events SET snapshot=? WHERE id=?", (path, row_id))

    def _prune(self, conn):
        if not self.retention_days:
            return
        cutoff = time.time() - self.retention_days * 86400
        try:
            cur = conn.execute("DELETE FROM events WHERE started_at < ?", (cutoff,))
            if cur.rowcount:
                conn.commit()
                print(f"[events] pruned {cur.rowcount} rows older than "
                      f"{self.retention_days}d")
        except Exception as e:
            print(f"[events] prune failed: {e}")

    # ---------------------------------------------------------

    def _submit(self, job):
        if not self.enabled:
            return
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                print(f"[events] queue full — dropped {self._dropped} events")

    def track_started(self, track):
        self._submit(("start", track.id, track.class_name,
                      track.first_seen, track.max_conf))

    def track_ended(self, track):
        self._submit(("end", track.id, track.last_seen, track.duration,
                      track.max_conf, track.hits))

    def attach_snapshot(self, track_id, path):
        self._submit(("snapshot", track_id, os.path.basename(path)))

    # ---------------------------------------------------------

    def recent(self, limit=50, class_name=None, since=None):
        if not self.enabled:
            return []
        conn = self._connect()
        try:
            sql = "SELECT * FROM events"
            where, args = [], []
            if class_name:
                where.append("class = ?")
                args.append(class_name)
            if since:
                where.append("started_at >= ?")
                args.append(float(since))
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started_at DESC LIMIT ?"
            args.append(int(limit))

            rows = conn.execute(sql, args).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(r):
        d = dict(r)
        d["started_iso"] = datetime.fromtimestamp(d["started_at"]).isoformat(timespec="seconds")
        if d.get("ended_at"):
            d["ended_iso"] = datetime.fromtimestamp(d["ended_at"]).isoformat(timespec="seconds")
        if d.get("duration_s") is not None:
            d["duration_s"] = round(d["duration_s"], 1)
        if d.get("max_conf") is not None:
            d["max_conf"] = round(d["max_conf"], 3)
        return d

    def summary(self, since=None):
        """Per-class totals — how many visits, total time in frame."""
        if not self.enabled:
            return {}
        conn = self._connect()
        try:
            sql = ("SELECT class, COUNT(*) AS events, "
                   "COALESCE(SUM(duration_s), 0) AS total_s, "
                   "COALESCE(MAX(max_conf), 0) AS peak_conf, "
                   "MAX(started_at) AS last_seen "
                   "FROM events")
            args = []
            if since:
                sql += " WHERE started_at >= ?"
                args.append(float(since))
            sql += " GROUP BY class ORDER BY events DESC"

            out = {}
            for r in conn.execute(sql, args).fetchall():
                out[r["class"]] = {
                    "events": r["events"],
                    "total_seconds": round(r["total_s"], 1),
                    "peak_conf": round(r["peak_conf"], 3),
                    "last_seen_iso": datetime.fromtimestamp(
                        r["last_seen"]).isoformat(timespec="seconds"),
                }
            return out
        finally:
            conn.close()

    def write_csv(self, fh, limit=10000):
        rows = self.recent(limit=limit)
        writer = csv.writer(fh)
        writer.writerow(["id", "track_id", "class", "started", "ended",
                         "duration_s", "max_conf", "frames", "snapshot"])
        for r in rows:
            writer.writerow([
                r["id"], r["track_id"], r["class"],
                r.get("started_iso", ""), r.get("ended_iso", ""),
                r.get("duration_s", ""), r.get("max_conf", ""),
                r.get("frames", ""), r.get("snapshot", "") or "",
            ])

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# ============================================================
# SNAPSHOTS
# ============================================================
class SnapshotStore:
    """Saves an annotated JPEG when something new shows up.

    Keeps at most `max_files`, deleting oldest first, so an unattended Pi can't
    fill its SD card over a long weekend.
    """

    def __init__(self, directory, max_files=500, cooldown_s=30.0, enabled=True):
        self.dir = directory
        self.max_files = max_files
        self.cooldown_s = cooldown_s
        self.enabled = enabled
        self._last = {}                 # class name -> last save time
        self._lock = threading.Lock()
        if self.enabled:
            os.makedirs(self.dir, exist_ok=True)

    def maybe_save(self, class_name, track_id, jpeg_bytes):
        """Returns the saved path, or None if skipped."""
        if not self.enabled or not jpeg_bytes:
            return None

        now = time.time()
        with self._lock:
            if now - self._last.get(class_name, 0) < self.cooldown_s:
                return None
            self._last[class_name] = now

        stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() else "_" for c in class_name)
        name = f"{stamp}_{safe}_{track_id}.jpg"
        path = os.path.join(self.dir, name)

        try:
            with open(path, "wb") as fh:
                fh.write(jpeg_bytes)
        except OSError as e:
            print(f"[snapshot] save failed: {e}")
            return None

        self._prune()
        return path

    def _prune(self):
        try:
            files = sorted(
                (os.path.join(self.dir, f) for f in os.listdir(self.dir)
                 if f.endswith(".jpg")),
                key=os.path.getmtime,
            )
            for old in files[:-self.max_files]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except OSError:
            pass

    def list_recent(self, limit=50):
        if not self.enabled or not os.path.isdir(self.dir):
            return []
        try:
            files = sorted(
                (f for f in os.listdir(self.dir) if f.endswith(".jpg")),
                key=lambda f: os.path.getmtime(os.path.join(self.dir, f)),
                reverse=True,
            )
            return files[:limit]
        except OSError:
            return []


# ============================================================
# WEBHOOKS
# ============================================================
class Webhook:
    """Fire-and-forget POST when a new object is confirmed.

    Runs on its own thread with a bounded queue — a slow or dead endpoint must
    not be able to stall the video pipeline.
    """

    def __init__(self, url, timeout=5.0, enabled=True):
        self.url = url
        self.timeout = timeout
        self.enabled = bool(url) and enabled
        self._q = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self.sent = 0
        self.failed = 0

        if self.enabled:
            threading.Thread(target=self._worker, daemon=True,
                             name="webhook").start()
            print(f"[webhook] enabled -> {self.url}")

    def _worker(self):
        while not self._stop.is_set():
            try:
                payload = self._q.get(timeout=1.0)
            except queue.Empty:
                continue

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.url, data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "hailo-tracker/1.0"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    self.sent += 1
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.failed += 1
                if self.failed % 10 == 1:
                    print(f"[webhook] POST failed ({self.failed} total): {e}")

    def notify(self, track, extra=None):
        if not self.enabled:
            return
        payload = {
            "event": "detection",
            "timestamp": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "track": track.to_dict(),
        }
        if extra:
            payload.update(extra)
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            self.failed += 1

    def close(self):
        self._stop.set()
