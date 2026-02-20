import os
import re
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from playwright.async_api import async_playwright


# ====== ENV ======
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)
BOT_TOKEN = os.getenv("BOT_TOKEN")


# ====== Адреса (у каждой кнопки свой сайт) ======
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
        "city": "м. Київ",       # <-- ПОТОМ заменишь на точные
        "street": "вул. Антоновича",    # <-- ПОТОМ заменишь
        "house": "88",          # <-- ПОТОМ заменишь
    },
}


def _extract_csrf(html: str) -> Optional[str]:
    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_update_timestamp(html: str) -> str:
    # Пример: updateTimestamp":"22:35 20.02.2026"
    m = re.search(r'updateTimestamp"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else ""


async def fetch_current_outage_via_browser(page_url: str, ajax_url: str, city: str, street: str) -> Dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # 1) Открываем страницу, чтобы получить cookies/защиту
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        html = await page.content()

        csrf = _extract_csrf(html)
        update_fact = _extract_update_timestamp(html)

        # 2) POST в ajax как браузер
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": page_url,
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf

        form = {
            "method": "getHomeNum",
            "data[0][name]": "city",
            "data[0][value]": city,
            "data[1][name]": "street",
            "data[1][value]": street,
            "data[2][name]": "updateFact",
            "data[2][value]": update_fact,
        }

        resp = await ctx.request.post(ajax_url, form=form, headers=headers, timeout=60000)
        text = await resp.text()

        # если вдруг пришел HTML
        ct = (resp.headers.get("content-type") or "").lower()
        if "application/json" not in ct:
            await browser.close()
            raise RuntimeError(f"DTEK повернув НЕ JSON. HTTP={resp.status} CT={ct} TEXT={text[:200]}")

        data = await resp.json()
        await browser.close()
        return data


def format_current_outage(api_json: Dict[str, Any], house: str) -> str:
    if not api_json.get("result"):
        return "❌ API повернув result=false"

    data = api_json.get("data", {}) or {}
    rec = data.get(house) or data.get("") or next(iter(data.values()), None)

    if not isinstance(rec, dict):
        return "❌ Не можу знайти дані по будинку"

    sub_type = rec.get("sub_type") or "—"
    start_date = rec.get("start_date") or "—"
    end_date = rec.get("end_date") or "—"
    type_ = str(rec.get("type") or "")
    reasons = rec.get("sub_type_reason") or []
    reason = reasons[0] if reasons else "—"
    upd = api_json.get("updateTimestamp") or "—"

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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(ADDRESSES["HOME"]["label"], callback_data="LIGHT_HOME")],
        [InlineKeyboardButton(ADDRESSES["MOM"]["label"], callback_data="LIGHT_MOM")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Обери адресу:", reply_markup=build_keyboard())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    key = "HOME" if q.data == "LIGHT_HOME" else "MOM" if q.data == "LIGHT_MOM" else None
    if not key:
        await q.message.reply_text("Невідома кнопка 😅")
        return

    cfg = ADDRESSES[key]

    try:
        api_json = await fetch_current_outage_via_browser(
            cfg["page_url"], cfg["ajax_url"], cfg["city"], cfg["street"]
        )
        msg = format_current_outage(api_json, cfg["house"])
        await q.message.reply_text(f"{cfg['label']}\n\n{msg}")
    except Exception as e:
        await q.message.reply_text(f"Не вдалося отримати дані 😕\nПомилка: {e}")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(f"BOT_TOKEN не знайдено. Перевір файл: {ENV_PATH}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()


if __name__ == "__main__":
    main()