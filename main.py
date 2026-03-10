import os, time, base64, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

app = FastAPI(title="Kalshi BTC Bot")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def load_key(pem_str):
    try:
        return serialization.load_pem_private_key(pem_str.strip().encode(), password=None, backend=default_backend())
    except Exception as e:
        raise HTTPException(400, f"Invalid private key: {e}")

def sign(pk, ts, method, path):
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def kheaders(pk, key_id, method, path):
    ts = str(int(time.time() * 1000))
    return {"Content-Type": "application/json", "KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": sign(pk, ts, method, path)}

class Auth(BaseModel):
    key_id: str
    private_key_pem: str

class Order(Auth):
    ticker: str
    side: str
    count: int
    price: int

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/balance")
async def get_balance(b: Auth):
    pk = load_key(b.private_key_pem)
    h = kheaders(pk, b.key_id, "GET", "/trade-api/v2/portfolio/balance")
    async with httpx.AsyncClient() as c:
        r = await c.get(KALSHI_BASE + "/portfolio/balance", headers=h, timeout=15)
    if r.status_code != 200: raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/markets")
async def get_markets(series: str = "KXBTC15M", status: str = "open", limit: int = 6):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{KALSHI_BASE}/markets", params={"series_ticker": series, "status": status, "limit": limit}, timeout=15)
    if r.status_code != 200: raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/order")
async def place_order(b: Order):
    pk = load_key(b.private_key_pem)
    h = kheaders(pk, b.key_id, "POST", "/trade-api/v2/portfolio/orders")
    payload = {"action": "buy", "count": b.count, "side": b.side, "ticker": b.ticker, "type": "limit",
               "yes_price": b.price if b.side == "yes" else 100 - b.price,
               "no_price": b.price if b.side == "no" else 100 - b.price,
               "client_order_id": f"btcbot_{int(time.time()*1000)}"}
    async with httpx.AsyncClient() as c:
        r = await c.post(KALSHI_BASE + "/portfolio/orders", headers=h, json=payload, timeout=15)
    if r.status_code not in (200, 201): raise HTTPException(r.status_code, r.text)
    return r.json()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BTC Signal Bot</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#03050a;color:#dce8f0;font-family:'Syne',sans-serif;min-height:100vh;padding:24px 20px}
.app{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
.hdr{display:flex;justify-content:space-between;align-items:center;padding-bottom:18px;border-bottom:1px solid #1a2d40}
.logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,#f7b731,#ff6b35);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;font-family:'IBM Plex Mono',monospace;font-size:11px}
.pill-off{background:rgba(74,96,112,.08);border:1px solid #1a2d40;color:#4a6070}
.pill-on{background:rgba(0,230,118,.08);border:1px solid rgba(0,230,118,.2);color:#00e676}
.dot{width:6px;height:6px;border-radius:50%;background:#00e676;animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.card{background:#080d14;border:1px solid #1a2d40;border-radius:14px;padding:22px}
.card h2{font-size:16px;margin-bottom:6px}
.card p{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-bottom:18px;line-height:1.7}
.warn{background:rgba(255,107,53,.06);border:1px solid rgba(255,107,53,.2);border-radius:8px;padding:11px 14px;margin-bottom:16px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:rgba(255,107,53,.9);line-height:1.6}
label{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:5px;display:block}
input,textarea{width:100%;background:#0c1520;border:1px solid #1a2d40;color:#dce8f0;padding:10px 13px;border-radius:8px;font-size:13px;font-family:'IBM Plex Mono',monospace;outline:none;margin-bottom:13px}
input:focus,textarea:focus{border-color:#f7b731}
textarea{resize:vertical;min-height:110px;font-size:11px;line-height:1.5}
.btn{width:100%;padding:13px;border-radius:10px;border:none;font-family:'Syne',sans-serif;font-size:15px;font-weight:800;cursor:pointer;transition:all .2s;margin-bottom:0}
.btn:hover{transform:translateY(-2px);filter:brightness(1.1)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.btn-primary{background:linear-gradient(135deg,#f7b731,#ff6b35);color:#050505}
.btn-yes{background:linear-gradient(135deg,#00c853,#00e676);color:#001a0a}
.btn-no{background:linear-gradient(135deg,#c62828,#ff1744);color:#1a0003}
.btn-sm{background:#0c1520;border:1px solid #1a2d40;color:#4a6070;padding:5px 12px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:11px;cursor:pointer;width:auto}
.btn-sm:hover{color:#dce8f0}
.btn-danger{background:rgba(255,23,68,.08);border:1px solid rgba(255,23,68,.2);color:#ff1744;padding:6px 13px;border-radius:6px;font-family:'IBM Plex Mono',monospace;font-size:11px;cursor:pointer}
.hero{background:linear-gradient(135deg,#080d14,#0a1520);border:1px solid #1a2d40;border-radius:14px;padding:24px 28px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:20px;position:relative;overflow:hidden}
.hero::before{content:'₿';position:absolute;right:-10px;top:-20px;font-size:150px;font-weight:900;color:rgba(247,183,49,.03);pointer-events:none}
.btc-lbl{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}
#btc-price{font-family:'IBM Plex Mono',monospace;font-size:40px;font-weight:700;color:#f7b731;line-height:1}
#btc-chg{font-family:'IBM Plex Mono',monospace;font-size:13px;margin-top:5px}
.stats{display:flex;gap:24px;flex-wrap:wrap}
.stat-l{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:3px}
.stat-v{font-family:'IBM Plex Mono',monospace;font-size:15px;font-weight:700}
.sig{border-radius:14px;border:2px solid #4a6070;padding:22px 26px;background:rgba(74,96,112,.04);transition:all .4s}
.sig.up{border-color:#00e676;background:rgba(0,230,118,.04)}
.sig.down{border-color:#ff1744;background:rgba(255,23,68,.04)}
.sig-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.sig-ttl{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase}
#sig-dir{font-family:'Syne',sans-serif;font-size:44px;font-weight:800;line-height:1;margin-bottom:7px;color:#4a6070}
#sig-reason{font-size:13px;color:#4a6070;line-height:1.5}
.sig-meta{display:flex;gap:20px;margin-top:14px;flex-wrap:wrap}
.meta-l{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1px;text-transform:uppercase}
.meta-v{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:700}
.mkt-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:560px){.mkt-grid{grid-template-columns:1fr}}
.mkt{background:#080d14;border:1px solid #1a2d40;border-radius:11px;padding:16px;cursor:pointer;transition:all .2s}
.mkt:hover,.mkt.sel{border-color:#f7b731}
.mkt.sel{background:rgba(247,183,49,.03)}
.mkt-title{font-size:13px;font-weight:600;margin-bottom:10px;line-height:1.3}
.mkt-prices{display:flex;gap:8px;margin-bottom:10px}
.pp{flex:1;padding:7px;border-radius:7px;text-align:center;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:700;cursor:pointer;border:1px solid;transition:all .15s}
.pp-y{background:rgba(0,230,118,.08);color:#00e676;border-color:rgba(0,230,118,.25)}
.pp-n{background:rgba(255,23,68,.08);color:#ff1744;border-color:rgba(255,23,68,.25)}
.pp-lbl{font-size:9px;opacity:.6;display:block;margin-bottom:1px}
.mkt-meta{display:flex;justify-content:space-between;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070}
.op{background:#080d14;border:1px solid #1a2d40;border-radius:14px;overflow:hidden}
.op-hdr{padding:14px 18px;border-bottom:1px solid #111d2a;display:flex;justify-content:space-between;align-items:center}
.op-ttl{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#4a6070}
.op-body{padding:18px}
.tabs{display:flex;gap:4px;margin-bottom:16px}
.tab{flex:1;padding:8px;border-radius:7px;text-align:center;font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;cursor:pointer;border:1px solid #1a2d40;background:#0c1520;color:#4a6070;transition:all .2s}
.tab.ty{background:rgba(0,230,118,.1);color:#00e676;border-color:rgba(0,230,118,.3)}
.tab.tn{background:rgba(255,23,68,.1);color:#ff1744;border-color:rgba(255,23,68,.3)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.payout{background:#0c1520;border:1px solid #111d2a;border-radius:7px;padding:11px 13px;margin-bottom:14px}
.prow{display:flex;justify-content:space-between;margin-bottom:5px;font-family:'IBM Plex Mono',monospace;font-size:12px}
.prow:last-child{margin-bottom:0;padding-top:7px;border-top:1px solid #111d2a}
.pk{color:#4a6070;font-size:11px}
.auto-row{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:#080d14;border:1px solid #1a2d40;border-radius:12px}
.auto-lbl{font-size:14px;font-weight:600}
.auto-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:2px}
.tog{position:relative;width:50px;height:26px;cursor:pointer;display:inline-block}
.tog input{opacity:0;width:0;height:0}
.tog-t{position:absolute;inset:0;border-radius:13px;background:#1a2d40;transition:.3s}
.tog input:checked+.tog-t{background:#00e676}
.tog-th{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:white;transition:.3s;box-shadow:0 2px 4px rgba(0,0,0,.4)}
.tog input:checked~.tog-th{left:27px}
.set-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.set-card{background:#0c1520;border:1px solid #111d2a;border-radius:9px;padding:13px}
.set-lbl{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:7px;display:block}
.set-val{font-family:'IBM Plex Mono',monospace;font-size:19px;font-weight:700;color:#f7b731}
.slider{width:100%;accent-color:#f7b731;cursor:pointer;margin-top:7px;display:block}
.log-wrap{background:#080d14;border:1px solid #1a2d40;border-radius:14px;overflow:hidden}
.log-hdr{padding:13px 18px;border-bottom:1px solid #111d2a;display:flex;justify-content:space-between;align-items:center;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;letter-spacing:2px;text-transform:uppercase}
.log-empty{padding:28px;text-align:center;color:#4a6070;font-family:'IBM Plex Mono',monospace;font-size:12px}
.log-row{padding:11px 18px;border-bottom:1px solid rgba(17,29,42,.5);display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-family:'IBM Plex Mono',monospace;font-size:11px}
.ls-y{background:rgba(0,230,118,.12);color:#00e676;padding:2px 7px;border-radius:3px;font-weight:700}
.ls-n{background:rgba(255,23,68,.12);color:#ff1744;padding:2px 7px;border-radius:3px;font-weight:700}
.sec{font-family:'IBM Plex Mono',monospace;font-size:10px;color:#4a6070;letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:9px}
.sec::after{content:'';flex:1;height:1px;background:#1a2d40}
.spin{display:inline-block;width:13px;height:13px;border:2px solid #1a2d40;border-top-color:#f7b731;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.toasts{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:7px;z-index:9000}
.toast{background:#0c1520;border:1px solid #1a2d40;padding:11px 15px;border-radius:9px;min-width:240px;display:flex;gap:9px;font-family:'IBM Plex Mono',monospace;font-size:12px;box-shadow:0 8px 28px rgba(0,0,0,.5)}
.tok{border-left:3px solid #00e676}.terr{border-left:3px solid #ff1744}.tinf{border-left:3px solid #f7b731}
</style>
</head>
<body>
<div class="app">

  <!-- HEADER -->
  <div class="hdr">
    <div>
      <div class="logo">BTC SIGNAL BOT</div>
      <div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070;margin-top:2px">kalshi 15-min up/down</div>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <span id="balance-display" style="font-family:'IBM Plex Mono',monospace;font-size:14px;color:#f7b731;font-weight:700;display:none"></span>
      <span id="conn-pill" class="pill pill-off"><span id="conn-dot" style="width:6px;height:6px;border-radius:50%;background:#4a6070"></span><span id="conn-txt">OFFLINE</span></span>
      <button id="disconnect-btn" class="btn-danger" style="display:none" onclick="disconnect()">Disconnect</button>
    </div>
  </div>

  <!-- CONNECT FORM -->
  <div id="connect-card" class="card">
    <h2>Connect Your Kalshi Account</h2>
    <p>Enter your Kalshi API Key ID and the full contents of your .key file.</p>
    <div class="warn">Your private key stays on this server and is never stored or logged.</div>
    <label>Kalshi API Key ID</label>
    <input id="key-id-input" type="text" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"/>
    <label>Private Key (.key file contents)</label>
    <textarea id="pem-input" placeholder="-----BEGIN PRIVATE KEY-----&#10;MIIEvgIBADANBg...&#10;-----END PRIVATE KEY-----"></textarea>
    <button class="btn btn-primary" onclick="handleConnect()">Connect &amp; Start Trading</button>
  </div>

  <!-- BTC PRICE -->
  <div class="hero">
    <div>
      <div class="btc-lbl">Bitcoin / USD</div>
      <div id="btc-price"><span class="spin"></span></div>
      <div id="btc-chg" style="margin-top:5px;font-family:'IBM Plex Mono',monospace;font-size:13px;color:#4a6070">—</div>
    </div>
    <div class="stats">
      <div><div class="stat-l">Markets</div><div class="stat-v" id="stat-markets">—</div></div>
      <div><div class="stat-l">Signal</div><div class="stat-v" id="stat-signal" style="color:#4a6070">—</div></div>
      <div><div class="stat-l">Confidence</div><div class="stat-v" id="stat-conf">—</div></div>
      <div><div class="stat-l">Trades</div><div class="stat-v" id="stat-trades">0</div></div>
    </div>
  </div>

  <!-- SIGNAL -->
  <div class="sec">AI Signal Engine</div>
  <div class="sig" id="sig-box">
    <div class="sig-hdr">
      <div class="sig-ttl" id="sig-ttl">Signal · pending</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="conf-badge" style="display:none;font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px;background:rgba(74,96,112,.2);color:#4a6070"></span>
        <button class="btn-sm" onclick="refreshSignal()">↻ Refresh</button>
      </div>
    </div>
    <div id="sig-dir" style="font-family:'Syne',sans-serif;font-size:44px;font-weight:800;line-height:1;margin-bottom:7px;color:#4a6070">─ NEUTRAL</div>
    <div id="sig-reason" style="font-size:13px;color:#4a6070;line-height:1.5">Waiting for BTC price data...</div>
    <div class="sig-meta">
      <div><div class="meta-l">Edge</div><div class="meta-v" id="sig-edge" style="color:#dce8f0;font-size:12px">—</div></div>
      <div><div class="meta-l">Entry</div><div class="meta-v" id="sig-entry" style="color:#f7b731">—</div></div>
      <div><div class="meta-l">Risk</div><div class="meta-v" id="sig-risk" style="color:#4a6070">—</div></div>
    </div>
  </div>

  <!-- AUTO TRADE -->
  <div class="auto-row">
    <div>
      <div class="auto-lbl">🤖 Auto-Trade Mode</div>
      <div class="auto-sub" id="auto-sub">Trades automatically when confidence meets threshold · Connect first</div>
    </div>
    <label class="tog">
      <input type="checkbox" id="auto-toggle" onchange="toggleAuto(this.checked)"/>
      <div class="tog-t"></div><div class="tog-th"></div>
    </label>
  </div>

  <!-- SETTINGS -->
  <div class="sec">Settings</div>
  <div class="set-grid">
    <div class="set-card">
      <span class="set-lbl">Max Bet Per Trade</span>
      <div class="set-val" id="max-bet-val">$5.00</div>
      <input type="range" class="slider" min="100" max="5000" step="100" value="500" oninput="document.getElementById('max-bet-val').textContent='$'+(this.value/100).toFixed(2);maxBet=+this.value"/>
    </div>
    <div class="set-card">
      <span class="set-lbl">Min Confidence</span>
      <div class="set-val" id="min-conf-val">65%</div>
      <input type="range" class="slider" min="50" max="95" step="5" value="65" oninput="document.getElementById('min-conf-val').textContent=this.value+'%';minConf=+this.value"/>
    </div>
  </div>

  <!-- MARKETS -->
  <div class="sec">Live BTC 15-Min Markets</div>
  <div id="markets-container">
    <div style="font-family:'IBM Plex Mono',monospace;font-size:12px;color:#4a6070;display:flex;gap:7px;align-items:center"><span class="spin"></span> Loading markets...</div>
  </div>

  <!-- ORDER -->
  <div class="sec">Place Trade</div>
  <div class="op">
    <div class="op-hdr">
      <div class="op-ttl" id="op-ticker">Select a market above</div>
      <div id="ai-suggest" style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:#4a6070"></div>
    </div>
    <div class="op-body">
      <div class="tabs">
        <div class="tab ty" id="tab-yes" onclick="setSide('yes')">▲ YES (UP)</div>
        <div class="tab" id="tab-no" onclick="setSide('no')">▼ NO (DOWN)</div>
      </div>
      <div class="grid2">
        <div><label>Price (cents)</label><input type="number" id="trade-price" min="1" max="99" value="50" oninput="updatePayout()"/></div>
        <div><label>Contracts</label><input type="number" id="trade-qty" min="1" value="1" oninput="updatePayout()"/></div>
      </div>
      <div class="payout">
        <div class="prow"><span class="pk">Cost</span><span id="p-cost" style="color:#ff1744;font-weight:700">—</span></div>
        <div class="prow"><span class="pk">Payout if correct</span><span id="p-payout" style="color:#00e676;font-weight:700">—</span></div>
        <div class="prow"><span class="pk">Profit</span><span id="p-profit" style="color:#00e676;font-weight:700">—</span></div>
      </div>
      <button class="btn btn-yes" id="trade-btn" onclick="handleTrade()">Connect First</button>
    </div>
  </div>

  <!-- LOG -->
  <div class="sec">Trade Log</div>
  <div class="log-wrap">
    <div class="log-hdr"><span>Orders (<span id="log-count">0</span>)</span><button onclick="clearLog()" style="background:none;border:none;color:#4a6070;cursor:pointer;font-family:'IBM Plex Mono',monospace;font-size:11px">Clear</button></div>
    <div id="log-body"><div class="log-empty">No trades yet</div></div>
  </div>

</div>
<div class="toasts" id="toasts"></div>

<script>
// ─── STATE ─────────────────────────────────────────────────────────────────
let keyId = "", pem = "", connected = false, balance = null;
let btcPrice = null, priceHist = [], markets = [], selMkt = null;
let signal = null, side = "yes", autoTrade = false, maxBet = 500, minConf = 65;
let tradeLog = [];

// ─── TOAST ─────────────────────────────────────────────────────────────────
function toast(msg, type="inf") {
  const el = document.createElement("div");
  el.className = `toast t${type}`;
  el.innerHTML = `<span>${type==="ok"?"✓":type==="err"?"✗":"ℹ"}</span><span>${msg}</span>`;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

// ─── CONNECT ───────────────────────────────────────────────────────────────
async function handleConnect() {
  keyId = document.getElementById("key-id-input").value.trim();
  pem = document.getElementById("pem-input").value.trim();
  if (!keyId || !pem) return toast("Enter Key ID and private key", "err");
  const btn = event.target; btn.textContent = "Connecting..."; btn.disabled = true;
  try {
    const r = await fetch("/api/balance", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key_id: keyId, private_key_pem: pem})});
    if (!r.ok) { const e = await r.json().catch(()=>{}); throw new Error(e?.detail || `HTTP ${r.status}`); }
    const j = await r.json();
    balance = (j.balance?.balance || 0) / 100;
    connected = true;
    document.getElementById("connect-card").style.display = "none";
    document.getElementById("conn-pill").className = "pill pill-on";
    document.getElementById("conn-dot").style.background = "#00e676";
    document.getElementById("conn-dot").style.boxShadow = "0 0 6px #00e676";
    document.getElementById("conn-dot").style.animation = "blink 1.2s infinite";
    document.getElementById("conn-txt").textContent = "LIVE";
    document.getElementById("balance-display").textContent = "$" + balance.toFixed(2);
    document.getElementById("balance-display").style.display = "";
    document.getElementById("disconnect-btn").style.display = "";
    updateTradeBtn();
    toast("Connected to Kalshi!", "ok");
  } catch(e) {
    toast(`Connection failed: ${e.message}`, "err");
    btn.textContent = "Connect & Start Trading"; btn.disabled = false;
  }
}

function disconnect() {
  connected = false; keyId = ""; pem = ""; balance = null;
  document.getElementById("connect-card").style.display = "";
  document.getElementById("conn-pill").className = "pill pill-off";
  document.getElementById("conn-dot").style.background = "#4a6070";
  document.getElementById("conn-dot").style.boxShadow = "none";
  document.getElementById("conn-txt").textContent = "OFFLINE";
  document.getElementById("balance-display").style.display = "none";
  document.getElementById("disconnect-btn").style.display = "none";
  document.getElementById("key-id-input").value = "";
  document.getElementById("pem-input").value = "";
  const btn = document.querySelector("#connect-card .btn"); btn.textContent = "Connect & Start Trading"; btn.disabled = false;
  toast("Disconnected", "inf");
}

// ─── BTC PRICE ─────────────────────────────────────────────────────────────
async function fetchBTC() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT");
    const p = parseFloat((await r.json()).price);
    const prev = btcPrice;
    btcPrice = p;
    priceHist.push(p); if (priceHist.length > 30) priceHist.shift();
    document.getElementById("btc-price").textContent = "$" + p.toLocaleString("en-US", {maximumFractionDigits:0});
    if (prev) {
      const chg = ((p - prev) / prev) * 100;
      const el = document.getElementById("btc-chg");
      el.textContent = (chg >= 0 ? "▲ +" : "▼ ") + chg.toFixed(3) + "% 1m";
      el.style.color = chg >= 0 ? "#00e676" : "#ff1744";
    }
  } catch {}
}

// ─── MARKETS ───────────────────────────────────────────────────────────────
async function fetchMarkets() {
  try {
    const r = await fetch("/api/markets");
    if (!r.ok) return;
    const d = await r.json();
    markets = d.markets || [];
    if (!selMkt && markets.length) selMkt = markets[0];
    document.getElementById("stat-markets").textContent = markets.length;
    renderMarkets();
  } catch {}
}

function renderMarkets() {
  const c = document.getElementById("markets-container");
  if (!markets.length) { c.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;color:#4a6070">No open markets found</div>'; return; }
  c.innerHTML = '<div class="mkt-grid">' + markets.map(m => `
    <div class="mkt ${selMkt?.ticker===m.ticker?'sel':''}" onclick="selectMkt('${m.ticker}')">
      <div class="mkt-title">${m.title||m.ticker}</div>
      <div class="mkt-prices">
        <div class="pp pp-y" onclick="event.stopPropagation();pickSide('yes','${m.ticker}',${m.yes_price})">
          <span class="pp-lbl">YES</span>${m.yes_price}¢
        </div>
        <div class="pp pp-n" onclick="event.stopPropagation();pickSide('no','${m.ticker}',${m.no_price})">
          <span class="pp-lbl">NO</span>${m.no_price}¢
        </div>
      </div>
      <div class="mkt-meta"><span>Vol: ${m.volume?.toLocaleString()||"—"}</span><span style="font-size:10px">${m.ticker}</span></div>
    </div>`).join('') + '</div>';
}

function selectMkt(ticker) {
  selMkt = markets.find(m => m.ticker === ticker);
  document.getElementById("op-ticker").textContent = ticker;
  renderMarkets();
  updateTradeBtn();
}

function pickSide(s, ticker, p) {
  selectMkt(ticker);
  setSide(s);
  document.getElementById("trade-price").value = p;
  updatePayout();
}

// ─── SIDE ──────────────────────────────────────────────────────────────────
function setSide(s) {
  side = s;
  document.getElementById("tab-yes").className = "tab" + (s==="yes"?" ty":"");
  document.getElementById("tab-no").className = "tab" + (s==="no"?" tn":"");
  const btn = document.getElementById("trade-btn");
  btn.className = "btn " + (s==="yes"?"btn-yes":"btn-no");
  updateTradeBtn();
}

function updateTradeBtn() {
  const btn = document.getElementById("trade-btn");
  if (!connected) { btn.textContent = "Connect First"; return; }
  if (!selMkt) { btn.textContent = "Select a Market"; return; }
  const price = +document.getElementById("trade-price").value;
  const qty = +document.getElementById("trade-qty").value;
  const cost = ((price/100)*qty).toFixed(2);
  btn.textContent = `Buy ${side.toUpperCase()} — $${cost}`;
}

function updatePayout() {
  const price = +document.getElementById("trade-price").value || 50;
  const qty = +document.getElementById("trade-qty").value || 1;
  const cost = (price/100)*qty;
  const payout = (100/price)*qty;
  document.getElementById("p-cost").textContent = "$" + cost.toFixed(2);
  document.getElementById("p-payout").textContent = "$" + payout.toFixed(2);
  document.getElementById("p-profit").textContent = "$" + (payout - cost).toFixed(2);
  updateTradeBtn();
}

// ─── AI SIGNAL ─────────────────────────────────────────────────────────────
async function refreshSignal() {
  document.getElementById("sig-ttl").innerHTML = '<span class="spin"></span> Analyzing...';
  const latest = priceHist[priceHist.length-1] || btcPrice || 0;
  const prev1 = priceHist[priceHist.length-2];
  const prev5 = priceHist[Math.max(0,priceHist.length-6)];
  const c1 = prev1 ? ((latest-prev1)/prev1)*100 : 0;
  const c5 = prev5 ? ((latest-prev5)/prev5)*100 : 0;
  const ms = markets.slice(0,4).map(m=>`${m.ticker}: YES=${m.yes_price}c NO=${m.no_price}c Vol=${m.volume}`).join("\\n");
  try {
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({model:"claude-sonnet-4-20250514", max_tokens:300,
        messages:[{role:"user",content:`Elite Kalshi BTC 15-min trader. BTC: $${latest?.toLocaleString()} | 1m: ${c1>=0?"+":""}${c1?.toFixed(3)}% | 5m: ${c5>=0?"+":""}${c5?.toFixed(3)}%\\nMARKETS:\\n${ms||"unavailable"}\\nReply ONLY JSON no markdown: {"direction":"UP","confidence":75,"side":"yes","reason":"Momentum positive","edge":"brief","recommended_price":55,"risk":"MEDIUM"}`}]})
    });
    const d = await r.json();
    signal = JSON.parse((d.content?.[0]?.text||"{}").replace(/```json|```/g,"").trim());
  } catch {
    signal = {direction:"NEUTRAL",confidence:50,side:"yes",reason:"Analysis unavailable",edge:"—",recommended_price:50,risk:"HIGH"};
  }
  renderSignal();
  if (autoTrade && connected && signal.confidence >= minConf && signal.direction !== "NEUTRAL" && markets[0]) {
    const mkt = markets[0];
    const s = signal.direction==="UP"?"yes":"no";
    const p = signal.recommended_price;
    const q = Math.max(1, Math.floor((maxBet/100)/(p/100)));
    try {
      const r = await fetch("/api/order",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({key_id:keyId,private_key_pem:pem,ticker:mkt.ticker,side:s,count:q,price:p})});
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      addLog(mkt.ticker,s,p,q,"AUTO");
      toast(`🤖 Auto: ${q}x ${s.toUpperCase()} @ ${p}¢`,"ok");
    } catch(e){toast(`Auto-trade failed: ${e.message}`,"err");}
  }
}

function renderSignal() {
  if (!signal) return;
  const box = document.getElementById("sig-box");
  const dir = signal.direction;
  box.className = "sig" + (dir==="UP"?" up":dir==="DOWN"?" down":"");
  document.getElementById("sig-ttl").textContent = "Signal · " + new Date().toLocaleTimeString("en-US",{hour12:false});
  const dirEl = document.getElementById("sig-dir");
  dirEl.textContent = dir==="UP"?"▲ UP":dir==="DOWN"?"▼ DOWN":"─ NEUTRAL";
  dirEl.style.color = dir==="UP"?"#00e676":dir==="DOWN"?"#ff1744":"#4a6070";
  document.getElementById("sig-reason").textContent = signal.reason;
  document.getElementById("sig-reason").style.color = "#4a6070";
  document.getElementById("sig-edge").textContent = signal.edge;
  document.getElementById("sig-entry").textContent = signal.recommended_price + "¢";
  const riskEl = document.getElementById("sig-risk");
  riskEl.textContent = signal.risk;
  riskEl.style.color = signal.risk==="LOW"?"#00e676":signal.risk==="HIGH"?"#ff1744":"#ffb800";
  const badge = document.getElementById("conf-badge");
  badge.textContent = signal.confidence + "% CONF";
  badge.style.display = "";
  badge.style.background = dir==="UP"?"rgba(0,230,118,.15)":dir==="DOWN"?"rgba(255,23,68,.15)":"rgba(74,96,112,.2)";
  badge.style.color = dir==="UP"?"#00e676":dir==="DOWN"?"#ff1744":"#4a6070";
  document.getElementById("stat-signal").textContent = dir;
  document.getElementById("stat-signal").style.color = dir==="UP"?"#00e676":dir==="DOWN"?"#ff1744":"#4a6070";
  document.getElementById("stat-conf").textContent = signal.confidence + "%";
  if (signal.recommended_price) {
    document.getElementById("trade-price").value = signal.recommended_price;
    updatePayout();
  }
  if (dir==="UP") setSide("yes");
  if (dir==="DOWN") setSide("no");
  document.getElementById("ai-suggest").innerHTML = `AI: <span style="color:${dir==="UP"?"#00e676":"#ff1744"};font-weight:700">${signal.side?.toUpperCase()} @ ${signal.recommended_price}¢</span>`;
}

// ─── TRADE ─────────────────────────────────────────────────────────────────
async function handleTrade() {
  if (!connected) return toast("Connect first","err");
  if (!selMkt) return toast("Select a market","err");
  const price = +document.getElementById("trade-price").value;
  const qty = +document.getElementById("trade-qty").value;
  const btn = document.getElementById("trade-btn");
  btn.disabled = true; btn.textContent = "Placing...";
  try {
    const r = await fetch("/api/order",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({key_id:keyId,private_key_pem:pem,ticker:selMkt.ticker,side,count:qty,price})});
    if (!r.ok){const e=await r.json().catch(()=>{});throw new Error(e?.detail||`HTTP ${r.status}`);}
    addLog(selMkt.ticker,side,price,qty,"PLACED");
    toast(`${qty}x ${side.toUpperCase()} @ ${price}¢ placed`,"ok");
  } catch(e){toast(`Order failed: ${e.message}`,"err");}
  btn.disabled = false; updateTradeBtn();
}

function addLog(ticker,s,price,qty,status) {
  tradeLog.unshift({time:new Date().toLocaleTimeString("en-US",{hour12:false}),ticker,side:s,price,qty,status});
  if (tradeLog.length > 50) tradeLog.pop();
  document.getElementById("stat-trades").textContent = tradeLog.length;
  document.getElementById("log-count").textContent = tradeLog.length;
  document.getElementById("log-body").innerHTML = tradeLog.map(t=>`
    <div class="log-row">
      <span style="color:#4a6070;font-size:10px">${t.time}</span>
      <span style="flex:1">${t.ticker}</span>
      <span class="${t.side==="yes"?"ls-y":"ls-n"}">${t.side==="yes"?"▲ YES":"▼ NO"}</span>
      <span style="font-weight:700">${t.qty}x @ ${t.price}¢</span>
      <span style="color:#4a6070;font-size:10px">${t.status}</span>
    </div>`).join("");
}

function clearLog() {
  tradeLog = [];
  document.getElementById("log-body").innerHTML = '<div class="log-empty">No trades yet</div>';
  document.getElementById("log-count").textContent = "0";
  document.getElementById("stat-trades").textContent = "0";
}

function toggleAuto(checked) {
  if (!connected && checked) {
    document.getElementById("auto-toggle").checked = false;
    return toast("Connect first","err");
  }
  autoTrade = checked;
  document.getElementById("auto-sub").textContent = checked ? `AUTO-TRADE ON · confidence >= ${minConf}%` : `Trades automatically when confidence >= ${minConf}% · Connect first`;
  toast(checked?"🤖 Auto-trade ENABLED":"Auto-trade disabled", checked?"ok":"inf");
}

// ─── INIT ──────────────────────────────────────────────────────────────────
updatePayout();
fetchBTC();
fetchMarkets();
setInterval(fetchBTC, 10000);
setInterval(fetchMarkets, 30000);
setInterval(() => { if (btcPrice) refreshSignal(); }, 60000);
setTimeout(() => { if (btcPrice) refreshSignal(); }, 3000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTML
