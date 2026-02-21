import os
import re
from typing import Any, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from playwright.async_api import async_playwright


BOT_TOKEN = os.getenv("BOT_TOKEN")

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


def _extract_csrf(html: str) -> Optional[str]:
    m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_update_fact(html: str) -> str:
    """
    DTEK часто кладёт updateFact/updateTimestamp в JS.
    Ищем максимально “широко”, чтобы не ловить пустую строку.
    """
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


async def fetch_current_outage(page_url: str, ajax_url: str, city: str, street: str) -> Dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        html = await page.content()

        csrf = _extract_csrf(html)
        update_fact = _extract_update_fact(html)

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
        ct = (resp.headers.get("content-type") or "").lower()

        if resp.status != 200:
            await browser.close()
            raise RuntimeError(f"DTEK HTTP={resp.status} CT={ct} TEXT={text[:250]}")

        if "application/json" not in ct and not text.strip().startswith("{"):
            await browser.close()
            raise RuntimeError(f"DTEK повернув НЕ JSON. CT={ct} TEXT={text[:250]}")

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
        api_json = await fetch_current_outage(cfg["page_url"], cfg["ajax_url"], cfg["city"], cfg["street"])
        msg = format_current_outage(api_json, cfg["house"])
        await q.message.reply_text(f"{cfg['label']}\n\n{msg}")
    except Exception as e:
        await q.message.reply_text(f"Не вдалося отримати дані 😕\nПомилка: {e}")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не знайдено. Додай його в Environment Variables як BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.run_polling()


if __name__ == "__main__":
    main()
