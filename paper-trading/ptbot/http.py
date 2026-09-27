"""Read-only HTTP client.

This bot never trades. The only network primitive it has is an HTTP GET
against an allowlist of public market-data hosts. There is no POST, no
signing, no API key, and no private key anywhere in this package.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_HOSTS = {
    "api.exchange.coinbase.com",
    "api.kraken.com",
    "api.binance.com",
    "gamma-api.polymarket.com",
    "clob.polymarket.com",
    "data-api.polymarket.com",
}

USER_AGENT = "ptbot-paper-trader/1.0 (read-only)"


class FetchError(RuntimeError):
    pass


def get_json(url, params=None, timeout=10, retries=2):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise FetchError(f"host not in read-only allowlist: {host}")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"GET {url} failed: {last}")
