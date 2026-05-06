import os
import logging
import httpx

logger = logging.getLogger(__name__)

CRYPTO_BOT_API = "https://pay.crypt.bot/api"
PRICE_RUB = 550
SUBSCRIPTION_DAYS = 5


def _token() -> str:
    return os.getenv("CRYPTO_BOT_TOKEN", "")


async def create_invoice(user_id: int) -> dict | None:
    """Creates a CryptoBot invoice and returns {invoice_id, pay_url}"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{CRYPTO_BOT_API}/createInvoice",
                headers={"Crypto-Pay-API-Token": _token()},
                json={
                    "currency_type": "fiat",
                    "fiat": "RUB",
                    "amount": str(PRICE_RUB),
                    "description": f"Avito Ringer — подписка на 5 дней",
                    "payload": str(user_id),
                    "paid_btn_name": "callback",
                    "paid_btn_url": "https://t.me/avtring_bot",
                    "expires_in": 3600,
                },
            )
            data = resp.json()
            if data.get("ok"):
                inv = data["result"]
                return {
                    "invoice_id": str(inv["invoice_id"]),
                    "pay_url": inv["bot_invoice_url"],
                }
            logger.error(f"CryptoBot createInvoice error: {data}")
    except Exception as e:
        logger.error(f"CryptoBot request failed: {e}")
    return None


async def check_invoice(invoice_id: str) -> bool:
    """Returns True if invoice is paid"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CRYPTO_BOT_API}/getInvoices",
                headers={"Crypto-Pay-API-Token": _token()},
                params={"invoice_ids": invoice_id},
            )
            data = resp.json()
            if data.get("ok"):
                items = data["result"].get("items", [])
                if items:
                    return items[0].get("status") == "paid"
    except Exception as e:
        logger.error(f"CryptoBot check failed: {e}")
    return False
