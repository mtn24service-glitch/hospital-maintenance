"""Keep-alive pinger for the Hospital Maintenance system.

Hits the deployed ``/health`` endpoint on a fixed interval so that free hosting
(Render, Replit, ...) and the serverless Neon database never spin down. That
keeps the QR codes, the public tracking page and the live dashboard working
24/7.

Usage:
    set BASE_URL https://your-app.onrender.com     (Windows CMD)
    $env:BASE_URL="https://your-app.onrender.com"  (PowerShell)
    python keepalive.py

Environment variables:
    BASE_URL   - public base URL of the deployment (required)
    INTERVAL   - seconds between pings (default 600 = 10 minutes)
"""

import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = (os.environ.get("BASE_URL") or "").strip().rstrip("/")
INTERVAL = int(os.environ.get("INTERVAL", "600"))


def ping() -> bool:
    url = f"{BASE_URL}/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hm-keepalive/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "ignore")
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {resp.status} {body}", flush=True)
        return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR {url}: {exc}", flush=True)
        return False


def main() -> int:
    if not BASE_URL:
        print("BASE_URL is not set. Example: BASE_URL=https://your-app.onrender.com python keepalive.py")
        return 2
    print(f"Keep-alive pinging {BASE_URL}/health every {INTERVAL}s (Ctrl+C to stop)")
    while True:
        ping()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
