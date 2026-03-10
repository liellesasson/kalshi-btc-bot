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

# ─── CONFIG (set via Render environment variables) ───────────────────────────
KEY_ID   = os.getenv("KALSHI_KEY_ID", "")
KEY_PEM  = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
MIN_CONF = int(os.getenv("MIN_CONFIDENCE", "70"))       # AI confidence threshold
MAX_BET  = int(os.getenv("MAX_BET_CENTS", "500"))       # max bet in cents ($5.00)
MOM_THRESHOLD = float(os.getenv("MOMENTUM_THRESHOLD", "0.15"))  # % move to confirm trade
BOT_ENABLED = os.getenv("BOT_ENABLED", "true").lower() == "true"
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL_SECS", "60"))

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# ─── SHARED STATE ────────────────────────────────────────────────────────────
state = {
    "bot_enabled": BOT_ENABLED,
    "btc_price": None,
    "btc_history": deque(maxlen=30),  # last 30 prices (5 min at 10s intervals)
    "last_signal": None,
    "last_signal_time": None,
    "trade_log": deque(maxlen=100),
    "loop_count": 0,
    "last_error": None,
    "balance": None,
    "markets": [],
}

# ─── KALSHI AUTH ─────────────────────────────────────────────────────────────
def load_key(pem_str: str):
    try:
        return serialization.load_pem_private_key(
            pem_str.strip().encode(), password=None, backend=default_backend()
        )
    except Exception as e:
        raise ValueError(f"Invalid private key: {e}")

def sign(pk, ts: str, method: str, path: str) -> str:
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                    salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def kheaders(pk, key_id: str, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(pk, ts, method, path),
    }

# ─── KALSHI API ───────────────────────────────────────────────────────────────
async def kalshi_balance(client, pk, key_id):
    h = kheaders(pk, key_id, "GET", "/trade-api/v2/portfolio/balance")
    r = await client.get(KALSHI_BASE + "/portfolio/balance", headers=h, timeout=15)
    r.raise_for_status()
    return r.json()

async def kalshi_markets(client):
    r = await client.get(f"{KALSHI_BASE}/markets",
        params={"series_ticker": "KXBTC15M", "status": "open", "limit": 4}, timeout=15)
    r.raise_for_status()
    return (r.json().get("markets") or [])

async def kalshi_order(client, pk, key_id, ticker, side, count, price_cents):
    h = kheaders(pk, key_id, "POST", "/trade-api/v2/portfolio/orders")
    payload = {
        "action": "buy", "count": count, "side": side,
        "ticker": ticker, "type": "limit",
        "yes_price": price_cents if side == "yes" else 100 - price_cents,
        "no_price":  price_cents if side == "no"  else 100 - price_cents,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    r = await client.post(KALSHI_BASE + "/portfolio/orders", headers=h, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── BTC PRICE ────────────────────────────────────────────────────────────────
async def fetch_btc(client) -> float | None:
    try:
        r = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=8)
        return float(r.json()["price"])
    except Exception:
        try:
            r = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=8)
            return float(r.json()["data"]["amount"])
        except Exception:
            return None

# ─── MOMENTUM CHECK ───────────────────────────────────────────────────────────
def momentum_signal(history: deque) -> dict:
    """Returns direction + % change over last 5 readings (≈5 min)."""
    prices = list(history)
    if len(prices) < 6:
        return {"direction": "NEUTRAL", "pct_5m": 0.0, "pct_1m": 0.0, "strong": False}
    pct_5m = ((prices[-1] - prices[-6]) / prices[-6]) * 100
    pct_1m = ((prices[-1] - prices[-2]) / prices[-2]) * 100
    direction = "UP" if pct_5m > 0 else "DOWN"
    strong = abs(pct_5m) >= MOM_THRESHOLD
    return {"direction": direction, "pct_5m": round(pct_5m, 4),
            "pct_1m": round(pct_1m, 4), "strong": strong}

# ─── AI SIGNAL ────────────────────────────────────────────────────────────────
async def ai_signal(client, price: float, mom: dict, markets: list) -> dict:
    mkt_str = "\n".join(
        f"{m['ticker']}: YES={m.get('yes_price','?')}¢ NO={m.get('no_price','?')}¢ Vol={m.get('volume','?')}"
        for m in markets[:4]
    ) or "unavailable"

    prompt = f"""You are an elite Kalshi BTC 15-minute prediction market trader.

BTC Price: ${price:,.2f}
1-min change: {mom['pct_1m']:+.3f}%
5-min change: {mom['pct_5m']:+.3f}%
Momentum direction: {mom['direction']} ({'STRONG' if mom['strong'] else 'WEAK'})

Open KXBTC15M markets:
{mkt_str}

Analyze: momentum strength, mean reversion probability, market pricing efficiency.
Reply ONLY with valid JSON (no markdown, no explanation):
{{"direction":"UP","confidence":75,"side":"yes","reason":"One clear sentence","recommended_price":55,"risk":"MEDIUM"}}"""

    try:
        r = await client.post(ANTHROPIC_URL,
            headers={"Content-Type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20)
        text = r.json()["content"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log.warning(f"AI signal failed: {e}")
        return {"direction": "NEUTRAL", "confidence": 0, "side": "yes",
                "reason": f"AI unavailable: {e}", "recommended_price": 50, "risk": "HIGH"}

# ─── MAIN TRADING LOOP ────────────────────────────────────────────────────────
async def trading_loop():
    await asyncio.sleep(5)  # let server start
    log.info("Trading loop started")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                state["loop_count"] += 1
                now = datetime.utcnow().strftime("%H:%M:%S")

                # 1. Fetch BTC price
                price = await fetch_btc(client)
                if price:
                    state["btc_price"] = price
                    state["btc_history"].append(price)
                    log.info(f"[{now}] BTC: ${price:,.2f}")

                # 2. Fetch open markets
                try:
                    markets = await kalshi_markets(client)
                    state["markets"] = markets
                except Exception as e:
                    log.warning(f"Markets fetch failed: {e}")
                    markets = state["markets"]

                # 3. Skip if bot disabled or no credentials
                if not state["bot_enabled"]:
                    log.info("Bot disabled, skipping trade logic")
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                if not KEY_ID or not KEY_PEM:
                    log.warning("No credentials set — set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY env vars")
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                if not markets:
                    log.warning("No open markets found")
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                if not price:
                    log.warning("No BTC price")
                    await asyncio.sleep(LOOP_INTERVAL)
                    continue

                # 4. Momentum check
                mom = momentum_signal(state["btc_history"])
                log.info(f"Momentum: {mom['direction']} {mom['pct_5m']:+.3f}% 5m | strong={mom['strong']}")

                # 5. AI signal
                sig = await ai_signal(client, price, mom, markets)
                state["last_signal"] = sig
                state["last_signal_time"] = now
                log.info(f"AI signal: {sig['direction']} conf={sig['confidence']}% price={sig.get('recommended_price')}¢")

                # 6. TRADE DECISION: both AI + momentum must agree
                ai_dir = sig["direction"]
                mom_dir = mom["direction"]
                conf = sig["confidence"]
                same_direction = ai_dir == mom_dir
                conf_ok = conf >= MIN_CONF
                mom_ok = mom["strong"]

                if ai_dir == "NEUTRAL":
                    log.info("AI says NEUTRAL — skipping")
                elif not same_direction:
                    log.info(f"AI ({ai_dir}) and momentum ({mom_dir}) disagree — skipping")
                elif not conf_ok:
                    log.info(f"Confidence {conf}% < {MIN_CONF}% threshold — skipping")
                elif not mom_ok:
                    log.info(f"Momentum {abs(mom['pct_5m']):.3f}% < {MOM_THRESHOLD}% threshold — skipping")
                else:
                    # ✅ ALL CONDITIONS MET — PLACE TRADE
                    mkt = markets[0]
                    side = sig["side"]
                    rec_price = sig.get("recommended_price", 50)
                    count = max(1, int((MAX_BET / 100) / (rec_price / 100)))

                    log.info(f"🔥 TRADING: {count}x {side.upper()} @ {rec_price}¢ on {mkt['ticker']}")
                    try:
                        pk = load_key(KEY_PEM)
                        # Refresh balance
                        try:
                            bal = await kalshi_balance(client, pk, KEY_ID)
                            state["balance"] = (bal.get("balance", {}).get("balance") or 0) / 100
                        except Exception:
                            pass

                        result = await kalshi_order(client, pk, KEY_ID, mkt["ticker"], side, count, rec_price)
                        entry = {
                            "time": now, "ticker": mkt["ticker"], "side": side,
                            "price": rec_price, "qty": count, "status": "PLACED",
                            "reason": sig["reason"], "conf": conf,
                            "mom_5m": mom["pct_5m"], "order_id": result.get("order", {}).get("order_id", "?")
                        }
                        state["trade_log"].appendleft(entry)
                        log.info(f"✅ Order placed: {entry}")
                    except Exception as e:
                        err_entry = {
                            "time": now, "ticker": mkt["ticker"], "side": side,
                            "price": rec_price, "qty": count, "status": f"FAILED: {e}",
                            "reason": sig["reason"], "conf": conf, "mom_5m": mom["pct_5m"]
                        }
                        state["trade_log"].appendleft(err_entry)
                        state["last_error"] = str(e)
                        log.error(f"Order failed: {e}")

            except Exception as e:
                state["last_error"] = str(e)
                log.error(f"Loop error: {e}")

            await asyncio.sleep(LOOP_INTERVAL)

# ─── FASTAPI ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(trading_loop())
    yield
    task.cancel()

app = FastAPI(title="Kalshi BTC Bot", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─── API ROUTES ───────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "loop_count": state["loop_count"], "bot_enabled": state["bot_enabled"]}

@app.get("/api/status")
def get_status():
    return {
        "bot_enabled": state["bot_enabled"],
        "btc_price": state["btc_price"],
        "last_signal": state["last_signal"],
        "last_signal_time": state["last_signal_time"],
        "balance": state["balance"],
        "markets": state["markets"],
        "trade_count": len(state["trade_log"]),
        "last_error": state["last_error"],
        "loop_count": state["loop_count"],
        "config": {
            "min_confidence": MIN_CONF,
            "max_bet_cents": MAX_BET,
            "momentum_threshold": MOM_THRESHOLD,
            "loop_interval_secs": LOOP_INTERVAL,
        }
    }

@app.get("/api/trades")
def get_trades():
    return {"trades": list(state["trade_log"])}

@app.post("/api/bot/enable")
def enable_bot():
    state["bot_enabled"] = True
    log.info("Bot ENABLED via API")
    return {"bot_enabled": True}

@app.post("/api/bot/disable")
def disable_bot():
    state["bot_enabled"] = False
    log.info("Bot DISABLED via API")
    return {"bot_enabled": False}

# Manual trade endpoint (for dashboard)
class Auth(BaseModel):
    key_id: str
    private_key_pem: str

class OrderReq(Auth):
    ticker: str
    side: str
    count: int
    price: int

@app.post("/api/balance")
async def api_balance(b: Auth):
    try:
        pk = load_key(b.private_key_pem)
    except ValueError as e:
        raise HTTPException(400, str(e))
    h = kheaders(pk, b.key_id, "GET", "/trade-api/v2/portfolio/balance")
    async with httpx.AsyncClient() as c:
        r = await c.get(KALSHI_BASE + "/portfolio/balance", headers=h, timeout=15)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/markets")
async def api_markets():
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{KALSHI_BASE}/markets",
            params={"series_ticker": "KXBTC15M", "status": "open", "limit": 6}, timeout=15)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/order")
async def api_order(b: OrderReq):
    try:
        pk = load_key(b.private_key_pem)
    except ValueError as e:
        raise HTTPException(400, str(e))
    h = kheaders(pk, b.key_id, "POST", "/trade-api/v2/portfolio/orders")
    payload = {
        "action": "buy", "count": b.count, "side": b.side,
        "ticker": b.ticker, "type": "limit",
        "yes_price": b.price if b.side == "yes" else 100 - b.price,
        "no_price":  b.price if b.side == "no"  else 100 - b.price,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    async with httpx.AsyncClient() as c:
        r = await c.post(KALSHI_BASE + "/portfolio/orders", headers=h, json=payload, timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, r.text)
    entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "ticker": b.ticker,
             "side": b.side, "price": b.price, "qty": b.count, "status": "MANUAL"}
    state["trade_log"].appendleft(entry)
    return r.json()

# ─── DASHBOARD UI ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return HTMLResponse(content=DASHBOARD_HTML)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BTC Signal Bot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03050a;color:#dce8f0;font-family:'Syne',sans-serif;min-height:100vh;padding:24px 20px}
.app{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
.hdr{display:flex;justify-content:space-between;align-items:center;padding-bottom:18px;border-bottom:1px solid #1a2d40}
.logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,#f7b731,#ff6b35);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;font-family:'IBM Plex Mono',monospace;font-size:11px}
.pill-off{background:rgba(74,96,112,.08);border:1px solid #1a2d40;color:#4a6070}
.pill-on{background:rgba(0,230,118,.08);border:1px solid rgba(0,230,118,.2);color:#00e676}
.dot{width:6px;height:6px;border-radius:50%;border-radius:50%}
.dot-on{background:#00e676;box-shadow:0 0 6px #00e676;animation:blink 1.2s infinite}
.dot-off{background:#4a6070}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:600px){.grid3{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}}
.card{background:#080d14;border:1px solid #1a2d40;border-radius:14px;padding:20px}
.card-lbl{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px}
.card-val{font-family:'IBM Plex Mono',monospace;font-size:28px;font-weight:700;color:#f7b731}
.card-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:4px}
.sig-box{border-radius:14px;border:2px solid #4a6070;padding:22px;background:rgba(74,96,112,.04);transition:all .4s}
.sig-box.up{border-color:#00e676;background:rgba(0,230,118,.04)}
.sig-box.down{border-color:#ff1744;background:rgba(255,23,68,.04)}
.sig-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.sig-lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase}
.conf-badge{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px}
.c-up{background:rgba(0,230,118,.15);color:#00e676}
.c-dn{background:rgba(255,23,68,.15);color:#ff1744}
.c-neu{background:rgba(74,96,112,.2);color:#4a6070}
.sig-dir{font-family:'Syne',sans-serif;font-size:40px;font-weight:800;line-height:1;margin-bottom:6px;color:#4a6070}
.sig-reason{font-size:13px;color:#4a6070;line-height:1.5;margin-bottom:12px}
.sig-meta{display:flex;gap:20px;flex-wrap:wrap}
.meta-l{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1px;text-transform:uppercase}
.meta-v{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:700}
.bot-ctrl{display:flex;align-items:center;justify-content:space-between;background:#080d14;border:1px solid #1a2d40;border-radius:14px;padding:18px 22px}
.bot-lbl{font-size:15px;font-weight:700}
.bot-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:3px}
.btn-enable{background:linear-gradient(135deg,#00c853,#00e676);color:#001a0a;border:none;padding:10px 22px;border-radius:8px;font-family:'Syne',sans-serif;font-size:13px;font-weight:800;cursor:pointer;transition:all .2s}
.btn-disable{background:rgba(255,23,68,.1);color:#ff1744;border:1px solid rgba(255,23,68,.3);padding:10px 22px;border-radius:8px;font-family:'Syne',sans-serif;font-size:13px;font-weight:800;cursor:pointer}
.cfg-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:700px){.cfg-grid{grid-template-columns:1fr 1fr}}
.cfg-card{background:#0c1520;border:1px solid #111d2a;border-radius:9px;padding:12px}
.cfg-lbl{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:5px}
.cfg-val{font-family:'IBM Plex Mono',monospace;font-size:16px;font-weight:700;color:#f7b731}
.log-wrap{background:#080d14;border:1px solid #1a2d40;border-radius:14px;overflow:hidden}
.log-hdr{padding:13px 18px;border-bottom:1px solid #111d2a;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;display:flex;justify-content:space-between;align-items:center}
.log-empty{padding:28px;text-align:center;color:#4a6070;font-family:'IBM Plex Mono',monospace;font-size:12px}
.log-row{padding:12px 18px;border-bottom:1px solid rgba(17,29,42,.5);display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
.log-row:last-child{border-bottom:none}
.ls-y{background:rgba(0,230,118,.12);color:#00e676;padding:2px 7px;border-radius:3px;font-weight:700;font-size:10px;white-space:nowrap}
.ls-n{background:rgba(255,23,68,.12);color:#ff1744;padding:2px 7px;border-radius:3px;font-weight:700;font-size:10px;white-space:nowrap}
.ls-f{background:rgba(255,152,0,.1);color:#ff9800;padding:2px 7px;border-radius:3px;font-size:10px;white-space:nowrap}
.sec{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:9px}
.sec::after{content:'';flex:1;height:1px;background:#1a2d40}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #1a2d40;border-top-color:#f7b731;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.up{color:#00e676}.dn{color:#ff1744}.acc{color:#f7b731}
.mkt-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #111d2a;font-family:'IBM Plex Mono',monospace;font-size:12px}
.mkt-row:last-child{border-bottom:none}
.mkt-prices{display:flex;gap:8px}
.pp-y{background:rgba(0,230,118,.08);color:#00e676;border:1px solid rgba(0,230,118,.2);padding:3px 9px;border-radius:5px;font-size:12px;font-weight:700}
.pp-n{background:rgba(255,23,68,.08);color:#ff1744;border:1px solid rgba(255,23,68,.2);padding:3px 9px;border-radius:5px;font-size:12px;font-weight:700}
.warn-banner{background:rgba(247,183,49,.05);border:1px solid rgba(247,183,49,.2);border-radius:10px;padding:14px 18px;font-family:'IBM Plex Mono',monospace;font-size:12px;color:rgba(247,183,49,.9);line-height:1.8}
</style>
</head>
<body>
<div class="app">

  <!-- HEADER -->
  <div class="hdr">
    <div>
      <div class="logo">BTC SIGNAL BOT</div>
      <div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:2px">server-side · kalshi 15-min · auto-trading</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <span id="balance-lbl" style="font-family:'IBM Plex Mono',monospace;font-size:13px;color:#f7b731;font-weight:700"></span>
      <span id="bot-pill" class="pill pill-off"><span id="bot-dot" class="dot dot-off"></span><span id="bot-txt">LOADING</span></span>
    </div>
  </div>

  <!-- SETUP WARNING -->
  <div class="warn-banner" id="setup-warn" style="display:none">
    ⚠ Bot credentials not configured. Set <strong>KALSHI_KEY_ID</strong> and <strong>KALSHI_PRIVATE_KEY</strong> as environment variables in your Render dashboard, then redeploy.
    <br/>Go to: Render → your service → <strong>Environment</strong> tab → Add environment variable.
  </div>

  <!-- STATS -->
  <div class="grid3">
    <div class="card">
      <div class="card-lbl">BTC Price</div>
      <div class="card-val" id="s-btc"><span class="spin"></span></div>
      <div class="card-sub" id="s-mom">momentum: —</div>
    </div>
    <div class="card">
      <div class="card-lbl">Trades Placed</div>
      <div class="card-val" id="s-trades">0</div>
      <div class="card-sub" id="s-loops">loop #0</div>
    </div>
    <div class="card">
      <div class="card-lbl">Last Error</div>
      <div class="card-val" style="font-size:13px;color:#ff9800" id="s-err">none</div>
    </div>
  </div>

  <!-- BOT CONTROL -->
  <div class="bot-ctrl">
    <div>
      <div class="bot-lbl">🤖 Trading Bot</div>
      <div class="bot-sub" id="bot-sub">Checking status...</div>
    </div>
    <div style="display:flex;gap:10px">
      <button class="btn-enable" onclick="setBotEnabled(true)">Enable</button>
      <button class="btn-disable" onclick="setBotEnabled(false)">Disable</button>
    </div>
  </div>

  <!-- AI SIGNAL -->
  <div class="sec">Last AI Signal</div>
  <div class="sig-box" id="sig-box">
    <div class="sig-hdr">
      <div class="sig-lbl">Signal · <span id="sig-time">pending</span></div>
      <span id="sig-badge" class="conf-badge c-neu">—</span>
    </div>
    <div class="sig-dir" id="sig-dir">─ NEUTRAL</div>
    <div class="sig-reason" id="sig-reason">Waiting for first loop...</div>
    <div class="sig-meta">
      <div><div class="meta-l">Risk</div><div class="meta-v" id="sig-risk" style="color:#4a6070">—</div></div>
      <div><div class="meta-l">Entry</div><div class="meta-v acc" id="sig-price">—</div></div>
      <div><div class="meta-l">Mom 5m</div><div class="meta-v" id="sig-mom">—</div></div>
    </div>
  </div>

  <!-- CONFIG -->
  <div class="sec">Bot Configuration</div>
  <div class="cfg-grid">
    <div class="cfg-card"><div class="cfg-lbl">Min Confidence</div><div class="cfg-val" id="cfg-conf">—</div></div>
    <div class="cfg-card"><div class="cfg-lbl">Max Bet</div><div class="cfg-val" id="cfg-bet">—</div></div>
    <div class="cfg-card"><div class="cfg-lbl">Mom Threshold</div><div class="cfg-val" id="cfg-mom">—</div></div>
    <div class="cfg-card"><div class="cfg-lbl">Loop Interval</div><div class="cfg-val" id="cfg-loop">—</div></div>
  </div>
  <div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;padding:4px 2px">
    Change these by setting environment variables in Render: MIN_CONFIDENCE, MAX_BET_CENTS, MOMENTUM_THRESHOLD, LOOP_INTERVAL_SECS
  </div>

  <!-- MARKETS -->
  <div class="sec">Open Markets</div>
  <div class="card" id="mkt-box">
    <div style="color:#4a6070;font-family:'IBM Plex Mono',monospace;font-size:12px;display:flex;gap:7px;align-items:center"><span class="spin"></span> Loading...</div>
  </div>

  <!-- TRADE LOG -->
  <div class="sec">Trade Log (server-side)</div>
  <div class="log-wrap">
    <div class="log-hdr"><span>Trades (<span id="log-count">0</span>)</span></div>
    <div id="log-body"><div class="log-empty">No trades yet — bot is running its first analysis loop</div></div>
  </div>

</div>

<script>
async function fetchStatus() {
  try {
    const r = await fetch("/api/status");
    const d = await r.json();

    // Header
    const pill = document.getElementById("bot-pill");
    const dot  = document.getElementById("bot-dot");
    const txt  = document.getElementById("bot-txt");
    if (d.bot_enabled) {
      pill.className = "pill pill-on"; dot.className = "dot dot-on"; txt.textContent = "BOT LIVE";
    } else {
      pill.className = "pill pill-off"; dot.className = "dot dot-off"; txt.textContent = "BOT OFF";
    }
    if (d.balance != null) document.getElementById("balance-lbl").textContent = "$" + d.balance.toFixed(2);

    // Setup warning
    const noKeys = !d.config || d.loop_count < 2 && d.last_error && d.last_error.includes("credential");
    document.getElementById("setup-warn").style.display = (d.loop_count > 3 && d.trade_count === 0 && !d.last_signal) ? "" : "none";

    // Stats
    if (d.btc_price) document.getElementById("s-btc").textContent = "$" + d.btc_price.toLocaleString("en-US",{maximumFractionDigits:0});
    document.getElementById("s-trades").textContent = d.trade_count;
    document.getElementById("s-loops").textContent  = "loop #" + d.loop_count;
    document.getElementById("s-err").textContent    = d.last_error || "none";
    document.getElementById("s-err").style.color    = d.last_error ? "#ff9800" : "#00e676";

    // Bot sub
    document.getElementById("bot-sub").textContent = d.bot_enabled
      ? `Running every ${d.config?.loop_interval_secs||60}s · AI + momentum confirmation`
      : "Bot is paused — click Enable to resume auto-trading";

    // Signal
    if (d.last_signal) {
      const s = d.last_signal;
      const dir = s.direction;
      const box = document.getElementById("sig-box");
      box.className = "sig-box" + (dir==="UP"?" up":dir==="DOWN"?" down":"");
      document.getElementById("sig-time").textContent = d.last_signal_time || "—";
      document.getElementById("sig-dir").textContent = dir==="UP"?"▲ UP":dir==="DOWN"?"▼ DOWN":"─ NEUTRAL";
      document.getElementById("sig-dir").style.color = dir==="UP"?"#00e676":dir==="DOWN"?"#ff1744":"#4a6070";
      document.getElementById("sig-reason").textContent = s.reason || "—";
      document.getElementById("sig-reason").style.color = "#dce8f0";
      document.getElementById("sig-price").textContent = (s.recommended_price||"?") + "¢";
      const badge = document.getElementById("sig-badge");
      badge.textContent = s.confidence + "% CONF";
      badge.className = "conf-badge " + (dir==="UP"?"c-up":dir==="DOWN"?"c-dn":"c-neu");
      const risk = s.risk || "—";
      const riskEl = document.getElementById("sig-risk");
      riskEl.textContent = risk;
      riskEl.style.color = risk==="LOW"?"#00e676":risk==="HIGH"?"#ff1744":"#ffb800";
    }

    // Config
    if (d.config) {
      document.getElementById("cfg-conf").textContent = d.config.min_confidence + "%";
      document.getElementById("cfg-bet").textContent  = "$" + (d.config.max_bet_cents/100).toFixed(2);
      document.getElementById("cfg-mom").textContent  = d.config.momentum_threshold + "%";
      document.getElementById("cfg-loop").textContent = d.config.loop_interval_secs + "s";
    }

    // Markets
    if (d.markets && d.markets.length) {
      document.getElementById("mkt-box").innerHTML = d.markets.map(m=>`
        <div class="mkt-row">
          <span style="flex:1;font-size:12px">${m.title||m.ticker}</span>
          <div class="mkt-prices">
            <span class="pp-y">YES ${m.yes_price}¢</span>
            <span class="pp-n">NO ${m.no_price}¢</span>
          </div>
        </div>`).join("");
    }
  } catch(e) {
    console.error("Status fetch failed:", e);
  }
}

async function fetchTrades() {
  try {
    const r = await fetch("/api/trades");
    const d = await r.json();
    const trades = d.trades || [];
    document.getElementById("log-count").textContent = trades.length;
    if (!trades.length) return;
    document.getElementById("log-body").innerHTML = trades.map(t => {
      const failed = t.status && t.status.startsWith("FAILED");
      const isAuto = t.status === "PLACED" || t.status === "AUTO";
      return `<div class="log-row">
        <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;white-space:nowrap">${t.time}</span>
        <span style="font-family:'IBM Plex Mono',monospace;font-size:11px;flex:1">${t.ticker}</span>
        <span class="${t.side==="yes"?"ls-y":"ls-n"}">${t.side==="yes"?"▲ YES":"▼ NO"}</span>
        <span style="font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700">${t.qty}x @ ${t.price}¢</span>
        <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:${failed?"#ff9800":isAuto?"#00e676":"#4a6070"}">${t.status}</span>
        ${t.conf ? `<span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070">${t.conf}% conf</span>` : ""}
        ${t.reason ? `<span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;width:100%;padding-top:4px">${t.reason}</span>` : ""}
      </div>`;
    }).join("");
  } catch(e) {}
}

async function setBotEnabled(enabled) {
  try {
    await fetch(enabled ? "/api/bot/enable" : "/api/bot/disable", {method:"POST"});
    fetchStatus();
  } catch(e) { alert("Failed: " + e.message); }
}

// Poll every 5 seconds
fetchStatus(); fetchTrades();
setInterval(fetchStatus, 5000);
setInterval(fetchTrades, 5000);
</script>
</body>
</html>
"""
