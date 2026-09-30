"""Optional sync: upload a dashboard snapshot to your own Vercel project.

This is the ONE place the bot sends data anywhere. It can only send to the
single sync URL in your config, which must be an https *.vercel.app address,
and it only sends the same read-only numbers the local dashboard shows. It
has no access to exchanges, keys or orders, so the no-trading guarantee
holds: nothing here can place a trade.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import dashboard, feeds


class SyncError(RuntimeError):
    pass


def check_url(url):
    p = urllib.parse.urlparse(url or "")
    if p.scheme != "https" or not (p.hostname or "").endswith(".vercel.app"):
        raise SyncError(f"sync url must be https://<something>.vercel.app, got {url!r}")
    return f"https://{p.hostname}"


def build_snapshot(cfg, here, momentum_cache):
    bt_path = os.path.join(here, "results", "backtest_last.json")
    backtest = None
    if os.path.exists(bt_path):
        with open(bt_path) as f:
            backtest = json.load(f)
    log = dashboard.read_lab_log(here)
    runs = [e for e in log if e.get("kind") == "backtest"]
    lab = {
        "backtest_runs": max(len(runs), 1 if backtest else 0),
        "stress_tested": any((e.get("entry_delay_s") or 0) > 0 for e in runs)
                         or bool(backtest and (backtest["assumptions"].get("entry_delay_s") or 0) > 0),
        "momentum_runs": sum(1 for e in log if e.get("kind") == "momentum"),
    }
    return {
        "version": 1,
        "synced_at": int(time.time()),
        "every_seconds": cfg["sync"].get("every_seconds", 1800),
        "state": dashboard.ledger_state(cfg),
        "backtest": backtest,
        "lab": lab,
        "momentum": momentum_cache,
    }


def upload(url, secret, snapshot, timeout=20):
    base = check_url(url)
    body = json.dumps(snapshot, default=str).encode()
    req = urllib.request.Request(
        base + "/api/ingest", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}",
                 "User-Agent": "ptbot-sync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, len(body)
    except urllib.error.HTTPError as e:
        raise SyncError(f"upload rejected: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SyncError(f"upload failed: {e}") from e


class Syncer:
    """Called from the bot's main loop. Uploads every `every_seconds`
    (default 30 min, to stay well inside Vercel's free Blob limits)."""

    def __init__(self, cfg, here, log=print):
        s = cfg.get("sync") or {}
        self.enabled = bool(s.get("url") and s.get("secret"))
        self.cfg, self.here, self.log = cfg, here, log
        self.every = max(300, int(s.get("every_seconds", 1800)))
        self.last = 0
        self.mom, self.mom_at = None, 0
        if self.enabled:
            check_url(s["url"])

    def maybe_sync(self, now=None):
        now = now or time.time()
        if not self.enabled or now - self.last < self.every:
            return None
        self.last = now
        if self.mom is None or now - self.mom_at > 6 * 3600:
            try:
                self.mom = dashboard.momentum_state(self.cfg, os.path.join(self.here, "bt_cache"))
                self.mom_at = now
            except feeds.FetchError as e:
                self.log(f"sync: momentum refresh failed, sending last copy: {e}")
        snap = build_snapshot(self.cfg, self.here, self.mom)
        status, size = upload(self.cfg["sync"]["url"], self.cfg["sync"]["secret"], snap)
        self.log(f"sync: uploaded {size // 1024} KB to Vercel (HTTP {status})")
        return status
