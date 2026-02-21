import os
import re
import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes


# -------------------------
# LOGGING (Render Logs)
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("dtek-bot")


# -------------------------
# CONFIG
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADDRESSES: Dict[str, Dict[str, str]] = {
    "HOME": {
        "label": "💡 Світло — Дім",
        "page_url": "https://www.dtek-krem.com.ua/ua/shutdowns",
        "ajax_url": "https://www.dtek-krem.com.ua/ua/ajax",
        "city": "с. Нове",
        "street": "вул. Незалежності",
        "house": "26",
    },
    "MOM": {
        "label": "💡 Світло — Мама",
        "page_url": "https://www.dtek-kem.com.ua/ua/shutdowns",
        "ajax_url": "https://www.dtek-kem.com.ua/ua/ajax",
        "city": "м. Київ",
        "street": "вул. Антоновича",
        "house": "88",
    },
}

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

TIMEOUT = 40
RETRIES = 2


# -------------------------
# HELPERS
# -------------------------
def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _extract_csrf(html: str) -> Optional[str]:
    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html, re.I)
    return m.group(1) if m else None


def _extract_update_fact(html: str) -> str:
    patterns = [
        r'updateFact"\s*:\s*"([^"]+)"',
        r'updateTimestamp"\s*:\s*"([^"]+)"',
        r'updateFact\s*=\s*"([^"]+)"',
        r'updateTimestamp\s*=\s*"([^"]+)"',
    ]
    for p in patterns:
        m = re.search(p, html)
        if m:
            return m.group(1)
    return ""


def _session() -> requests.Session:
    s = requests.Session()
    # можно добавить базовые заголовки в сессию
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
            "Connection": "keep-alive",
        }
    )
    return s


def _fetch_current_outage_sync(
    page_url: str, ajax_url: str, city: str, street: str
) -> Dict[str, Any]:
    """
    1) GET сторінки => cookies + csrf + updateFact
    2) POST /ajax method=getHomeNum
    """
    s = _session()

    # --- 1) GET
    headers_get = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    r = s.get(page_url, headers=headers_get, timeout=TIMEOUT)
    r.raise_for_status()
    html = r.text

    csrf = _extract_csrf(html)
    update_fact = _extract_update_fact(html)

    # --- 2) POST
    headers_post = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": page_url,
        "Origin": _origin(page_url),
    }
    if csrf:
        headers_post["X-CSRF-Token"] = csrf

    form = {
        "method": "getHomeNum",
        "data[0][name]": "city",
        "data[0][value]": city,
        "data[1][name]": "street",
        "data[1][value]": street,
        "data[2][name]": "updateFact",
        "data[2][value]": update_fact,
    }

    rr = s.post(ajax_url, data=form, headers=headers_post, timeout=TIMEOUT)
    ct = (rr.headers.get("content-type") or "").lower()
    text = rr.text or ""

    if rr.status_code != 200:
        raise RuntimeError(f"DTEK HTTP={rr.status_code} CT={ct} TEXT={text[:300]}")

    if "application/json" not in ct and not text.lstrip().startswith("{"):
        raise RuntimeError(f"DTEK повернув НЕ JSON. CT={ct} TEXT={text[:300]}")

    return rr.json()


async def fetch_current_outage(
    page_url: str, ajax_url: str, city: str, street: str
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(RETRIES + 1):
        try:
            return await asyncio.to_thread(
                _fetch_current_outage_sync, page_url, ajax_url, city, street
            )
        except Exception as e:
            last_err = e
            log.warning("DTEK fetch failed (attempt %s/%s): %s", attempt + 1, RETRIES + 1, e)
            if attempt < RETRIES:
                await asyncio.sleep(1.0)
    raise last_err if last_err else RuntimeError("Unknown DTEK error")


def format_current_outage(api_json: Dict[str, Any], house: str) -> str:
    if not api_json.get("result"):
        return "❌ API повернув result=false (DTEK не прийняв запит або немає даних)"

    data = api_json.get("data", {}) or {}
    rec = data.get(house) or data.get("") or next(iter(data.values()), None)

    if not isinstance(rec, dict):
        return f"❌ Не можу знайти дані по будинку. Відповідь: {str(api_json)[:250]}"

    sub_type = rec.get("sub_type") or "—"
    start_date = rec.get("start_date") or "—"
    end_date = rec.get("end_date") or "—"
    type_ = str(rec.get("type") or "")
    reasons = rec.get("sub_type_reason") or []
    reason = reasons[0] if reasons else "—"
    upd = api_json.get("updateTimestamp") or api_json.get("updateFact") or "—"

    has_outage = (type_ == "2") and (start_date != "—") and (end_date != "—")
    status_line = "🔴 Немає світла" if has_outage else "🟢 Світло є (або немає відключення зараз)"

    return (
        f"{status_line}\n"
        f"Причина: {sub_type}\n"
        f"Група/черга: {reason}\n"
        f"Початок: {start_date}\n"
        f"Орієнтовно до: {end_date}\n"
        f"Оновлено: {upd}"
    )


def build_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(ADDRESSES["HOME"]["label"], callback_data="LIGHT_HOME")],
            [InlineKeyboardButton(ADDRESSES["MOM"]["label"], callback_data="LIGHT_MOM")],
        ]
    )


# -------------------------
# TELEGRAM HANDLERS
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text("Обери адресу:", reply_markup=build_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return

    await q.answer()

    key = "HOME" if q.data == "LIGHT_HOME" else "MOM" if q.data == "LIGHT_MOM" else None
    if not key:
        if q.message:
            await q.message.reply_text("Невідома кнопка 😅")
        return

    cfg = ADDRESSES[key]

    # сразу покажем "думаю" (по желанию)
    if q.message:
        await q.message.reply_text("⏳ Перевіряю…")

    try:
        api_json = await fetch_current_outage(
            cfg["page_url"], cfg["ajax_url"], cfg["city"], cfg["street"]
        )
        msg = format_current_outage(api_json, cfg["house"])
        if q.message:
            await q.message.reply_text(f"{cfg['label']}\n\n{msg}")
    except Exception as e:
        log.exception("Button handler error: %s", e)
        if q.message:
            await q.message.reply_text(
                "Не вдалося отримати дані 😕\n"
                f"Помилка: {e}"
            )


# -------------------------
# MAIN
# -------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не знайдено. Додай його в Render -> Environment як BOT_TOKEN.")

    # ✅ FIX для Python 3.14 (Render): вручну створюємо event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    log.info("Starting bot...")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))

    # stop_signals=None — чтобы Render не ломался на сигналах
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
