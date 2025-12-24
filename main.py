import os
import time
import json
import requests
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import create_engine, String, Integer, Float, Boolean, ForeignKey, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship

# =========================
# ENV VARS (set in Render)
# =========================
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "dev_verify_token_change_me")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")

# SQLite file (Render note: filesystem may reset on redeploy/free tier)
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./mvp.db")

GRAPH_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"

# =========================
# DB
# =========================
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    wa_id: Mapped[str] = mapped_column(String, primary_key=True)  # WhatsApp sender id
    balance: Mapped[float] = mapped_column(Float, default=1000.0)

class Market(Base):
    __tablename__ = "markets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    yes_price: Mapped[float] = mapped_column(Float, default=0.50)
    no_price: Mapped[float] = mapped_column(Float, default=0.50)

    bets = relationship("Bet", back_populates="market")

class Bet(Base):
    __tablename__ = "bets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_id: Mapped[str] = mapped_column(ForeignKey("users.wa_id"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    side: Mapped[str] = mapped_column(String)  # YES/NO
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[int] = mapped_column(Integer)
    ts: Mapped[int] = mapped_column(Integer, default=lambda: int(time.time()))

    market = relationship("Market", back_populates="bets")

Base.metadata.create_all(bind=engine)

# =========================
# WhatsApp send helpers
# =========================
def send_whatsapp(payload: dict):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("Missing WHATSAPP_TOKEN or PHONE_NUMBER_ID env vars")
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    r = requests.post(GRAPH_URL, headers=headers, data=json.dumps(payload), timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"WhatsApp send failed {r.status_code}: {r.text}")
    return r.json()

def send_text(to_wa_id: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": text},
    }
    return send_whatsapp(payload)

def send_yes_no_buttons(to_wa_id: str, market: Market):
    # Reply buttons (up to 3) are supported by WhatsApp Cloud API. :contentReference[oaicite:2]{index=2}
    body = f"{market.question}\n\nYES: {market.yes_price:.2f} | NO: {market.no_price:.2f}\n\nChoose:"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"BET|{market.id}|YES", "title": f"YES ({market.yes_price:.2f})"}},
                    {"type": "reply", "reply": {"id": f"BET|{market.id}|NO",  "title": f"NO ({market.no_price:.2f})"}},
                ]
            },
        },
    }
    return send_whatsapp(payload)

# =========================
# Market mechanics (toy)
# =========================
def apply_price_impact(m: Market, side: str, qty: int):
    impact = min(0.10, 0.001 * qty)
    if side == "YES":
        m.yes_price = min(0.99, m.yes_price + impact)
        m.no_price = max(0.01, 1.0 - m.yes_price)
    else:
        m.no_price = min(0.99, m.no_price + impact)
        m.yes_price = max(0.01, 1.0 - m.no_price)

# =========================
# FastAPI app
# =========================
app = FastAPI(title="WhatsApp Prediction MVP (Play Money)")

def ensure_seed_markets():
    with SessionLocal() as db:
        count = db.scalar(select(Market).count())  # SQLAlchemy 2: doesn't support .count() like this in all DBs
        # safer:
        count = db.scalar(select(Market.id).limit(1))
        if count is not None:
            return
        db.add_all([
            Market(question="Will India win the next match?", yes_price=0.50, no_price=0.50),
            Market(question="Will it rain in Mumbai tomorrow?", yes_price=0.45, no_price=0.55),
        ])
        db.commit()

ensure_seed_markets()

def list_markets_text(db) -> str:
    markets = db.scalars(select(Market).order_by(Market.id.asc())).all()
    lines = ["Available markets (send the market number):"]
    for m in markets:
        status = "OPEN" if m.is_open else "CLOSED"
        lines.append(f"{m.id}) {m.question}  [YES {m.yes_price:.2f} / NO {m.no_price:.2f}] ({status})")
    lines.append("\nCommands: markets | balance | <market_id>")
    return "\n".join(lines)

def get_or_create_user(db, wa_id: str) -> User:
    u = db.get(User, wa_id)
    if not u:
        u = User(wa_id=wa_id, balance=1000.0)
        db.add(u)
        db.commit()
        db.refresh(u)
    return u

def place_bet(db, wa_id: str, market_id: int, side: str, qty: int = 10):
    u = get_or_create_user(db, wa_id)
    m = db.get(Market, market_id)
    if not m:
        send_text(wa_id, "Market not found. Send 'markets'.")
        return
    if not m.is_open:
        send_text(wa_id, "Market is closed.")
        return

    side = side.upper()
    if side not in ("YES", "NO"):
        send_text(wa_id, "Invalid side.")
        return

    price = m.yes_price if side == "YES" else m.no_price
    cost = qty * price
    if u.balance < cost:
        send_text(wa_id, f"Insufficient balance. Need {cost:.2f}, you have {u.balance:.2f}")
        return

    u.balance -= cost
    b = Bet(wa_id=wa_id, market_id=m.id, side=side, price=price, qty=qty)
    db.add(b)
    apply_price_impact(m, side, qty)
    db.commit()

    send_text(
        wa_id,
        f"✅ Bet placed!\nMarket {m.id}: {m.question}\nYou: BUY {side} @ {price:.2f} × {qty}\nBalance: {u.balance:.2f}"
    )

# -------- Webhook verify (GET) --------
@app.get("/webhook", response_class=PlainTextResponse)
def verify_webhook(request: Request):
    # Meta webhook verification expects hub.challenge echo if verify token matches. :contentReference[oaicite:3]{index=3}
    qp = request.query_params
    mode = qp.get("hub.mode")
    token = qp.get("hub.verify_token")
    challenge = qp.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return challenge
    raise HTTPException(status_code=403, detail="Webhook verification failed")

# -------- Incoming messages (POST) --------
@app.post("/webhook")
async def inbound(request: Request):
    data = await request.json()

    try:
        entry = (data.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return {"ok": True}  # statuses etc.

        msg = messages[0]
        wa_id = msg.get("from")
        if not wa_id:
            return {"ok": True}

        with SessionLocal() as db:
            get_or_create_user(db, wa_id)

            # 1) Button tap
            if msg.get("type") == "interactive":
                inter = msg.get("interactive") or {}
                if inter.get("type") == "button_reply":
                    btn_id = inter["button_reply"]["id"]  # BET|<market_id>|YES
                    parts = btn_id.split("|")
                    if len(parts) == 3 and parts[0] == "BET":
                        market_id = int(parts[1])
                        side = parts[2]
                        place_bet(db, wa_id, market_id, side, qty=10)
                        return {"ok": True}

            # 2) Text message
            text = ""
            if msg.get("type") == "text":
                text = (msg["text"]["body"] or "").strip().lower()

            if text in ("hi", "hello", "start"):
                send_text(wa_id, "Welcome! Type 'markets' to see questions, or 'balance'.")
            elif text == "markets":
                send_text(wa_id, list_markets_text(db))
            elif text == "balance":
                u = db.get(User, wa_id)
                send_text(wa_id, f"Your balance: {u.balance:.2f}")
            elif text.isdigit():
                m = db.get(Market, int(text))
                if not m:
                    send_text(wa_id, "Market not found. Type 'markets'.")
                else:
                    send_yes_no_buttons(wa_id, m)
            else:
                send_text(wa_id, "Send: markets | balance | <market_id> (example: 1)")

        return {"ok": True}
    except Exception as e:
        # Never crash WhatsApp webhook
        return {"ok": False, "error": str(e)}

# Health check
@app.get("/")
def root():
    return {"ok": True, "service": "whatsapp-prediction-mvp"}
