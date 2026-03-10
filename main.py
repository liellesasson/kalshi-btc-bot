import os, time, base64, httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

app = FastAPI(title="Kalshi BTC Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def load_key(pem_str: str):
    try:
        return serialization.load_pem_private_key(
            pem_str.encode(), password=None, backend=default_backend()
        )
    except Exception as e:
        raise HTTPException(400, f"Invalid private key: {e}")

def sign(private_key, timestamp: str, method: str, path: str) -> str:
    path_no_query = path.split("?")[0]
    msg = f"{timestamp}{method}{path_no_query}".encode()
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
        r = await client.get(KALSHI_BASE + "/portfolio/balance", headers=headers, timeout=10)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/positions")
async def get_positions(body: AuthBase):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/positions"
    headers = kalshi_headers(pk, body.key_id, "GET", path)
    async with httpx.AsyncClient() as client:
        r = await client.get(KALSHI_BASE + "/portfolio/positions", headers=headers, timeout=10)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.get("/api/markets")
async def get_markets(series: str = "KXBTC15M", status: str = "open", limit: int = 6):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{KALSHI_BASE}/markets",
            params={"series_ticker": series, "status": status, "limit": limit},
            timeout=10,
        )
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/order")
async def place_order(body: OrderRequest):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_headers(pk, body.key_id, "POST", path)
    payload = {
        "action": "buy",
        "count": body.count,
        "side": body.side,
        "ticker": body.ticker,
        "type": "limit",
        "yes_price": body.price if body.side == "yes" else 100 - body.price,
        "no_price": body.price if body.side == "no" else 100 - body.price,
        "client_order_id": f"btcbot_{int(time.time()*1000)}",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(KALSHI_BASE + "/portfolio/orders", headers=headers, json=payload, timeout=10)
    if r.status_code not in (200, 201):
        raise HTTPException(r.status_code, r.text)
    return r.json()

@app.post("/api/orders")
async def list_orders(body: AuthBase):
    pk = load_key(body.private_key_pem)
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_headers(pk, body.key_id, "GET", path)
    async with httpx.AsyncClient() as client:
        r = await client.get(KALSHI_BASE + "/portfolio/orders?limit=20", headers=headers, timeout=10)
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return r.json()
