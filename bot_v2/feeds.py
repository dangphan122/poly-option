"""
feeds.py - Real data feeds for the paper trading bot.
- DeribitOracle: Fetches IV from Deribit, computes N(d2)
- PolymarketFeed: Discovers brackets, WebSocket order books
- Binance: Live BTC price
"""

import asyncio
import json
import logging
import re
import socket
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import aiohttp
import aiohttp.abc
import numpy as np
import requests
from scipy.stats import norm

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets")

log = logging.getLogger("feeds")

# ===================== CONSTANTS =====================================
GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_URL = "https://clob.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
DERIBIT_API = ("https://www.deribit.com/api/v2/public/"
               "get_book_summary_by_currency?currency=BTC&kind=option")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SECONDS_PER_YEAR = 31_536_000.0
_INST_RE = re.compile(r"^BTC-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-(C|P)$")
_MONTH = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


# ===================== DATA MODELS ===================================

@dataclass
class Bracket:
    question: str
    strike: int
    condition_id: str
    end_ts: float
    yes_tid: str
    no_tid: str
    event_title: str = ""


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    ts: float = 0.0

    def bb(self):
        return max((float(p) for p in self.bids), default=None)

    def ba(self):
        return min((float(p) for p in self.asks), default=None)

    def bb_size(self):
        b = self.bb()
        if b is None: return 0.0
        for p, s in self.bids.items():
            if abs(float(p) - b) < 0.0001: return s
        return 0.0

    def ba_size(self):
        a = self.ba()
        if a is None: return 0.0
        for p, s in self.asks.items():
            if abs(float(p) - a) < 0.0001: return s
        return 0.0

    def mid(self):
        b, a = self.bb(), self.ba()
        if b and a: return (b + a) / 2
        return b or a

    def spread(self):
        b, a = self.bb(), self.ba()
        return (a - b) if (b and a) else None


# ===================== WINDOWS DNS FIX ===============================

class _ManualResolver(aiohttp.abc.AbstractResolver):
    """Windows DNS workaround for aiohttp."""
    def __init__(self):
        self._cache = {}
    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host not in self._cache:
            self._cache[host] = socket.gethostbyname(host)
            log.info("DNS: %s -> %s", host, self._cache[host])
        return [{"hostname": host, "host": self._cache[host], "port": port,
                 "family": family, "proto": 0, "flags": socket.AI_NUMERICHOST}]
    async def close(self):
        pass


# ===================== DERIBIT ORACLE ================================

def _parse_deribit_expiry(s):
    day = int(s[:-5])
    return datetime(2000+int(s[-2:]), _MONTH[s[-5:-2]], day,
                    hour=8, tzinfo=timezone.utc)


class DeribitOracle:
    """Fetches Deribit IV and computes N(d2) probabilities."""

    def __init__(self):
        self.targets = {}   # {end_ts: set(strikes)}
        self.latest = {}    # {end_ts: {spot, t_years, probabilities}}
        self._running = False

    def update_targets(self, targets):
        self.targets = {ts: set(strikes) for ts, strikes in targets.items()}

    @staticmethod
    def _norm_exp(s):
        dt = _parse_deribit_expiry(s)
        return f"{dt.day}{dt.strftime('%b%y').upper()}"

    @staticmethod
    def _nd2(spot, strike, iv, t_years):
        if t_years <= 0 or iv <= 0:
            return 1.0 if spot >= strike else 0.0
        sqrt_t = np.sqrt(t_years)
        d1 = (np.log(spot/strike) + (iv**2/2)*t_years) / (iv*sqrt_t)
        return float(norm.cdf(d1 - iv*sqrt_t))

    async def _fetch(self, session):
        for attempt in range(3):
            try:
                async with session.get(DERIBIT_API,
                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    break
            except Exception as e:
                if attempt == 2: return None
                await asyncio.sleep(1.5 * (2**attempt))
        results = data.get("result", []) if data else []
        if not results: return None

        spot = 0.0
        for item in results:
            edp = item.get("estimated_delivery_price")
            if edp and edp > 0: spot = float(edp); break
        if spot <= 0: return None

        avail = set()
        for item in results:
            m = _INST_RE.match(item.get("instrument_name", ""))
            if m: avail.add(m.group(1))

        now = datetime.now(timezone.utc)
        future = []
        for s in avail:
            try:
                dt = _parse_deribit_expiry(s)
                if dt > now: future.append((dt, s))
            except: pass
        future.sort()

        out = {}
        for exp_ts, strikes in self.targets.items():
            poly_exp = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
            t_years = (poly_exp - now).total_seconds() / SECONDS_PER_YEAR

            active = None
            if future:
                poly_date = poly_exp.date()
                for dt, s in future:
                    if dt.date() >= poly_date:
                        active = self._norm_exp(s); break
                if not active:
                    active = self._norm_exp(future[0][1])

            probs = {}
            if active:
                for item in results:
                    m = _INST_RE.match(item.get("instrument_name", ""))
                    if not m or m.group(3) != "C" or m.group(1) != active:
                        continue
                    strike = int(m.group(2))
                    if strike not in strikes: continue
                    raw_iv = item.get("mark_iv")
                    if raw_iv is None or raw_iv <= 0: continue
                    iv = raw_iv / 100.0
                    p_yes = self._nd2(spot, strike, iv, t_years)
                    probs[strike] = {"mark_iv": round(iv, 6),
                                     "p_real_yes": round(p_yes, 6),
                                     "p_real_no": round(1.0-p_yes, 6)}

            out[exp_ts] = {"spot_price": round(spot, 2),
                           "t_years": round(t_years, 8),
                           "deribit_expiry": active,
                           "probabilities": probs}
        return out

    async def run(self, poll_sec=5):
        self._running = True
        ssl_ctx = ssl.create_default_context()
        conn = aiohttp.TCPConnector(resolver=_ManualResolver(),
                                     ssl=ssl_ctx, limit=5, force_close=True)
        async with aiohttp.ClientSession(connector=conn) as session:
            log.info("Oracle running (poll %ds)", poll_sec)
            while self._running:
                t0 = asyncio.get_event_loop().time()
                try:
                    r = await self._fetch(session)
                    if r:
                        self.latest = r
                        ns = sum(len(d["probabilities"]) for d in r.values())
                        log.info("Oracle tick | expiries=%d strikes=%d",
                                 len(r), ns)
                except Exception:
                    log.exception("Oracle tick failed")
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0, poll_sec - elapsed))

    def stop(self):
        self._running = False


# ===================== POLYMARKET FEED ===============================

def parse_strike(q):
    m = re.search(r'\$?([\d,]+)', q)
    return int(m.group(1).replace(",", "")) if m else 0


def _parse_event_markets(ev, seen, now):
    brackets = []
    tl = ev.get("title", "").lower()
    if "bitcoin price on" in tl: return brackets
    if "bitcoin" not in tl: return brackets
    for m in ev.get("markets", []):
        cid = m.get("conditionId", "")
        if cid in seen: continue
        seen.add(cid)
        end_str = m.get("endDate", "")
        if not end_str: continue
        try:
            end_ts = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")).timestamp()
        except: continue
        if end_ts <= now: continue
        q = m.get("question", "")
        if not re.search(r"bitcoin.*?above", q, re.IGNORECASE): continue
        strike = parse_strike(q)
        if strike == 0: continue
        tids = m.get("clobTokenIds", "[]")
        if isinstance(tids, str): tids = json.loads(tids)
        outs = m.get("outcomes", "[]")
        if isinstance(outs, str): outs = json.loads(outs)
        if len(tids) < 2: continue
        yt, nt = str(tids[0]), str(tids[1])
        for i, label in enumerate(outs):
            if str(label).lower() == "yes" and i < len(tids): yt = str(tids[i])
            elif str(label).lower() == "no" and i < len(tids): nt = str(tids[i])
        brackets.append(Bracket(question=q, strike=strike, condition_id=cid,
                                end_ts=end_ts, yes_tid=yt, no_tid=nt,
                                event_title=ev.get("title", "")))
    return brackets


def discover_bracket_events():
    now = time.time()
    brackets, seen = [], set()
    today = datetime.now(timezone.utc)
    for delta in range(0, 14):
        d = today + timedelta(days=delta)
        slug = f"bitcoin-above-on-{d.strftime('%B').lower()}-{d.day}"
        try:
            r = requests.get(GAMMA_URL, params={"slug": slug, "limit": "1"},
                             headers=UA, timeout=10)
            r.raise_for_status()
            for ev in r.json():
                brackets.extend(_parse_event_markets(ev, seen, now))
        except: continue
    for params in [
        {"tag": "bitcoin", "active": "true", "closed": "false", "limit": "50"},
        {"tag": "crypto", "active": "true", "closed": "false", "limit": "100"},
    ]:
        try:
            r = requests.get(GAMMA_URL, params=params, headers=UA, timeout=15)
            r.raise_for_status()
            for ev in r.json():
                brackets.extend(_parse_event_markets(ev, seen, now))
        except: continue
    brackets.sort(key=lambda b: (b.end_ts, b.strike))
    return brackets


class PolymarketFeed:
    """Live order books + BTC price."""

    def __init__(self):
        self.brackets = []
        self.books = {}
        self.btc_price = None
        self.btc_history = deque(maxlen=400)
        self._running = False
        self.poly_ok = False
        self.bnc_ok = False

    def get_book(self, tid): return self.books.get(tid)
    def get_btc_price(self): return self.btc_price
    def get_brackets(self):
        now = time.time()
        return [b for b in self.brackets if b.end_ts > now]

    def refresh(self):
        self.brackets = discover_bracket_events()
        log.info("Discovered %d brackets", len(self.brackets))
        return len(self.brackets) > 0

    async def run(self):
        self._running = True
        self.refresh()
        await asyncio.gather(
            self._ws_poly(), self._ws_bnc(),
            self._poll_books(), self._refresh_loop())

    async def _ws_poly(self):
        while self._running:
            if not self.brackets:
                await asyncio.sleep(5); continue
            tids = []
            for b in self.brackets: tids.extend([b.yes_tid, b.no_tid])
            try:
                async with websockets.connect(
                    POLY_WS, additional_headers={"User-Agent": "Mozilla/5.0"},
                    ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"assets_ids": tids, "type": "market"}))
                    self.poly_ok = True
                    async for msg in ws:
                        if not self._running: break
                        try:
                            raw = json.loads(msg)
                            items = raw if isinstance(raw, list) else [raw]
                            for item in items:
                                if isinstance(item, dict):
                                    self._handle_poly(item)
                        except json.JSONDecodeError: pass
            except Exception as e:
                log.warning("Poly WS: %s", e)
            self.poly_ok = False
            if self._running: await asyncio.sleep(5)

    def _handle_poly(self, d):
        et, aid = d.get("event_type", ""), d.get("asset_id", "")
        if not aid: return
        if et == "book":
            bk = Book()
            for b in d.get("bids", []):
                if isinstance(b, dict):
                    p, s = str(b.get("price","0")), float(b.get("size",0))
                    if float(p) > 0 and s > 0: bk.bids[p] = s
            for a in d.get("asks", []):
                if isinstance(a, dict):
                    p, s = str(a.get("price","0")), float(a.get("size",0))
                    if float(p) > 0 and s > 0: bk.asks[p] = s
            bk.ts = time.time()
            self.books[aid] = bk
        elif et == "price_change":
            bk = self.books.setdefault(aid, Book())
            for c in (d.get("changes") or d.get("price_changes") or []):
                if not isinstance(c, dict): continue
                side = c.get("side", "")
                price = str(c.get("price", "0"))
                size = float(c.get("size", 0))
                if size == 0:
                    if side == "buy": bk.bids.pop(price, None)
                    elif side == "sell": bk.asks.pop(price, None)
                else:
                    if side == "buy": bk.bids[price] = size
                    elif side == "sell": bk.asks[price] = size
            bk.ts = time.time()

    async def _ws_bnc(self):
        while self._running:
            try:
                async with websockets.connect(BINANCE_WS,
                        ping_interval=20, ping_timeout=10) as ws:
                    self.bnc_ok = True
                    async for msg in ws:
                        if not self._running: break
                        try:
                            d = json.loads(msg)
                            if d.get("e") == "trade":
                                self.btc_price = float(d["p"])
                                self.btc_history.append(
                                    {"p": self.btc_price,
                                     "t": float(d["T"])/1000})
                        except: pass
            except Exception as e:
                log.warning("Bnc WS: %s", e)
            self.bnc_ok = False
            if self._running: await asyncio.sleep(3)

    async def _poll_books(self):
        await asyncio.sleep(8)
        while self._running:
            for br in self.brackets:
                if not self._running: break
                for tid in [br.yes_tid, br.no_tid]:
                    try:
                        r = requests.get(f"{CLOB_URL}/book",
                                         params={"token_id": tid},
                                         headers=UA, timeout=5)
                        r.raise_for_status()
                        data = r.json()
                        bk = Book()
                        for b in data.get("bids", []):
                            p = str(b.get("price","0"))
                            s = float(b.get("size",0))
                            if float(p)>0 and s>0: bk.bids[p] = s
                        for a in data.get("asks", []):
                            p = str(a.get("price","0"))
                            s = float(a.get("size",0))
                            if float(p)>0 and s>0: bk.asks[p] = s
                        bk.ts = time.time()
                        self.books[tid] = bk
                    except: pass
                await asyncio.sleep(0.2)
            await asyncio.sleep(15)

    async def _refresh_loop(self):
        while self._running:
            await asyncio.sleep(120)
            if self._running: self.refresh()

    def stop(self):
        self._running = False
