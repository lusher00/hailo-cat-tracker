#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
tracker.py — lightweight multi-object tracker.

Greedy IoU association with constant-velocity prediction. No SciPy, no filterpy,
no extra wheels to build on a Pi — just NumPy, and it costs well under a
millisecond for the handful of objects a home camera actually sees.

Gives each object a stable integer ID that persists across frames, so you can
say "cat #3 was in frame for 90 seconds" instead of "there was a cat in 2,700
separate frames".
"""

import time
import math
from collections import deque


def iou(a, b):
    """Intersection over union of two (x1, y1, x2, y2) boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)

    iw, ih = ix2 - ix1, iy2 - iy1
    if iw <= 0 or ih <= 0:
        return 0.0

    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class Track:
    """One tracked object, alive across frames."""

    __slots__ = (
        "id", "class_id", "class_name", "box", "conf", "max_conf",
        "hits", "misses", "age", "first_seen", "last_seen",
        "trail", "vx", "vy", "confirmed", "event_id",
        "snapshot_taken", "notified",
    )

    def __init__(self, track_id, class_id, class_name, box, conf, trail_len, now=None):
        now = now if now is not None else time.time()
        self.id = track_id
        self.class_id = class_id
        self.class_name = class_name
        self.box = tuple(float(v) for v in box)
        self.conf = float(conf)
        self.max_conf = float(conf)
        self.hits = 1
        self.misses = 0
        self.age = 1
        self.first_seen = now
        self.last_seen = now
        self.trail = deque(maxlen=trail_len)
        self.trail.append(self.centroid)
        self.vx = 0.0
        self.vy = 0.0
        self.confirmed = False
        self.event_id = None
        self.snapshot_taken = False
        self.notified = False

    # ---------------------------------------------------------

    @property
    def centroid(self):
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) * 0.5, (y1 + y2) * 0.5)

    @property
    def width(self):
        return self.box[2] - self.box[0]

    @property
    def height(self):
        return self.box[3] - self.box[1]

    @property
    def area(self):
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def duration(self):
        return self.last_seen - self.first_seen

    @property
    def speed(self):
        """Pixels per frame, magnitude of the smoothed velocity."""
        return math.hypot(self.vx, self.vy)

    # ---------------------------------------------------------

    def update(self, box, conf, now=None):
        now = now if now is not None else time.time()
        px, py = self.centroid
        self.box = tuple(float(v) for v in box)
        cx, cy = self.centroid

        # Exponential smoothing on velocity — raw frame deltas are far too
        # jittery to draw or reason about.
        self.vx = 0.7 * self.vx + 0.3 * (cx - px)
        self.vy = 0.7 * self.vy + 0.3 * (cy - py)

        self.conf = float(conf)
        self.max_conf = max(self.max_conf, float(conf))
        self.hits += 1
        self.age += 1
        self.misses = 0
        self.last_seen = now
        self.trail.append((cx, cy))

    def predict(self):
        """Coast on last known velocity while the detector can't see us."""
        x1, y1, x2, y2 = self.box
        self.box = (x1 + self.vx, y1 + self.vy, x2 + self.vx, y2 + self.vy)
        self.misses += 1
        self.age += 1
        # Bleed velocity off so a lost track doesn't sail across the frame.
        self.vx *= 0.8
        self.vy *= 0.8

    def to_dict(self):
        x1, y1, x2, y2 = (int(v) for v in self.box)
        return {
            "id": self.id,
            "class": self.class_name,
            "class_id": self.class_id,
            "box": [x1, y1, x2, y2],
            "conf": round(self.conf, 3),
            "max_conf": round(self.max_conf, 3),
            "hits": self.hits,
            "misses": self.misses,
            "duration_s": round(self.duration, 1),
            "speed_px": round(self.speed, 1),
            "confirmed": self.confirmed,
        }


class Tracker:
    """Greedy IoU tracker.

    min_hits   — frames an object must be seen before it counts as real. Filters
                 the single-frame false positives YOLO throws on textured
                 backgrounds (rugs and blankets love to be 'cat').
    max_misses — frames to keep coasting a lost object before giving up. At 30fps
                 the default of 15 is half a second, enough to ride out an
                 occlusion behind a chair leg.
    """

    def __init__(self, iou_threshold=0.3, max_misses=15, min_hits=3, trail_len=48):
        self.iou_threshold = iou_threshold
        self.max_misses = max_misses
        self.min_hits = min_hits
        self.trail_len = trail_len
        self.tracks = []
        self._next_id = 1
        self.finished = []          # tracks that ended since the last drain

    # ---------------------------------------------------------

    def update(self, detections, now=None):
        """detections: iterable of (class_id, class_name, box, conf).

        Returns the list of currently live tracks.
        """
        now = now if now is not None else time.time()
        detections = list(detections)

        # Build all plausible (track, detection) pairs, best IoU first. Greedy
        # assignment is O(n*m) but n and m are tiny here, and it behaves far
        # better than nearest-centroid when two objects cross.
        pairs = []
        for ti, track in enumerate(self.tracks):
            for di, (class_id, _name, box, _conf) in enumerate(detections):
                if class_id != track.class_id:
                    continue
                score = iou(track.box, box)
                if score >= self.iou_threshold:
                    pairs.append((score, ti, di))

        pairs.sort(reverse=True)

        used_tracks = set()
        used_dets = set()
        for score, ti, di in pairs:
            if ti in used_tracks or di in used_dets:
                continue
            used_tracks.add(ti)
            used_dets.add(di)
            _cid, _name, box, conf = detections[di]
            self.tracks[ti].update(box, conf, now)

        # Unmatched detections become new tracks
        for di, (class_id, class_name, box, conf) in enumerate(detections):
            if di in used_dets:
                continue
            self.tracks.append(
                Track(self._next_id, class_id, class_name, box, conf,
                      self.trail_len, now)
            )
            self._next_id += 1

        # Unmatched tracks coast, then die
        survivors = []
        for ti, track in enumerate(self.tracks):
            if ti not in used_tracks and track.hits > 0 and track.last_seen != now:
                track.predict()

            if track.misses > self.max_misses:
                if track.confirmed:
                    self.finished.append(track)
                continue

            if not track.confirmed and track.hits >= self.min_hits:
                track.confirmed = True

            survivors.append(track)

        self.tracks = survivors
        return self.tracks

    # ---------------------------------------------------------

    def confirmed_tracks(self):
        return [t for t in self.tracks if t.confirmed]

    def drain_finished(self):
        """Pop the tracks that ended since last call, for event logging."""
        out, self.finished = self.finished, []
        return out

    def counts_by_class(self):
        counts = {}
        for t in self.tracks:
            if t.confirmed:
                counts[t.class_name] = counts.get(t.class_name, 0) + 1
        return counts

    def reset(self):
        for t in self.tracks:
            if t.confirmed:
                self.finished.append(t)
        self.tracks = []
