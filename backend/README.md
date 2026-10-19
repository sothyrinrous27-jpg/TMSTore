# TM Store backend (starter)

This service is the backend for the existing HTML/JavaScript Telegram Mini App.

## Important security status

This is a starter, not ready for live payments yet. Before enabling checkout, finish Telegram Mini App `initData` HMAC verification in `create_order`, validate the full Bakong transaction response (receiver, currency, and amount), and test the installed KHQR SDK version against your merchant's Bakong account. Do not deploy with real payments until those checks pass. Never trust prices or payment status supplied by the browser.

## Files

- `main.py`: FastAPI endpoints, PostgreSQL persistence, KHQR generation/status checks, Telegram admin webhook.
- `requirements.txt`: Python dependencies.
- `.env.example`: environment variable template; copy values into hosting environment settings, never commit real secrets.

## Local setup

1. Create a PostgreSQL database (Supabase is one option).
2. Install Python 3.11 or 3.12.
3. From this folder run:
   `python -m venv .venv`
   Activate it, then run `pip install -r requirements.txt`.
4. Set environment variables using a local `.env` file (copy `.env.example`; never commit the real `.env`).
5. Run `uvicorn main:app --reload --port 8000`.
6. Open `http://localhost:8000/health` to test.

## Hosting

Deploy this folder as a Python web service. Build command: `pip install -r requirements.txt`; start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Add every environment variable from `.env.example` in the host dashboard. Use a persistent PostgreSQL database; do not rely on a host's temporary filesystem for orders.

## Telegram webhook

After deployment, configure Telegram's webhook to:
`https://YOUR_BACKEND_DOMAIN/telegram/webhook`
using Telegram's `setWebhook` API and the same `TELEGRAM_WEBHOOK_SECRET` as `secret_token`. Set the bot's admin chat ID to your numeric Telegram user ID. The bot must be started by the admin account first.

## KHQR

The backend uses the `bakong-khqr` Python package for QR generation and MD5 payment lookup. SDK behavior and the exact account/currency fields must be verified with the merchant's official integration details before production. Payment status must only change to PAID after a confirmed transaction matching the expected merchant, amount, and currency.
