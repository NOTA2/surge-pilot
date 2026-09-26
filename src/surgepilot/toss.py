"""Small client for the official Toss Securities REST specification.

Reference: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
No key or token is written to disk.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


class TossError(RuntimeError):
    pass


class TossClient:
    base_url = "https://openapi.tossinvest.com"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None, account_seq: str | None = None):
        self.client_id = client_id or os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("TOSS_CLIENT_SECRET", "")
        self.account_seq = account_seq or os.getenv("TOSS_ACCOUNT_SEQ", "")
        if not self.client_id or not self.client_secret:
            raise TossError("TOSS_CLIENT_ID and TOSS_CLIENT_SECRET are required")
        self._token: str | None = None
        self._expires_at = 0.0
        self._auth_lock = threading.Lock()

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None, account: bool = False):
        if path != "/oauth2/token" and (self._token is None or time.time() > self._expires_at - 60):
            with self._auth_lock:
                if self._token is None or time.time() > self._expires_at - 60:
                    self._authenticate()
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json", "User-Agent": "SurgePilot/0.1"}
        if path != "/oauth2/token":
            headers["Authorization"] = f"Bearer {self._token}"
        if account:
            if not self.account_seq:
                raise TossError("TOSS_ACCOUNT_SEQ is required for account APIs")
            headers["X-Tossinvest-Account"] = self.account_seq
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    payload = json.load(response)
                    return payload if path == "/oauth2/token" else payload["result"]
            except urllib.error.HTTPError as exc:
                detail = exc.read(500).decode("utf-8", "replace")
                if exc.code == 429 and attempt < 3:
                    time.sleep(min(float(exc.headers.get("Retry-After", "2")), 30))
                    continue
                raise TossError(f"Toss API {exc.code} at {path}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 3 and method == "GET":
                    time.sleep(2 ** attempt)
                    continue
                raise TossError(f"Toss API connection failed at {path}: {exc}") from exc
        raise TossError("request retries exhausted")

    def _authenticate(self):
        encoded = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                          "client_secret": self.client_secret}).encode()
        for attempt in range(3):
            req = urllib.request.Request(self.base_url + "/oauth2/token", data=encoded,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    time.sleep(min(float(exc.headers.get("Retry-After", "5")), 30))
                    continue
                raise TossError(f"Toss authentication failed: HTTP {exc.code}") from exc
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload["expires_in"])

    def candles(self, symbol: str, before: str | None = None) -> dict:
        params = {"symbol": symbol, "interval": "1m", "count": 200, "adjusted": "false"}
        if before:
            params["before"] = before
        return self._request("GET", "/api/v1/candles", params)

    def prices(self, symbols: list[str]) -> list[dict]:
        if not symbols or len(symbols) > 200:
            raise ValueError("prices requires 1-200 symbols")
        return self._request("GET", "/api/v1/prices", {"symbols": ",".join(symbols)})

    def rankings(self, ranking_type: str = "MARKET_TRADING_VOLUME", duration: str = "realtime") -> dict:
        return self._request("GET", "/api/v1/rankings", {"type": ranking_type,
                              "marketCountry": "US", "duration": duration, "count": 100})

    def market_day(self, us_date: str) -> dict:
        return self._request("GET", "/api/v1/market-calendar/US", {"date": us_date})["today"]

    def stocks(self, market: str) -> list[dict]:
        return self._request("GET", "/api/v1/stocks/all", {"market": market, "status": "ACTIVE",
                              "securityType": "STOCK", "commonShare": "true"})

    def accounts(self) -> list[dict]:
        return self._request("GET", "/api/v1/accounts")

    def buying_power(self) -> dict:
        return self._request("GET", "/api/v1/buying-power", {"currency": "USD"}, account=True)

    def order(self, symbol: str, side: str, quantity: int, order_id: str) -> dict:
        if side not in ("BUY", "SELL") or quantity < 1:
            raise ValueError("invalid order")
        return self._request("POST", "/api/v1/orders", body={"clientOrderId": order_id,
                              "symbol": symbol, "side": side, "orderType": "MARKET", "quantity": str(quantity)}, account=True)


def collect_candles(client: TossClient, symbols: list[str], since: datetime, destination):
    """Download available minute bars. Historical coverage varies by symbol/provider."""
    import csv
    from pathlib import Path

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for symbol in symbols:
            before = None
            seen = set()
            written = set()
            while True:
                page = client.candles(symbol, before)
                candles = page.get("candles", [])
                for candle in candles:
                    timestamp = datetime.fromisoformat(candle["timestamp"])
                    if timestamp < since or candle["timestamp"] in written:
                        continue
                    written.add(candle["timestamp"])
                    writer.writerow([candle["timestamp"], symbol, candle["openPrice"], candle["highPrice"],
                                     candle["lowPrice"], candle["closePrice"], candle["volume"]])
                    count += 1
                next_before = page.get("nextBefore")
                if not candles or not next_before or next_before in seen or datetime.fromisoformat(candles[-1]["timestamp"]) < since:
                    break
                seen.add(next_before)
                before = next_before
    return count
