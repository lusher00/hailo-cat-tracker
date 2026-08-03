#!/usr/bin/env python3
"""End-to-end test with a fake camera and a fake NPU.

Everything between them is the real code: JPEG demux, letterbox, detection
parsing, the tracker, annotation, the frame publisher, the event log, and the
whole Flask surface.

Run from the project root:  python3 tests/test_pipeline.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = int(os.environ.get("TEST_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)
    return condition


def get(path, timeout=10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.read(), r.headers


def get_json(path, timeout=10):
    status, body, _ = get(path, timeout)
    return status, json.loads(body.decode())


def post_json(path, payload, timeout=10):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def main():
    workdir = tempfile.mkdtemp(prefix="hailo-test-")
    bindir = os.path.join(workdir, "bin")
    os.makedirs(bindir)

    # Shim rpicam-vid onto PATH
    shim = os.path.join(bindir, "rpicam-vid")
    with open(shim, "w") as fh:
        fh.write("#!/bin/sh\nexec %s %s \"$@\"\n"
                 % (sys.executable, os.path.join(HERE, "fake_rpicam_vid.py")))
    os.chmod(shim, 0o755)

    # Shim hailo_platform onto PYTHONPATH
    shutil.copy(os.path.join(HERE, "fake_hailo_platform.py"),
                os.path.join(workdir, "hailo_platform.py"))

    # A .hef only has to exist; the fake never reads it
    hef = os.path.join(workdir, "yolov8s.hef")
    open(hef, "wb").write(b"\x00")

    env = dict(os.environ)
    env["PATH"] = bindir + os.pathsep + env["PATH"]
    env["PYTHONPATH"] = workdir + os.pathsep + ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.update({
        "HTTP_PORT": str(PORT),
        "HEF_PATH": hef,
        "EVENT_DB": os.path.join(workdir, "events.db"),
        "SNAPSHOT_DIR": os.path.join(workdir, "snapshots"),
        "SNAPSHOT_ON_DETECT": "true",
        "SNAPSHOT_COOLDOWN": "0",
        "TRACK_MIN_HITS": "2",
        "CAM_WIDTH": "1280",
        "CAM_HEIGHT": "720",
        "DETECTION_LOG": "true",
        "PYTHONUNBUFFERED": "1",
    })

    print(f"\nWorkdir: {workdir}")
    print(f"Starting hailo_tracker.py on port {PORT} ...\n")

    log_path = os.path.join(workdir, "server.log")
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "hailo_tracker.py")],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    try:
        # ---- wait for it to come up ----
        up = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                get("/healthz", timeout=2)
                up = True
                break
            except Exception:
                time.sleep(0.5)

        if not up:
            print("Server never came up. Log:\n")
            print(open(log_path).read())
            return 1

        print("Server up. Waiting for frames to flow...\n")
        time.sleep(6)

        # ---------------- HTTP surface ----------------
        print("HTTP surface")
        status, body, _ = get("/")
        check("GET / returns the UI", status == 200 and b"Hailo Tracker" in body)
        check("UI inlines the COCO list", b'"toothbrush"' in body)

        status, st = get_json("/stats")
        check("GET /stats", status == 200)
        check("frames are being processed", st["frames"] > 10, f"frames={st['frames']}")
        check("pipeline reports healthy", st["healthy"] is True)
        check("fps is plausible", 1.0 < st["fps"] < 120.0, f"fps={st['fps']:.1f}")
        check("no camera restarts", st["capture_errors"] == 0,
              f"errors={st['capture_errors']}")

        # ---------------- detection + tracking ----------------
        print("\nDetection and tracking")
        status, tracks = get_json("/tracks")
        check("GET /tracks", status == 200)
        check("both synthetic objects are tracked", len(tracks) >= 2,
              f"got {len(tracks)}")

        names = {t["class"] for t in tracks}
        check("red block classified as cat", "cat" in names, str(sorted(names)))
        check("blue block classified as person", "person" in names, str(sorted(names)))
        check("tracks carry stable IDs", all(t["id"] > 0 for t in tracks))
        check("tracks are confirmed", all(t["confirmed"] for t in tracks))

        # Geometry: the stationary blue block is drawn at (120,90)-(260,400)
        # in a 1280x720 frame. If the letterbox round trip is right, the
        # reported box should land within a few px of that.
        person = next((t for t in tracks if t["class"] == "person"), None)
        if person:
            x1, y1, x2, y2 = person["box"]
            tol = 18
            ok = (abs(x1 - 120) < tol and abs(y1 - 90) < tol and
                  abs(x2 - 260) < tol and abs(y2 - 400) < tol)
            check("letterbox round trip puts the box back where it belongs",
                  ok, f"got {person['box']}, expected ~[120, 90, 260, 400]")
        else:
            check("letterbox round trip puts the box back where it belongs", False,
                  "no person track")

        # IDs must survive across frames, not churn every frame
        ids_before = {t["class"]: t["id"] for t in tracks}
        time.sleep(3)
        _, tracks2 = get_json("/tracks")
        ids_after = {t["class"]: t["id"] for t in tracks2}
        stable = all(ids_before.get(k) == v for k, v in ids_after.items()
                     if k in ids_before)
        check("track IDs persist across frames", stable,
              f"{ids_before} -> {ids_after}")

        # ---------------- live config ----------------
        print("\nLive reconfiguration")
        status, res = post_json("/api/config", {"tracked_classes": ["cat"]})
        check("POST /api/config accepted", status == 200 and res["ok"])
        time.sleep(2.5)
        _, tracks3 = get_json("/tracks")
        only_cats = tracks3 and all(t["class"] == "cat" for t in tracks3)
        check("class filter applies without restart", only_cats,
              str(sorted({t["class"] for t in tracks3})))

        status, res = post_json("/api/config", {"conf_thresh": 0.99})
        time.sleep(2.5)
        _, tracks4 = get_json("/tracks")
        check("raising confidence to 0.99 drops everything", len(tracks4) == 0,
              f"{len(tracks4)} tracks remain")

        post_json("/api/config", {"conf_thresh": 0.40, "tracked_classes": []})
        time.sleep(2.5)
        _, tracks5 = get_json("/tracks")
        check("restoring config brings detections back", len(tracks5) >= 2,
              f"{len(tracks5)} tracks")

        status, res = post_json("/api/config", {"conf_thresh": 5.0})
        _, cfg = get_json("/api/config")
        check("out-of-range config is rejected", cfg["conf_thresh"] <= 1.0,
              f"conf={cfg['conf_thresh']}")

        # ---------------- images ----------------
        print("\nImage output")
        status, jpeg, headers = get("/snapshot")
        check("GET /snapshot returns JPEG", status == 200 and jpeg[:2] == b"\xff\xd8",
              f"{len(jpeg)} bytes")
        check("snapshot content-type", headers["Content-Type"] == "image/jpeg")

        out_jpg = os.path.join(ROOT, "tests", "output_sample.jpg")
        with open(out_jpg, "wb") as fh:
            fh.write(jpeg)
        print(f"       wrote {out_jpg} for visual inspection")

        # ---------------- MJPEG multi-client ----------------
        print("\nMJPEG stream (two simultaneous clients)")
        counts = _count_frames_two_clients(BASE + "/video", seconds=4.0)
        check("client A received frames", counts[0] > 10, f"{counts[0]} frames")
        check("client B received frames", counts[1] > 10, f"{counts[1]} frames")
        ratio = min(counts) / max(counts) if max(counts) else 0
        check("both clients got the same stream (no frame stealing)", ratio > 0.75,
              f"A={counts[0]} B={counts[1]} ratio={ratio:.2f}")

        # ---------------- events ----------------
        print("\nEvent log")
        status, events = get_json("/events?limit=50")
        check("GET /events", status == 200)
        check("events were recorded", len(events) >= 2, f"{len(events)} rows")
        if events:
            e = events[0]
            check("event rows are well formed",
                  all(k in e for k in ("class", "started_at", "max_conf", "started_iso")),
                  str(sorted(e.keys()))[:90])

        status, summary = get_json("/events/summary")
        check("GET /events/summary", status == 200 and isinstance(summary, dict),
              str(list(summary.keys())))

        status, csv_body, headers = get("/events.csv")
        check("GET /events.csv", status == 200 and b"track_id,class" in csv_body)
        check("CSV is an attachment",
              "attachment" in headers.get("Content-Disposition", ""))

        status, snaps = get_json("/snapshots")
        check("snapshots were saved on detection", len(snaps) >= 1,
              f"{len(snaps)} files")

        # ---------------- metrics + health ----------------
        print("\nMonitoring endpoints")
        status, metrics, headers = get("/metrics")
        check("GET /metrics", status == 200 and b"hailo_fps" in metrics)
        check("metrics include per-class counters",
              b"hailo_detections_total{class=" in metrics)
        check("metrics are Prometheus text",
              headers["Content-Type"].startswith("text/plain"))

        status, health = get_json("/healthz")
        check("GET /healthz reports ok", status == 200 and health["ok"])

        # ---------------- shutdown ----------------
        print("\nShutdown")
        proc.terminate()
        try:
            proc.wait(timeout=12)
            clean = proc.returncode in (0, -15, 143)
        except subprocess.TimeoutExpired:
            proc.kill()
            clean = False
        check("terminates cleanly on SIGTERM", clean, f"rc={proc.returncode}")

        log.close()
        log_text = open(log_path).read()
        check("no tracebacks in the log", "Traceback" not in log_text)
        check("detections were logged to stdout", "[detect]" in log_text)

        # ---- summary ----
        print("\n" + "=" * 58)
        print(f"  {len(PASS)} passed, {len(FAIL)} failed")
        if FAIL:
            for f in FAIL:
                print(f"    FAILED: {f}")
            print("\n  Server log tail:")
            print("    " + "\n    ".join(log_text.strip().splitlines()[-25:]))
        print("=" * 58 + "\n")
        return 1 if FAIL else 0

    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            log.close()
        except Exception:
            pass


def _count_frames_two_clients(url, seconds=4.0):
    """Open two MJPEG readers at once and count boundaries each receives."""
    import threading

    results = [0, 0]

    def reader(idx):
        try:
            with urllib.request.urlopen(url, timeout=seconds + 5) as r:
                deadline = time.time() + seconds
                buf = b""
                while time.time() < deadline:
                    chunk = r.read(16384)
                    if not chunk:
                        break
                    buf += chunk
                    results[idx] += buf.count(b"--frame")
                    buf = buf[-16:]
        except Exception:
            pass

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=seconds + 8)
    return results


if __name__ == "__main__":
    sys.exit(main())
