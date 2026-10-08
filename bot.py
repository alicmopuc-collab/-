import asyncio
import html
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
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
# Папка с изменяемыми данными (база, каталог). В Docker монтируется как volume.
DATA_DIR = Path(os.getenv("DATA_DIR", BASE))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "shop.db"

router = Router()


# ---------- каталог ----------
CATALOG_PATH = DATA_DIR / "catalog.json"
if not CATALOG_PATH.exists():  # первый запуск с чистой DATA_DIR: берём каталог-образец из репозитория
    CATALOG_PATH.write_text((BASE / "catalog.json").read_text(encoding="utf-8"), encoding="utf-8")
BRANDS: dict = {}
PRODUCTS: dict = {}


def rebuild():
    """Пересобирает индекс моделей. Словари меняются на месте, ссылки на них остаются валидными."""
    PRODUCTS.clear()
    for b in BRANDS.values():
        for p in b["models"]:
            p["brand"] = b["name"]
            p["brand_id"] = b["id"]
            PRODUCTS[p["id"]] = p


def load_catalog():
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    BRANDS.clear()
    BRANDS.update({b["id"]: b for b in data["brands"]})
    rebuild()


def save_catalog():
    data = {
        "brands": [
            {
                "id": b["id"],
                "name": b["name"],
                "models": [
                    {k: p.get(k, "") for k in ("id", "name", "price", "desc", "photo")} for p in b["models"]
                ],
            }
            for b in BRANDS.values()
        ]
    }
    tmp = CATALOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CATALOG_PATH)
    rebuild()


load_catalog()


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
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
                subscribed INTEGER DEFAULT 1, blocked INTEGER DEFAULT 0,
                joined TEXT DEFAULT CURRENT_TIMESTAMP);
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
    if uid in ADMIN_IDS:
        kb.button(text="⚙️ Каталог (админ)", callback_data="ad:menu")
    kb.adjust(1, 1, 2, 1)
    return kb.as_markup()


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup, photo: str | None = None):
    """Заменяет текущее сообщение (удаляет и шлёт новое, т.к. фото <-> текст не редактируется)."""
    try:
        await cb.message.delete()
    except Exception:
        pass
    if photo:
        # URL и Telegram file_id передаём строкой, локальный файл — как файл
        file = FSInputFile(BASE / photo) if (BASE / photo).is_file() else photo
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


async def track_user(handler, event, data):
    """Запоминает каждого, кто писал боту — это база для рассылки."""
    u = event.from_user
    if u and not u.is_bot:
        with closing(db()) as con, con:
            con.execute(
                "INSERT INTO users(user_id, name, username) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, username=excluded.username, blocked=0",
                (u.id, u.full_name, u.username),
            )
    return await handler(event, data)


@router.message(Command("stop"))
async def stop(m: Message):
    with closing(db()) as con, con:
        con.execute("UPDATE users SET subscribed=0 WHERE user_id=?", (m.from_user.id,))
    await m.answer("Рассылка отключена. Чтобы включить снова, нажмите /start.")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    with closing(db()) as con, con:
        con.execute("UPDATE users SET subscribed=1 WHERE user_id=?", (m.from_user.id,))
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
async def catalog(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search"),
        InlineKeyboardButton(text="💰 По цене", callback_data="price"),
    )
    for b in BRANDS.values():
        kb.button(text=b["name"], callback_data=f"b:{b['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="◀️ Меню", callback_data="home"))
    await show(cb, "<b>Выберите бренд</b>", kb.as_markup())
    await cb.answer()


# ---------- поиск и фильтр по цене ----------
MAX_RESULTS = 30


def price_buckets():
    """Четыре диапазона по квартилям цен каталога: [(lo, hi)], hi=0 — без верхней границы."""
    prices = sorted(p["price"] for p in PRODUCTS.values())
    if not prices:
        return []
    step = 1000 if prices[-1] >= 10000 else 100
    edges = sorted({max(step, round(prices[len(prices) * k // 4] / step) * step) for k in (1, 2, 3)})
    bounds = [0, *edges, 0]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def bucket_label(lo, hi):
    if not lo:
        return f"до {money(hi)}"
    if not hi:
        return f"от {money(lo)}"
    return f"{money(lo)} – {money(hi)}"


def in_range(price, lo, hi):
    return price >= lo and (not hi or price <= hi)


def results_view(title, items, back="catalog"):
    items = sorted(items, key=lambda p: p["price"])
    kb = InlineKeyboardBuilder()
    if not items:
        kb.button(text="🔍 Искать ещё", callback_data="search")
        kb.button(text="◀️ Каталог", callback_data=back)
        kb.adjust(1)
        return f"{title}\n\nНичего не найдено.", kb.as_markup()
    for p in items[:MAX_RESULTS]:
        kb.button(text=f"{p['brand']} {p['name']} — {money(p['price'])}", callback_data=f"m:{p['id']}")
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search"),
        InlineKeyboardButton(text="💰 По цене", callback_data="price"),
    )
    kb.row(InlineKeyboardButton(text="◀️ Каталог", callback_data=back))
    more = f"\n(показаны первые {MAX_RESULTS} из {len(items)})" if len(items) > MAX_RESULTS else ""
    return f"{title}\nНайдено: {len(items)}{more}", kb.as_markup()


class Search(StatesGroup):
    query = State()


@router.callback_query(F.data == "search")
async def search_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Search.query)
    kb = InlineKeyboardBuilder().button(text="◀️ Каталог", callback_data="catalog").as_markup()
    await show(
        cb,
        "<b>Поиск</b>\n\nНапишите бренд или модель, например <i>rolex</i> или <i>submariner</i>.\n"
        "Можно искать по цене: <i>5000-10000</i>, <i>до 5000</i> или <i>от 10000</i>.",
        kb,
    )
    await cb.answer()


RANGE_RE = re.compile(r"^\s*(?:(\d[\d\s]*)\s*[-–—]\s*(\d[\d\s]*)|(до)\s*(\d[\d\s]*)|(от)\s*(\d[\d\s]*))\s*$", re.I)


def parse_range(text):
    m = RANGE_RE.match(text)
    if not m:
        return None
    num = lambda s: int(re.sub(r"\s", "", s))
    if m.group(1):
        lo, hi = sorted((num(m.group(1)), num(m.group(2))))
        return lo, hi
    if m.group(3):
        return 0, num(m.group(4))
    return num(m.group(6)), 0


@router.message(Search.query, F.text, ~F.text.startswith("/"))
async def search_run(m: Message, state: FSMContext):
    q = m.text.strip().lower()
    rng = parse_range(q)
    if rng:
        found = [p for p in PRODUCTS.values() if in_range(p["price"], *rng)]
        title = f"<b>Цена: {bucket_label(*rng)}</b>"
    else:
        words = q.split()
        found = [
            p for p in PRODUCTS.values()
            if all(w in f"{p['brand']} {p['name']} {p['desc']}".lower() for w in words)
        ]
        title = f"<b>Поиск: {html.escape(q)}</b>"
    await state.clear()
    text, kb = results_view(title, found)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data == "price")
async def price_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for lo, hi in price_buckets():
        kb.button(text=bucket_label(lo, hi), callback_data=f"p:{lo}:{hi}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="◀️ Каталог", callback_data="catalog"))
    await show(cb, "<b>Выберите ценовой диапазон</b>", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def price_run(cb: CallbackQuery):
    lo, hi = (int(x) for x in cb.data[2:].split(":"))
    found = [p for p in PRODUCTS.values() if in_range(p["price"], lo, hi)]
    text, kb = results_view(f"<b>Цена: {bucket_label(lo, hi)}</b>", found, back="price")
    await show(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("b:"))
async def brand(cb: CallbackQuery):
    b = BRANDS.get(cb.data[2:])
    if not b:
        await cb.answer("Бренд больше недоступен", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p in b["models"]:
        kb.button(text=f"{p['name']} — {money(p['price'])}", callback_data=f"m:{p['id']}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="◀️ Бренды", callback_data="catalog"))
    await show(cb, f"<b>{html.escape(b['name'])}</b>\nВыберите модель:", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("m:"))
async def model(cb: CallbackQuery):
    p = PRODUCTS.get(cb.data[2:])
    if not p:
        await cb.answer("Модель больше недоступна", show_alert=True)
        return
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
    if pid not in PRODUCTS:
        await cb.answer("Модель больше недоступна", show_alert=True)
        return
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
async def cart(cb: CallbackQuery, state: FSMContext):
    await state.clear()
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


# ---------- админ: редактирование каталога ----------
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

FIELDS = {"name": "название", "price": "цену (числом)", "desc": "описание", "photo": "фото"}
LIMITS = {"name": 60, "desc": 600}


class Edit(StatesGroup):
    brand_name = State()
    name = State()
    price = State()
    desc = State()
    photo = State()
    field = State()


def kb_of(*rows):
    """rows: списки (текст, callback_data)."""
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return kb.as_markup()


def new_id(prefix):
    while True:
        i = f"{prefix}-{secrets.token_hex(3)}"
        if i not in PRODUCTS and i not in BRANDS:
            return i


def admin_menu():
    return "<b>⚙️ Редактор каталога</b>", kb_of(
        [("➕ Добавить модель", "ad:add")],
        [("✏️ Изменить / удалить модель", "ad:edit")],
        [("➕ Новый бренд", "ad:nb"), ("🗑 Удалить бренд", "ad:db")],
        [("📣 Рассылка клиентам", "ad:bc")],
        [("◀️ Меню", "home")],
    )


def brand_picker(prefix, title, extra=()):
    kb = InlineKeyboardBuilder()
    for b in BRANDS.values():
        kb.button(text=b["name"], callback_data=f"{prefix}:{b['id']}")
    kb.adjust(2)
    for t, d in extra:
        kb.row(InlineKeyboardButton(text=t, callback_data=d))
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="ad:menu"))
    return title, kb.as_markup()


def admin_card(p):
    text = (
        f"<b>{html.escape(p['brand'])} {html.escape(p['name'])}</b>\n"
        f"Цена: {money(p['price'])}\n"
        f"Фото: {'есть' if p.get('photo') else 'нет'}\n\n{html.escape(p['desc'])}"
    )
    return text, kb_of(
        [("Название", f"af:name:{p['id']}"), ("Цена", f"af:price:{p['id']}")],
        [("Описание", f"af:desc:{p['id']}"), ("Фото", f"af:photo:{p['id']}")],
        [("🗑 Удалить модель", f"ax:{p['id']}")],
        [("◀️ К списку", f"ae:{p['brand_id']}")],
    )


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    text, kb = admin_menu()
    await m.answer(text, reply_markup=kb)


@admin.callback_query(F.data == "ad:menu")
async def admin_menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = admin_menu()
    await show(cb, text, kb)
    await cb.answer()


# --- бренды ---
@admin.callback_query(F.data == "ad:nb")
async def brand_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.brand_name)
    await state.update_data(then=None)
    await show(cb, "Название нового бренда:", kb_of([("◀️ Отмена", "ad:menu")]))
    await cb.answer()


@admin.message(Edit.brand_name, F.text, ~F.text.startswith("/"))
async def brand_new_name(m: Message, state: FSMContext):
    name = m.text.strip()[:40]
    if not name:
        await m.answer("Введите название.")
        return
    bid = new_id("b")
    BRANDS[bid] = {"id": bid, "name": name, "models": []}
    save_catalog()
    data = await state.get_data()
    await state.clear()
    if data.get("then") == "add_model":
        await state.update_data(brand_id=bid)
        await state.set_state(Edit.name)
        await m.answer(f"Бренд «{html.escape(name)}» создан ✅\nНазвание модели:")
    else:
        text, kb = admin_menu()
        await m.answer(f"Бренд «{html.escape(name)}» создан ✅\n\n{text}", reply_markup=kb)


@admin.callback_query(F.data == "ad:db")
async def brand_del(cb: CallbackQuery):
    text, kb = brand_picker("adb", "Какой бренд удалить? Вместе с ним удалятся все его модели.")
    await show(cb, text, kb)
    await cb.answer()


@admin.callback_query(F.data.startswith("adb:"))
async def brand_del_ask(cb: CallbackQuery):
    b = BRANDS.get(cb.data[4:])
    if not b:
        await cb.answer("Нет такого бренда", show_alert=True)
        return
    await show(
        cb,
        f"Удалить бренд <b>{html.escape(b['name'])}</b> и моделей: {len(b['models'])}?",
        kb_of([("🗑 Да, удалить", f"adc:{b['id']}")], [("◀️ Отмена", "ad:menu")]),
    )
    await cb.answer()


@admin.callback_query(F.data.startswith("adc:"))
async def brand_del_do(cb: CallbackQuery):
    BRANDS.pop(cb.data[4:], None)
    save_catalog()
    text, kb = admin_menu()
    await show(cb, "Бренд удалён ✅\n\n" + text, kb)
    await cb.answer()


# --- добавить модель ---
@admin.callback_query(F.data == "ad:add")
async def model_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = brand_picker("aa", "Выберите бренд для новой модели:", [("➕ Новый бренд", "aa:new")])
    await show(cb, text, kb)
    await cb.answer()


@admin.callback_query(F.data.startswith("aa:"))
async def model_add_brand(cb: CallbackQuery, state: FSMContext):
    key = cb.data[3:]
    if key == "new":
        await state.set_state(Edit.brand_name)
        await state.update_data(then="add_model")
        await show(cb, "Название нового бренда:", kb_of([("◀️ Отмена", "ad:menu")]))
    elif key in BRANDS:
        await state.set_state(Edit.name)
        await state.update_data(brand_id=key)
        await show(cb, "Название модели:", kb_of([("◀️ Отмена", "ad:menu")]))
    await cb.answer()


@admin.message(Edit.name, F.text, ~F.text.startswith("/"))
async def add_name(m: Message, state: FSMContext):
    name = m.text.strip()[: LIMITS["name"]]
    if not name:
        await m.answer("Введите название.")
        return
    await state.update_data(name=name)
    await state.set_state(Edit.price)
    await m.answer(f"Цена, {CUR} (только число):")


def parse_price(text):
    digits = re.sub(r"[\s_,.]", "", text or "")
    return int(digits) if digits.isdigit() and int(digits) > 0 else None


@admin.message(Edit.price, F.text, ~F.text.startswith("/"))
async def add_price(m: Message, state: FSMContext):
    price = parse_price(m.text)
    if price is None:
        await m.answer("Нужно число больше нуля, например 12900.")
        return
    await state.update_data(price=price)
    await state.set_state(Edit.desc)
    await m.answer("Описание (несколько строк можно). Или «-», чтобы оставить пустым:")


@admin.message(Edit.desc, F.text, ~F.text.startswith("/"))
async def add_desc(m: Message, state: FSMContext):
    desc = "" if m.text.strip() == "-" else m.text.strip()[: LIMITS["desc"]]
    await state.update_data(desc=desc)
    await state.set_state(Edit.photo)
    await m.answer("Пришлите фото модели (или ссылку на него). Или «-», чтобы добавить без фото:")


def photo_from(m: Message):
    """Фото из сообщения: file_id, ссылка, '' (без фото) или None (непонятный ввод)."""
    if m.photo:
        return m.photo[-1].file_id
    t = (m.text or "").strip()
    if t == "-":
        return ""
    return t if t.startswith("http") else None


@admin.message(Edit.photo, ~F.text.startswith("/"))
async def add_photo(m: Message, state: FSMContext):
    photo = photo_from(m)
    if photo is None:
        await m.answer("Пришлите фото, ссылку на него или «-».")
        return
    d = await state.get_data()
    pid = new_id(d["brand_id"])
    BRANDS[d["brand_id"]]["models"].append(
        {"id": pid, "name": d["name"], "price": d["price"], "desc": d["desc"], "photo": photo}
    )
    save_catalog()
    await state.clear()
    text, kb = admin_card(PRODUCTS[pid])
    await m.answer("Модель добавлена ✅\n\n" + text, reply_markup=kb)


# --- изменить / удалить модель ---
@admin.callback_query(F.data == "ad:edit")
async def model_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = brand_picker("ae", "Выберите бренд:")
    await show(cb, text, kb)
    await cb.answer()


@admin.callback_query(F.data.startswith("ae:"))
async def model_edit_list(cb: CallbackQuery):
    b = BRANDS.get(cb.data[3:])
    if not b:
        await cb.answer("Нет такого бренда", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p in b["models"]:
        kb.button(text=f"{p['name']} — {money(p['price'])}", callback_data=f"am:{p['id']}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="◀️ Назад", callback_data="ad:edit"))
    await show(cb, f"<b>{html.escape(b['name'])}</b>: выберите модель" if b["models"] else "Моделей пока нет.", kb.as_markup())
    await cb.answer()


@admin.callback_query(F.data.startswith("am:"))
async def model_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    p = PRODUCTS.get(cb.data[3:])
    if not p:
        await cb.answer("Модель не найдена", show_alert=True)
        return
    text, kb = admin_card(p)
    await show(cb, text, kb, p.get("photo") or None)
    await cb.answer()


@admin.callback_query(F.data.startswith("af:"))
async def field_ask(cb: CallbackQuery, state: FSMContext):
    _, field, pid = cb.data.split(":", 2)
    if pid not in PRODUCTS or field not in FIELDS:
        await cb.answer("Модель не найдена", show_alert=True)
        return
    await state.set_state(Edit.field)
    await state.update_data(pid=pid, field=field)
    hint = " Или «-», чтобы убрать фото." if field == "photo" else ""
    await cb.message.answer(f"Новое значение: {FIELDS[field]}.{hint}", reply_markup=kb_of([("◀️ Отмена", f"am:{pid}")]))
    await cb.answer()


@admin.message(Edit.field, ~F.text.startswith("/"))
async def field_save(m: Message, state: FSMContext):
    d = await state.get_data()
    p = PRODUCTS.get(d["pid"])
    if not p:
        await state.clear()
        await m.answer("Модель уже удалена.")
        return
    field = d["field"]
    if field == "photo":
        value = photo_from(m)
    elif field == "price":
        value = parse_price(m.text)
    else:
        value = (m.text or "").strip()[: LIMITS[field]] or None
    if value is None:
        await m.answer("Не подходит, попробуйте ещё раз.")
        return
    p[field] = value
    save_catalog()
    await state.clear()
    text, kb = admin_card(PRODUCTS[p["id"]])
    await m.answer("Сохранено ✅\n\n" + text, reply_markup=kb)


@admin.callback_query(F.data.startswith("ax:"))
async def model_del_ask(cb: CallbackQuery):
    p = PRODUCTS.get(cb.data[3:])
    if not p:
        await cb.answer("Модель не найдена", show_alert=True)
        return
    await show(
        cb,
        f"Удалить <b>{html.escape(p['brand'])} {html.escape(p['name'])}</b>?",
        kb_of([("🗑 Да, удалить", f"axc:{p['id']}")], [("◀️ Отмена", f"am:{p['id']}")]),
    )
    await cb.answer()


@admin.callback_query(F.data.startswith("axc:"))
async def model_del_do(cb: CallbackQuery):
    p = PRODUCTS.get(cb.data[4:])
    if p:
        BRANDS[p["brand_id"]]["models"].remove(p)
        save_catalog()
    text, kb = admin_menu()
    await show(cb, "Модель удалена ✅\n\n" + text, kb)
    await cb.answer()


# --- рассылка ---
class Broadcast(StatesGroup):
    msg = State()


def recipients():
    with closing(db()) as con:
        rows = con.execute("SELECT user_id FROM users WHERE subscribed=1 AND blocked=0").fetchall()
    return [r["user_id"] for r in rows]


@admin.callback_query(F.data == "ad:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await show(
        cb,
        f"<b>📣 Рассылка</b>\n\nПолучателей: {len(recipients())}\n"
        "Пришлите сообщение, которое нужно разослать: текст или фото с подписью. "
        "Перед отправкой я покажу предпросмотр.",
        kb_of([("◀️ Отмена", "ad:menu")]),
    )
    await cb.answer()


@admin.message(Broadcast.msg, ~F.text.startswith("/"))
async def bc_preview(m: Message, state: FSMContext):
    n = len(recipients())
    await state.update_data(msg_id=m.message_id)
    await m.answer("Так увидят клиенты 👇")
    try:
        await m.copy_to(m.chat.id)
    except TelegramAPIError:
        await m.answer("Этот тип сообщения не получится разослать. Пришлите текст или фото.")
        return
    await m.answer(
        f"Отправить {n} получателям?",
        reply_markup=kb_of([(f"✅ Отправить ({n})", "bc:go")], [("❌ Отмена", "ad:menu")]),
    )


@admin.callback_query(F.data == "bc:go")
async def bc_send(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    msg_id = data.get("msg_id")
    await state.clear()
    if not msg_id:
        await cb.answer("Сообщение не найдено, начните заново", show_alert=True)
        return
    ids = recipients()
    await cb.message.edit_text(f"Отправляю {len(ids)} получателям…")
    await cb.answer()
    sent = blocked = failed = 0
    for uid in ids:
        for attempt in (1, 2):
            try:
                await cb.bot.copy_message(uid, cb.message.chat.id, msg_id)
                sent += 1
            except TelegramRetryAfter as e:
                if attempt == 1:
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                failed += 1
            except TelegramForbiddenError:
                blocked += 1
                with closing(db()) as con, con:
                    con.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
            except TelegramAPIError as e:
                log.warning("broadcast to %s failed: %s", uid, e)
                failed += 1
            break
        await asyncio.sleep(0.05)  # ~20 сообщений/с, в рамках лимитов Telegram
    await cb.message.answer(
        f"Рассылка завершена ✅\nДоставлено: {sent}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
    )


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


async def main():
    init_db()
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан — уведомления о заказах приходить не будут")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(track_user)
    dp.callback_query.outer_middleware(track_user)
    dp.include_router(admin)
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
