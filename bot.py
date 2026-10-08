import asyncio
import html
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

BASE = Path(__file__).parent
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
CUR = os.getenv("CURRENCY", "₽")
SHOP = os.getenv("SHOP_NAME", "Time Boutique")
CONTACT = os.getenv("CONTACT", "")
DB_PATH = BASE / "shop.db"

router = Router()


# ---------- каталог ----------
def load_catalog():
    data = json.loads((BASE / "catalog.json").read_text(encoding="utf-8"))
    brands = {b["id"]: b for b in data["brands"]}
    products = {}
    for b in data["brands"]:
        for p in b["models"]:
            p["brand"] = b["name"]
            p["brand_id"] = b["id"]
            products[p["id"]] = p
    return brands, products


BRANDS, PRODUCTS = load_catalog()


def money(v) -> str:
    return f"{v:,}".replace(",", " ") + f" {CUR}"


# ---------- база ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con, con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS cart(
                user_id INTEGER, product_id TEXT, qty INTEGER,
                PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, username TEXT, name TEXT, contact TEXT,
                items TEXT, total INTEGER, status TEXT DEFAULT 'new',
                created TEXT DEFAULT CURRENT_TIMESTAMP);
            """
        )


def cart_get(uid):
    with closing(db()) as con:
        rows = con.execute("SELECT product_id, qty FROM cart WHERE user_id=?", (uid,)).fetchall()
    return [(PRODUCTS[r["product_id"]], r["qty"]) for r in rows if r["product_id"] in PRODUCTS]


def cart_change(uid, pid, delta):
    with closing(db()) as con, con:
        con.execute("INSERT OR IGNORE INTO cart VALUES(?,?,0)", (uid, pid))
        con.execute("UPDATE cart SET qty=qty+? WHERE user_id=? AND product_id=?", (delta, uid, pid))
        con.execute("DELETE FROM cart WHERE qty<=0")


def cart_clear(uid):
    with closing(db()) as con, con:
        con.execute("DELETE FROM cart WHERE user_id=?", (uid,))


def cart_count(uid):
    with closing(db()) as con:
        return con.execute("SELECT COALESCE(SUM(qty),0) FROM cart WHERE user_id=?", (uid,)).fetchone()[0]


# ---------- клавиатуры / вывод ----------
def main_kb(uid):
    n = cart_count(uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="🕰 Каталог", callback_data="catalog")
    kb.button(text=f"🛒 Корзина{f' ({n})' if n else ''}", callback_data="cart")
    kb.button(text="ℹ️ О магазине", callback_data="about")
    kb.button(text="💬 Связаться", callback_data="contact")
    kb.adjust(1, 1, 2)
    return kb.as_markup()


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup, photo: str | None = None):
    """Заменяет текущее сообщение (удаляет и шлёт новое, т.к. фото <-> текст не редактируется)."""
    try:
        await cb.message.delete()
    except Exception:
        pass
    if photo:
        file = photo if photo.startswith("http") else FSInputFile(BASE / photo)
        try:
            await cb.message.answer_photo(file, caption=text, reply_markup=kb)
            return
        except Exception as e:
            log.warning("photo failed (%s): %s", photo, e)
    await cb.message.answer(text, reply_markup=kb)


def welcome():
    return (
        f"<b>{html.escape(SHOP)}</b>\n\n"
        "Часы премиального качества: реплики ведущих мировых брендов.\n"
        "Выберите раздел 👇"
    )


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(welcome(), reply_markup=main_kb(m.from_user.id))


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, welcome(), main_kb(cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data == "about")
async def about(cb: CallbackQuery):
    kb = InlineKeyboardBuilder().button(text="◀️ Меню", callback_data="home").as_markup()
    await show(
        cb,
        "<b>О магазине</b>\n\n"
        "• Все модели — реплики высокого качества\n"
        "• Механизмы, сапфировое стекло, металл — как в оригинале по ощущениям\n"
        "• Фото и подробности — в карточке каждой модели\n"
        "• Оплата без онлайн-эквайринга: после заказа мы свяжемся с вами и договоримся об оплате и доставке",
        kb,
    )
    await cb.answer()


@router.callback_query(F.data == "contact")
async def contact(cb: CallbackQuery):
    kb = InlineKeyboardBuilder().button(text="◀️ Меню", callback_data="home").as_markup()
    await show(cb, f"<b>Связаться</b>\n\nПо любым вопросам: {html.escape(CONTACT) or 'напишите нам в этот чат'}", kb)
    await cb.answer()


# ---------- каталог ----------
@router.callback_query(F.data == "catalog")
async def catalog(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for b in BRANDS.values():
        kb.button(text=b["name"], callback_data=f"b:{b['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="◀️ Меню", callback_data="home"))
    await show(cb, "<b>Выберите бренд</b>", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("b:"))
async def brand(cb: CallbackQuery):
    b = BRANDS[cb.data[2:]]
    kb = InlineKeyboardBuilder()
    for p in b["models"]:
        kb.button(text=f"{p['name']} — {money(p['price'])}", callback_data=f"m:{p['id']}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="◀️ Бренды", callback_data="catalog"))
    await show(cb, f"<b>{html.escape(b['name'])}</b>\nВыберите модель:", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("m:"))
async def model(cb: CallbackQuery):
    p = PRODUCTS[cb.data[2:]]
    text = (
        f"<b>{html.escape(p['brand'])} {html.escape(p['name'])}</b>\n"
        f"<i>Реплика высокого качества</i>\n\n"
        f"{html.escape(p['desc'])}\n\n"
        f"<b>{money(p['price'])}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ В корзину", callback_data=f"add:{p['id']}")
    kb.button(text=f"🛒 Корзина ({cart_count(cb.from_user.id)})", callback_data="cart")
    kb.button(text=f"◀️ {p['brand']}", callback_data=f"b:{p['brand_id']}")
    kb.adjust(1, 1, 1)
    await show(cb, text, kb.as_markup(), p.get("photo") or None)
    await cb.answer()


@router.callback_query(F.data.startswith("add:"))
async def add(cb: CallbackQuery):
    pid = cb.data[4:]
    cart_change(cb.from_user.id, pid, 1)
    await cb.answer("Добавлено в корзину ✅")
    # обновить счётчик на кнопке корзины
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Ещё одну", callback_data=f"add:{pid}")
    kb.button(text=f"🛒 Корзина ({cart_count(cb.from_user.id)})", callback_data="cart")
    kb.button(text=f"◀️ {PRODUCTS[pid]['brand']}", callback_data=f"b:{PRODUCTS[pid]['brand_id']}")
    kb.adjust(1, 1, 1)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass


# ---------- корзина ----------
def cart_view(uid):
    items = cart_get(uid)
    kb = InlineKeyboardBuilder()
    if not items:
        kb.button(text="🕰 В каталог", callback_data="catalog")
        kb.button(text="◀️ Меню", callback_data="home")
        kb.adjust(1)
        return "🛒 Корзина пуста", kb.as_markup()
    total = 0
    lines = []
    for p, q in items:
        total += p["price"] * q
        lines.append(f"• {html.escape(p['brand'])} {html.escape(p['name'])}\n   {q} × {money(p['price'])}")
        kb.row(
            InlineKeyboardButton(text="➖", callback_data=f"dec:{p['id']}"),
            InlineKeyboardButton(text=f"{p['name']} · {q}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"inc:{p['id']}"),
        )
    kb.row(InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout"))
    kb.row(
        InlineKeyboardButton(text="🗑 Очистить", callback_data="clear"),
        InlineKeyboardButton(text="🕰 Ещё часы", callback_data="catalog"),
    )
    return "<b>🛒 Ваша корзина</b>\n\n" + "\n".join(lines) + f"\n\n<b>Итого: {money(total)}</b>", kb.as_markup()


@router.callback_query(F.data == "cart")
async def cart(cb: CallbackQuery):
    text, kb = cart_view(cb.from_user.id)
    await show(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^(inc|dec):"))
async def inc_dec(cb: CallbackQuery):
    act, pid = cb.data.split(":", 1)
    cart_change(cb.from_user.id, pid, 1 if act == "inc" else -1)
    text, kb = cart_view(cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "clear")
async def clear(cb: CallbackQuery):
    cart_clear(cb.from_user.id)
    text, kb = cart_view(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("Корзина очищена")


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ---------- оформление ----------
class Checkout(StatesGroup):
    contact = State()


@router.callback_query(F.data == "checkout")
async def checkout(cb: CallbackQuery, state: FSMContext):
    if not cart_get(cb.from_user.id):
        await cb.answer("Корзина пуста", show_alert=True)
        return
    await state.set_state(Checkout.contact)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)], [KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cb.message.answer(
        "Как с вами связаться?\nНажмите кнопку ниже или напишите телефон / удобный способ связи.",
        reply_markup=kb,
    )
    await cb.answer()


@router.message(Checkout.contact)
async def checkout_contact(m: Message, state: FSMContext):
    if m.text and m.text.strip().lower() == "отмена":
        await state.clear()
        await m.answer("Оформление отменено.", reply_markup=ReplyKeyboardRemove())
        await m.answer(welcome(), reply_markup=main_kb(m.from_user.id))
        return
    contact_text = m.contact.phone_number if m.contact else (m.text or "").strip()
    if not contact_text:
        await m.answer("Отправьте номер телефона или текстом способ связи.")
        return

    u = m.from_user
    items = cart_get(u.id)
    if not items:
        await state.clear()
        await m.answer("Корзина пуста.", reply_markup=ReplyKeyboardRemove())
        return
    total = sum(p["price"] * q for p, q in items)
    items_json = json.dumps(
        [{"id": p["id"], "title": f"{p['brand']} {p['name']}", "qty": q, "price": p["price"]} for p, q in items],
        ensure_ascii=False,
    )
    with closing(db()) as con, con:
        cur = con.execute(
            "INSERT INTO orders(user_id, username, name, contact, items, total) VALUES(?,?,?,?,?,?)",
            (u.id, u.username, u.full_name, contact_text, items_json, total),
        )
        oid = cur.lastrowid
    cart_clear(u.id)
    await state.clear()

    await m.answer(
        f"✅ Заказ <b>№{oid}</b> принят!\nМы скоро свяжемся с вами и договоримся об оплате и доставке.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await m.answer(welcome(), reply_markup=main_kb(u.id))

    # уведомление админам
    text = order_text(oid, u.id, u.username, u.full_name, contact_text, json.loads(items_json), total, "new")
    kb = InlineKeyboardBuilder().button(text="✅ Связался", callback_data=f"done:{oid}").as_markup()
    for admin in ADMIN_IDS:
        try:
            await m.bot.send_message(admin, "🔔 <b>Новый заказ!</b>\n\n" + text, reply_markup=kb)
        except Exception as e:
            log.warning("cannot notify admin %s: %s", admin, e)


def order_text(oid, uid, username, name, contact, items, total, status):
    lines = "\n".join(f"• {html.escape(i['title'])} — {i['qty']} × {money(i['price'])}" for i in items)
    who = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
    if username:
        who += f" (@{html.escape(username)})"
    mark = "🆕 новый" if status == "new" else "✅ обработан"
    return (
        f"<b>Заказ №{oid}</b> · {mark}\n"
        f"👤 {who}\n"
        f"📞 {html.escape(contact)}\n\n{lines}\n\n<b>Итого: {money(total)}</b>"
    )


# ---------- админ ----------
def is_admin(uid):
    return uid in ADMIN_IDS


@router.callback_query(F.data.startswith("done:"))
async def done(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer()
        return
    oid = int(cb.data[5:])
    with closing(db()) as con, con:
        con.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
    await cb.answer("Отмечено")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(Command("orders"))
async def orders(m: Message):
    if not is_admin(m.from_user.id):
        return
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await m.answer("Заказов пока нет.")
        return
    for r in rows:
        text = order_text(r["id"], r["user_id"], r["username"], r["name"], r["contact"],
                          json.loads(r["items"]), r["total"], r["status"])
        kb = None
        if r["status"] == "new":
            kb = InlineKeyboardBuilder().button(text="✅ Связался", callback_data=f"done:{r['id']}").as_markup()
        await m.answer(text, reply_markup=kb)


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


async def main():
    init_db()
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — уведомления о заказах приходить не будут")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
