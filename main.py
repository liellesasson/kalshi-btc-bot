import os, time, base64, httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

app = FastAPI(title="Kalshi BTC Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def load_key(pem_str: str):
    try:
        return serialization.load_pem_private_key(
            pem_str.strip().encode(), password=None, backend=default_backend()
        )
    except Exception as e:
        raise HTTPException(400, f"Invalid private key: {e}")

def sign(private_key, timestamp: str, method: str, path: str) -> str:
    msg = f"{timestamp}{method}{path.split('?')[0]}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()

def kalshi_headers(private_key, key_id: str, method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts, method, path),
    }

class AuthBase(BaseModel):
    key_id: str
    private_key_pem: str

class OrderRequest(AuthBase):
    ticker: str
    side: str
    count: int
    price: int

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/balance")
async def get_balance(body: AuthBase):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/balance"
    headers = kalshi_headers(pk, body.key_id, "GET", path)
    async with httpx.AsyncClient() as client:
        r = await client.get(KALSHI_BASE + "/portfolio/balance", headers=headers, timeout=15)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/markets")
async def get_markets(series: str = "KXBTC15M", status: str = "open", limit: int = 6):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{KALSHI_BASE}/markets",
            params={"series_ticker": series, "status": status, "limit": limit}, timeout=15)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/order")
async def place_order(body: OrderRequest):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_headers(pk, body.key_id, "POST", path)
    payload = {
        "action": "buy", "count": body.count, "side": body.side,
        "ticker": body.ticker, "type": "limit",
        "yes_price": body.price if body.side == "yes" else 100 - body.price,
        "no_price": body.price if body.side == "no" else 100 - body.price,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(KALSHI_BASE + "/portfolio/orders", headers=headers, json=payload, timeout=15)
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/orders")
async def list_orders(body: AuthBase):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_headers(pk, body.key_id, "GET", path)
    async with httpx.AsyncClient() as client:
        r = await client.get(KALSHI_BASE + "/portfolio/orders?limit=20", headers=headers, timeout=15)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>BTC Signal Bot</title>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&family=Syne:wght@400;800&display=swap" rel="stylesheet"/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#03050a;--card:#080d14;--card2:#0c1520;--border:#1a2d40;--accent:#f7b731;--accent2:#ff6b35;--up:#00e676;--down:#ff1744;--text:#dce8f0;--muted:#4a6070;--mono:'IBM Plex Mono',monospace;--sans:'Syne',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh}
.app{max-width:900px;margin:0 auto;padding:24px 20px;display:flex;flex-direction:column;gap:18px}
.hdr{display:flex;justify-content:space-between;align-items:center;padding-bottom:18px;border-bottom:1px solid var(--border)}
.logo{font-size:20px;font-weight:800;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.pill{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;font-family:var(--mono);font-size:11px}
.pill-live{background:rgba(0,230,118,.08);border:1px solid rgba(0,230,118,.2);color:var(--up)}
.pill-off{background:rgba(74,96,112,.08);border:1px solid var(--border);color:var(--muted)}
.dot{width:6px;height:6px;border-radius:50%;animation:blink 1.2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px}
.card-title{font-size:16px;font-weight:800;margin-bottom:6px}
.card-sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-bottom:18px;line-height:1.7}
.warn{background:rgba(255,107,53,.06);border:1px solid rgba(255,107,53,.2);border-radius:8px;padding:11px 14px;margin-bottom:16px;font-family:var(--mono);font-size:11px;color:rgba(255,107,53,.9);line-height:1.6}
.lbl{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:5px;display:block}
.inp{width:100%;background:var(--card2);border:1px solid var(--border);color:var(--text);padding:10px 13px;border-radius:8px;font-size:13px;font-family:var(--mono);outline:none;transition:border-color .2s}
.inp:focus{border-color:var(--accent)}
.inp::placeholder{color:var(--muted)}
textarea.inp{resize:vertical;min-height:120px;font-size:11px;line-height:1.5}
.row{margin-bottom:13px}
.btn{width:100%;padding:13px;border-radius:10px;border:none;font-family:var(--sans);font-size:15px;font-weight:800;cursor:pointer;transition:all .2s}
.btn:hover{transform:translateY(-2px);filter:brightness(1.1)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#050505}
.btn-yes{background:linear-gradient(135deg,#00c853,#00e676);color:#001a0a}
.btn-no{background:linear-gradient(135deg,#c62828,#ff1744);color:#1a0003}
.btn-sm{background:var(--card2);border:1px solid var(--border);color:var(--muted);padding:5px 11px;border-radius:6px;font-family:var(--mono);font-size:11px;cursor:pointer;width:auto;border:none}
.btn-danger{background:rgba(255,23,68,.08);border:1px solid rgba(255,23,68,.2);color:var(--down);padding:6px 13px;border-radius:6px;font-family:var(--mono);font-size:11px;cursor:pointer}
.hero{background:linear-gradient(135deg,#080d14,#0a1520,#080d14);border:1px solid var(--border);border-radius:14px;padding:24px 28px;position:relative;overflow:hidden;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:20px}
.hero::before{content:'₿';position:absolute;right:-15px;top:-25px;font-size:160px;font-weight:900;color:rgba(247,183,49,.03);pointer-events:none}
.btc-lbl{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}
.btc-val{font-family:var(--mono);font-size:40px;font-weight:700;color:var(--accent);line-height:1}
.btc-chg{font-family:var(--mono);font-size:13px;margin-top:5px}
.up{color:var(--up)}.dn{color:var(--down)}
.stats{display:flex;gap:24px;flex-wrap:wrap}
.stat-l{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:3px}
.stat-v{font-family:var(--mono);font-size:15px;font-weight:700}
.sig{border-radius:14px;border:2px solid;padding:22px 26px;position:relative;overflow:hidden;transition:all .4s}
.sig-up{border-color:var(--up);background:rgba(0,230,118,.04)}
.sig-dn{border-color:var(--down);background:rgba(255,23,68,.04)}
.sig-neu{border-color:var(--muted);background:rgba(74,96,112,.04)}
.sig-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.sig-ttl{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:7px}
.conf{font-family:var(--mono);font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px}
.c-up{background:rgba(0,230,118,.15);color:var(--up)}.c-dn{background:rgba(255,23,68,.15);color:var(--down)}.c-neu{background:rgba(74,96,112,.2);color:var(--muted)}
.sig-dir{font-family:var(--sans);font-size:44px;font-weight:800;line-height:1;margin-bottom:7px}
.sig-reason{font-size:13px;color:var(--muted);line-height:1.5;max-width:480px}
.sig-meta{display:flex;gap:20px;margin-top:14px;flex-wrap:wrap}
.meta-l{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}
.meta-v{font-family:var(--mono);font-size:13px;font-weight:700}
.mkt-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:560px){.mkt-grid{grid-template-columns:1fr}}
.mkt{background:var(--card);border:1px solid var(--border);border-radius:11px;padding:16px;cursor:pointer;transition:all .2s}
.mkt:hover{border-color:var(--accent);transform:translateY(-2px)}
.mkt.sel{border-color:var(--accent);background:rgba(247,183,49,.03)}
.mkt-title{font-size:13px;font-weight:600;margin-bottom:10px;line-height:1.3}
.mkt-prices{display:flex;gap:8px;margin-bottom:10px}
.pp{flex:1;padding:7px 9px;border-radius:7px;text-align:center;font-family:var(--mono);font-size:13px;font-weight:700;cursor:pointer;border:1px solid;transition:all .15s}
.pp-y{background:rgba(0,230,118,.08);color:var(--up);border-color:rgba(0,230,118,.25)}
.pp-n{background:rgba(255,23,68,.08);color:var(--down);border-color:rgba(255,23,68,.25)}
.pp:hover{filter:brightness(1.2);transform:scale(1.02)}
.pp-lbl{font-size:9px;letter-spacing:1px;opacity:.6;display:block;margin-bottom:1px}
.mkt-meta{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--muted)}
.op{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.op-hdr{padding:14px 18px;border-bottom:1px solid rgba(17,29,42,.8);display:flex;justify-content:space-between;align-items:center}
.op-ttl{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted)}
.op-body{padding:18px}
.tabs{display:flex;gap:4px;margin-bottom:16px}
.tab{flex:1;padding:8px;border-radius:7px;text-align:center;font-family:var(--mono);font-size:12px;font-weight:700;cursor:pointer;border:1px solid var(--border);background:var(--card2);color:var(--muted);transition:all .2s}
.tab-y{background:rgba(0,230,118,.1);color:var(--up);border-color:rgba(0,230,118,.3)}
.tab-n{background:rgba(255,23,68,.1);color:var(--down);border-color:rgba(255,23,68,.3)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.payout{background:var(--card2);border:1px solid rgba(17,29,42,.8);border-radius:7px;padding:11px 13px;margin-bottom:14px}
.prow{display:flex;justify-content:space-between;margin-bottom:5px}
.prow:last-child{margin-bottom:0;padding-top:7px;border-top:1px solid rgba(17,29,42,.8)}
.pk{font-family:var(--mono);font-size:11px;color:var(--muted)}
.pv{font-family:var(--mono);font-size:12px;font-weight:700}
.pv-g{color:var(--up)}.pv-r{color:var(--down)}
.auto-row{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:var(--card);border:1px solid var(--border);border-radius:12px}
.auto-lbl{font-size:14px;font-weight:600}
.auto-sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:2px}
.tog{position:relative;width:50px;height:26px;cursor:pointer;display:inline-block}
.tog input{opacity:0;width:0;height:0}
.tog-t{position:absolute;inset:0;border-radius:13px;background:var(--border);transition:.3s}
.tog input:checked+.tog-t{background:var(--up)}
.tog-th{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:white;transition:.3s}
.tog input:checked~.tog-th{left:27px}
.set-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.set-card{background:var(--card2);border:1px solid rgba(17,29,42,.8);border-radius:9px;padding:13px}
.set-lbl{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:7px;display:block}
.set-val{font-family:var(--mono);font-size:19px;font-weight:700;color:var(--accent)}
.slider{width:100%;accent-color:var(--accent);cursor:pointer;margin-top:7px;display:block}
.log{background:var(--card);border:1px solid var(--border);border-radius:14px;overflow:hidden}
.log-hdr{padding:13px 18px;border-bottom:1px solid rgba(17,29,42,.8);display:flex;justify-content:space-between;align-items:center}
.log-ttl{font-family:var(--mono);font-size:11px;color:var(--muted);letter-spacing:2px;text-transform:uppercase}
.log-empty{padding:28px;text-align:center;color:var(--muted);font-family:var(--mono);font-size:12px}
.log-row{padding:11px 18px;border-bottom:1px solid rgba(17,29,42,.5);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.log-row:last-child{border-bottom:none}
.lt{font-family:var(--mono);font-size:10px;color:var(--muted);white-space:nowrap}
.lk{font-family:var(--mono);font-size:11px;flex:1}
.ls{font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 7px;border-radius:3px}
.ls-y{background:rgba(0,230,118,.12);color:var(--up)}.ls-n{background:rgba(255,23,68,.12);color:var(--down)}
.lp{font-family:var(--mono);font-size:12px;font-weight:700}
.sec{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;display:flex;align-items:center;gap:9px}
.sec::after{content:'';flex:1;height:1px;background:var(--border)}
.spin{width:13px;height:13px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;display:inline-block;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.toasts{position:fixed;bottom:22px;right:22px;display:flex;flex-direction:column;gap:7px;z-index:9000}
.toast{background:var(--card2);border:1px solid var(--border);padding:11px 15px;border-radius:9px;min-width:240px;display:flex;gap:9px;align-items:flex-start;box-shadow:0 8px 28px rgba(0,0,0,.5);font-family:var(--mono);font-size:12px}
.toast.ok{border-left:3px solid var(--up)}.toast.err{border-left:3px solid var(--down)}.toast.inf{border-left:3px solid var(--accent)}
</style>
</head>
<body>
<div id="root"></div>
<script type="text/babel">
const {useState,useEffect,useCallback,useRef} = React;
const BASE = window.location.origin;

async function fetchBTC(){
  try{const r=await fetch("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT");return parseFloat((await r.json()).price)}
  catch{try{const r=await fetch("https://api.coinbase.com/v2/prices/BTC-USD/spot");return parseFloat((await r.json()).data.amount)}catch{return null}}
}

async function getAISignal(price,c1,c5,mkts){
  const ms=mkts.slice(0,4).map(m=>`${m.ticker}: YES=${m.yes_price}c NO=${m.no_price}c Vol=${m.volume}`).join("\\n");
  try{
    const r=await fetch("https://api.anthropic.com/v1/messages",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model:"claude-sonnet-4-20250514",max_tokens:300,
        messages:[{role:"user",content:`Elite Kalshi BTC 15-min trader. BTC: $${price?.toLocaleString()} | 1m: ${c1>=0?"+":""}${c1?.toFixed(3)}% | 5m: ${c5>=0?"+":""}${c5?.toFixed(3)}%\\nMARKETS:\\n${ms||"unavailable"}\\nReply ONLY JSON no markdown: {"direction":"UP"|"DOWN"|"NEUTRAL","confidence":0-100,"side":"yes"|"no","reason":"1-2 sentences","edge":"brief","recommended_price":1-99,"risk":"LOW"|"MEDIUM"|"HIGH"}`}]})});
    const d=await r.json();
    return JSON.parse((d.content?.[0]?.text||"{}").replace(/\`\`\`json|\`\`\`/g,"").trim());
  }catch{return{direction:"NEUTRAL",confidence:50,side:"yes",reason:"Analysis unavailable.",edge:"—",recommended_price:50,risk:"HIGH"}}
}

const fmt$=n=>n==null?"—":`$${Number(n).toFixed(2)}`;
const ftime=()=>new Date().toLocaleTimeString("en-US",{hour12:false});

function App(){
  const [keyId,setKeyId]=useState("");
  const [pem,setPem]=useState("");
  const [connected,setConnected]=useState(false);
  const [connecting,setConnecting]=useState(false);
  const [balance,setBalance]=useState(null);
  const [btcPrice,setBtcPrice]=useState(null);
  const [hist,setHist]=useState([]);
  const [markets,setMarkets]=useState([]);
  const [selMkt,setSelMkt]=useState(null);
  const [signal,setSignal]=useState(null);
  const [sigLoading,setSigLoading]=useState(false);
  const [sigTime,setSigTime]=useState(null);
  const [side,setSide]=useState("yes");
  const [qty,setQty]=useState(1);
  const [price,setPrice]=useState(50);
  const [trading,setTrading]=useState(false);
  const [autoTrade,setAutoTrade]=useState(false);
  const [maxBet,setMaxBet]=useState(500);
  const [minConf,setMinConf]=useState(65);
  const [tlog,setTlog]=useState([]);
  const [toasts,setToasts]=useState([]);

  const refs=useRef({});
  refs.current={keyId,pem,autoTrade,markets,minConf,maxBet};
  const btcRef=useRef(null);btcRef.current=btcPrice;
  const histRef=useRef([]);histRef.current=hist;
  const mktRef=useRef([]);mktRef.current=markets;

  const toast=useCallback((msg,type="inf")=>{
    const id=Date.now();
    setToasts(t=>[...t,{id,msg,type}]);
    setTimeout(()=>setToasts(t=>t.filter(x=>x.id!==id)),4500);
  },[]);

  const handleConnect=useCallback(async()=>{
    setConnecting(true);
    try{
      const r=await fetch(`${BASE}/api/balance`,{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({key_id:keyId,private_key_pem:pem})});
      if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.detail||`HTTP ${r.status}`);}
      const j=await r.json();
      setBalance(j.balance?.balance/100);
      setConnected(true);
      toast("Connected to Kalshi!","ok");
    }catch(e){toast(`Connection failed: ${e.message}`,"err");}
    setConnecting(false);
  },[keyId,pem,toast]);

  const refreshMarkets=useCallback(async()=>{
    try{
      const r=await fetch(`${BASE}/api/markets`);
      if(!r.ok)return;
      const d=await r.json();
      const ms=d.markets||[];
      if(ms.length){setMarkets(ms);setSelMkt(s=>s||ms[0]);}
    }catch{}
  },[]);

  const refreshBTC=useCallback(async()=>{
    const p=await fetchBTC();
    if(p){setBtcPrice(p);setHist(h=>[...h,{price:p}].slice(-30));}
  },[]);

  const refreshSignal=useCallback(async()=>{
    setSigLoading(true);
    const h=histRef.current;
    const latest=h[h.length-1]?.price||btcRef.current||0;
    const prev1=h[h.length-2]?.price;
    const prev5=h[Math.max(0,h.length-6)]?.price;
    const c1=prev1?((latest-prev1)/prev1)*100:0;
    const c5=prev5?((latest-prev5)/prev5)*100:0;
    const sig=await getAISignal(latest,c1,c5,mktRef.current);
    setSignal(sig);setSigTime(ftime());
    if(sig.recommended_price)setPrice(sig.recommended_price);
    if(sig.direction==="UP")setSide("yes");
    if(sig.direction==="DOWN")setSide("no");
    setSigLoading(false);
    const r=refs.current;
    if(r.autoTrade&&r.keyId&&sig.confidence>=r.minConf&&sig.direction!=="NEUTRAL"&&r.markets[0]){
      const mkt=r.markets[0];
      const s=sig.direction==="UP"?"yes":"no";
      const p=sig.recommended_price;
      const q=Math.max(1,Math.floor((r.maxBet/100)/(p/100)));
      try{
        const res=await fetch(`${BASE}/api/order`,{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({key_id:r.keyId,private_key_pem:r.pem,ticker:mkt.ticker,side:s,count:q,price:p})});
        if(!res.ok)throw new Error(`HTTP ${res.status}`);
        setTlog(l=>[{time:ftime(),ticker:mkt.ticker,side:s,price:p,qty:q,status:"AUTO"},...l.slice(0,49)]);
        toast(`Auto: ${q}x ${s.toUpperCase()} @ ${p}c`,"ok");
      }catch(e){toast(`Auto-trade failed: ${e.message}`,"err");}
    }
  },[toast]);

  const handleTrade=useCallback(async()=>{
    if(!selMkt)return toast("Select a market","err");
    if(!connected)return toast("Not connected","err");
    setTrading(true);
    try{
      const r=await fetch(`${BASE}/api/order`,{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({key_id:keyId,private_key_pem:pem,ticker:selMkt.ticker,side,count:qty,price})});
      if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.detail||`HTTP ${r.status}`);}
      setTlog(l=>[{time:ftime(),ticker:selMkt.ticker,side,price,qty,status:"PLACED"},...l.slice(0,49)]);
      toast(`${qty}x ${side.toUpperCase()} @ ${price}c placed`,"ok");
    }catch(e){toast(`Order failed: ${e.message}`,"err");}
    setTrading(false);
  },[connected,keyId,pem,selMkt,side,qty,price,toast]);

  useEffect(()=>{refreshBTC();const i=setInterval(refreshBTC,10000);return()=>clearInterval(i);},[refreshBTC]);
  useEffect(()=>{refreshMarkets();const i=setInterval(refreshMarkets,30000);return()=>clearInterval(i);},[refreshMarkets]);
  useEffect(()=>{
    if(!btcPrice)return;
    refreshSignal();
    const i=setInterval(refreshSignal,60000);
    return()=>clearInterval(i);
  },[btcPrice]);

  const chg1m=hist.length>=2?((hist[hist.length-1].price-hist[hist.length-2].price)/hist[hist.length-2].price)*100:0;
  const cost=((price/100)*qty).toFixed(2);
  const payout=price>0?((100/price)*qty).toFixed(2):"0";
  const sc=!signal?"neu":signal.direction==="UP"?"up":signal.direction==="DOWN"?"dn":"neu";

  return(
    <div className="app">
      <div className="hdr">
        <div>
          <div className="logo">BTC SIGNAL BOT</div>
          <div style={{fontFamily:"var(--mono)",fontSize:"11px",color:"var(--muted)",marginTop:"2px"}}>kalshi 15-min up/down</div>
        </div>
        <div style={{display:"flex",gap:"10px",alignItems:"center"}}>
          {connected&&balance!=null&&<span style={{fontFamily:"var(--mono)",fontSize:"14px",color:"var(--accent)",fontWeight:700}}>${balance?.toFixed(2)}</span>}
          <div className={`pill ${connected?"pill-live":"pill-off"}`}>
            <div className="dot" style={{background:connected?"var(--up)":"var(--muted)"}}/>
            {connected?"LIVE":"OFFLINE"}
          </div>
          {connected&&<button className="btn-danger" onClick={()=>{setConnected(false);setBalance(null);}}>Disconnect</button>}
        </div>
      </div>

      {!connected&&(
        <div className="card">
          <div className="card-title">Connect Your Kalshi Account</div>
          <div className="card-sub">Enter your Kalshi API Key ID and the contents of your .key file below.</div>
          <div className="warn">Your private key stays on this server and is never stored or logged anywhere.</div>
          <div className="row"><label className="lbl">Kalshi API Key ID</label>
            <input className="inp" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" value={keyId} onChange={e=>setKeyId(e.target.value)}/>
          </div>
          <div className="row"><label className="lbl">Private Key (paste full .key file)</label>
            <textarea className="inp" placeholder={"-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBg...\n-----END PRIVATE KEY-----"} value={pem} onChange={e=>setPem(e.target.value)}/>
          </div>
          <button className="btn btn-primary" onClick={handleConnect} disabled={connecting||!keyId.trim()||!pem.trim()}>
            {connecting?"Connecting...":"Connect & Start Trading"}
          </button>
        </div>
      )}

      <div className="hero">
        <div>
          <div className="btc-lbl">Bitcoin / USD</div>
          <div className="btc-val">{btcPrice?`$${btcPrice.toLocaleString("en-US",{maximumFractionDigits:0})}`:<span className="spin"/>}</div>
          <div className="btc-chg"><span className={chg1m>=0?"up":"dn"}>{chg1m>=0?"▲":"▼"} {Math.abs(chg1m).toFixed(3)}%</span> <span style={{color:"var(--muted)",fontSize:"11px"}}>1m</span></div>
        </div>
        <div className="stats">
          {[["Markets",markets.length,"var(--text)"],["Signal",signal?.direction||"—",sc==="up"?"var(--up)":sc==="dn"?"var(--down)":"var(--muted)"],["Conf",signal?`${signal.confidence}%`:"—","var(--text)"],["Trades",tlog.length,"var(--text)"]].map(([k,v,c])=>(
            <div key={k}><div className="stat-l">{k}</div><div className="stat-v" style={{color:c}}>{v}</div></div>
          ))}
        </div>
      </div>

      <div className="sec">AI Signal Engine</div>
      <div className={`sig sig-${sc}`}>
        <div style={{position:"absolute",top:"-60px",right:"-60px",width:"200px",height:"200px",borderRadius:"50%",background:sc==="up"?"radial-gradient(circle,rgba(0,230,118,.1) 0%,transparent 70%)":sc==="dn"?"radial-gradient(circle,rgba(255,23,68,.1) 0%,transparent 70%)":"none",pointerEvents:"none"}}/>
        <div className="sig-hdr">
          <div className="sig-ttl">{sigLoading&&<span className="spin"/>}{sigLoading?" Analyzing...":`Signal · ${sigTime||"pending"}`}</div>
          <div style={{display:"flex",gap:"7px",alignItems:"center"}}>
            {signal&&<span className={`conf ${sc==="up"?"c-up":sc==="dn"?"c-dn":"c-neu"}`}>{signal.confidence}% CONF</span>}
            <button className="btn-sm" onClick={refreshSignal} disabled={sigLoading} style={{background:"var(--card2)",border:"1px solid var(--border)",color:"var(--muted)",padding:"5px 11px",borderRadius:"6px",cursor:"pointer"}}>{sigLoading?"...":"↻ Refresh"}</button>
          </div>
        </div>
        {signal?(
          <>
            <div className="sig-dir" style={{color:sc==="up"?"var(--up)":sc==="dn"?"var(--down)":"var(--muted)"}}>{signal.direction==="UP"?"▲ UP":signal.direction==="DOWN"?"▼ DOWN":"─ NEUTRAL"}</div>
            <div className="sig-reason">{signal.reason}</div>
            <div className="sig-meta">
              <div><div className="meta-l">Edge</div><div className="meta-v" style={{color:"var(--text)",fontSize:"12px"}}>{signal.edge}</div></div>
              <div><div className="meta-l">Entry</div><div className="meta-v" style={{color:"var(--accent)"}}>{signal.recommended_price}c</div></div>
              <div><div className="meta-l">Risk</div><div className="meta-v" style={{color:signal.risk==="LOW"?"var(--up)":signal.risk==="HIGH"?"var(--down)":"#ffb800"}}>{signal.risk}</div></div>
            </div>
          </>
        ):<div style={{color:"var(--muted)",fontFamily:"var(--mono)",fontSize:"13px",padding:"10px 0"}}>{sigLoading?"Fetching BTC data...":"Waiting for data..."}</div>}
      </div>

      <div className="auto-row">
        <div><div className="auto-lbl">🤖 Auto-Trade Mode</div><div className="auto-sub">Trades automatically when confidence ≥ {minConf}%{!connected&&" · Connect first"}</div></div>
        <label className="tog">
          <input type="checkbox" checked={autoTrade} onChange={e=>{
            if(!connected&&e.target.checked)return toast("Connect first","err");
            setAutoTrade(e.target.checked);
            toast(e.target.checked?"Auto-trade ENABLED":"Auto-trade disabled",e.target.checked?"ok":"inf");
          }}/>
          <div className="tog-t"/><div className="tog-th"/>
        </label>
      </div>

      <div className="sec">Settings</div>
      <div className="set-grid">
        <div className="set-card"><span className="set-lbl">Max Bet</span><div className="set-val">${(maxBet/100).toFixed(2)}</div><input type="range" className="slider" min={100} max={5000} step={100} value={maxBet} onChange={e=>setMaxBet(Number(e.target.value))}/></div>
        <div className="set-card"><span className="set-lbl">Min Confidence</span><div className="set-val">{minConf}%</div><input type="range" className="slider" min={50} max={95} step={5} value={minConf} onChange={e=>setMinConf(Number(e.target.value))}/></div>
      </div>

      <div className="sec">Live BTC 15-Min Markets</div>
      {markets.length===0?<div style={{fontFamily:"var(--mono)",fontSize:"12px",color:"var(--muted)",display:"flex",gap:"7px",alignItems:"center"}}><span className="spin"/>Loading markets...</div>:
        <div className="mkt-grid">{markets.map(m=>(
          <div key={m.ticker} className={`mkt ${selMkt?.ticker===m.ticker?"sel":""}`} onClick={()=>setSelMkt(m)}>
            <div className="mkt-title">{m.title||m.ticker}</div>
            <div className="mkt-prices">
              <div className="pp pp-y" onClick={e=>{e.stopPropagation();setSide("yes");setPrice(m.yes_price);setSelMkt(m);}}><span className="pp-lbl">YES</span>{m.yes_price}c</div>
              <div className="pp pp-n" onClick={e=>{e.stopPropagation();setSide("no");setPrice(m.no_price);setSelMkt(m);}}><span className="pp-lbl">NO</span>{m.no_price}c</div>
            </div>
            <div className="mkt-meta"><span>Vol: {m.volume?.toLocaleString()||"—"}</span><span style={{fontSize:"10px"}}>{m.ticker}</span></div>
          </div>
        ))}</div>}

      <div className="sec">Place Trade</div>
      <div className="op">
        <div className="op-hdr">
          <div className="op-ttl">{selMkt?.ticker||"Select a market"}</div>
          {signal&&<div style={{fontFamily:"var(--mono)",fontSize:"11px",color:"var(--muted)"}}>AI: <span style={{color:signal.direction==="UP"?"var(--up)":"var(--down)",fontWeight:700}}>{signal.side?.toUpperCase()} @ {signal.recommended_price}c</span></div>}
        </div>
        <div className="op-body">
          <div className="tabs">
            <div className={`tab ${side==="yes"?"tab-y":""}`} onClick={()=>setSide("yes")}>▲ YES (UP)</div>
            <div className={`tab ${side==="no"?"tab-n":""}`} onClick={()=>setSide("no")}>▼ NO (DOWN)</div>
          </div>
          <div className="grid2">
            <div className="row"><label className="lbl">Price (cents)</label><input className="inp" type="number" min={1} max={99} value={price} onChange={e=>setPrice(Number(e.target.value))}/></div>
            <div className="row"><label className="lbl">Contracts</label><input className="inp" type="number" min={1} value={qty} onChange={e=>setQty(Number(e.target.value))}/></div>
          </div>
          <div className="payout">
            <div className="prow"><span className="pk">Cost</span><span className="pv pv-r">{fmt$(parseFloat(cost))}</span></div>
            <div className="prow"><span className="pk">Payout if correct</span><span className="pv pv-g">{fmt$(parseFloat(payout))}</span></div>
            <div className="prow"><span className="pk">Profit</span><span className="pv pv-g">{fmt$(parseFloat(payout)-parseFloat(cost))}</span></div>
          </div>
          <button className={`btn ${side==="yes"?"btn-yes":"btn-no"}`} onClick={handleTrade} disabled={trading||!connected||!selMkt}>
            {trading?"Placing...":!connected?"Connect First":`Buy ${side.toUpperCase()} — ${fmt$(parseFloat(cost))}`}
          </button>
        </div>
      </div>

      <div className="sec">Trade Log</div>
      <div className="log">
        <div className="log-hdr"><div className="log-ttl">Orders ({tlog.length})</div>{tlog.length>0&&<button onClick={()=>setTlog([])} style={{background:"none",border:"none",color:"var(--muted)",cursor:"pointer",fontFamily:"var(--mono)",fontSize:"11px"}}>Clear</button>}</div>
        {tlog.length===0?<div className="log-empty">No trades yet</div>:tlog.map((t,i)=>(
          <div key={i} className="log-row">
            <span className="lt">{t.time}</span><span className="lk">{t.ticker}</span>
            <span className={`ls ${t.side==="yes"?"ls-y":"ls-n"}`}>{t.side==="yes"?"▲ YES":"▼ NO"}</span>
            <span className="lp">{t.qty}x @ {t.price}c</span>
            <span style={{fontFamily:"var(--mono)",fontSize:"10px",color:"var(--muted)"}}>{t.status}</span>
          </div>
        ))}
      </div>

      <div className="toasts">{toasts.map(t=>(
        <div key={t.id} className={`toast ${t.type}`}>
          <span>{t.type==="ok"?"✓":t.type==="err"?"✗":"ℹ"}</span><span>{t.msg}</span>
        </div>
      ))}</div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTML
