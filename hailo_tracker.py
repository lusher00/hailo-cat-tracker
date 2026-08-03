#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
hailo_tracker.py — Real-time object detection and tracking on Raspberry Pi 5
with a Hailo-8/8L NPU. Streams annotated MJPEG over HTTP.

Labels every detected object by default (all 80 COCO classes, each with its own
colour and a stable track ID). Narrow it down live from the web UI, or pin a
class list in the config.

Pipeline:

    rpicam-vid ──> capture thread ──> NPU ──> tracker ──> render thread ──┐
                                                                          │
                             all browser clients read the same ───────────┘
                             published frame (no per-client re-encode)

Every CONFIG constant can be overridden by an environment variable of the same
name, or a --flag on the command line. Precedence: CLI > env > file default.
"""

import os
import sys
import csv
import io
import json
import time
import queue
import signal
import socket
import argparse
import threading
import subprocess
from collections import deque, defaultdict

# HailoRT built from source drops its Python bindings outside the default path
# on some distros (notably Ubuntu on Pi). Harmless when hailo_platform came
# from `apt install hailo-all`.
for _p in (
    "/usr/lib/aarch64-linux-gnu/python3.12/site-packages",
    "/usr/lib/aarch64-linux-gnu/python3.11/site-packages",
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from tracker import Tracker
from events import EventLog, SnapshotStore, Webhook
import webui

HERE = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# CONFIG
# ============================================================
def _env(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        print(f"[WARN] Bad value for {name}={raw!r} — using default {default!r}")
        return default


# ---------- server ----------
HTTP_PORT = _env("HTTP_PORT", 8080, int)
HTTP_HOST = _env("HTTP_HOST", "0.0.0.0")

# ---------- model ----------
NN_SIZE = _env("NN_SIZE", 640, int)
HEF_CANDIDATES = [
    _env("HEF_PATH", ""),
    os.path.join(HERE, "yolov8s.hef"),
    "/usr/share/hailo-models/yolov8s_h8l.hef",
    "/usr/share/hailo-models/yolov8s.hef",
]

# Hailo's NMS post-process emits either [x1,y1,x2,y2,score] or [y1,x1,y2,x2,score]
# depending on how the model was compiled. If every box looks mirrored across the
# diagonal, flip this to "yxyx".
BOX_ORDER = _env("BOX_ORDER", "xyxy")

# ---------- detection ----------
CONF_THRESH = _env("CONF_THRESH", 0.40, float)
TRACKED_CLASSES = [c.strip().lower() for c in _env("TRACKED_CLASSES", "").split(",") if c.strip()]

# Run the NPU on every Nth frame; the tracker coasts through the gaps. Leave at 1
# unless you're running a heavier model and would rather have smooth video than
# a detection on every single frame.
INFER_EVERY_N = max(1, _env("INFER_EVERY_N", 1, int))

# Ignore boxes smaller than this fraction of the frame — kills the speckle of
# tiny false positives on textured backgrounds.
MIN_BOX_AREA_FRAC = _env("MIN_BOX_AREA_FRAC", 0.0005, float)

# ---------- tracking ----------
TRACK_ENABLED    = _env("TRACK_ENABLED", True, bool)
TRACK_IOU        = _env("TRACK_IOU", 0.30, float)
TRACK_MAX_MISSES = _env("TRACK_MAX_MISSES", 15, int)
TRACK_MIN_HITS   = _env("TRACK_MIN_HITS", 3, int)
TRAIL_LENGTH     = _env("TRAIL_LENGTH", 48, int)

# ---------- camera ----------
# Defaults carried over from the cat-tracker build, which was aimed at a moving
# animal indoors. 720p keeps latency down (the net downsamples to 640 anyway),
# and a pinned shutter/gain stops auto-exposure hunting from smearing motion.
CAM_WIDTH     = _env("CAM_WIDTH", 1280, int)
CAM_HEIGHT    = _env("CAM_HEIGHT", 720, int)
CAM_FRAMERATE = _env("CAM_FRAMERATE", 30, int)
CAM_AUTOFOCUS = _env("CAM_AUTOFOCUS", "continuous")   # continuous|manual|auto
CAM_LENS_POS  = _env("CAM_LENS_POSITION", "")         # dioptres, manual AF only
CAM_SHUTTER   = _env("CAM_SHUTTER", "20000")          # µs; "" = auto
CAM_GAIN      = _env("CAM_GAIN", "2")                 # "" = auto
CAM_EV        = _env("CAM_EV", "0")
CAM_DENOISE   = _env("CAM_DENOISE", "off")
CAM_HFLIP     = _env("CAM_HFLIP", False, bool)
CAM_VFLIP     = _env("CAM_VFLIP", False, bool)
CAM_EXTRA_ARGS = _env("CAM_EXTRA_ARGS", "")           # raw passthrough

ROTATE_DEGREES = _env("ROTATE_DEGREES", 0, int)

# ---------- output ----------
JPEG_QUALITY   = _env("JPEG_QUALITY", 85, int)
SHOW_HUD       = _env("SHOW_HUD", True, bool)
SHOW_LABELS    = _env("SHOW_LABELS", True, bool)
SHOW_IDS       = _env("SHOW_IDS", True, bool)
SHOW_TRAILS    = _env("SHOW_TRAILS", False, bool)
SHOW_ROI       = _env("SHOW_ROI", False, bool)
CONFIRMED_ONLY = _env("CONFIRMED_ONLY", True, bool)
STREAM_MAX_FPS = _env("STREAM_MAX_FPS", 0, float)   # 0 = as fast as frames arrive

# ---------- region of interest ----------
# Normalised polygon, JSON: ROI_POLYGON='[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]'
# Detections whose centre falls outside are ignored entirely.
ROI_POLYGON = _env("ROI_POLYGON", "")

# ---------- events / snapshots / webhook ----------
EVENT_LOG          = _env("EVENT_LOG", True, bool)
EVENT_DB           = _env("EVENT_DB", os.path.join(HERE, "events.db"))
EVENT_RETENTION_D  = _env("EVENT_RETENTION_DAYS", 30, int)
SNAPSHOT_ON_DETECT = _env("SNAPSHOT_ON_DETECT", False, bool)
SNAPSHOT_DIR       = _env("SNAPSHOT_DIR", os.path.join(HERE, "snapshots"))
SNAPSHOT_MAX_FILES = _env("SNAPSHOT_MAX_FILES", 500, int)
SNAPSHOT_COOLDOWN  = _env("SNAPSHOT_COOLDOWN", 30.0, float)
WEBHOOK_URL        = _env("WEBHOOK_URL", "")

DETECTION_LOG          = _env("DETECTION_LOG", True, bool)
DETECTION_LOG_COOLDOWN = _env("DETECTION_LOG_COOLDOWN", 10.0, float)


# ============================================================
# COCO CLASSES
# ============================================================
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]
CLASS_NAME_TO_ID = {name: i for i, name in enumerate(COCO_CLASSES)}


def _class_color(class_id):
    """Stable, reasonably separated BGR colour per class."""
    rng = np.random.default_rng(class_id + 42)
    return tuple(int(x) for x in rng.integers(80, 255, size=3))


CLASS_COLORS = [_class_color(i) for i in range(len(COCO_CLASSES))]
CLASS_COLORS[CLASS_NAME_TO_ID["cat"]] = (0, 255, 0)      # cats stay green
CLASS_COLORS[CLASS_NAME_TO_ID["dog"]] = (0, 200, 255)
CLASS_COLORS[CLASS_NAME_TO_ID["person"]] = (255, 180, 60)

CSS_COLORS = [f"rgb({r},{g},{b})" for (b, g, r) in CLASS_COLORS]


def resolve_class_ids(names):
    if not names:
        return set(range(len(COCO_CLASSES)))
    ids = set()
    for n in names:
        key = str(n).strip().lower()
        if key in CLASS_NAME_TO_ID:
            ids.add(CLASS_NAME_TO_ID[key])
        elif key.isdigit() and 0 <= int(key) < len(COCO_CLASSES):
            ids.add(int(key))
        else:
            print(f"[WARN] Unknown class {n!r} — ignored")
    return ids or set(range(len(COCO_CLASSES)))


# ============================================================
# RUNTIME SETTINGS (mutable from the web UI, no restart)
# ============================================================
class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self.conf_thresh = CONF_THRESH
        self.tracked_names = list(TRACKED_CLASSES)
        self.tracked_ids = resolve_class_ids(TRACKED_CLASSES)
        self.show_labels = SHOW_LABELS
        self.show_ids = SHOW_IDS
        self.show_trails = SHOW_TRAILS
        self.show_hud = SHOW_HUD
        self.show_roi = SHOW_ROI
        self.confirmed_only = CONFIRMED_ONLY
        self.jpeg_quality = JPEG_QUALITY

    def snapshot(self):
        with self._lock:
            return {
                "conf_thresh": self.conf_thresh,
                "tracked_classes": sorted(COCO_CLASSES[i] for i in self.tracked_ids)
                                   if self.tracked_names else [],
                "show_labels": self.show_labels,
                "show_ids": self.show_ids,
                "show_trails": self.show_trails,
                "show_hud": self.show_hud,
                "show_roi": self.show_roi,
                "confirmed_only": self.confirmed_only,
                "jpeg_quality": self.jpeg_quality,
            }

    def apply(self, data):
        changed = []
        with self._lock:
            if "conf_thresh" in data:
                v = float(data["conf_thresh"])
                if 0.01 <= v <= 0.99:
                    self.conf_thresh = v
                    changed.append("conf_thresh")
            if "tracked_classes" in data:
                names = [str(n).lower() for n in data["tracked_classes"] or []]
                self.tracked_names = names
                self.tracked_ids = resolve_class_ids(names)
                changed.append("tracked_classes")
            for key in ("show_labels", "show_ids", "show_trails", "show_hud",
                        "show_roi", "confirmed_only"):
                if key in data:
                    setattr(self, key, bool(data[key]))
                    changed.append(key)
            if "jpeg_quality" in data:
                q = int(data["jpeg_quality"])
                if 10 <= q <= 100:
                    self.jpeg_quality = q
                    changed.append("jpeg_quality")
        return changed

    # Cheap reads for the hot path
    def draw_opts(self):
        with self._lock:
            return (self.show_labels, self.show_ids, self.show_trails,
                    self.show_hud, self.show_roi, self.confirmed_only,
                    self.jpeg_quality)

    def detect_opts(self):
        with self._lock:
            return self.conf_thresh, self.tracked_ids


SETTINGS = Settings()


# ============================================================
# STATS
# ============================================================
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.frames = 0
        self.dropped = 0
        self.capture_errors = 0
        self.total_tracks = 0
        self.last_frame_at = 0.0
        self._frame_times = deque(maxlen=90)
        self._infer_times = deque(maxlen=90)
        self._encode_times = deque(maxlen=90)
        self.class_counts = defaultdict(int)

    def frame(self, infer_s):
        with self._lock:
            now = time.time()
            self.frames += 1
            self.last_frame_at = now
            self._frame_times.append(now)
            self._infer_times.append(infer_s)

    def encode(self, sec):
        with self._lock:
            self._encode_times.append(sec)

    def fps(self):
        with self._lock:
            if len(self._frame_times) < 2:
                return 0.0
            span = self._frame_times[-1] - self._frame_times[0]
            return (len(self._frame_times) - 1) / span if span > 0 else 0.0

    def _mean_ms(self, dq):
        return (sum(dq) / len(dq) * 1000.0) if dq else 0.0

    def infer_ms(self):
        with self._lock:
            return self._mean_ms(self._infer_times)

    def encode_ms(self):
        with self._lock:
            return self._mean_ms(self._encode_times)

    def healthy(self):
        with self._lock:
            return (time.time() - self.last_frame_at) < 10.0 if self.last_frame_at else False


STATS = Stats()


# ============================================================
# FRAME PUBLISHER — one encode, many clients
# ============================================================
class FramePublisher:
    """Single-writer / many-reader latest-frame holder.

    The originals called frame_q.get() inside the MJPEG generator, so two open
    browser tabs each got half the frames and annotated them twice. Here the
    render thread publishes once and every client reads the same bytes.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._jpeg = None
        self._seq = 0

    def publish(self, jpeg_bytes):
        with self._cv:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cv.notify_all()

    def latest(self):
        with self._cv:
            return self._jpeg

    def wait_for(self, last_seq, timeout=5.0):
        """Block until a frame newer than last_seq. Returns (seq, jpeg) or (last_seq, None)."""
        with self._cv:
            if self._seq == last_seq:
                self._cv.wait(timeout)
            if self._seq == last_seq:
                return last_seq, None
            return self._seq, self._jpeg

    @property
    def seq(self):
        with self._cv:
            return self._seq


PUBLISHER = FramePublisher()
detect_q: "queue.Queue" = queue.Queue(maxsize=2)
_shutdown = threading.Event()


# ============================================================
# ROI
# ============================================================
def _parse_roi(raw):
    if not raw:
        return None
    try:
        pts = json.loads(raw)
        if not isinstance(pts, list) or len(pts) < 3:
            raise ValueError("need at least 3 points")
        return np.array([[float(x), float(y)] for x, y in pts], dtype=np.float32)
    except Exception as e:
        print(f"[WARN] ROI_POLYGON ignored ({e}). "
              f'Expected JSON like [[0.1,0.1],[0.9,0.1],[0.9,0.9]]')
        return None


ROI_NORM = _parse_roi(ROI_POLYGON)


def roi_pixels(w, h):
    if ROI_NORM is None:
        return None
    return (ROI_NORM * np.array([w, h], dtype=np.float32)).astype(np.int32)


def in_roi(cx, cy, poly_px):
    if poly_px is None:
        return True
    return cv2.pointPolygonTest(poly_px, (float(cx), float(cy)), False) >= 0


# ============================================================
# HAILO
# ============================================================
from hailo_platform import (VDevice, HEF, ConfigureParams, InferVStreams,   # noqa: E402
                            InputVStreamParams, OutputVStreamParams,
                            HailoStreamInterface)

_hailo = {"hef": None, "target": None, "ng": None, "pipe": None,
          "activated": None, "in": None, "out": None, "path": None}
_infer_lock = threading.Lock()


def find_hef():
    for path in HEF_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "No .hef model found. Looked in:\n  " +
        "\n  ".join(p for p in HEF_CANDIDATES if p) +
        "\n\nRun ./download_model.sh, or set HEF_PATH."
    )


def init_hailo():
    path = find_hef()
    print(f"[hailo] Loading {path} ...")

    hef = HEF(path)
    target = VDevice()
    cfg = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
    ng = target.configure(hef, cfg)[0]
    ng_params = ng.create_params()

    in_params = InputVStreamParams.make_from_network_group(ng)
    out_params = OutputVStreamParams.make_from_network_group(ng)

    activated = ng.activate(ng_params)
    activated.__enter__()
    pipe = InferVStreams(ng, in_params, out_params)
    pipe.__enter__()

    in_info = ng.get_input_vstream_infos()[0]
    out_info = ng.get_output_vstream_infos()[0]

    _hailo.update({"hef": hef, "target": target, "ng": ng, "pipe": pipe,
                   "activated": activated, "in": in_info.name,
                   "out": out_info.name, "path": path})

    print(f"[hailo] Ready — in {in_info.name} {in_info.shape}, out {out_info.name}")

    # Reading the output name off the HEF instead of hardcoding
    # 'yolov8s/yolov8_nms_postprocess' means swapping models just works.
    try:
        model_side = int(in_info.shape[0])
        if model_side != NN_SIZE:
            print(f"[WARN] Model wants {model_side}px input but NN_SIZE={NN_SIZE}. "
                  f"Set NN_SIZE={model_side} or every box will be misplaced.")
    except Exception:
        pass


def shutdown_hailo():
    try:
        if _hailo["pipe"] is not None:
            _hailo["pipe"].__exit__(None, None, None)
        if _hailo["activated"] is not None:
            _hailo["activated"].__exit__(None, None, None)
    except Exception as e:
        print(f"[hailo] shutdown warning: {e}")


def run_inference(nn_frame):
    arr = np.expand_dims(nn_frame, axis=0)
    with _infer_lock:
        out = _hailo["pipe"].infer({_hailo["in"]: arr})
    return out[_hailo["out"]][0]        # [num_classes][N, 5]


# ============================================================
# GEOMETRY
# ============================================================
def letterbox(frame, size=NN_SIZE):
    h, w = frame.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    pad_y = (size - nh) // 2
    pad_x = (size - nw) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def parse_detections(raw, scale, pad_x, pad_y, frame_w, frame_h, conf_thresh, tracked_ids):
    """Hailo NMS output -> [(class_id, name, (x1,y1,x2,y2), conf)] in frame pixels."""
    out = []
    min_area = MIN_BOX_AREA_FRAC * frame_w * frame_h

    for class_id, class_dets in enumerate(raw):
        if class_id not in tracked_ids:
            continue
        if class_id >= len(COCO_CLASSES):
            continue
        name = COCO_CLASSES[class_id]

        for det in class_dets:
            try:
                d = np.asarray(det, dtype=np.float32).reshape(-1)
                if d.size < 5:
                    continue

                conf = float(d[4])
                if conf < conf_thresh:
                    continue

                if BOX_ORDER == "yxyx":
                    y1, x1, y2, x2 = d[0], d[1], d[2], d[3]
                else:
                    x1, y1, x2, y2 = d[0], d[1], d[2], d[3]

                # 0..1 on the NN_SIZE letterbox canvas -> original frame pixels
                x1 = (x1 * NN_SIZE - pad_x) / scale
                y1 = (y1 * NN_SIZE - pad_y) / scale
                x2 = (x2 * NN_SIZE - pad_x) / scale
                y2 = (y2 * NN_SIZE - pad_y) / scale

                x1 = max(0.0, min(x1, frame_w - 1))
                x2 = max(0.0, min(x2, frame_w - 1))
                y1 = max(0.0, min(y1, frame_h - 1))
                y2 = max(0.0, min(y2, frame_h - 1))

                if x2 <= x1 or y2 <= y1:
                    continue
                if (x2 - x1) * (y2 - y1) < min_area:
                    continue

                out.append((class_id, name, (x1, y1, x2, y2), conf))

            except Exception as e:
                print(f"[parse] {e}")

    return out


# ============================================================
# CAMERA
# ============================================================
def camera_cmd():
    cmd = [
        "rpicam-vid",
        "--codec", "mjpeg",
        "--inline", "--nopreview", "--flush",
        "--width", str(CAM_WIDTH),
        "--height", str(CAM_HEIGHT),
        "--framerate", str(CAM_FRAMERATE),
        "--timeout", "0",
        "--output", "-",
    ]
    if CAM_DENOISE:
        cmd += ["--denoise", CAM_DENOISE]
    if CAM_AUTOFOCUS:
        cmd += ["--autofocus-mode", CAM_AUTOFOCUS]
    if CAM_AUTOFOCUS == "manual" and CAM_LENS_POS:
        cmd += ["--lens-position", CAM_LENS_POS]
    if CAM_EV != "":
        cmd += ["--ev", CAM_EV]
    if CAM_SHUTTER != "":
        cmd += ["--shutter", CAM_SHUTTER]
    if CAM_GAIN != "":
        cmd += ["--gain", CAM_GAIN]
    if CAM_HFLIP:
        cmd += ["--hflip"]
    if CAM_VFLIP:
        cmd += ["--vflip"]
    if CAM_EXTRA_ARGS:
        cmd += CAM_EXTRA_ARGS.split()
    return cmd


_ROTATE_MAP = {0: None, 90: cv2.ROTATE_90_CLOCKWISE,
               180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
ROTATE = _ROTATE_MAP.get(ROTATE_DEGREES)
if ROTATE_DEGREES not in _ROTATE_MAP:
    print(f"[WARN] ROTATE_DEGREES={ROTATE_DEGREES} invalid (use 0/90/180/270). Ignoring.")


def capture_loop():
    """rpicam-vid -> decode -> NPU -> detect_q.

    Wrapped so a camera hiccup restarts the pipeline with backoff instead of
    killing the thread and leaving a live page serving a frozen image.
    """
    backoff = 1.0
    frame_index = 0
    last_raw = []

    while not _shutdown.is_set():
        proc = None
        try:
            cmd = camera_cmd()
            print(f"[camera] {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
            print("[camera] capture started")
            backoff = 1.0
            buf = b""

            while not _shutdown.is_set():
                chunk = proc.stdout.read(1 << 20)
                if not chunk:
                    if proc.poll() is not None:
                        raise RuntimeError(f"rpicam-vid exited {proc.returncode}")
                    continue
                buf += chunk

                # Drain to the NEWEST complete JPEG sitting in the buffer.
                #
                # rpicam-vid never stops producing. If decode + inference ever
                # falls behind 30fps — even briefly — taking the oldest frame
                # each pass means we render an ever-growing backlog of stale
                # video and the latency never recovers. Anything older than the
                # last complete frame is worthless, so throw it away.
                jpg = None
                stale = 0
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        break
                    if jpg is not None:
                        stale += 1
                    jpg = buf[start:end + 2]
                    buf = buf[end + 2:]

                if jpg is None:
                    if len(buf) > 8 << 20:
                        print("[camera] resyncing — no complete JPEG in 8MB")
                        buf = b""
                    continue

                if stale:
                    with STATS._lock:
                        STATS.dropped += stale

                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                if ROTATE is not None:
                    frame = cv2.rotate(frame, ROTATE)

                frame_index += 1
                infer_s = 0.0

                if frame_index % INFER_EVERY_N == 0:
                    nn_frame, scale, pad_x, pad_y = letterbox(frame)
                    t0 = time.perf_counter()
                    raw = run_inference(nn_frame)
                    infer_s = time.perf_counter() - t0
                    last_raw = (raw, scale, pad_x, pad_y)

                STATS.frame(infer_s)

                if not last_raw:
                    continue

                if detect_q.full():
                    with STATS._lock:
                        STATS.dropped += 1
                    try:
                        detect_q.get_nowait()      # drop the stale one, keep latest
                    except queue.Empty:
                        pass
                detect_q.put((frame, last_raw))

        except Exception as e:
            with STATS._lock:
                STATS.capture_errors += 1
            print(f"[camera] error: {e} — restarting in {backoff:.0f}s")
            _shutdown.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


# ============================================================
# ANNOTATION
# ============================================================
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _label_scale(box_w):
    """Shrink text for small boxes so labels don't swamp what they describe."""
    return max(0.4, min(0.7, box_w / 320.0))


def draw_track(frame, track, show_labels, show_ids, show_trails):
    x1, y1, x2, y2 = (int(v) for v in track.box)
    h, w = frame.shape[:2]
    x1, x2 = max(0, min(x1, w - 1)), max(0, min(x2, w - 1))
    y1, y2 = max(0, min(y1, h - 1)), max(0, min(y2, h - 1))
    if x2 <= x1 or y2 <= y1:
        return

    color = CLASS_COLORS[track.class_id] if track.class_id < len(CLASS_COLORS) else (200, 200, 200)
    thickness = 2 if track.confirmed else 1

    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    if show_trails and len(track.trail) > 1:
        pts = np.array([[int(px), int(py)] for px, py in track.trail], np.int32)
        cv2.polylines(frame, [pts], False, color, 2)
        cx, cy = track.trail[-1]
        cv2.circle(frame, (int(cx), int(cy)), 3, color, -1)

    if not show_labels:
        return

    parts = [track.class_name]
    if show_ids:
        parts.append(f"#{track.id}")
    parts.append(f"{track.conf:.0%}")
    text = " ".join(parts)

    scale = _label_scale(x2 - x1)
    (tw, th), base = cv2.getTextSize(text, FONT, scale, 1)

    # Prefer above the box; drop inside it when we're against the top edge.
    ty = y1 - 6
    if ty - th - 4 < 0:
        ty = y1 + th + 8

    bx1, by1 = x1, ty - th - 5
    bx2, by2 = x1 + tw + 8, ty + base
    bx2 = min(bx2, w - 1)

    cv2.rectangle(frame, (bx1, by1), (bx2, by2), color, -1)
    cv2.putText(frame, text, (x1 + 4, ty - 1), FONT, scale, (10, 10, 10), 1, cv2.LINE_AA)


def draw_hud(frame, live_count):
    fps = STATS.fps()
    ms = STATS.infer_ms()
    text = f"{fps:4.1f} fps | {ms:4.1f} ms | {live_count} obj"
    cv2.putText(frame, text, (10, 24), FONT, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(frame, text, (10, 24), FONT, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def draw_roi(frame, poly_px):
    if poly_px is None:
        return
    overlay = frame.copy()
    cv2.fillPoly(overlay, [poly_px], (60, 60, 60))
    cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)
    cv2.polylines(frame, [poly_px], True, (0, 200, 255), 2)


# ============================================================
# RENDER THREAD
# ============================================================
TRACKER = Tracker(iou_threshold=TRACK_IOU, max_misses=TRACK_MAX_MISSES,
                  min_hits=TRACK_MIN_HITS, trail_len=TRAIL_LENGTH)

EVENTS = EventLog(EVENT_DB, EVENT_RETENTION_D, enabled=EVENT_LOG)
SNAPSHOTS = SnapshotStore(SNAPSHOT_DIR, SNAPSHOT_MAX_FILES, SNAPSHOT_COOLDOWN,
                          enabled=SNAPSHOT_ON_DETECT)
HOOK = Webhook(WEBHOOK_URL)

_last_logged = {}
_live_tracks_json = []
_live_lock = threading.Lock()


def render_loop():
    global _live_tracks_json

    while not _shutdown.is_set():
        try:
            frame, (raw, scale, pad_x, pad_y) = detect_q.get(timeout=2.0)
        except queue.Empty:
            continue

        h, w = frame.shape[:2]
        conf_thresh, tracked_ids = SETTINGS.detect_opts()
        (show_labels, show_ids, show_trails,
         show_hud, show_roi, confirmed_only, quality) = SETTINGS.draw_opts()

        poly_px = roi_pixels(w, h)

        dets = parse_detections(raw, scale, pad_x, pad_y, w, h,
                                conf_thresh, tracked_ids)

        if poly_px is not None:
            dets = [d for d in dets
                    if in_roi((d[2][0] + d[2][2]) / 2, (d[2][1] + d[2][3]) / 2, poly_px)]

        if TRACK_ENABLED:
            tracks = TRACKER.update(dets)
            visible = [t for t in tracks if (t.confirmed or not confirmed_only)]
            _handle_track_events(tracks)
        else:
            # Tracking off: synthesise throwaway objects so drawing is uniform.
            visible = [_FakeTrack(cid, name, box, conf)
                       for cid, name, box, conf in dets]

        if show_roi:
            draw_roi(frame, poly_px)

        for t in visible:
            draw_track(frame, t, show_labels, show_ids, show_trails)

        if show_hud:
            draw_hud(frame, len(visible))

        t0 = time.perf_counter()
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        STATS.encode(time.perf_counter() - t0)
        if not ok:
            continue

        data = jpg.tobytes()
        PUBLISHER.publish(data)

        with _live_lock:
            _live_tracks_json = [t.to_dict() for t in visible]

        if TRACK_ENABLED:
            _handle_snapshots(visible, data)


class _FakeTrack:
    """Duck-typed stand-in so draw_track works with tracking disabled."""

    __slots__ = ("id", "class_id", "class_name", "box", "conf", "confirmed", "trail")

    def __init__(self, class_id, class_name, box, conf):
        self.id = 0
        self.class_id = class_id
        self.class_name = class_name
        self.box = box
        self.conf = conf
        self.confirmed = True
        self.trail = ()

    def to_dict(self):
        x1, y1, x2, y2 = (int(v) for v in self.box)
        return {"id": 0, "class": self.class_name, "class_id": self.class_id,
                "box": [x1, y1, x2, y2], "conf": round(self.conf, 3),
                "confirmed": True}


def _handle_track_events(tracks):
    now = time.time()

    for t in tracks:
        if t.confirmed and not t.notified:
            t.notified = True
            with STATS._lock:
                STATS.total_tracks += 1
                STATS.class_counts[t.class_name] += 1
            EVENTS.track_started(t)
            HOOK.notify(t)
            if DETECTION_LOG and now - _last_logged.get(t.class_id, 0) > DETECTION_LOG_COOLDOWN:
                _last_logged[t.class_id] = now
                x1, y1, x2, y2 = (int(v) for v in t.box)
                print(f"[detect] {t.class_name} #{t.id} {t.conf:.0%} "
                      f"at ({x1},{y1})-({x2},{y2})")

    for t in TRACKER.drain_finished():
        EVENTS.track_ended(t)
        if DETECTION_LOG:
            print(f"[detect] {t.class_name} #{t.id} left after {t.duration:.1f}s "
                  f"({t.hits} frames, peak {t.max_conf:.0%})")


def _handle_snapshots(tracks, jpeg_bytes):
    if not SNAPSHOTS.enabled:
        return
    for t in tracks:
        if getattr(t, "confirmed", False) and not getattr(t, "snapshot_taken", True):
            t.snapshot_taken = True
            path = SNAPSHOTS.maybe_save(t.class_name, t.id, jpeg_bytes)
            if path:
                EVENTS.attach_snapshot(t.id, path)
                print(f"[snapshot] {os.path.basename(path)}")


# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/")
def index():
    _, tracked_ids = SETTINGS.detect_opts()
    return webui.render(COCO_CLASSES, CSS_COLORS, tracked_ids)


def mjpeg_stream():
    seq = 0
    min_interval = (1.0 / STREAM_MAX_FPS) if STREAM_MAX_FPS > 0 else 0.0
    last_sent = 0.0

    while not _shutdown.is_set():
        seq, data = PUBLISHER.wait_for(seq, timeout=5.0)
        if data is None:
            continue
        if min_interval:
            now = time.time()
            if now - last_sent < min_interval:
                continue
            last_sent = now
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" +
               data + b"\r\n")


@app.route("/video")
def video():
    return Response(mjpeg_stream(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/snapshot")
def snapshot():
    data = PUBLISHER.latest()
    if data is None:
        return "No frame yet", 503
    return Response(data, mimetype="image/jpeg")


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    data = PUBLISHER.latest()
    if data is None:
        return jsonify({"saved": None, "error": "no frame"}), 503
    store = SNAPSHOTS if SNAPSHOTS.enabled else SnapshotStore(
        SNAPSHOT_DIR, SNAPSHOT_MAX_FILES, cooldown_s=0.0, enabled=True)
    path = store.maybe_save("manual", 0, data)
    return jsonify({"saved": os.path.basename(path) if path else None})


@app.route("/snapshots")
def snapshots_list():
    return jsonify(SNAPSHOTS.list_recent(limit=int(request.args.get("limit", 50))))


@app.route("/snapshots/<path:name>")
def snapshots_file(name):
    return send_from_directory(SNAPSHOT_DIR, name)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(SETTINGS.snapshot())

    data = request.get_json(silent=True) or {}
    changed = SETTINGS.apply(data)
    if changed:
        print(f"[config] updated: {', '.join(sorted(set(changed)))}")
    return jsonify({"ok": True, "changed": sorted(set(changed)),
                    "config": SETTINGS.snapshot()})


@app.route("/tracks")
def tracks():
    with _live_lock:
        return jsonify(_live_tracks_json)


@app.route("/stats")
def stats():
    with STATS._lock:
        class_counts = dict(STATS.class_counts)
        frames, dropped = STATS.frames, STATS.dropped
        errors, total_tracks = STATS.capture_errors, STATS.total_tracks
        started = STATS.started_at

    with _live_lock:
        live = len(_live_tracks_json)

    return jsonify({
        "fps": round(STATS.fps(), 2),
        "inference_ms": round(STATS.infer_ms(), 2),
        "encode_ms": round(STATS.encode_ms(), 2),
        "frames": frames,
        "dropped": dropped,
        "capture_errors": errors,
        "live_tracks": live,
        "total_tracks": total_tracks,
        "class_counts": class_counts,
        "uptime_s": round(time.time() - started, 1),
        "healthy": STATS.healthy(),
        "model": os.path.basename(_hailo["path"] or "?"),
        "camera": f"{CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FRAMERATE}",
        "clients_seq": PUBLISHER.seq,
        "webhook": {"sent": HOOK.sent, "failed": HOOK.failed} if HOOK.enabled else None,
        **SETTINGS.snapshot(),
    })


@app.route("/events")
def events():
    return jsonify(EVENTS.recent(
        limit=int(request.args.get("limit", 50)),
        class_name=request.args.get("class"),
        since=request.args.get("since"),
    ))


@app.route("/events/summary")
def events_summary():
    return jsonify(EVENTS.summary(since=request.args.get("since")))


@app.route("/events.csv")
def events_csv():
    buf = io.StringIO()
    EVENTS.write_csv(buf)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=hailo-events.csv"})


@app.route("/metrics")
def metrics():
    """Plain-text Prometheus exposition — point a scraper at it if you like."""
    with STATS._lock:
        class_counts = dict(STATS.class_counts)
        frames, dropped = STATS.frames, STATS.dropped
        errors, total_tracks = STATS.capture_errors, STATS.total_tracks
    with _live_lock:
        live = len(_live_tracks_json)

    lines = [
        "# HELP hailo_fps Frames per second through the pipeline",
        "# TYPE hailo_fps gauge",
        f"hailo_fps {STATS.fps():.3f}",
        "# HELP hailo_inference_ms Mean NPU inference time",
        "# TYPE hailo_inference_ms gauge",
        f"hailo_inference_ms {STATS.infer_ms():.3f}",
        "# HELP hailo_frames_total Frames processed",
        "# TYPE hailo_frames_total counter",
        f"hailo_frames_total {frames}",
        "# HELP hailo_frames_dropped_total Frames dropped under backpressure",
        "# TYPE hailo_frames_dropped_total counter",
        f"hailo_frames_dropped_total {dropped}",
        "# HELP hailo_capture_errors_total Camera pipeline restarts",
        "# TYPE hailo_capture_errors_total counter",
        f"hailo_capture_errors_total {errors}",
        "# HELP hailo_live_tracks Objects currently tracked",
        "# TYPE hailo_live_tracks gauge",
        f"hailo_live_tracks {live}",
        "# HELP hailo_tracks_total Tracks confirmed since start",
        "# TYPE hailo_tracks_total counter",
        f"hailo_tracks_total {total_tracks}",
        "# HELP hailo_detections_total Confirmed tracks by class",
        "# TYPE hailo_detections_total counter",
    ]
    for name, count in sorted(class_counts.items()):
        lines.append(f'hailo_detections_total{{class="{name}"}} {count}')

    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.route("/healthz")
def healthz():
    ok = STATS.healthy()
    return jsonify({"ok": ok, "fps": round(STATS.fps(), 2)}), (200 if ok else 503)


# ============================================================
# CLI
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Real-time object detection and tracking on Hailo-8L.",
        epilog="Any CONFIG constant can also be set via an env var of the same name.")
    p.add_argument("--port", type=int, help="HTTP port (default 8080)")
    p.add_argument("--classes", help="comma-separated class filter, e.g. cat,dog")
    p.add_argument("--conf", type=float, help="confidence threshold 0-1")
    p.add_argument("--model", help="path to .hef")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270])
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--fps", type=int, dest="framerate")
    p.add_argument("--no-track", action="store_true", help="disable ID tracking")
    p.add_argument("--no-events", action="store_true", help="disable the event log")
    p.add_argument("--snapshots", action="store_true", help="save a JPEG per new detection")
    p.add_argument("--webhook", help="POST detections to this URL")
    p.add_argument("--list-classes", action="store_true")
    return p.parse_args()


def apply_args(a):
    """CLI flags win over env, which wins over the defaults in this file."""
    global HTTP_PORT, ROTATE, ROTATE_DEGREES, CAM_WIDTH, CAM_HEIGHT
    global CAM_FRAMERATE, TRACK_ENABLED

    if a.port:
        HTTP_PORT = a.port
    if a.model:
        HEF_CANDIDATES.insert(0, a.model)
    if a.rotate is not None:
        ROTATE_DEGREES = a.rotate
        ROTATE = _ROTATE_MAP.get(a.rotate)
    if a.width:
        CAM_WIDTH = a.width
    if a.height:
        CAM_HEIGHT = a.height
    if a.framerate:
        CAM_FRAMERATE = a.framerate
    if a.no_track:
        TRACK_ENABLED = False
    if a.no_events:
        EVENTS.enabled = False
    if a.snapshots:
        SNAPSHOTS.enabled = True
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    if a.webhook:
        HOOK.url = a.webhook
        HOOK.enabled = True

    patch = {}
    if a.conf is not None:
        patch["conf_thresh"] = a.conf
    if a.classes is not None:
        patch["tracked_classes"] = [c.strip() for c in a.classes.split(",") if c.strip()]
    if patch:
        SETTINGS.apply(patch)


# ============================================================
# MAIN
# ============================================================
def local_ip():
    """gethostbyname returns 127.0.1.1 on Debian — ask the routing table instead."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


_shutdown_done = threading.Event()


def _shutdown_all(signum=None, _frame=None):
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()

    if signum:
        print(f"\n[main] signal {signum} — shutting down")
    _shutdown.set()
    PUBLISHER.publish(PUBLISHER.latest() or b"")   # wake any blocked clients
    try:
        TRACKER.reset()
        for t in TRACKER.drain_finished():
            EVENTS.track_ended(t)
    except Exception:
        pass
    EVENTS.close()          # flushes pending rows before we go
    HOOK.close()
    shutdown_hailo()

    if signum:
        # Flask's dev server has no clean stop from a signal handler, and
        # streaming /video responses keep worker threads alive indefinitely.
        # Everything that needed flushing is flushed, so leave hard — otherwise
        # systemd waits out its 90s TimeoutStopSec before SIGKILL on every
        # `systemctl restart`.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def main():
    args = parse_args()

    if args.list_classes:
        for i, name in enumerate(COCO_CLASSES):
            print(f"{i:3d}  {name}")
        return

    apply_args(args)

    signal.signal(signal.SIGTERM, _shutdown_all)
    signal.signal(signal.SIGINT, _shutdown_all)

    init_hailo()

    threading.Thread(target=capture_loop, daemon=True, name="capture").start()
    threading.Thread(target=render_loop, daemon=True, name="render").start()

    cfg = SETTINGS.snapshot()
    tracking = ", ".join(cfg["tracked_classes"]) if cfg["tracked_classes"] else "all 80 classes"

    print("\n  Hailo Tracker")
    print(f"  Model:    {os.path.basename(_hailo['path'])}")
    print(f"  Camera:   {CAM_WIDTH}x{CAM_HEIGHT} @ {CAM_FRAMERATE}fps, "
          f"AF={CAM_AUTOFOCUS}, rotate={ROTATE_DEGREES}deg")
    print(f"  Tracking: {tracking}  (conf >= {cfg['conf_thresh']:.0%})")
    print(f"  IDs:      {'on' if TRACK_ENABLED else 'off'}   "
          f"Events: {'on' if EVENTS.enabled else 'off'}   "
          f"Snapshots: {'on' if SNAPSHOTS.enabled else 'off'}")
    if ROI_NORM is not None:
        print(f"  ROI:      {len(ROI_NORM)}-point polygon active")
    print(f"\n  http://{local_ip()}:{HTTP_PORT}\n")

    try:
        app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True,
                use_reloader=False)
    finally:
        _shutdown_all()


if __name__ == "__main__":
    main()
