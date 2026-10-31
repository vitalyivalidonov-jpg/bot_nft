import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlparse

import aiohttp
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BusinessConnection, BufferedInputFile, LinkPreviewOptions
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB = "data/gift_offer_bot.sqlite3"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("GiftOfferBot")

if not TOKEN:
    raise RuntimeError("Заполни BOT_TOKEN в .env")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
NFT_RE = re.compile(r"^https://t\.me/nft/[A-Za-z0-9_-]+/?$", re.I)

# Активные Business-подключения администраторов.
business_connections = {}

async def db_init():
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS offers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
            business_connection_id TEXT, nft_url TEXT NOT NULL, nft_title TEXT,
            amount TEXT NOT NULL, currency TEXT NOT NULL, network TEXT,
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS connections(
            admin_id INTEGER PRIMARY KEY, connection_id TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 1)""")
        await db.commit()

async def save_connection(admin_id, connection_id, enabled):
    async with aiosqlite.connect(DB) as db:
        await db.execute("""INSERT INTO connections(admin_id,connection_id,is_enabled)
            VALUES(?,?,?) ON CONFLICT(admin_id) DO UPDATE SET
            connection_id=excluded.connection_id,is_enabled=excluded.is_enabled""",
            (admin_id, connection_id, int(enabled)))
        await db.commit()

async def get_connection(admin_id):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT connection_id,is_enabled FROM connections WHERE admin_id=?", (admin_id,))
        row = await cur.fetchone()
        return row[0] if row and row[1] else None

async def create_offer(data):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("""INSERT INTO offers
            (admin_id,recipient_id,business_connection_id,nft_url,nft_title,amount,currency,network)
            VALUES(?,?,?,?,?,?,?,?)""", data)
        await db.commit()
        return cur.lastrowid

async def get_offer(oid):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM offers WHERE id=?", (oid,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def update_status(oid, status):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE offers SET status=? WHERE id=?", (status, oid))
        await db.commit()

async def preview(url):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent":"Mozilla/5.0"}) as s:
            async with s.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    return None, None
                html = await r.text(errors="ignore")
        def meta(prop):
            patterns = [
                rf"""<meta[^>]+property=["']{prop}["'][^>]+content=["']([^"']+)""" ,
                rf"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']{prop}""" ,
            ]
            for p in patterns:
                m = re.search(p, html, re.I)
                if m: return m.group(1).replace("&amp;", "&")
            return None
        image, title = meta("og:image"), meta("og:title")
        if image and (urlparse(image).scheme != "https" or not urlparse(image).netloc):
            image = None
        return image, title
    except Exception as e:
        log.warning("NFT preview unavailable: %s", e)
        return None, None

def admin(uid):
    return uid in ADMINS

def admin_keyboard(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"paid:{oid}")],
        [InlineKeyboardButton(text="❌ Отклонить оплату", callback_data=f"reject:{oid}")],
        [InlineKeyboardButton(text="Отменить сделку", callback_data=f"cancel:{oid}")]
    ])

def client_keyboard(oid, nft_url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✖ Отказаться", callback_data=f"clientclose:{oid}"),
            InlineKeyboardButton(text="✓ Согласиться", callback_data=f"clientaccept:{oid}")
        ]
    ])

# Кнопка отправки туториала в чат
def transfer_keyboard(oid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Как передать NFT?", callback_data=f"show_tutorial:{oid}")]
    ])

# Кнопка закрытия туториала
def close_tutorial_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_tutorial")]
    ])

# Функция для генерации красивой валюты со своим ID
def get_currency_display(currency, network=None):
    if currency == "STARS":
        return '<tg-emoji emoji-id="5463289097336405244">⭐</tg-emoji>'
    elif currency == "TON":
        return '<tg-emoji emoji-id="5424608493019633959">💎</tg-emoji>'
    elif currency == "UAH":
        return '<tg-emoji emoji-id="5377505475015235101">🇺🇦</tg-emoji>'
    elif currency == "USDT":
        return f'<tg-emoji emoji-id="5345889288741461772">💵</tg-emoji> ({network})'
    return currency

# Фоновая задача для живого таймера
async def start_offer_timer(chat_id: int, message_id: int, oid: int, business_connection_id: str):
    total_seconds = 24 * 3600
    end_time = datetime.now() + timedelta(seconds=total_seconds)
    
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        remaining = (end_time - now).total_seconds()
        
        offer = await get_offer(oid)
        if not offer or offer["status"] not in ("awaiting_payment", "accepted"):
            break
            
        if remaining <= 0:
            await update_status(oid, "expired")
            try:
                expired_text = "<b>⏰ Срок действия предложения истёк.</b>"
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=expired_text,
                    business_connection_id=business_connection_id,
                    reply_markup=None
                )
            except Exception:
                pass
            break
            
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        time_str = f"Действует {hours}ч {minutes}м."
        
        try:
            sender_chat = await bot.get_chat(offer["admin_id"])
            sender_username = sender_chat.username or "Покупатель"
            
            display_currency = get_currency_display(offer["currency"], offer["network"])
            nft_title_str = f'<a href="{escape(offer["nft_url"], quote=True)}">{escape(offer["nft_title"] or "NFT")}</a>'
            
            updated_caption = (
                f"<b>@{escape(sender_username)} предлагает вам обменять "
                f"{nft_title_str} "
                f"на {escape(offer['amount'])} {display_currency}.</b>\n\n"
                f"<i>{time_str}</i>"
            )
            
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=updated_caption,
                business_connection_id=business_connection_id,
                reply_markup=client_keyboard(oid, offer["nft_url"]),
                link_preview_options=LinkPreviewOptions(is_disabled=False, show_above_text=True)
            )
        except Exception:
            pass

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    user_id = connection.user.id
    if admin(user_id):
        await save_connection(user_id, connection.id, connection.is_enabled)
        business_connections[user_id] = connection.id if connection.is_enabled else None
        log.info("Business connection updated for admin %s enabled=%s", user_id, connection.is_enabled)

@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "👋 <b>GiftOfferBot</b>\n\n"
        "Управляющий бот для визуальных NFT-сделок.\n"
        "Предложения клиентам отправляются в личный чат через подключённый Telegram Business.\n\n"
        "/help — инструкция\n/status — проверка Business-подключения"
    )

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(
        "<b>Создание сделки</b>\n"
        "<code>/send ID NFT_URL СУММА ВАЛЮТА [СЕТЬ]</code>\n\n"
        "Примеры:\n"
        "<code>/send 123456789 https://t.me/nft/GiftName 790 STARS</code>\n"
        "<code>/send 123456789 https://t.me/nft/GiftName 5.5 TON</code>\n"
        "<code>/send 123456789 https://t.me/nft/GiftName 790 UAH</code>\n"
        "<code>/send 123456789 https://t.me/nft/GiftName 20 USDT TRC20</code>\n\n"
        "Получатель должен иметь возможность получать сообщения в личном чате. "
        "Бизнес-подключение должно быть включено."
    )

@dp.message(Command("status"))
async def status(m: Message):
    if not admin(m.from_user.id):
        return
    cid = await get_connection(m.from_user.id)
    await m.answer("✅ Telegram Business подключён." if cid else
        "⚠️ Business-подключение не найдено.\nПодключи этого бота в Telegram: Настройки → Telegram Business → Чат-боты и выдай право отправлять сообщения.")

@dp.message(Command("send"))
async def send(m: Message):
    if not admin(m.from_user.id):
        await m.answer("Команда доступна только администратору.")
        return
    p = (m.text or "").split()
    if len(p) not in (5, 6):
        await m.answer("Формат:\n<code>/send ID https://t.me/nft/GiftName СУММА STARS|TON|UAH|USDT [TON|TRC20]</code>")
        return
    _, rid, url, amount, currency, *net = p
    currency = currency.upper()
    network = net[0].upper() if net else None
    if not rid.isdigit() or not NFT_RE.fullmatch(url):
        await m.answer("Проверь ID и ссылку. Нужен формат https://t.me/nft/...")
        return
    if not re.fullmatch(r"\d+(\.\d{1,6})?", amount) or float(amount) <= 0:
        await m.answer("Укажи корректную положительную сумму.")
        return
    if currency not in ("STARS", "TON", "UAH", "USDT"):
        await m.answer("Валюта должна быть STARS, TON, UAH или USDT.")
        return
    if currency == "USDT" and network not in ("TON", "TRC20"):
        await m.answer("Для USDT обязательно укажи сеть TON или TRC20.")
        return
    if currency != "USDT" and network:
        await m.answer("Сеть указывается только для USDT.")
        return
    cid = await get_connection(m.from_user.id)
    if not cid:
        await m.answer("Сначала подключи Telegram Business и проверь /status.")
        return

    img, title = await preview(url)
    title = title or url.rsplit("/", 1)[-1]
    oid = await create_offer((m.from_user.id, int(rid), cid, url, title, amount, currency, network))
    
    label = get_currency_display(currency, network)
    try:
        sender_chat = await bot.get_chat(m.from_user.id)
        sender_username = sender_chat.username or "Покупатель"
    except Exception:
        sender_username = "Покупатель"

    display_currency = get_currency_display(currency, network)

    caption = (
        f"<b>@{escape(sender_username)} предлагает вам обменять "
        f"<a href='{escape(url, quote=True)}'>{escape(title)}</a> "
        f"на {escape(amount)} {display_currency}.</b>\n\n"
        "<i>Действует 23ч 59м.</i>"
    )
    try:
        markup = client_keyboard(oid, url)
        sent_msg = await bot.send_message(
            chat_id=int(rid),
            text=caption,
            reply_markup=markup,
            business_connection_id=cid,
            link_preview_options=LinkPreviewOptions(is_disabled=False, show_above_text=True)
        )
        
        asyncio.create_task(start_offer_timer(int(rid), sent_msg.message_id, oid, cid))

        await m.answer(f"✅ Сделка #{oid} отправлена в личный чат с клиентом.")
        await m.answer(f"💼 <b>Сделка #{oid}</b>\nКлиент: <code>{rid}</code>\nСумма: <b>{escape(amount)} {label}</b>\nСтатус: ожидает оплаты.", reply_markup=admin_keyboard(oid))
    except Exception as e:
        await update_status(oid, "send_failed")
        log.exception("Business send failed")
        await m.answer("❌ Не удалось отправить в личный чат. Проверь Business-права, доступность диалога и ID.\n<code>"+escape(str(e)[:350])+"</code>")

@dp.callback_query(F.data.startswith(("paid:", "reject:", "cancel:")))
async def admin_action(c: CallbackQuery):
    if not admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True); return
    action, oid_s = c.data.split(":")
    offer = await get_offer(int(oid_s))
    if not offer or offer["admin_id"] != c.from_user.id:
        await c.answer("Сделка не найдена", show_alert=True); return

    status_map = {"paid":"payment_confirmed", "reject":"payment_rejected", "cancel":"cancelled"}
    new_status = status_map[action]
    await update_status(offer["id"], new_status)
    if action == "paid":
        paid_currency = get_currency_display(offer["currency"], offer["network"])
        gift_emoji = '<tg-emoji emoji-id="5269416360949064906">🎁</tg-emoji>'
        text = (f"<b>@{escape((await bot.get_chat(offer['admin_id'])).username or 'Покупатель')} "
            f"Оплатил {escape(offer['amount'])} {paid_currency}.</b>\n\n"
            f"Ожидается передача NFT {gift_emoji}. После чего сумма будет зачислена на удобный вам кошелек.")
    elif action == "reject":
        text = "⚠️ Оплата пока не подтверждена. Пожалуйста, проверьте платёжные данные и свяжитесь с покупателем."
    else:
        text = "❌ Сделка отменена."
    try:
        await bot.send_message(
            chat_id=offer["recipient_id"],
            text=text,
            business_connection_id=offer["business_connection_id"],
            reply_markup=transfer_keyboard(offer["id"]) if action == "paid" else None
        )
    except Exception:
        log.exception("Could not notify client about payment state")
    await c.message.edit_text(f"Сделка #{offer['id']}: <b>{new_status}</b>")
    await c.answer("Статус обновлён")

@dp.callback_query(F.data.startswith(("clientaccept:", "clientclose:")))
async def client_offer_action(c: CallbackQuery):
    action, oid_s = c.data.split(":")
    offer = await get_offer(int(oid_s))
    if not offer or c.from_user.id != offer["recipient_id"]:
        await c.answer("Эта кнопка предназначена получателю предложения.", show_alert=True)
        return

    if action == "clientaccept":
        result = "Клиент нажал «Заключить»"
        await c.answer()
    else:
        result = "Клиент нажал «Закрыть»"
        await c.answer()

    try:
        await bot.send_message(
            chat_id=offer["admin_id"],
            text=f"📩 По сделке #{offer['id']}: <b>{result}</b>."
        )
    except Exception:
        log.exception("Could not notify admin about client action")

    if action == "clientclose":
        await update_status(offer["id"], "cancelled")
        username = c.from_user.username
        client_label = f"@{escape(username)}" if username else escape(c.from_user.first_name or "Клиент")
        nft_title = (
            f'<a href="{escape(offer["nft_url"], quote=True)}">'
            f'{escape(offer["nft_title"] or "NFT")}</a>'
        )
        currency_label = get_currency_display(offer["currency"], offer["network"])

        declined_text = (
            f"<b>{client_label} отказался обменять {nft_title} "
            f"на сумму {escape(offer['amount'])} {currency_label}.</b>"
        )
        try:
            await bot.edit_message_text(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                text=declined_text,
                business_connection_id=offer["business_connection_id"],
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(show_above_text=True)
            )
        except Exception:
            log.exception("Could not replace declined offer message")
            try:
                await c.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.exception("Could not remove client offer buttons")
    else:
        await update_status(offer["id"], "accepted")
        try:
            try:
                sender_chat = await bot.get_chat(offer["admin_id"])
                sender_username = sender_chat.username
                sender_name = f"@{escape(sender_username)}" if sender_username else escape(sender_chat.first_name or "Покупатель")
            except Exception:
                sender_name = "покупатель"

            recipient_username = c.from_user.username
            recipient_name = (
                f"@{escape(recipient_username)}"
                if recipient_username
                else escape(c.from_user.first_name or "получатель")
            )
            nft_title = (
                f'<a href="{escape(offer["nft_url"], quote=True)}">'
                f'{escape(offer["nft_title"] or "NFT")}</a>'
            )

            currency_label = get_currency_display(offer["currency"], offer["network"])

            accepted_text = (
                f"Ждём, пока {sender_name} оплатит сумму "
                f"<b>{escape(offer['amount'])} {currency_label}</b>, "
                f"чтобы {recipient_name} передал ему свой {nft_title}."
            )
            await bot.edit_message_text(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                text=accepted_text,
                business_connection_id=offer["business_connection_id"],
                reply_markup=None,
                link_preview_options=LinkPreviewOptions(show_above_text=True)
            )
        except Exception:
            log.exception("Could not update accepted offer message")
            try:
                await c.message.edit_reply_markup(reply_markup=None)
            except Exception:
                log.exception("Could not remove client offer buttons")

@dp.callback_query(F.data.startswith("payinfo:"))
async def payinfo(c: CallbackQuery):
    oid = int(c.data.split(":")[1]); offer = await get_offer(oid)
    if not offer or c.from_user.id != offer["recipient_id"]:
        await c.answer("Нет доступа", show_alert=True); return
    label = get_currency_display(offer["currency"], offer["network"])
    text = (
        f"💳 <b>Условия предложения</b>\n\n"
        f"Сумма: <b>{escape(offer['amount'])} {label}</b>\n"
        "Это демонстрационная информация: бот не принимает платежи и не показывает адрес кошелька.\n"
        "Порядок расчёта согласовывается сторонами отдельно."
    )
    await c.message.answer(text)
    await c.answer()

# Отправка туториала отдельным сообщением в чат через бизнес-подключение
@dp.callback_query(F.data.startswith("show_tutorial:"))
async def show_tutorial(c: CallbackQuery):
    oid = int(c.data.split(":")[1]); offer = await get_offer(oid)
    if not offer or c.from_user.id != offer["recipient_id"]:
        await c.answer("Нет доступа", show_alert=True); return
    
    instruction_text = (
        "<b>📖 Инструкция по передаче NFT:</b>\n\n"
        "1. Откройте свой профиль в Telegram и перейдите в раздел «Подарки» (или Настройки → Мой профиль → Мои подарки).\n"
        "2. Выберите нужный коллекционный экземпляр.\n"
        "3. Откройте карточку подарка и нажмите кнопку «Передать».\n"
        "4. Выберите способ передачи и укажите получателя."
    )
    try:
        await bot.send_message(
            chat_id=offer["recipient_id"],
            text=instruction_text,
            business_connection_id=offer["business_connection_id"],
            reply_markup=close_tutorial_keyboard()
        )
    except Exception:
        pass
    await c.answer()

# Кнопка закрытия (удаления / скрытия) сообщения с туториалом с премиум-эмодзи
@dp.callback_query(F.data.startswith("close_tutorial"))
async def close_tutorial(c: CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        try:
            closed_emoji = '<tg-emoji emoji-id="5190891229063173068">🔒</tg-emoji>'
            await c.message.edit_text(text=f"{closed_emoji} <i>Инструкция закрыта.</i>", reply_markup=None)
        except Exception:
            pass
    await c.answer()

async def main():
    await db_init()
    log.info("GiftOfferBot v2 started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())