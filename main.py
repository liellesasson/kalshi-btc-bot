import os, time, base64, httpx, asyncio, json, logging
from datetime import datetime
from collections import deque
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kalshi-bot")

# ─── CONFIG (set via Render environment variables) ────────────────────────────
KEY_ID        = os.getenv("KALSHI_KEY_ID", "")
KEY_PEM       = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MIN_EDGE      = float(os.getenv("MIN_EDGE", "3.0"))          # min pricing edge in cents
MAX_KELLY     = float(os.getenv("MAX_KELLY_FRAC", "0.15"))   # max fraction of balance per trade
MIN_BAL       = float(os.getenv("MIN_BALANCE_CENTS", "300")) # min balance to trade (cents)
MOM_THRESH    = float(os.getenv("MOMENTUM_THRESHOLD", "0.10"))
MIN_CONF      = int(os.getenv("MIN_CONFIDENCE", "65"))
BOT_ENABLED   = os.getenv("BOT_ENABLED", "true").lower() == "true"
LOOP_SECS     = int(os.getenv("LOOP_INTERVAL_SECS", "60"))

KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ─── SHARED STATE ─────────────────────────────────────────────────────────────
state = {
    "bot_enabled":      BOT_ENABLED,
    "btc_price":        None,
    "btc_history":      deque(maxlen=30),
    "eth_price":        None,
    "eth_history":      deque(maxlen=30),
    "last_signal":      None,
    "last_signal_time": None,
    "last_edge":        None,
    "skip_reason":      None,
    "trade_log":        deque(maxlen=200),
    "loop_count":       0,
    "last_error":       None,
    "balance":          None,   # in cents
    "markets":          [],
    "traded_tickers":   set(),  # tickers we already have open positions on
}

# ─── KALSHI AUTH ──────────────────────────────────────────────────────────────
def load_key(pem_str: str):
    try:
        return serialization.load_pem_private_key(
            pem_str.strip().encode(), password=None, backend=default_backend()
        )
    except Exception as e:
        raise ValueError(f"Invalid private key: {e}")

def sign(pk, ts, method, path):
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def kheaders(pk, key_id, method, path):
    ts = str(int(time.time() * 1000))
    return {
        "Content-Type":           "application/json",
        "KALSHI-ACCESS-KEY":      key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(pk, ts, method, path),
    }

# ─── KALSHI API ───────────────────────────────────────────────────────────────
async def kalshi_balance(client, pk, key_id):
    h = kheaders(pk, key_id, "GET", "/trade-api/v2/portfolio/balance")
    r = await client.get(KALSHI_BASE + "/portfolio/balance", headers=h, timeout=15)
    r.raise_for_status()
    data = r.json()
    bal = data.get("balance", data)
    if isinstance(bal, dict):
        return bal.get("balance") or bal.get("available_balance") or 0
    return int(bal) if bal else 0

async def kalshi_markets(client, series="KXBTC15M"):
    r = await client.get(f"{KALSHI_BASE}/markets",
        params={"series_ticker": series, "status": "open", "limit": 6}, timeout=15)
    r.raise_for_status()
    return r.json().get("markets") or []

async def kalshi_order(client, pk, key_id, ticker, side, count, price_cents):
    h = kheaders(pk, key_id, "POST", "/trade-api/v2/portfolio/orders")
    payload = {
        "action": "buy", "count": count, "side": side,
        "ticker": ticker, "type": "limit",
        "yes_price": price_cents if side == "yes" else 100 - price_cents,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    r = await client.post(KALSHI_BASE + "/portfolio/orders", headers=h, json=payload, timeout=15)
    if not r.is_success:
        raise Exception(f"Kalshi {r.status_code}: {r.text}")
    return r.json()

# ─── BTC PRICE ────────────────────────────────────────────────────────────────
async def fetch_btc(client):
    try:
        r = await client.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
        return float(r.json()["price"])
    except Exception:
        try:
            r = await client.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
            return float(r.json()["data"]["amount"])
        except Exception:
            return None

async def fetch_eth(client):
    try:
        r = await client.get(
            'https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT', timeout=8)
        return float(r.json()['price'])
    except Exception:
        try:
            r = await client.get(
                'https://api.coinbase.com/v2/prices/ETH-USD/spot', timeout=8)
            return float(r.json()['data']['amount'])
        except Exception:
            return None

# ─── MOMENTUM ─────────────────────────────────────────────────────────────────
def momentum(history: deque) -> dict:
    prices = list(history)
    if len(prices) < 6:
        return {"direction": "NEUTRAL", "pct_5m": 0.0, "pct_1m": 0.0, "strong": False}
    pct_5m = ((prices[-1] - prices[-6]) / prices[-6]) * 100
    pct_1m = ((prices[-1] - prices[-2]) / prices[-2]) * 100
    return {
        "direction": "UP" if pct_5m > 0 else "DOWN",
        "pct_5m":    round(pct_5m, 4),
        "pct_1m":    round(pct_1m, 4),
        "strong":    abs(pct_5m) >= MOM_THRESH,
    }

# ─── EDGE DETECTION (core arbitrage logic) ────────────────────────────────────
def detect_edge(market: dict, mom: dict) -> dict:
    """
    Finds pricing inefficiencies in the Kalshi market.

    Key insight: YES ask + NO ask should sum to ~100c (efficient market).
    When they diverge, or when market price disagrees with momentum,
    there is a tradeable edge.

    Returns:
        side       - "yes" or "no"
        edge_cents - how many cents of edge we have
        fair_value - our estimated fair probability
        entry_price- the price we'd pay
        ev         - expected value per dollar risked
        reason     - human readable explanation
    """
    # Handle both integer cent fields and dollar string fields from Kalshi API
    def to_cents(market, key, default):
        v = market.get(key)
        if v is not None:
            return int(v)
        # Fall back to dollar field (e.g. yes_ask_dollars -> "0.0600" -> 6)
        dv = market.get(key + "_dollars")
        if dv is not None:
            try:
                return round(float(dv) * 100)
            except Exception:
                pass
        return default

    yes_ask = to_cents(market, "yes_ask", 50)
    no_ask  = to_cents(market, "no_ask",  50)
    yes_bid = to_cents(market, "yes_bid", 49)
    no_bid  = to_cents(market, "no_bid",  49)

    # 0. LIQUIDITY FILTER — skip markets with wide bid-ask spreads or thin order books
    yes_spread = yes_ask - yes_bid
    no_spread  = no_ask  - no_bid
    if yes_spread > 15 or no_spread > 15:
        return {"side": None, "edge_cents": 0, "fair_value": 50,
                "entry_price": 50, "ev": 0,
                "reason": f"Low liquidity: YES spread={yes_spread}c NO spread={no_spread}c (max 15c)"}

    # Minimum ask size filter — skip if fewer than 10 contracts available
    def to_size(market, key):
        v = market.get(key)
        if v is not None:
            try: return float(v)
            except: pass
        return 0

    yes_ask_size = to_size(market, "yes_ask_size_fp")
    no_ask_size  = to_size(market, "no_ask_size_fp")
    MIN_ASK_SIZE = 10

    import math

    # 1. FAIR VALUE — normal CDF calibrated to BTC 15-min volatility
    # vol=57 means $57 is 1 std dev of BTC price movement in 15 minutes.
    # This calibration matches observed Kalshi market pricing:
    #   $117 above strike → ~98% YES (matches market)
    #   $50  above strike → ~81% YES
    #   at strike         → 50% YES
    strike    = market.get("floor_strike", 0)
    cur_price = mom.get("current_price", 0)
    distance  = cur_price - strike if (strike and cur_price) else 0

    # ETH has lower absolute vol (~$5-10 per 15min), use proportional vol
    is_eth = "KXETH" in market.get("ticker", "")
    vol = 5 if is_eth else 57  # 1-std-dev move in dollars

    def norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    z = distance / vol if vol else 0
    fair_yes_raw = norm_cdf(z) * 100

    # Small momentum nudge — max ±3 cents, momentum is weak signal
    pct = mom["pct_5m"]
    nudge = 0.0
    if abs(pct) >= 0.05:
        nudge = max(-3.0, min(3.0, pct / MOM_THRESH * 2.0))
    fair_yes = max(5.0, min(95.0, fair_yes_raw + nudge))
    fair_no  = 100.0 - fair_yes

    # 2. FIND MISPRICED SIDE
    yes_mispricing = fair_yes - yes_ask
    no_mispricing  = fair_no  - no_ask

    # Minimum distance filter — skip near-strike coin flips
    # BTC: must be >$80 from strike. ETH: must be >$8 from strike.
    min_dist = 8 if is_eth else 80
    if abs(distance) < min_dist:
        return {"side": None, "edge_cents": 0, "fair_value": round(fair_yes, 1),
                "entry_price": 50, "ev": 0,
                "reason": f"Too close to strike: {distance:+.0f} (min {min_dist})"}

    # Momentum conflict check — if 1m contradicts 5m, trend is reversing, skip
    pct_1m = mom.get("pct_1m", 0)
    mom_conflict = (pct_5m > 0 and pct_1m < -0.05) or (pct_5m < 0 and pct_1m > 0.05)
    if mom_conflict:
        return {"side": None, "edge_cents": 0, "fair_value": round(fair_yes, 1),
                "entry_price": 50, "ev": 0,
                "reason": f"Momentum conflict: 5m={pct_5m:+.3f}% vs 1m={pct_1m:+.3f}% — trend reversing"}

    # Only buy a side if our model agrees with the direction
    # (fair > 52 guard prevents betting against a strongly-priced market)
    if yes_mispricing >= no_mispricing and yes_mispricing > 0 and fair_yes > 52:
        side        = "yes"
        entry_price = yes_ask
        edge_cents  = yes_mispricing
        fair_value  = fair_yes
    elif no_mispricing > 0 and fair_no > 52:
        side        = "no"
        entry_price = no_ask
        edge_cents  = no_mispricing
        fair_value  = fair_no
    else:
        return {"side": None, "edge_cents": 0, "fair_value": round(fair_yes, 1),
                "entry_price": 50, "ev": 0,
                "reason": f"No edge: fair={fair_yes:.1f}c YES={yes_ask}c NO={no_ask}c dist={distance:+.0f}"}

    # 3b. LIQUIDITY CHECK on chosen side
    chosen_ask_size = yes_ask_size if side == "yes" else no_ask_size
    if chosen_ask_size < MIN_ASK_SIZE:
        return {"side": None, "edge_cents": 0, "fair_value": round(fair_yes, 1),
                "entry_price": entry_price, "ev": 0,
                "reason": f"Thin {side.upper()} book: ask size={chosen_ask_size:.0f} (min {MIN_ASK_SIZE})"}

    # 4. EXPECTED VALUE per contract
    # EV = (prob_win * profit_per_contract) - (prob_lose * cost_per_contract)
    prob_win  = fair_value / 100
    profit    = 100 - entry_price   # cents profit if correct
    loss      = entry_price          # cents lost if wrong
    ev        = (prob_win * profit) - ((1 - prob_win) * loss)

    reason = (f"Fair={fair_value:.1f}c market={entry_price}c "
              f"edge={edge_cents:.1f}c EV={ev:.1f}c "
              f"mom={mom['pct_5m']:+.3f}%")

    return {
        "side":        side,
        "edge_cents":  round(edge_cents, 2),
        "fair_value":  round(fair_value, 1),
        "entry_price": entry_price,
        "ev":          round(ev, 2),
        "reason":      reason,
    }

# ─── KELLY POSITION SIZING ────────────────────────────────────────────────────
def kelly_size(balance_cents: float, edge: dict) -> int:
    """
    Kelly criterion: bet fraction = edge / odds
    Scaled down by MAX_KELLY to be conservative.
    Returns number of contracts to buy.
    """
    if balance_cents < MIN_BAL:
        return 0

    entry = edge["entry_price"]
    if entry <= 0 or entry >= 100:
        return 0

    fair  = edge["fair_value"] / 100
    odds  = (100 - entry) / entry   # payout odds

    # Full Kelly fraction
    kelly_frac = (fair * odds - (1 - fair)) / odds
    kelly_frac = max(0, kelly_frac)

    # Apply max cap for safety
    bet_frac  = min(kelly_frac, MAX_KELLY)
    bet_cents = balance_cents * bet_frac

    # Each contract costs entry_price cents
    contracts = int(bet_cents / entry)
    return max(1, contracts)

# ─── AI SIGNAL ────────────────────────────────────────────────────────────────
async def ai_signal(client, price: float, mom: dict, edge: dict, market: dict) -> dict:
    if not ANTHROPIC_KEY:
        return {"direction": "NEUTRAL", "confidence": 0,
                "reason": "No Anthropic API key set", "agree": False}

    prompt = f"""You are an expert Kalshi BTC 15-min prediction market trader.

BTC: ${price:,.2f} | 1m: {mom['pct_1m']:+.3f}% | 5m: {mom['pct_5m']:+.3f}%
Market: {market.get('ticker')} | YES ask: {market.get('yes_ask')}c | NO ask: {market.get('no_ask')}c
Strike: ${market.get('floor_strike', '?'):,}
Our edge analysis: {edge['reason']}
We want to BUY {edge['side'].upper()} at {edge['entry_price']}c (fair value: {edge['fair_value']}c)

Do you agree this trade has positive expected value? Consider:
1. Is the momentum direction sustainable for 15 minutes?
2. Is the market price genuinely inefficient or is our model wrong?
3. Any mean-reversion risk?

Reply ONLY valid JSON:
{{"direction":"UP"|"DOWN"|"NEUTRAL","confidence":0-100,"agree":true|false,"reason":"one sentence"}}"""

    try:
        r = await client.post(ANTHROPIC_URL,
            headers={
                "Content-Type":    "application/json",
                "x-api-key":       ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 150,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        resp = r.json()
        if "error" in resp:
            raise Exception(f"API error: {resp['error'].get('message', resp['error'])}")
        text = resp["content"][0]["text"].replace("```json","").replace("```","").strip()
        result = json.loads(text)
        result["agree"] = bool(result.get("agree", False))
        return result
    except Exception as e:
        log.warning(f"AI signal failed: {e}")
        return {"direction": "NEUTRAL", "confidence": 0,
                "reason": f"AI unavailable: {e}", "agree": False}

# ─── KEEP ALIVE ───────────────────────────────────────────────────────────────
async def keep_alive():
    """Ping ourselves every 10 min so Render free tier never sleeps."""
    await asyncio.sleep(30)
    url = os.getenv("RENDER_EXTERNAL_URL", "http://localhost:10000")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get(f"{url}/health", timeout=10)
                log.info("Keep-alive ping sent")
            except Exception as e:
                log.warning(f"Keep-alive failed: {e}")
            await asyncio.sleep(600)

# ─── MAIN TRADING LOOP ────────────────────────────────────────────────────────
async def trading_loop():
    await asyncio.sleep(15)
    log.info("🚀 Trading loop started")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                state["loop_count"] += 1
                now = datetime.utcnow().strftime("%H:%M:%S")
                log.info(f"─── Loop #{state['loop_count']} at {now} ───")

                # 1. Fetch BTC + ETH
                price = await fetch_btc(client)
                if price:
                    state["btc_price"] = price
                    state["btc_history"].append(price)
                eth_price = await fetch_eth(client)
                if eth_price:
                    state["eth_price"] = eth_price
                    state["eth_history"].append(eth_price)

                # 2. Fetch markets every 2 loops (~3 min), or immediately if none loaded
                if True:  # fetch markets every loop to catch new market opens
                    await asyncio.sleep(5)
                    try:
                        btc_markets = await kalshi_markets(client, "KXBTC15M")
                        await asyncio.sleep(3)
                        try:
                            eth_markets = await kalshi_markets(client, "KXETH15M")
                        except Exception:
                            eth_markets = []
                        state["markets"] = btc_markets + eth_markets
                        log.info(f"Fetched {len(btc_markets)} BTC + {len(eth_markets)} ETH markets")
                    except Exception as e:
                        log.warning(f"Markets failed: {e}")
                markets = state["markets"]

                # 3. Skip checks
                if not state["bot_enabled"]:
                    state["skip_reason"] = "Bot disabled"
                    await asyncio.sleep(LOOP_SECS); continue

                if not KEY_ID or not KEY_PEM:
                    state["skip_reason"] = "No Kalshi credentials — set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY"
                    state["last_error"]  = "Missing credentials"
                    await asyncio.sleep(LOOP_SECS); continue

                if not markets or not price:
                    state["skip_reason"] = "Waiting for market/price data"
                    await asyncio.sleep(LOOP_SECS); continue

                # 4. Refresh balance
                await asyncio.sleep(2)
                try:
                    pk = load_key(KEY_PEM)
                    bal = await kalshi_balance(client, pk, KEY_ID)
                    state["balance"] = bal
                    log.info(f"Balance: {bal}c (${bal/100:.2f})")
                except Exception as e:
                    state["last_error"] = f"Balance fetch failed: {e}"
                    log.error(state["last_error"])
                    await asyncio.sleep(LOOP_SECS); continue

                if state["balance"] < MIN_BAL:
                    state["skip_reason"] = f"Balance too low: {state['balance']}c < {MIN_BAL}c minimum"
                    await asyncio.sleep(LOOP_SECS); continue

                # 5. Momentum — compute for both BTC and ETH
                btc_mom = momentum(state["btc_history"])
                eth_mom = momentum(state["eth_history"])
                log.info(f"BTC Momentum: {btc_mom['direction']} {btc_mom['pct_5m']:+.3f}% strong={btc_mom['strong']}")
                log.info(f"ETH Momentum: {eth_mom['direction']} {eth_mom['pct_5m']:+.3f}% strong={eth_mom['strong']}")

                # Check if any market has price far from strike (late-market high-prob setup)
                def price_far_from_strike(mkt, cur_price):
                    strike = mkt.get("floor_strike", 0)
                    if not strike or not cur_price:
                        return False
                    return abs(cur_price - strike) >= 100

                btc_far = any(price_far_from_strike(m, price)
                              for m in markets if "KXBTC" in m.get("ticker",""))
                eth_far = any(price_far_from_strike(m, state["eth_price"])
                              for m in markets if "KXETH" in m.get("ticker",""))

                if not btc_mom["strong"] and not eth_mom["strong"] and not btc_far and not eth_far:
                    state["skip_reason"] = f"Weak momentum: BTC {abs(btc_mom['pct_5m']):.3f}% ETH {abs(eth_mom['pct_5m']):.3f}% and no price far from strike"
                    await asyncio.sleep(LOOP_SECS); continue

                # 6. Find best edge across all markets (BTC + ETH)
                best_edge   = None
                best_market = None
                now_ts = time.time()
                for mkt in markets:
                    # Pick correct momentum for this market
                    is_eth = "KXETH" in mkt.get("ticker", "")
                    mom = eth_mom if is_eth else btc_mom
                    cur_price = state["eth_price"] if is_eth else price
                    far_from_strike = price_far_from_strike(mkt, cur_price)
                    if not mom["strong"] and not far_from_strike and MOM_THRESH > 0.001:
                        continue
                    # Skip markets closing in less than 3 minutes
                    close_time = mkt.get("close_time", "")
                    if close_time:
                        from datetime import timezone
                        try:
                            ct = datetime.fromisoformat(close_time.replace("Z","+00:00"))
                            secs_left = (ct - datetime.now(timezone.utc)).total_seconds()
                            if secs_left < 180:
                                log.info(f"Skipping {mkt['ticker']}: closes in {secs_left:.0f}s")
                                continue
                        except Exception:
                            pass
                    # Skip if we already have a position on this ticker
                    if mkt["ticker"] in state["traded_tickers"]:
                        log.info(f"Skipping {mkt['ticker']}: already have position")
                        continue
                    mom_with_price = {**mom, "current_price": cur_price}
                    e = detect_edge(mkt, mom_with_price)
                    if e["side"] and e["edge_cents"] > (best_edge["edge_cents"] if best_edge else 0):
                        best_edge   = e
                        best_market = mkt

                state["last_edge"] = best_edge

                if not best_edge or best_edge["edge_cents"] < MIN_EDGE:
                    edge_info = f"{best_edge['edge_cents']:.1f}c" if best_edge else "none"
                    state["skip_reason"] = f"Edge too small: {edge_info} < {MIN_EDGE}c minimum"
                    await asyncio.sleep(LOOP_SECS); continue

                log.info(f"Edge found: {best_edge['reason']}")

                # 7. AI confirmation
                sig = await ai_signal(client, price, mom, best_edge, best_market)
                state["last_signal"]      = sig
                state["last_signal_time"] = now
                log.info(f"AI: {sig['direction']} conf={sig['confidence']}% agree={sig['agree']} — {sig['reason']}")

                if sig["confidence"] < MIN_CONF:
                    state["skip_reason"] = f"AI confidence {sig['confidence']}% < {MIN_CONF}% minimum"
                    await asyncio.sleep(LOOP_SECS); continue

                if not sig["agree"]:
                    state["skip_reason"] = f"AI disagrees: {sig['reason']}"
                    await asyncio.sleep(LOOP_SECS); continue

                # 8. Kelly sizing
                contracts = kelly_size(state["balance"], best_edge)
                if contracts < 1:
                    state["skip_reason"] = "Kelly sizing returned 0 contracts"
                    await asyncio.sleep(LOOP_SECS); continue

                cost = contracts * best_edge["entry_price"]
                if cost > state["balance"] * 0.9:
                    contracts = max(1, int(state["balance"] * 0.5 / best_edge["entry_price"]))
                    cost = contracts * best_edge["entry_price"]

                # 9. ✅ PLACE TRADE — re-check distance hasn't shrunk below minimum
                is_eth_mkt = "KXETH" in best_market.get("ticker", "")
                cur_price_now = state["eth_price"] if is_eth_mkt else state["btc_price"]
                strike_now = best_market.get("floor_strike", 0)
                dist_now = abs(cur_price_now - strike_now) if (cur_price_now and strike_now) else 0
                min_dist_now = 8 if is_eth_mkt else 80
                if dist_now < min_dist_now:
                    state["skip_reason"] = f"Distance shrunk before fill: {dist_now:.0f} < {min_dist_now} — aborting"
                    log.warning(state["skip_reason"])
                    state["traded_tickers"].discard(best_market["ticker"])
                    await asyncio.sleep(LOOP_SECS); continue

                log.info(f"🔥 TRADING: {contracts}x {best_edge['side'].upper()} @ "
                         f"{best_edge['entry_price']}c on {best_market['ticker']} "
                         f"(edge={best_edge['edge_cents']}c EV={best_edge['ev']}c)")
                await asyncio.sleep(3)  # rate limit buffer before order
                try:
                    # Add 1c slippage to ensure order fills
                    fill_price = min(99, best_edge["entry_price"] + 1)
                    result = await kalshi_order(
                        client, pk, KEY_ID,
                        best_market["ticker"],
                        best_edge["side"],
                        contracts,
                        fill_price,
                    )
                    entry = {
                        "time":       now,
                        "ticker":     best_market["ticker"],
                        "side":       best_edge["side"],
                        "price":      best_edge["entry_price"],
                        "qty":        contracts,
                        "cost":       cost,
                        "edge":       best_edge["edge_cents"],
                        "ev":         best_edge["ev"],
                        "fair":       best_edge["fair_value"],
                        "conf":       sig["confidence"],
                        "mom_5m":     mom["pct_5m"],
                        "status":     "PLACED",
                        "reason":     sig["reason"],
                        "order_id":   result.get("order", {}).get("order_id", "?"),
                    }
                    state["trade_log"].appendleft(entry)
                    state["traded_tickers"].add(best_market["ticker"])
                    state["skip_reason"] = None
                    state["last_error"]  = None
                    log.info(f"✅ Order placed: {entry['order_id']}")
                except Exception as e:
                    state["last_error"] = f"Order failed: {e}"
                    state["trade_log"].appendleft({
                        "time": now, "ticker": best_market["ticker"],
                        "side": best_edge["side"], "price": best_edge["entry_price"],
                        "qty": contracts, "cost": cost, "edge": best_edge["edge_cents"],
                        "ev": best_edge["ev"], "fair": best_edge["fair_value"],
                        "conf": sig["confidence"], "mom_5m": mom["pct_5m"],
                        "status": f"FAILED: {e}", "reason": sig["reason"],
                    })
                    log.error(state["last_error"])
                    # Remove ticker so bot can retry when liquidity appears
                    state["traded_tickers"].discard(best_market["ticker"])

            except Exception as e:
                state["last_error"] = str(e)
                log.error(f"Loop error: {e}")

            await asyncio.sleep(LOOP_SECS)

# ─── FASTAPI ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(trading_loop())
    asyncio.create_task(keep_alive())
    yield
    task.cancel()

app = FastAPI(title="Kalshi BTC Arb Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"status": "ok", "loop": state["loop_count"], "bot": state["bot_enabled"]}

@app.get("/api/status")
def get_status():
    return {
        "bot_enabled":      state["bot_enabled"],
        "btc_price":        state["btc_price"],
        "eth_price":        state["eth_price"],
        "balance_cents":    state["balance"],
        "balance_dollars":  round(state["balance"] / 100, 2) if state["balance"] else None,
        "last_signal":      state["last_signal"],
        "last_signal_time": state["last_signal_time"],
        "last_edge":        state["last_edge"],
        "skip_reason":      state["skip_reason"],
        "markets":          state["markets"],
        "trade_count":      len(state["trade_log"]),
        "last_error":       state["last_error"],
        "loop_count":       state["loop_count"],
        "config": {
            "min_edge":           MIN_EDGE,
            "max_kelly_frac":     MAX_KELLY,
            "min_balance_cents":  MIN_BAL,
            "momentum_threshold": MOM_THRESH,
            "min_confidence":     MIN_CONF,
            "loop_secs":          LOOP_SECS,
        },
    }

@app.get("/api/trades")
def get_trades():
    return {"trades": list(state["trade_log"])}

@app.post("/api/bot/enable")
def enable_bot():
    state["bot_enabled"] = True
    return {"bot_enabled": True}

@app.post("/api/bot/disable")
def disable_bot():
    state["bot_enabled"] = False
    return {"bot_enabled": False}

class Auth(BaseModel):
    key_id: str
    private_key_pem: str

class OrderReq(Auth):
    ticker: str; side: str; count: int; price: int

@app.post("/api/balance")
async def api_balance(b: Auth):
    try:    pk = load_key(b.private_key_pem)
    except ValueError as e: raise HTTPException(400, str(e))
    h = kheaders(pk, b.key_id, "GET", "/trade-api/v2/portfolio/balance")
    async with httpx.AsyncClient() as c:
        r = await c.get(KALSHI_BASE + "/portfolio/balance", headers=h, timeout=15)
    if r.status_code != 200: raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/markets")
async def api_markets():
    # Return cached markets from bot state instead of hitting Kalshi directly
    return {"markets": state["markets"]}

@app.post("/api/order")
async def api_order(b: OrderReq):
    try:    pk = load_key(b.private_key_pem)
    except ValueError as e: raise HTTPException(400, str(e))
    h = kheaders(pk, b.key_id, "POST", "/trade-api/v2/portfolio/orders")
    payload = {
        "action": "buy", "count": b.count, "side": b.side, "ticker": b.ticker,
        "type": "limit",
        "yes_price": b.price if b.side == "yes" else 100 - b.price,
        "no_price":  b.price if b.side == "no"  else 100 - b.price,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(KALSHI_BASE + "/portfolio/orders", headers=h, json=payload, timeout=15)
    if r.status_code not in (200, 201): raise HTTPException(r.status_code, r.text)
    state["trade_log"].appendleft({
        "time": datetime.utcnow().strftime("%H:%M:%S"), "ticker": b.ticker,
        "side": b.side, "price": b.price, "qty": b.count,
        "cost": b.count * b.price, "status": "MANUAL",
    })
    return r.json()

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BTC Arb Bot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03050a;color:#dce8f0;font-family:'Syne',sans-serif;min-height:100vh;padding:24px 20px}
.app{max-width:960px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
.hdr{display:flex;justify-content:space-between;align-items:center;padding-bottom:16px;border-bottom:1px solid #1a2d40}
.logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,#f7b731,#ff6b35);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;font-family:'IBM Plex Mono',monospace;font-size:11px}
.pill-off{background:rgba(74,96,112,.08);border:1px solid #1a2d40;color:#4a6070}
.pill-on{background:rgba(0,230,118,.08);border:1px solid rgba(0,230,118,.2);color:#00e676}
.dot{width:6px;height:6px;border-radius:50%}
.dot-on{background:#00e676;box-shadow:0 0 6px #00e676;animation:blink 1.2s infinite}
.dot-off{background:#4a6070}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:640px){.grid4{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}}
.card{background:#080d14;border:1px solid #1a2d40;border-radius:12px;padding:16px}
.card-lbl{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:6px}
.card-val{font-family:'IBM Plex Mono',monospace;font-size:24px;font-weight:700;color:#f7b731}
.card-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:3px}
.edge-box{background:#080d14;border:2px solid #1a2d40;border-radius:12px;padding:20px;transition:border-color .3s}
.edge-box.has-edge{border-color:#f7b731}
.edge-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.edge-lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase}
.edge-badge{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px;background:rgba(247,183,49,.15);color:#f7b731}
.edge-val{font-family:'Syne',sans-serif;font-size:36px;font-weight:800;color:#4a6070;margin-bottom:6px}
.edge-val.active{color:#f7b731}
.edge-reason{font-family:'IBM Plex Mono',monospace;font-size:12px;color:#4a6070;line-height:1.6;margin-bottom:12px}
.edge-meta{display:flex;gap:18px;flex-wrap:wrap}
.meta-l{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1px;text-transform:uppercase}
.meta-v{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:700}
.sig-box{background:#080d14;border:1px solid #1a2d40;border-radius:12px;padding:18px}
.sig-box.up{border-color:#00e676;background:rgba(0,230,118,.03)}
.sig-box.down{border-color:#ff1744;background:rgba(255,23,68,.03)}
.sig-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.sig-lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase}
.sig-dir{font-family:'Syne',sans-serif;font-size:32px;font-weight:800;color:#4a6070;margin-bottom:5px}
.sig-reason{font-size:13px;color:#4a6070;line-height:1.5}
.conf-badge{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px}
.c-up{background:rgba(0,230,118,.15);color:#00e676}
.c-dn{background:rgba(255,23,68,.15);color:#ff1744}
.c-neu{background:rgba(74,96,112,.2);color:#4a6070}
.bot-ctrl{display:flex;align-items:center;justify-content:space-between;background:#080d14;border:1px solid #1a2d40;border-radius:12px;padding:16px 20px}
.bot-lbl{font-size:15px;font-weight:700}
.bot-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:3px}
.btn-on{background:linear-gradient(135deg,#00c853,#00e676);color:#001a0a;border:none;padding:9px 20px;border-radius:7px;font-family:'Syne',sans-serif;font-size:13px;font-weight:800;cursor:pointer}
.btn-off{background:rgba(255,23,68,.1);color:#ff1744;border:1px solid rgba(255,23,68,.3);padding:9px 20px;border-radius:7px;font-family:'Syne',sans-serif;font-size:13px;font-weight:800;cursor:pointer}
.skip-box{background:rgba(247,183,49,.04);border:1px solid rgba(247,183,49,.15);border-radius:10px;padding:12px 16px;font-family:'IBM Plex Mono',monospace;font-size:12px;color:rgba(247,183,49,.8);line-height:1.5}
.cfg-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
@media(max-width:600px){.cfg-grid{grid-template-columns:1fr 1fr}}
.cfg-card{background:#0c1520;border:1px solid #111d2a;border-radius:9px;padding:11px}
.cfg-lbl{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:4px}
.cfg-val{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:700;color:#f7b731}
.log-wrap{background:#080d14;border:1px solid #1a2d40;border-radius:12px;overflow:hidden}
.log-hdr{padding:12px 16px;border-bottom:1px solid #111d2a;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;display:flex;justify-content:space-between}
.log-empty{padding:24px;text-align:center;color:#4a6070;font-family:'IBM Plex Mono',monospace;font-size:12px}
.log-row{padding:10px 16px;border-bottom:1px solid rgba(17,29,42,.5);font-family:'IBM Plex Mono',monospace;font-size:11px}
.log-row:last-child{border-bottom:none}
.log-main{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:3px}
.log-detail{font-size:10px;color:#4a6070;line-height:1.4}
.ls-y{background:rgba(0,230,118,.12);color:#00e676;padding:2px 6px;border-radius:3px;font-weight:700}
.ls-n{background:rgba(255,23,68,.12);color:#ff1744;padding:2px 6px;border-radius:3px;font-weight:700}
.ls-f{background:rgba(255,152,0,.1);color:#ff9800;padding:2px 6px;border-radius:3px;font-size:10px}
.sec{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:9px}
.sec::after{content:'';flex:1;height:1px;background:#1a2d40}
.up{color:#00e676}.dn{color:#ff1744}.acc{color:#f7b731}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #1a2d40;border-top-color:#f7b731;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="app">

<div class="hdr">
  <div>
    <div class="logo">BTC ARB BOT</div>
    <div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:2px">edge detection · kelly sizing · ai confirmation · 24/7</div>
  </div>
  <div style="display:flex;gap:10px;align-items:center">
    <span id="bal-lbl" style="font-family:'IBM Plex Mono',monospace;font-size:13px;color:#f7b731;font-weight:700"></span>
    <span id="bot-pill" class="pill pill-off"><span id="bot-dot" class="dot dot-off"></span><span id="bot-txt">LOADING</span></span>
  </div>
</div>

<!-- STATS -->
<div class="grid4">
  <div class="card">
    <div class="card-lbl">BTC Price</div>
    <div class="card-val" id="s-btc"><span class="spin"></span></div>
    <div class="card-sub" id="s-btc-mom">momentum: —</div>
  </div>
  <div class="card">
    <div class="card-lbl">ETH Price</div>
    <div class="card-val" id="s-eth"><span class="spin"></span></div>
    <div class="card-sub" id="s-eth-mom">momentum: —</div>
  </div>
  <div class="card">
    <div class="card-lbl">Balance</div>
    <div class="card-val" id="s-bal">—</div>
    <div class="card-sub" id="s-bal-c">— cents</div>
  </div>
  <div class="card">
    <div class="card-lbl">Trades · Loop</div>
    <div class="card-val" id="s-trades">0</div>
    <div class="card-sub" id="s-loop">loop #0</div>
  </div>
</div>

<!-- BOT CONTROL -->
<div class="bot-ctrl">
  <div>
    <div class="bot-lbl">🤖 Arbitrage Bot</div>
    <div class="bot-sub" id="bot-sub">Checking status...</div>
  </div>
  <div style="display:flex;gap:8px">
    <button class="btn-on" onclick="setBot(true)">Enable</button>
    <button class="btn-off" onclick="setBot(false)">Disable</button>
  </div>
</div>

<!-- SKIP REASON -->
<div class="skip-box" id="skip-box" style="display:none">
  ⏸ <span id="skip-reason"></span>
</div>

<!-- EDGE ANALYSIS -->
<div class="sec">Live Edge Analysis</div>
<div class="edge-box" id="edge-box">
  <div class="edge-hdr">
    <div class="edge-lbl">Pricing Edge</div>
    <span id="edge-badge" style="display:none" class="edge-badge"></span>
  </div>
  <div class="edge-val" id="edge-val">No edge detected</div>
  <div class="edge-reason" id="edge-reason">Scanning markets for pricing inefficiencies...</div>
  <div class="edge-meta">
    <div><div class="meta-l">Side</div><div class="meta-v" id="edge-side" style="color:#4a6070">—</div></div>
    <div><div class="meta-l">Entry</div><div class="meta-v acc" id="edge-entry">—</div></div>
    <div><div class="meta-l">Fair Value</div><div class="meta-v" id="edge-fair" style="color:#dce8f0">—</div></div>
    <div><div class="meta-l">EV / contract</div><div class="meta-v" id="edge-ev" style="color:#00e676">—</div></div>
  </div>
</div>

<!-- AI SIGNAL -->
<div class="sec">AI Confirmation</div>
<div class="sig-box" id="sig-box">
  <div class="sig-hdr">
    <div class="sig-lbl">Signal · <span id="sig-time">pending</span></div>
    <span id="sig-badge" class="conf-badge c-neu">—</span>
  </div>
  <div class="sig-dir" id="sig-dir">─ NEUTRAL</div>
  <div class="sig-reason" id="sig-reason">Waiting for edge to analyze...</div>
</div>

<!-- CONFIG -->
<div class="sec">Configuration</div>
<div class="cfg-grid">
  <div class="cfg-card"><div class="cfg-lbl">Min Edge</div><div class="cfg-val" id="cfg-edge">—</div></div>
  <div class="cfg-card"><div class="cfg-lbl">Max Kelly %</div><div class="cfg-val" id="cfg-kelly">—</div></div>
  <div class="cfg-card"><div class="cfg-lbl">Min Confidence</div><div class="cfg-val" id="cfg-conf">—</div></div>
  <div class="cfg-card"><div class="cfg-lbl">Min Balance</div><div class="cfg-val" id="cfg-minbal">—</div></div>
  <div class="cfg-card"><div class="cfg-lbl">Mom Threshold</div><div class="cfg-val" id="cfg-mom">—</div></div>
  <div class="cfg-card"><div class="cfg-lbl">Loop Interval</div><div class="cfg-val" id="cfg-loop">—</div></div>
</div>
<div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070">
  Tune via Render env vars: MIN_EDGE, MAX_KELLY_FRAC, MIN_CONFIDENCE, MOMENTUM_THRESHOLD, MIN_BALANCE_CENTS, LOOP_INTERVAL_SECS
</div>

<!-- TRADE LOG -->
<div class="sec">Trade Log</div>
<div class="log-wrap">
  <div class="log-hdr"><span>Trades (<span id="log-cnt">0</span>)</span><span id="s-loop2" style="color:#4a6070;font-size:10px"></span></div>
  <div id="log-body"><div class="log-empty">No trades yet — bot is scanning for edges every 60s</div></div>
</div>

</div>
<script>
async function poll(){
  try{
    const [sr, tr] = await Promise.all([fetch("/api/status"), fetch("/api/trades")]);
    const s = await sr.json();
    const t = await tr.json();

    // pill
    const on = s.bot_enabled;
    document.getElementById("bot-pill").className = "pill " + (on?"pill-on":"pill-off");
    document.getElementById("bot-dot").className  = "dot " + (on?"dot-on":"dot-off");
    document.getElementById("bot-txt").textContent = on?"BOT LIVE":"BOT OFF";

    // balance
    if(s.balance_dollars!=null){
      document.getElementById("bal-lbl").textContent = "$"+s.balance_dollars.toFixed(2);
      document.getElementById("s-bal").textContent = "$"+s.balance_dollars.toFixed(2);
      document.getElementById("s-bal-c").textContent = s.balance_cents+"c";
    }

    // btc + eth prices
    if(s.btc_price) document.getElementById("s-btc").textContent = "$"+Math.round(s.btc_price).toLocaleString();
    if(s.eth_price) document.getElementById("s-eth").textContent = "$"+Math.round(s.eth_price).toLocaleString();

    // stats
    document.getElementById("s-trades").textContent = s.trade_count;
    document.getElementById("s-loop").textContent   = "loop #"+s.loop_count;
    document.getElementById("s-loop2").textContent  = "loop #"+s.loop_count;

    // bot sub
    document.getElementById("bot-sub").textContent = on
      ? `Running every ${s.config?.loop_secs||60}s · Edge→Momentum→AI→Kelly sizing`
      : "Paused — click Enable to start auto-trading";

    // skip reason
    const skipEl = document.getElementById("skip-box");
    if(s.skip_reason){
      skipEl.style.display="";
      document.getElementById("skip-reason").textContent = s.skip_reason;
    } else { skipEl.style.display="none"; }

    // edge
    const e = s.last_edge;
    const edgeBox = document.getElementById("edge-box");
    if(e && e.side){
      edgeBox.className = "edge-box has-edge";
      document.getElementById("edge-val").className = "edge-val active";
      document.getElementById("edge-val").textContent = e.edge_cents.toFixed(1)+"¢ EDGE";
      document.getElementById("edge-reason").textContent = e.reason;
      document.getElementById("edge-side").textContent  = e.side.toUpperCase();
      document.getElementById("edge-side").style.color  = e.side==="yes"?"#00e676":"#ff1744";
      document.getElementById("edge-entry").textContent = e.entry_price+"¢";
      document.getElementById("edge-fair").textContent  = e.fair_value+"¢";
      document.getElementById("edge-ev").textContent    = (e.ev>0?"+":"")+e.ev.toFixed(1)+"¢";
      document.getElementById("edge-ev").style.color    = e.ev>0?"#00e676":"#ff1744";
      const badge = document.getElementById("edge-badge");
      badge.textContent = "EV "+e.ev.toFixed(1)+"¢"; badge.style.display="";
    } else {
      edgeBox.className = "edge-box";
      document.getElementById("edge-val").className = "edge-val";
      document.getElementById("edge-val").textContent = "No edge detected";
      if(e) document.getElementById("edge-reason").textContent = e.reason||"Scanning...";
    }

    // signal
    const sig = s.last_signal;
    if(sig){
      const dir = sig.direction;
      const box = document.getElementById("sig-box");
      box.className = "sig-box"+(dir==="UP"?" up":dir==="DOWN"?" down":"");
      document.getElementById("sig-time").textContent = s.last_signal_time||"—";
      document.getElementById("sig-dir").textContent  = dir==="UP"?"▲ UP":dir==="DOWN"?"▼ DOWN":"─ NEUTRAL";
      document.getElementById("sig-dir").style.color  = dir==="UP"?"#00e676":dir==="DOWN"?"#ff1744":"#4a6070";
      document.getElementById("sig-reason").textContent = sig.reason||"—";
      document.getElementById("sig-reason").style.color = "#dce8f0";
      const badge = document.getElementById("sig-badge");
      badge.textContent = sig.confidence+"% · "+(sig.agree?"✓ AGREES":"✗ DISAGREES");
      badge.className = "conf-badge "+(dir==="UP"?"c-up":dir==="DOWN"?"c-dn":"c-neu");
    }

    // config
    if(s.config){
      document.getElementById("cfg-edge").textContent   = s.config.min_edge+"¢";
      document.getElementById("cfg-kelly").textContent  = (s.config.max_kelly_frac*100)+"%";
      document.getElementById("cfg-conf").textContent   = s.config.min_confidence+"%";
      document.getElementById("cfg-minbal").textContent = "$"+(s.config.min_balance_cents/100).toFixed(2);
      document.getElementById("cfg-mom").textContent    = s.config.momentum_threshold+"%";
      document.getElementById("cfg-loop").textContent   = s.config.loop_secs+"s";
    }

    // trades
    const trades = t.trades||[];
    document.getElementById("log-cnt").textContent = trades.length;
    if(trades.length){
      document.getElementById("log-body").innerHTML = trades.map(t=>{
        const failed = t.status&&t.status.startsWith("FAILED");
        return `<div class="log-row">
          <div class="log-main">
            <span style="color:#4a6070;font-size:10px">${t.time}</span>
            <span style="flex:1">${t.ticker}</span>
            <span style="font-size:10px;padding:2px 5px;border-radius:3px;background:${t.ticker&&t.ticker.includes('ETH')?'rgba(98,126,234,.15)':'rgba(247,183,49,.1)'};color:${t.ticker&&t.ticker.includes('ETH')?'#627eea':'#f7b731'}">${t.ticker&&t.ticker.includes('ETH')?'ETH':'BTC'}</span>
            <span class="${t.side==="yes"?"ls-y":"ls-n"}">${t.side==="yes"?"▲ YES":"▼ NO"}</span>
            <span style="font-weight:700">${t.qty}x @ ${t.price}¢</span>
            <span style="color:${failed?"#ff9800":t.status==="MANUAL"?"#4a6070":"#00e676"}">${t.status}</span>
          </div>
          <div class="log-detail">
            ${t.edge?`edge=${t.edge}¢ · `:""}${t.ev?`EV=${t.ev}¢ · `:""}${t.fair?`fair=${t.fair}¢ · `:""}${t.conf?`AI=${t.conf}% · `:""}${t.mom_5m!==undefined?`mom=${t.mom_5m>0?"+":""}${t.mom_5m}%`:""}
            ${t.reason?`<br/>${t.reason}`:""}
          </div>
        </div>`;
      }).join("");
    }
  } catch(e){ console.error(e); }
}

async function setBot(on){
  await fetch(on?"/api/bot/enable":"/api/bot/disable",{method:"POST"});
  poll();
}

poll();
setInterval(poll, 5000);
</script>
</body>
</html>""")
