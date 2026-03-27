#!/usr/bin/env python3
"""
server.py — sostituisce "python -m http.server 8080"
Serve il sito normalmente + le immagini segmentate da /img/segmented/
Dopo ogni salvataggio annotazioni, pusha automaticamente su GitHub (debounce 15s).

Esegui:
    python server.py
poi vai su http://localhost:8080
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
PORT          = 8080

SHA_RE = re.compile(r"^/img/segmented/([a-f0-9]{64})\.(png|jpg|jpeg|webp)$", re.IGNORECASE)

# ── Auto-push su GitHub Pages ─────────────────────────────────────────────────
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
            ["git", "add", "data/annotations_local.json"],
            cwd=SITE_DIR, capture_output=True
        )
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=SITE_DIR
        )
        if diff.returncode != 0:  # ci sono modifiche staged
            subprocess.run(
                ["git", "commit", "-m", "auto: update annotations"],
                cwd=SITE_DIR, capture_output=True
            )
            result = subprocess.run(
                ["git", "push", "origin", "HEAD:main"],
                cwd=SITE_DIR, capture_output=True, text=True
            )
            if result.returncode == 0:
                print("✓  Annotations pushed to GitHub Pages")
            else:
                print(f"✗  Push failed: {result.stderr.strip()}")
        else:
            print("·  No annotation changes to push")
    except Exception as e:
        print(f"✗  Push error: {e}")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/api/annotations":
            try:
                data = ANNOT_FILE.read_text(encoding="utf-8") if ANNOT_FILE.exists() else "{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data.encode())
            except Exception:
                self.send_response(500)
                self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/annotations":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                ANNOT_FILE.parent.mkdir(exist_ok=True)
                with open(ANNOT_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                schedule_push()   # ← push automatico dopo 15s di inattività
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
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
print(f"Kellogg Catalog Site  →  http://localhost:{PORT}")
print(f"Immagini segmentate   →  {SEGMENTED_DIR}")
print("Auto-push su GitHub Pages dopo 15s dall'ultimo salvataggio")
print("Ctrl+C per fermare\n")
HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
