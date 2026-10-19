import base64
import io
import json
import os
import secrets
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import qrcode
import requests
from bakong_khqr import KHQR
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
MINI_APP_URL = os.environ.get("MINI_APP_URL", "")
BAKONG_TOKEN = os.environ.get("BAKONG_TOKEN", "")
BAKONG_ACCOUNT_ID = os.environ.get("BAKONG_ACCOUNT_ID", "")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "TM Store")
MERCHANT_CITY = os.environ.get("MERCHANT_CITY", "Phnom Penh")
PAYMENT_CURRENCY = os.environ.get("PAYMENT_CURRENCY", "USD").upper()
EXCHANGE_RATE_KHR = Decimal(os.environ.get("EXCHANGE_RATE_KHR", "4100"))
PAYMENT_EXPIRY_MINUTES = int(os.environ.get("PAYMENT_EXPIRY_MINUTES", "15"))

app = FastAPI(title="TM Store Sales API", version="0.1.0")

# For production, set CORS_ORIGINS to your exact GitHub Pages/custom domain.
cors_origins = [x.strip() for x in os.environ.get("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Bot-Api-Secret-Token"],
)

def db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'General',
            price_usd NUMERIC(12,2) NOT NULL CHECK (price_usd >= 0),
            emoji TEXT NOT NULL DEFAULT '🛍️',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS orders (
            id BIGSERIAL PRIMARY KEY,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            delivery_address TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL,
            total_amount NUMERIC(12,2) NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'PENDING',
            order_status TEXT NOT NULL DEFAULT 'NEW',
            khqr_string TEXT,
            khqr_md5 TEXT,
            notified_paid BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id BIGSERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            unit_price_usd NUMERIC(12,2) NOT NULL,
            subtotal_usd NUMERIC(12,2) NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS orders_khqr_md5_unique
        ON orders(khqr_md5) WHERE khqr_md5 IS NOT NULL;
        """)
        cur.execute("SELECT COUNT(*) AS n FROM products")
        if cur.fetchone()["n"] == 0:
            cur.executemany(
                "INSERT INTO products (id, name, category, price_usd, emoji) VALUES (%s,%s,%s,%s,%s)",
                [
                    (1, "Lon Koom Nooble Black Eau de Parfum", "Beauty", 9, "🧴"),
                    (2, "Smart Watch", "Electronics", 25, "⌚"),
                    (3, "Casual T-Shirt", "Fashion", 8, "👕"),
                    (4, "Travel Backpack", "Fashion", 18, "🎒"),
                    (5, "Water Bottle", "Lifestyle", 5, "🥤"),
                    (6, "Desk Lamp", "Lifestyle", 12, "💡"),
                ],
            )
        conn.commit()

@app.on_event("startup")
def startup():
    if DATABASE_URL:
        init_db()

class OrderItemIn(BaseModel):
    id: int
    quantity: int = Field(ge=1, le=99)

class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=3, max_length=30)
    deliveryAddress: str = Field(min_length=3, max_length=500)
    notes: str = Field(default="", max_length=500)

class OrderIn(BaseModel):
    customer: CustomerIn
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)

def telegram_send(text: str, reply_markup: dict | None = None):
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        return
    payload: dict[str, Any] = {"chat_id": ADMIN_CHAT_ID, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=12,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Do not expose credentials or Telegram's full request URL to customers.
        print("Telegram notification failed:", type(exc).__name__)

def money(v):
    return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def make_khqr(amount: Decimal, order_id: int):
    if not BAKONG_TOKEN or not BAKONG_ACCOUNT_ID:
        raise HTTPException(status_code=503, detail="Bakong payment is not configured yet")
    if PAYMENT_CURRENCY not in ("USD", "KHR"):
        raise HTTPException(status_code=500, detail="PAYMENT_CURRENCY must be USD or KHR")
    try:
        khqr = KHQR(BAKONG_TOKEN)
        payment_amount = amount if PAYMENT_CURRENCY == "USD" else int(
            (amount * EXCHANGE_RATE_KHR).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        qr_string = khqr.create_qr(
            account_id=BAKONG_ACCOUNT_ID,
            merchant_name=MERCHANT_NAME,
            merchant_city=MERCHANT_CITY,
            amount=float(payment_amount) if PAYMENT_CURRENCY == "USD" else payment_amount,
            currency=PAYMENT_CURRENCY,
            bill_number=f"TM{order_id}",
        )
        md5 = khqr.generate_md5(qr_string)
        image = qrcode.make(qr_string)
        output = io.BytesIO()
        image.save(output, format="PNG")
        image_data = base64.b64encode(output.getvalue()).decode("ascii")
        return qr_string, md5, f"data:image/png;base64,{image_data}", payment_amount
    except HTTPException:
        raise
    except Exception as exc:
        print("KHQR generation failed:", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not generate KHQR. Check the Bakong configuration and SDK version.")

@app.get("/health")
def health():
    return {"ok": True, "service": "tm-store-sales-api"}

@app.get("/api/products")
def list_products():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, name, category AS cat, price_usd AS price, emoji
            FROM products WHERE active = TRUE ORDER BY id
        """)
        rows = cur.fetchall()
    return [dict(row, price=float(row["price"])) for row in rows]

@app.post("/api/orders")
def create_order(body: OrderIn, request: Request):
    # Telegram Mini App initData MUST be validated server-side before production.
    # This endpoint is not safe to expose publicly until that validation is implemented.
    raw_init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not raw_init_data:
        raise HTTPException(status_code=401, detail="Open the store from Telegram to place an order")
    # TODO: verify Telegram WebApp initData HMAC using BOT_TOKEN and enforce a short auth_date age.
    # Never trust product names or prices sent by the browser; look up active prices here.
    unique_ids = list({item.id for item in body.items})
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, price_usd FROM products WHERE active = TRUE AND id = ANY(%s)",
            (unique_ids,),
        )
        product_map = {row["id"]: row for row in cur.fetchall()}
        if len(product_map) != len(unique_ids):
            raise HTTPException(status_code=400, detail="One or more products are unavailable")
        total = Decimal("0.00")
        checked_items = []
        for item in body.items:
            product = product_map[item.id]
            unit = money(product["price_usd"])
            subtotal = money(unit * item.quantity)
            total += subtotal
            checked_items.append((product, item.quantity, unit, subtotal))
        total = money(total)
        cur.execute("""
            INSERT INTO orders (customer_name, phone, delivery_address, notes, currency, total_amount)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, (body.customer.name.strip(), body.customer.phone.strip(),
              body.customer.deliveryAddress.strip(), body.customer.notes.strip(),
              PAYMENT_CURRENCY, total))
        order_id = cur.fetchone()["id"]
        for product, quantity, unit, subtotal in checked_items:
            cur.execute("""
                INSERT INTO order_items (order_id, product_id, product_name, quantity, unit_price_usd, subtotal_usd)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (order_id, product["id"], product["name"], quantity, unit, subtotal))
        qr_string, md5, qr_data_url, payment_amount = make_khqr(total, order_id)
        cur.execute(
            "UPDATE orders SET khqr_string=%s, khqr_md5=%s WHERE id=%s",
            (qr_string, md5, order_id),
        )
        conn.commit()

    item_lines = "\n".join(f"• {p['name']} × {q} = ${s}" for p, q, _, s in checked_items)
    telegram_send(
        f"🛒 NEW TM STORE ORDER #{order_id}\n"
        f"Customer: {body.customer.name}\nPhone: {body.customer.phone}\n"
        f"Address: {body.customer.deliveryAddress}\n"
        f"Items:\n{item_lines}\nTotal: {total} USD\n"
        f"Payment: PENDING\nUse /orders to review."
    )
    return {
        "orderId": order_id,
        "totalUSD": float(total),
        "paymentCurrency": PAYMENT_CURRENCY,
        "paymentAmount": float(payment_amount) if PAYMENT_CURRENCY == "USD" else int(payment_amount),
        "qrString": qr_string,
        "qrImageDataUrl": qr_data_url,
        "paymentStatus": "PENDING",
        "expiresInMinutes": PAYMENT_EXPIRY_MINUTES,
    }

@app.get("/api/orders/{order_id}/status")
def order_status(order_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
        order = cur.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["payment_status"] != "PAID" and order["khqr_md5"]:
            if not BAKONG_TOKEN:
                raise HTTPException(status_code=503, detail="Bakong payment is not configured")
            try:
                result = KHQR(BAKONG_TOKEN).check_payment(order["khqr_md5"])
                # SDK releases may return a status string or a response object.
                status = result.get("status") if isinstance(result, dict) else str(result)
                status = str(status).upper()
                if status == "PAID" or status == "SUCCESS":
                    cur.execute(
                        "UPDATE orders SET payment_status='PAID', order_status='PAID' "
                        "WHERE id=%s AND payment_status <> 'PAID' RETURNING id",
                        (order_id,),
                    )
                    changed = cur.fetchone()
                    conn.commit()
                    if changed:
                        telegram_send(
                            f"✅ PAYMENT CONFIRMED — TM STORE ORDER #{order_id}\n"
                            f"Amount: {order['total_amount']} {order['currency']}\n"
                            f"Customer: {order['customer_name']}\nPhone: {order['phone']}"
                        )
            except HTTPException:
                raise
            except Exception as exc:
                print("Bakong status check failed:", type(exc).__name__)
                # Keep status pending on API/network errors; never infer a payment.
        cur.execute(
            "SELECT id, payment_status AS \"paymentStatus\", order_status AS \"orderStatus\", "
            "total_amount AS total, currency, created_at FROM orders WHERE id=%s",
            (order_id,),
        )
        row = cur.fetchone()
    return dict(row, total=float(row["total"]), createdAt=row["created_at"].isoformat())

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not WEBHOOK_SECRET or not secrets.compare_digest(x_telegram_bot_api_secret_token or "", WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    chat_id = str(chat.get("id", ""))
    if not text.startswith("/") or not ADMIN_CHAT_ID or chat_id != ADMIN_CHAT_ID:
        return {"ok": True}
    command = text.split()[0].split("@")[0].lower()
    if command == "/start":
        reply = "TM Store admin bot is online. Commands: /orders, /products, /help"
        if MINI_APP_URL:
            reply_markup = {"inline_keyboard": [[{"text": "Open TM Store", "web_app": {"url": MINI_APP_URL}}]]}
            telegram_send_to(chat_id, reply, reply_markup)
            return {"ok": True}
    elif command == "/help":
        reply = "Admin commands:\n/orders - latest orders\n/products - list products\n/addproduct Name | Category | PriceUSD | Emoji"
    elif command == "/products":
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,name,price_usd,active FROM products ORDER BY id")
            rows = cur.fetchall()
        reply = "\n".join(f"#{r['id']} {r['name']} — ${r['price_usd']} {'(active)' if r['active'] else '(hidden)'}" for r in rows) or "No products"
    elif command == "/orders":
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,customer_name,total_amount,currency,payment_status,order_status,created_at FROM orders ORDER BY id DESC LIMIT 10")
            rows = cur.fetchall()
        reply = "\n".join(
            f"#{r['id']} {r['customer_name']} — {r['total_amount']} {r['currency']} — {r['payment_status']} — {r['order_status']}"
            for r in rows
        ) or "No orders yet"
    elif command == "/addproduct":
        parts = text.partition(" ")[2].split("|")
        if len(parts) < 3:
            reply = "Format: /addproduct Name | Category | PriceUSD | Emoji"
        else:
            try:
                name, category, price = parts[0].strip(), parts[1].strip(), money(parts[2].strip())
                emoji = parts[3].strip() if len(parts) > 3 and parts[3].strip() else "🛍️"
                if not name or not category or price < 0:
                    raise ValueError()
                with db() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO products(name,category,price_usd,emoji) VALUES(%s,%s,%s,%s) RETURNING id",
                        (name, category, price, emoji),
                    )
                    product_id = cur.fetchone()["id"]
                    conn.commit()
                reply = f"Added product #{product_id}: {name} — ${price}"
            except Exception:
                reply = "Could not add product. Check format and ensure price is a number."
    else:
        reply = "Unknown command. Send /help."
    telegram_send_to(chat_id, reply)
    return {"ok": True}

def telegram_send_to(chat_id: str, text: str, reply_markup: dict | None = None):
    if not BOT_TOKEN:
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload, timeout=12,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print("Telegram reply failed:", type(exc).__name__)
