#!/usr/bin/env python3
"""
server.py — local dev server for Scribble Studies
Serves the site + segmented images; auto-pushes to GitHub Pages after saves.

Endpoints:
  GET  /api/annotations       → annotations_local.json
  POST /api/annotations       → save + schedule push
  GET  /api/k_to_is           → k_to_is_local.json
  POST /api/k_to_is           → save
  GET  /api/is_sessions       → is_sessions_local.json  (skeleton only, no images)
  POST /api/is_sessions       → merge sessions by session_id, schedule push

Run:
    python server.py
then open http://localhost:8080
"""
import json
import os
import re
import subprocess
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

SITE_DIR      = Path(__file__).resolve().parent
SEGMENTED_DIR = SITE_DIR / ".." / ".." / "kellogg_downloader" / "data" / "images_segmented"
ANNOT_FILE    = SITE_DIR / "data" / "annotations_local.json"
IS_FILE       = SITE_DIR / "data" / "is_sessions_local.json"
K_TO_IS_FILE  = SITE_DIR / "data" / "k_to_is_local.json"
PORT          = 8080

SHA_RE = re.compile(r"^/img/segmented/([a-f0-9]{64})\.(png|jpg|jpeg|webp)$", re.IGNORECASE)

# ── Auto-push to GitHub Pages (debounce 15 s) ─────────────────────────────────
_push_timer = None
_push_lock  = threading.Lock()

def schedule_push():
    global _push_timer
    with _push_lock:
        if _push_timer:
            _push_timer.cancel()
        _push_timer = threading.Timer(15.0, _do_push)
        _push_timer.daemon = True
        _push_timer.start()

def _do_push():
    try:
        subprocess.run(
            ["git", "add",
             "data/annotations_local.json",
             "data/is_sessions_local.json"],
            cwd=SITE_DIR, capture_output=True
        )
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=SITE_DIR
        )
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "auto: sync annotations + IS sessions"],
                cwd=SITE_DIR, capture_output=True
            )
            result = subprocess.run(
                ["git", "push", "origin", "HEAD:main"],
                cwd=SITE_DIR, capture_output=True, text=True
            )
            if result.returncode == 0:
                print("✓  Data pushed to GitHub Pages")
            else:
                print("·  Push rejected, pulling remote changes first...")
                subprocess.run(
                    ["git", "pull", "--rebase", "origin", "main"],
                    cwd=SITE_DIR, capture_output=True
                )
                result2 = subprocess.run(
                    ["git", "push", "origin", "HEAD:main"],
                    cwd=SITE_DIR, capture_output=True, text=True
                )
                if result2.returncode == 0:
                    print("✓  Data pushed to GitHub Pages (after pull)")
                else:
                    print(f"✗  Push failed: {result2.stderr.strip()}")
        else:
            print("·  No changes to push")
    except Exception as e:
        print(f"✗  Push error: {e}")


def _load_is_sessions():
    """Load IS sessions file; return list (strip only full_dataurl, keep thumb)."""
    if not IS_FILE.exists():
        return []
    try:
        data = json.loads(IS_FILE.read_text(encoding="utf-8"))
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        out = []
        for s in sessions:
            s2 = {k: v for k, v in s.items() if k != "full_dataurl"}
            out.append(s2)
        return out
    except Exception:
        return []


def _save_is_sessions(incoming):
    """Merge incoming sessions (list) with existing ones by session_id, then persist."""
    existing = {}
    if IS_FILE.exists():
        try:
            data = json.loads(IS_FILE.read_text(encoding="utf-8"))
            raw = data if isinstance(data, list) else data.get("sessions", [])
            for s in raw:
                existing[s["session_id"]] = s
        except Exception:
            pass
    for s in incoming:
        sid = s.get("session_id")
        if not sid:
            continue
        if sid in existing:
            # Update all fields except full_dataurl (thumb is allowed)
            existing[sid].update({k: v for k, v in s.items()
                                   if k != "full_dataurl"})
        else:
            existing[sid] = s
    merged = list(existing.values())
    IS_FILE.parent.mkdir(exist_ok=True)
    with open(IS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def do_GET(self):
        if self.path == "/api/is_sessions":
            try:
                sessions = _load_is_sessions()
                self._send_json({"sessions": sessions})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/k_to_is":
            try:
                if K_TO_IS_FILE.exists():
                    self._send_json(json.loads(K_TO_IS_FILE.read_text(encoding="utf-8")))
                else:
                    self.send_response(404)
                    self.end_headers()
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/annotations":
            try:
                data = ANNOT_FILE.read_text(encoding="utf-8") if ANNOT_FILE.exists() else "{}"
                body = data.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/is_sessions":
            try:
                data = json.loads(self._read_body())
                incoming = data.get("sessions", data) if isinstance(data, dict) else data
                _save_is_sessions(incoming)
                self._send_json({"ok": True})
                print(f"✓  IS sessions updated ({len(incoming)} submitted)")
                schedule_push()
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/k_to_is":
            try:
                data = json.loads(self._read_body())
                K_TO_IS_FILE.parent.mkdir(exist_ok=True)
                with open(K_TO_IS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({"ok": True})
                print("✓  K_TO_IS saved locally")
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/annotations":
            try:
                data = json.loads(self._read_body())
                ANNOT_FILE.parent.mkdir(exist_ok=True)
                with open(ANNOT_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({"ok": True})
                schedule_push()
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self.send_response(404)
            self.end_headers()

    def translate_path(self, path):
        m = SHA_RE.match(path)
        if m:
            sha, ext = m.group(1).lower(), m.group(2).lower()
            p = SEGMENTED_DIR / f"{sha}.{ext}"
            if p.exists():
                return str(p)
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)


os.chdir(SITE_DIR)
print(f"Scribble Studies  →  http://localhost:{PORT}")
print(f"Segmented images  →  {SEGMENTED_DIR}")
print("Auto-push to GitHub Pages 15s after last save (annotations + IS sessions)")
print("Ctrl+C to stop\n")
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
