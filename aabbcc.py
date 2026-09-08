import asyncio
import datetime
import html
import math
import os
import random
import re
import sqlite3
import time
import urllib.parse
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

TOKEN = "8908366093:AAH7Io2b027KHlyvMCMu5x7F7w8vmC18E5U"
ADMIN_USERNAME = "lithromantov"
DONATE_TARGET = "lithromantov"

# Папка со скриптом
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MIN_DEPOSIT = 10.0

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Игровые сессии
games = {}          # Мины[cite: 1]
joker_games = {}    # Башня Джокер[cite: 1]
bj_games = {}       # 21 Очко[cite: 1]

custom_next_mines = {}
nextgame_setups = {}

TOTAL_TILES = 25
HOUSE_EDGE = 0.95
BONUS_AMOUNT = 5000.0
START_BALANCE = 1000.0
MAX_SAFE_VALUE = 1_000_000_000.0

JOKER_MULTS = [1.35, 1.81, 2.45, 3.60, 5.20, 7.80, 11.50, 17.00]

DECK = [
    ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7),
    ("8", 8), ("9", 9), ("10", 10), ("J", 2), ("Q", 3), ("K", 4), ("A", 11)
] * 4


# ================= БАЗА ДАННЫХ =================
def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance REAL DEFAULT {START_BALANCE},
                last_bonus TIMESTAMP,
                default_mines INTEGER DEFAULT 5,
                max_balance REAL DEFAULT {START_BALANCE},
                max_deposit REAL DEFAULT 0.0,
                total_deposits INTEGER DEFAULT 0,
                games_won INTEGER DEFAULT 0,
                has_xray INTEGER DEFAULT 0,
                is_frozen INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                chat_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        cursor = conn.execute("PRAGMA table_info(users)")
        columns = [row["name"] for row in cursor.fetchall()]

        if "first_name" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
        if "default_mines" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN default_mines INTEGER DEFAULT 5")
        if "max_balance" not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN max_balance REAL DEFAULT {START_BALANCE}")
        if "max_deposit" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN max_deposit REAL DEFAULT 0.0")
        if "total_deposits" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN total_deposits INTEGER DEFAULT 0")
        if "games_won" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN games_won INTEGER DEFAULT 0")
        if "has_xray" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN has_xray INTEGER DEFAULT 0")
        if "is_frozen" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_frozen INTEGER DEFAULT 0")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance)")
        conn.commit()


def register_chat_member(chat_id: int, user_id: int):
    if chat_id < 0:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO chat_members (chat_id, user_id) VALUES (?, ?)", (chat_id, user_id))
            conn.commit()


def get_user(user_id: int, username: str = None, first_name: str = None):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                """INSERT INTO users 
                   (user_id, username, first_name, balance, default_mines, max_balance, max_deposit, total_deposits, games_won, has_xray, is_frozen) 
                   VALUES (?, ?, ?, ?, 5, ?, 0.0, 0, 0, 0, 0)""",
                (user_id, username.lower() if username else None, first_name, START_BALANCE, START_BALANCE)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        else:
            updates = []
            params = []
            if username:
                updates.append("username = ?")
                params.append(username.lower())
            if first_name:
                updates.append("first_name = ?")
                params.append(first_name)

            if updates:
                params.append(user_id)
                conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)
                conn.commit()
                row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

        user_dict = dict(row)
        if user_dict.get("balance") is None or user_dict["balance"] > MAX_SAFE_VALUE:
            user_dict["balance"] = START_BALANCE
        return user_dict


def update_balance(user_id: int, amount: float):
    with get_db() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        current = float(row["balance"]) if row and row["balance"] <= MAX_SAFE_VALUE else START_BALANCE
        new_bal = max(0.0, min(MAX_SAFE_VALUE, current + amount))
        conn.execute("""
            UPDATE users 
            SET balance = ROUND(?, 2),
                max_balance = MIN(?, MAX(COALESCE(max_balance, 0), ROUND(?, 2)))
            WHERE user_id = ?
        """, (new_bal, MAX_SAFE_VALUE, new_bal, user_id))
        conn.commit()


def add_deposit_amount(user_id: int, amount: float):
    with get_db() as conn:
        row = conn.execute("SELECT balance, max_deposit FROM users WHERE user_id = ?", (user_id,)).fetchone()
        current = float(row["balance"]) if row and row["balance"] <= MAX_SAFE_VALUE else START_BALANCE
        current_max_dep = float(row["max_deposit"]) if row and row["max_deposit"] <= MAX_SAFE_VALUE else 0.0
        new_bal = max(0.0, min(MAX_SAFE_VALUE, current + amount))
        new_max_dep = min(MAX_SAFE_VALUE, max(current_max_dep, amount))

        conn.execute("""
            UPDATE users 
            SET balance = ROUND(?, 2),
                max_balance = MIN(?, MAX(COALESCE(max_balance, 0), ROUND(?, 2))),
                max_deposit = ROUND(?, 2)
            WHERE user_id = ?
        """, (new_bal, MAX_SAFE_VALUE, new_bal, new_max_dep, user_id))
        conn.commit()


def record_game_result(user_id: int, won: bool = False):
    with get_db() as conn:
        if won:
            conn.execute("""
                UPDATE users 
                SET total_deposits = MIN(1000000, COALESCE(total_deposits, 0) + 1),
                    games_won = MIN(1000000, COALESCE(games_won, 0) + 1)
                WHERE user_id = ?
            """, (user_id,))
        else:
            conn.execute("""
                UPDATE users 
                SET total_deposits = MIN(1000000, COALESCE(total_deposits, 0) + 1)
                WHERE user_id = ?
            """, (user_id,))
        conn.commit()


def set_balance(user_id: int, amount: float):
    new_bal = max(0.0, min(MAX_SAFE_VALUE, amount))
    with get_db() as conn:
        conn.execute("""
            UPDATE users 
            SET balance = ROUND(?, 2),
                max_balance = MIN(?, MAX(COALESCE(max_balance, 0), ROUND(?, 2)))
            WHERE user_id = ?
        """, (new_bal, MAX_SAFE_VALUE, new_bal, user_id))
        conn.commit()


def set_user_mines(user_id: int, mines_count: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET default_mines = ? WHERE user_id = ?", (mines_count, user_id))
        conn.commit()


def set_user_xray(user_id: int, status: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET has_xray = ? WHERE user_id = ?", (status, user_id))
        conn.commit()


def set_user_freeze(user_id: int, status: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_frozen = ? WHERE user_id = ?", (status, user_id))
        conn.commit()


def has_xray_access(user_id: int, username: str = None) -> bool:
    if username and username.lower() == ADMIN_USERNAME.lower():
        return True
    user = get_user(user_id, username)
    return bool(user.get("has_xray", 0))


def get_user_by_username(username: str):
    clean_username = username.lstrip("@").lower()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (clean_username,)).fetchone()
        if not row:
            return None
        return dict(row)


def get_name(user_id: int, username: str, first_name: str) -> str:
    return f"@{username}" if username else html.escape(first_name or f"ID {user_id}")


# ================= ВСПОМОГАТЕЛЬНЫЕ РАСЧЁТЫ =================
def calculate_multiplier(mines: int, opened_count: int) -> float:
    if opened_count == 0:
        return 1.0
    if mines == 1:
        if opened_count == 1:
            return 1.01
        safe_tiles = TOTAL_TILES - mines
        prob = math.comb(safe_tiles, opened_count) / math.comb(TOTAL_TILES, opened_count)
        return round(max(1.01, (1.0 / prob) * 0.98), 2)
    if mines == 24 and opened_count == 1:
        return 23.75

    safe_tiles = TOTAL_TILES - mines
    if opened_count > safe_tiles:
        return 1.0
    prob = math.comb(safe_tiles, opened_count) / math.comb(TOTAL_TILES, opened_count)
    return round(max(1.05, (1.0 / prob) * HOUSE_EDGE), 2)


def calc_hand(cards):
    total = sum(c[1] for c in cards)
    aces = sum(1 for c in cards if c[0] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def format_hand(cards):
    return " ".join(f"[{c[0]}]" for c in cards)


# ================= КЛАВИАТУРЫ =================
def generate_game_keyboard(game_id: str, reveal_all: bool = False) -> InlineKeyboardMarkup:
    game = games[game_id]
    grid = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if idx in game["opened"]:
                btn_text = "💎"
                callback_data = "ignore"
            elif reveal_all:
                btn_text = "💣" if idx in game["mines_pos"] else "💎"
                callback_data = "ignore"
            else:
                btn_text = "❓"
                callback_data = f"tile:{game_id}:{idx}"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=callback_data))
        grid.append(row)

    if not reveal_all:
        cur_mult = calculate_multiplier(game["mines_count"], len(game["opened"]))
        cur_win = round(game["bet"] * cur_mult, 2)
        grid.append([
            InlineKeyboardButton(
                text=f"💰 Забрать {cur_win:.2f} TON ({cur_mult}x)",
                callback_data=f"cashout:{game_id}"
            )
        ])
        grid.append([
            InlineKeyboardButton(text="Отмена 🚫", callback_data=f"cancel:{game_id}"),
            InlineKeyboardButton(text="👁", callback_data=f"xray:{game_id}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=grid)


def generate_joker_keyboard(game_id: str, reveal_all: bool = False) -> InlineKeyboardMarkup:
    game = joker_games[game_id]
    grid = []
    history = game["history"]
    curr_level = len(history)

    for data in history:
        skull_col = data["skull"]
        row = []
        for col_idx in range(3):
            if col_idx == skull_col:
                row.append(InlineKeyboardButton(text="💀", callback_data="ignore"))
            else:
                row.append(InlineKeyboardButton(text="🃏", callback_data="ignore"))
        grid.append(row)

    if not reveal_all and curr_level < len(JOKER_MULTS):
        active_row = [
            InlineKeyboardButton(text="🎴", callback_data=f"jk_pick:{game_id}:{c}") for c in range(3)
        ]
        grid.append(active_row)
        if curr_level > 0:
            grid.append([
                InlineKeyboardButton(text="💸 Забрать выигрыш", callback_data=f"jk_cash:{game_id}")
            ])

    return InlineKeyboardMarkup(inline_keyboard=grid)


def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    prefilled_text = "привет,хочу купить ton'ы/рентген в @litrtonbot"
    donate_url = f"https://t.me/{DONATE_TARGET}?text={urllib.parse.quote(prefilled_text)}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎁 Бонус (5000 TON)", callback_data="get_bonus"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="my_profile")
        ],
        [
            InlineKeyboardButton(text="💳 Донат", url=donate_url),
            InlineKeyboardButton(text="🏆 Топ игроков", callback_data="show_top")
        ],
        [
            InlineKeyboardButton(text="👥 Группа с ботом", url="https://t.me/screwchats")
        ]
    ])


def generate_nextgame_keyboard(user_id: int) -> InlineKeyboardMarkup:
    setup = nextgame_setups[user_id]
    grid = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            btn_text = "💣" if idx in setup["chosen_tiles"] else "⬜"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"ng_tile:{idx}"))
        grid.append(row)

    placed = len(setup["chosen_tiles"])
    target = setup["total_mines"]
    if placed == target:
        grid.append([InlineKeyboardButton(text="✅ Сохранить расстановку", callback_data="ng_save")])
    else:
        grid.append([InlineKeyboardButton(text=f"Выбрано {placed}/{target} мин", callback_data="ignore")])
    grid.append([InlineKeyboardButton(text="❌ Отменить", callback_data="ng_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=grid)


async def animate_loss(msg: Message, player_name: str, bet: float, kb: InlineKeyboardMarkup):
    base_header = (
        f"💥 <b>БАБАХ! {player_name} подорвался на мине!</b>\n"
        f"Потеряно: <b>{bet:.2f} TON</b>\n\n"
    )
    frames = [
        f"{base_header}💀 <b>you...</b>",
        f"{base_header}💀 <b>you lose..</b>",
        f"{base_header}💀 <b>YOU LOSE..</b>\n\nПоле раскрыто:"
    ]
    for frame in frames:
        try:
            await msg.edit_text(frame, reply_markup=kb, parse_mode="HTML")
            await asyncio.sleep(0.9)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except Exception:
            break


# ================= СКРЫТАЯ КОМАНДА PING =================
@dp.message(F.text.lower() == ".ping")
async def handle_ping(message: Message):
    start_time = time.perf_counter()
    msg = await message.reply("🏓 Pong...")
    end_time = time.perf_counter()
    latency_ms = (end_time - start_time) * 1000
    await msg.edit_text(f"pong! 🏓 <code>{latency_ms:.1f}ms</code>", parse_mode="HTML")


# ================= СТАРТ И ПРОФИЛЬ =================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
    bal = float(user.get("balance") or 0.0)[cite: 1]
    await message.answer(
        f"👋 Привет, <b>{html.escape(message.from_user.first_name)}</b>!\n\n"
        f"💎 Баланс: <b>{bal:.2f} TON</b>\n\n"
        "🎮 <b>Доступные игры:</b>\n"
        "• <code>мины 100</code> или <code>мины 100 10 шт</code>\n"
        "• <code>джокер 100</code> — Башня Джокер 🃏\n"
        "• <code>21 100</code> — Блэкджек против дилера ♠️\n\n"
        "📊 <b>Стата:</b> <code>стата</code> или /stat\n"
        "🎁 <b>Бонус:</b> <code>бонус</code> (5000 TON)\n"
        "💳 <b>Баланс:</b> <code>б</code> или <code>баланс</code>\n"
        "💸 <b>Перевод:</b> <code>п @юзер 50</code>\n"
        "🏆 <b>Топ:</b> /top",
        reply_markup=get_main_menu_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "my_profile")
async def cb_my_profile(callback: CallbackQuery):
    user = get_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)[cite: 1]
    bal = float(user.get("balance") or 0.0)[cite: 1]
    max_bal = float(user.get("max_balance") or bal)[cite: 1]
    max_dep = float(user.get("max_deposit") or 0.0)[cite: 1]
    total_games = int(user.get("total_deposits") or 0)[cite: 1]
    games_won = int(user.get("games_won") or 0)[cite: 1]
    winrate = (games_won / total_games * 100) if total_games > 0 else 0.0[cite: 1]
    xray_status = "Есть ✅" if (user.get("has_xray") or (callback.from_user.username or "").lower() == ADMIN_USERNAME.lower()) else "Нет ❌"[cite: 1]

    await callback.answer(
        f"ID: {user['user_id']}\n"
        f"max. деп: {max_dep:.2f} TON\n"
        f"max баланс: {max_bal:.2f} TON\n"
        f"всего депов: {total_games}\n"
        f"побед: {games_won} ({winrate:.1f}%)\n"
        f"💳 Баланс: {bal:.2f} TON\n"
        f"👁 Рентген: {xray_status}",
        show_alert=True
    )


@dp.message(F.text.lower().in_(["стата", "статистика", "/stat"]))
async def handle_show_stat(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    if message.reply_to_message:
        target = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )[cite: 1]
    else:
        target = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]

    max_bal = float(target.get("max_balance") or 0.0)[cite: 1]
    max_dep = float(target.get("max_deposit") or 0.0)[cite: 1]
    total_games = int(target.get("total_deposits") or 0)[cite: 1]
    games_won = int(target.get("games_won") or 0)[cite: 1]
    winrate = (games_won / total_games * 100) if total_games > 0 else 0.0[cite: 1]

    await message.reply(
        f"id: <code>{target['user_id']}</code>\n"
        f"max. деп: <code>{max_dep:.2f} TON</code>\n"
        f"max баланс: <code>{max_bal:.2f} TON</code>\n"
        f"всего депов: <code>{total_games}</code>\n"
        f"побед: <code>{games_won}</code> (винрейт: <code>{winrate:.1f}%</code>)",
        parse_mode="HTML"
    )


# ================= 1. МИНЫ =================
@dp.message(F.text.lower().startswith("мины"))
async def handle_mines_command(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    user_id = message.from_user.id[cite: 1]
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)[cite: 1]
    if user.get("is_frozen"):[cite: 1]
        await message.reply("⛔ Ваш аккаунт заморожен администратором.")[cite: 1]
        return

    user_bal = float(user.get("balance") or 0.0)[cite: 1]
    text = message.text.lower().strip()[cite: 1]

    args_text = text[4:].strip()[cite: 1]
    if not args_text:[cite: 1]
        await message.reply("❌ Введите сумму ставки. Пример: <code>мины 100</code> или <code>мины 100 24 шт</code>", parse_mode="HTML")[cite: 1]
        return

    allowed_mines = [1, 5, 10, 15, 20, 24][cite: 1]
    mines_count = None[cite: 1]
    bet = None[cite: 1]

    mines_match = re.search(r"\b(\d+)\s*(?:шт|мин|м)\b", args_text)[cite: 1]
    if mines_match:[cite: 1]
        val = int(mines_match.group(1))[cite: 1]
        if val in allowed_mines:[cite: 1]
            mines_count = val[cite: 1]
            set_user_mines(user_id, mines_count)[cite: 1]
            args_text = args_text[:mines_match.start()] + " " + args_text[mines_match.end():][cite: 1]
        else:
            await message.reply(f"❌ Недопустимое число мин. Доступны: {', '.join(f'{x}шт' for x in allowed_mines)}")[cite: 1]
            return

    tokens = args_text.replace(",", ".").split()[cite: 1]
    numbers = [][cite: 1]
    for t in tokens:[cite: 1]
        try:
            numbers.append(float(t))[cite: 1]
        except ValueError:
            pass

    if not numbers:[cite: 1]
        await message.reply("❌ Не указана сумма ставки. Пример: <code>мины 50 10 шт</code>", parse_mode="HTML")[cite: 1]
        return

    if mines_count is None and len(numbers) >= 2:[cite: 1]
        if int(numbers[1]) in allowed_mines:[cite: 1]
            bet = numbers[0][cite: 1]
            mines_count = int(numbers[1])[cite: 1]
            set_user_mines(user_id, mines_count)[cite: 1]
        elif int(numbers[0]) in allowed_mines:[cite: 1]
            mines_count = int(numbers[0])[cite: 1]
            bet = numbers[1][cite: 1]
            set_user_mines(user_id, mines_count)[cite: 1]

    if bet is None:[cite: 1]
        bet = numbers[0][cite: 1]
    if mines_count is None:[cite: 1]
        mines_count = user.get("default_mines") or 5[cite: 1]

    bet = round(bet, 2)[cite: 1]
    if bet <= 0 or user_bal < bet:[cite: 1]
        await message.reply(f"❌ Недостаточно средств! Баланс: <b>{user_bal:.2f} TON</b>", parse_mode="HTML")[cite: 1]
        return

    update_balance(user_id, -bet)[cite: 1]
    player_name = get_name(user_id, message.from_user.username, message.from_user.first_name)[cite: 1]

    sent_msg = await message.answer(
        f"💣 <b>Игра началась!</b>\n\n"
        f"👤 Игрок: <b>{player_name}</b>\n"
        f"💵 Ставка: <b>{bet:.2f} TON</b>\n"
        f"🧨 Мин: <b>{mines_count} шт.</b> | 💎 Безопасных клеток: <b>{TOTAL_TILES - mines_count}</b>\n\n"
        "Открывайте клетки:",
        parse_mode="HTML"
    )[cite: 1]

    game_id = f"{sent_msg.chat.id}_{sent_msg.message_id}"[cite: 1]
    if user_id in custom_next_mines and len(custom_next_mines[user_id]) == mines_count:[cite: 1]
        mines_positions = custom_next_mines.pop(user_id)[cite: 1]
    else:
        mines_positions = set(random.sample(range(TOTAL_TILES), mines_count))[cite: 1]

    games[game_id] = {
        "user_id": user_id,
        "first_name": message.from_user.first_name,
        "username": message.from_user.username,
        "bet": bet,
        "mines_count": mines_count,
        "mines_pos": mines_positions,
        "opened": set(),
        "last_click": 0.0
    }[cite: 1]

    await sent_msg.edit_reply_markup(reply_markup=generate_game_keyboard(game_id))[cite: 1]


@dp.callback_query(F.data.startswith("tile:"))
async def handle_click_tile(callback: CallbackQuery):
    _, game_id, tile_str = callback.data.split(":", 2)[cite: 1]
    tile_idx = int(tile_str)[cite: 1]

    if game_id not in games:[cite: 1]
        await callback.answer("Игра уже завершена.", show_alert=True)[cite: 1]
        return

    game = games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return

    now = time.time()[cite: 1]
    if now - game.get("last_click", 0) < 0.35:[cite: 1]
        await callback.answer("⏳ Не спешите!", show_alert=False)[cite: 1]
        return
    game["last_click"] = now[cite: 1]

    if tile_idx in game["opened"]:[cite: 1]
        await callback.answer()[cite: 1]
        return

    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]

    if tile_idx in game["mines_pos"]:[cite: 1]
        bet = game["bet"][cite: 1]
        kb = generate_game_keyboard(game_id, reveal_all=True)[cite: 1]
        user_id = game["user_id"][cite: 1]
        del games[game_id][cite: 1]
        record_game_result(user_id, won=False)[cite: 1]
        await callback.answer()[cite: 1]
        asyncio.create_task(animate_loss(callback.message, player_name, bet, kb))[cite: 1]
        return

    game["opened"].add(tile_idx)[cite: 1]
    opened_len = len(game["opened"])[cite: 1]
    safe_total = TOTAL_TILES - game["mines_count"][cite: 1]

    if opened_len == safe_total:[cite: 1]
        mult = calculate_multiplier(game["mines_count"], opened_len)[cite: 1]
        win_sum = round(game["bet"] * mult, 2)[cite: 1]
        update_balance(game["user_id"], win_sum)[cite: 1]
        kb = generate_game_keyboard(game_id, reveal_all=True)[cite: 1]
        user_id = game["user_id"][cite: 1]
        del games[game_id][cite: 1]
        record_game_result(user_id, won=True)[cite: 1]

        try:
            await callback.message.edit_text(
                f"🏆 <b>Идеальная зачистка!</b>\n\n"
                f"👤 Игрок: <b>{player_name}</b>\n"
                f"Множитель: <b>x{mult}</b>\n"
                f"Выигрыш: <b>+{win_sum:.2f} TON</b> 💎",
                reply_markup=kb,
                parse_mode="HTML"
            )[cite: 1]
        except Exception:
            pass
        await callback.answer()[cite: 1]
        return

    cur_mult = calculate_multiplier(game["mines_count"], opened_len)[cite: 1]
    cur_win = round(game["bet"] * cur_mult, 2)[cite: 1]

    try:
        await callback.message.edit_text(
            f"🎮 <b>Мины ({game['mines_count']} шт.)</b>\n"
            f"👤 Игрок: <b>{player_name}</b>\n"
            f"💎 Найдено: <b>{opened_len}/{safe_total}</b>\n"
            f"Куш: <b>{cur_win:.2f} TON</b> (x{cur_mult})\n\n"
            "Открывайте следующую клетку или забирайте TON:",
            reply_markup=generate_game_keyboard(game_id),
            parse_mode="HTML"
        )[cite: 1]
    except TelegramRetryAfter as e:[cite: 1]
        await callback.answer(f"⏳ Слишком быстро! Подождите {e.retry_after} сек.", show_alert=True)[cite: 1]
        return
    except Exception:
        pass
    await callback.answer()[cite: 1]


@dp.callback_query(F.data.startswith("cancel:"))
async def handle_cancel_game(callback: CallbackQuery):
    game_id = callback.data.split(":", 1)[1][cite: 1]
    if game_id not in games:[cite: 1]
        await callback.answer("Игра уже завершена.", show_alert=True)[cite: 1]
        return
    game = games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return
    if len(game["opened"]) > 0:[cite: 1]
        await callback.answer("⚠️ Вы уже открыли ячейку! Отмена невозможна.", show_alert=True)[cite: 1]
        return

    update_balance(game["user_id"], game["bet"])[cite: 1]
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]
    del games[game_id][cite: 1]

    await callback.message.edit_text(
        f"🚫 <b>Игра отменена!</b>\n\n👤 Игрок: <b>{player_name}</b>\n💵 Ставка <b>{game['bet']:.2f} TON</b> возвращена.",
        reply_markup=None,
        parse_mode="HTML"
    )[cite: 1]
    await callback.answer("Игра отменена!")[cite: 1]


@dp.callback_query(F.data.startswith("cashout:"))
async def handle_cashout(callback: CallbackQuery):
    game_id = callback.data.split(":", 1)[1][cite: 1]
    if game_id not in games:[cite: 1]
        await callback.answer("Игра завершена.", show_alert=True)[cite: 1]
        return
    game = games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return
    if len(game["opened"]) == 0:[cite: 1]
        await callback.answer("Откройте хотя бы одну ячейку!", show_alert=True)[cite: 1]
        return

    final_mult = calculate_multiplier(game["mines_count"], len(game["opened"]))[cite: 1]
    win_sum = round(game["bet"] * final_mult, 2)[cite: 1]
    update_balance(game["user_id"], win_sum)[cite: 1]
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]

    kb = generate_game_keyboard(game_id, reveal_all=True)[cite: 1]
    user_id = game["user_id"][cite: 1]
    del games[game_id][cite: 1]
    record_game_result(user_id, won=True)[cite: 1]

    try:
        await callback.message.edit_text(
            f"💰 <b>{player_name} забрал куш!</b>\n\n"
            f"Множитель: <b>x{final_mult}</b>\n"
            f"Выигрыш: <b>+{win_sum:.2f} TON</b> 💎\n\nКарта раунда:",
            reply_markup=kb,
            parse_mode="HTML"
        )[cite: 1]
    except Exception:
        pass
    await callback.answer()[cite: 1]


# ================= 2. БАШНЯ ДЖОКЕР =================
@dp.message(F.text.lower().startswith("джокер"))
async def handle_joker_command(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    user_id = message.from_user.id[cite: 1]
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)[cite: 1]
    if user.get("is_frozen"):[cite: 1]
        await message.reply("⛔ Ваш аккаунт заморожен.")[cite: 1]
        return

    parts = message.text.strip().split()[cite: 1]
    if len(parts) < 2:[cite: 1]
        await message.reply("❌ Введите ставку. Пример: <code>джокер 100</code>", parse_mode="HTML")[cite: 1]
        return

    try:
        bet = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
    except ValueError:
        return

    if bet <= 0 or float(user["balance"]) < bet:[cite: 1]
        await message.reply("❌ Недостаточно средств!")[cite: 1]
        return

    update_balance(user_id, -bet)[cite: 1]
    player_name = get_name(user_id, message.from_user.username, message.from_user.first_name)[cite: 1]
    display_title = f"{html.escape(message.from_user.first_name)} {player_name}"[cite: 1]

    sent_msg = await message.answer(
        f"<b>{display_title}</b>, вы начали игру джокер!\n"
        f"💰 Ставка: {int(bet) if bet.is_integer() else bet} TON\n\n"
        "Выберите карту:",
        parse_mode="HTML"
    )[cite: 1]

    game_id = f"{sent_msg.chat.id}_{sent_msg.message_id}"[cite: 1]
    joker_games[game_id] = {
        "user_id": user_id,
        "first_name": message.from_user.first_name,
        "username": message.from_user.username,
        "bet": bet,
        "history": [],
        "current_skull": random.randint(0, 2),
        "last_click": 0.0
    }[cite: 1]
    await sent_msg.edit_reply_markup(reply_markup=generate_joker_keyboard(game_id))[cite: 1]


@dp.callback_query(F.data.startswith("jk_pick:"))
async def handle_joker_pick(callback: CallbackQuery):
    _, game_id, col_str = callback.data.split(":")[cite: 1]
    col_idx = int(col_str)[cite: 1]

    if game_id not in joker_games:[cite: 1]
        await callback.answer("Игра завершена.", show_alert=True)[cite: 1]
        return

    game = joker_games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return

    now = time.time()[cite: 1]
    if now - game.get("last_click", 0) < 0.35:[cite: 1]
        await callback.answer("⏳ Не спешите!", show_alert=False)[cite: 1]
        return
    game["last_click"] = now[cite: 1]

    skull_col = game["current_skull"][cite: 1]
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]
    display_title = f"{html.escape(game['first_name'])} {player_name}"[cite: 1]

    if col_idx == skull_col:[cite: 1]
        game["history"].append({"chosen": col_idx, "skull": skull_col})[cite: 1]
        kb = generate_joker_keyboard(game_id, reveal_all=True)[cite: 1]
        user_id = game["user_id"][cite: 1]
        bet = game["bet"][cite: 1]
        del joker_games[game_id][cite: 1]
        record_game_result(user_id, won=False)[cite: 1]

        try:
            await callback.message.edit_text(
                f"<b>{display_title}</b>, вы проиграли!\n"
                f"💰 Ставка: {int(bet) if bet.is_integer() else bet} TON",
                reply_markup=kb,
                parse_mode="HTML"
            )[cite: 1]
        except Exception:
            pass
        await callback.answer()[cite: 1]
        return

    game["history"].append({"chosen": col_idx, "skull": skull_col})[cite: 1]
    curr_level = len(game["history"])[cite: 1]
    mult = JOKER_MULTS[curr_level - 1][cite: 1]
    curr_win = round(game["bet"] * mult, 2)[cite: 1]

    if curr_level >= len(JOKER_MULTS):[cite: 1]
        update_balance(game["user_id"], curr_win)[cite: 1]
        kb = generate_joker_keyboard(game_id, reveal_all=True)[cite: 1]
        user_id = game["user_id"][cite: 1]
        del joker_games[game_id][cite: 1]
        record_game_result(user_id, won=True)[cite: 1]

        await callback.message.edit_text(
            f"<b>{display_title}</b>, максимальный выигрыш!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {curr_win:.2f} TON",
            reply_markup=kb,
            parse_mode="HTML"
        )[cite: 1]
        await callback.answer()[cite: 1]
        return

    game["current_skull"] = random.randint(0, 2)[cite: 1]
    try:
        await callback.message.edit_text(
            f"<b>{display_title}</b>, вы начали игру джокер!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {curr_win:.2f} TON",
            reply_markup=generate_joker_keyboard(game_id),
            parse_mode="HTML"
        )[cite: 1]
    except Exception:
        pass
    await callback.answer()[cite: 1]


@dp.callback_query(F.data.startswith("jk_cash:"))
async def handle_joker_cashout(callback: CallbackQuery):
    game_id = callback.data.split(":")[1][cite: 1]
    if game_id not in joker_games:[cite: 1]
        await callback.answer("Игра уже завершена.", show_alert=True)[cite: 1]
        return
    game = joker_games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return

    mult = JOKER_MULTS[len(game["history"]) - 1][cite: 1]
    win_sum = round(game["bet"] * mult, 2)[cite: 1]
    update_balance(game["user_id"], win_sum)[cite: 1]
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]
    display_title = f"{html.escape(game['first_name'])} {player_name}"[cite: 1]

    kb = generate_joker_keyboard(game_id, reveal_all=True)[cite: 1]
    user_id = game["user_id"][cite: 1]
    del joker_games[game_id][cite: 1]
    record_game_result(user_id, won=True)[cite: 1]

    try:
        await callback.message.edit_text(
            f"<b>{display_title}</b>, вы забрали выигрыш!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {win_sum:.2f} TON",
            reply_markup=kb,
            parse_mode="HTML"
        )[cite: 1]
    except Exception:
        pass
    await callback.answer("Зачислено на баланс!")[cite: 1]


# ================= 3. 21 ОЧКО (BLACKJACK) =================
@dp.message(F.text.lower().startswith("21"))
async def handle_blackjack(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
    if user.get("is_frozen"):[cite: 1]
        await message.reply("⛔ Ваш аккаунт заморожен.")[cite: 1]
        return

    parts = message.text.strip().split()[cite: 1]
    if len(parts) < 2:[cite: 1]
        await message.reply("❌ Введите сумму ставки. Пример: <code>21 100</code>", parse_mode="HTML")[cite: 1]
        return

    try:
        bet = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
    except ValueError:
        return

    if bet <= 0 or float(user["balance"]) < bet:[cite: 1]
        await message.reply("❌ Недостаточно средств!")[cite: 1]
        return

    update_balance(user["user_id"], -bet)[cite: 1]
    deck = DECK.copy()[cite: 1]
    random.shuffle(deck)[cite: 1]

    player_cards = [deck.pop(), deck.pop()][cite: 1]
    dealer_cards = [deck.pop(), deck.pop()][cite: 1]

    player_pts = calc_hand(player_cards)[cite: 1]
    player_name = get_name(user["user_id"], user["username"], message.from_user.first_name)[cite: 1]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🃏 Взять карту", callback_data="bj_hit"),
        InlineKeyboardButton(text="🛑 Вскрыться", callback_data="bj_stand")
    ]])[cite: 1]

    msg = await message.answer(
        f"♠️ <b>21 Очко</b>\n\n"
        f"👤 Игрок: <b>{player_name}</b> | Ставка: <b>{bet:.2f} TON</b>\n"
        f"Твои карты: {format_hand(player_cards)} (<b>{player_pts}</b>)\n"
        f"Карта дилера: [{dealer_cards[0][0]}] [❓]",
        reply_markup=kb,
        parse_mode="HTML"
    )[cite: 1]

    bj_games[f"{msg.chat.id}_{msg.message_id}"] = {
        "user_id": user["user_id"],
        "username": user["username"],
        "first_name": message.from_user.first_name,
        "bet": bet,
        "deck": deck,
        "player": player_cards,
        "dealer": dealer_cards
    }[cite: 1]


@dp.callback_query(F.data.in_(["bj_hit", "bj_stand"]))
async def handle_bj_actions(callback: CallbackQuery):
    game_id = f"{callback.message.chat.id}_{callback.message.message_id}"[cite: 1]
    if game_id not in bj_games:[cite: 1]
        await callback.answer("Раунд завершен.", show_alert=True)[cite: 1]
        return

    game = bj_games[game_id][cite: 1]
    if callback.from_user.id != game["user_id"]:[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return

    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))[cite: 1]

    if callback.data == "bj_hit":[cite: 1]
        game["player"].append(game["deck"].pop())[cite: 1]
        pts = calc_hand(game["player"])[cite: 1]

        if pts > 21:[cite: 1]
            del bj_games[game_id][cite: 1]
            record_game_result(game["user_id"], won=False)[cite: 1]
            await callback.message.edit_text(
                f"💥 <b>Перебор! ({pts} очков)</b>\n\n"
                f"👤 {player_name} проиграл <b>{game['bet']:.2f} TON</b>\n"
                f"Карты: {format_hand(game['player'])}",
                reply_markup=None,
                parse_mode="HTML"
            )[cite: 1]
            await callback.answer()[cite: 1]
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🃏 Взять карту", callback_data="bj_hit"),
            InlineKeyboardButton(text="🛑 Вскрыться", callback_data="bj_stand")
        ]])[cite: 1]
        await callback.message.edit_text(
            f"♠️ <b>21 Очко</b>\n\n"
            f"👤 {player_name} | Ставка: <b>{game['bet']:.2f} TON</b>\n"
            f"Твои карты: {format_hand(game['player'])} (<b>{pts}</b>)\n"
            f"Карта дилера: [{game['dealer'][0][0]}] [❓]",
            reply_markup=kb,
            parse_mode="HTML"
        )[cite: 1]
        await callback.answer()[cite: 1]
        return

    if callback.data == "bj_stand":[cite: 1]
        del bj_games[game_id][cite: 1]
        player_pts = calc_hand(game["player"])[cite: 1]
        dealer_pts = calc_hand(game["dealer"])[cite: 1]

        while dealer_pts < 17:[cite: 1]
            game["dealer"].append(game["deck"].pop())[cite: 1]
            dealer_pts = calc_hand(game["dealer"])[cite: 1]

        if dealer_pts > 21 or player_pts > dealer_pts:[cite: 1]
            win = round(game["bet"] * 2.0, 2)[cite: 1]
            update_balance(game["user_id"], win)[cite: 1]
            record_game_result(game["user_id"], won=True)[cite: 1]
            res_text = f"🏆 <b>ПОБЕДА!</b>\nВыигрыш: <b>+{win:.2f} TON</b> 💎"[cite: 1]
        elif player_pts == dealer_pts:[cite: 1]
            update_balance(game["user_id"], game["bet"])[cite: 1]
            res_text = "🤝 <b>Ничья!</b> Ставка возвращена."[cite: 1]
        else:
            record_game_result(game["user_id"], won=False)[cite: 1]
            res_text = f"💀 <b>Дилер победил!</b> Потеряно: <b>{game['bet']:.2f} TON</b>"[cite: 1]

        await callback.message.edit_text(
            f"♠️ <b>Итог игры 21 Очко</b>\n\n"
            f"👤 Твои карты: {format_hand(game['player'])} (<b>{player_pts}</b>)\n"
            f"🤖 Карты дилера: {format_hand(game['dealer'])} (<b>{dealer_pts}</b>)\n\n"
            f"{res_text}",
            reply_markup=None,
            parse_mode="HTML"
        )[cite: 1]
        await callback.answer()[cite: 1]


# ================= .nextgame =================
@dp.message(F.text.lower().startswith(".nextgame"))
async def handle_nextgame(message: Message):
    user_name = (message.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await message.reply("⛔ Команда доступна только администратору @lithromantov.")[cite: 1]
        return

    if message.chat.type != "private":[cite: 1]
        await message.reply("❌ Настройку .nextgame можно вызывать только в ЛС с ботом.")[cite: 1]
        return

    user_id = message.from_user.id[cite: 1]
    parts = message.text.strip().split()[cite: 1]
    if len(parts) < 2:[cite: 1]
        await message.reply("❌ Укажите количество мин. Пример: <code>.nextgame 15шт</code>", parse_mode="HTML")[cite: 1]
        return

    match = re.search(r"(\d+)", parts[1])[cite: 1]
    if not match:[cite: 1]
        await message.reply("❌ Число мин не распознано. Пример: <code>.nextgame 15шт</code>", parse_mode="HTML")[cite: 1]
        return

    total_mines = int(match.group(1))[cite: 1]
    if not (1 <= total_mines <= 24):[cite: 1]
        await message.reply("❌ Допустимое количество мин от 1 до 24.")[cite: 1]
        return

    nextgame_setups[user_id] = {
        "total_mines": total_mines,
        "chosen_tiles": set()
    }[cite: 1]

    await message.answer(
        f"🛠 <b>Настройка следующей игры ({total_mines} мин)</b>\n\n"
        f"Кликайте по ячейкам, чтобы расставить мины (нужно выбрать ровно {total_mines} шт.):",
        reply_markup=generate_nextgame_keyboard(user_id),
        parse_mode="HTML"
    )[cite: 1]


@dp.callback_query(F.data.startswith("ng_tile:"))
async def handle_ng_tile(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)[cite: 1]
        return

    user_id = callback.from_user.id[cite: 1]
    if user_id not in nextgame_setups:[cite: 1]
        await callback.answer("Сессия настройки истекла.", show_alert=True)[cite: 1]
        return

    tile_idx = int(callback.data.split(":", 1)[1])[cite: 1]
    setup = nextgame_setups[user_id][cite: 1]

    if tile_idx in setup["chosen_tiles"]:[cite: 1]
        setup["chosen_tiles"].remove(tile_idx)[cite: 1]
    else:
        if len(setup["chosen_tiles"]) >= setup["total_mines"]:[cite: 1]
            await callback.answer(f"⚠️ Уже выбрано максимум мин ({setup['total_mines']} шт.)!", show_alert=True)[cite: 1]
            return
        setup["chosen_tiles"].add(tile_idx)[cite: 1]

    await callback.message.edit_reply_markup(reply_markup=generate_nextgame_keyboard(user_id))[cite: 1]
    await callback.answer()[cite: 1]


@dp.callback_query(F.data == "ng_save")
async def handle_ng_save(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)[cite: 1]
        return

    user_id = callback.from_user.id[cite: 1]
    setup = nextgame_setups.get(user_id)[cite: 1]
    if not setup or len(setup["chosen_tiles"]) != setup["total_mines"]:[cite: 1]
        await callback.answer(f"Выберите ровно {setup['total_mines']} мин!", show_alert=True)[cite: 1]
        return

    custom_next_mines[user_id] = set(setup["chosen_tiles"])[cite: 1]
    mines_count = setup["total_mines"][cite: 1]
    del nextgame_setups[user_id][cite: 1]

    await callback.message.edit_text(
        f"✅ <b>Расстановка сохранена!</b>\n\n"
        f"В твоей следующей игре на <b>{mines_count} мин</b> мины окажутся точно на выбранных позициях.",
        reply_markup=None,
        parse_mode="HTML"
    )[cite: 1]
    await callback.answer("Сохранено!")[cite: 1]


@dp.callback_query(F.data == "ng_cancel")
async def handle_ng_cancel(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)[cite: 1]
        return

    user_id = callback.from_user.id[cite: 1]
    if user_id in nextgame_setups:[cite: 1]
        del nextgame_setups[user_id][cite: 1]
    await callback.message.edit_text("❌ Настройка следующей игры отменена.", reply_markup=None)[cite: 1]
    await callback.answer("Отменено!")[cite: 1]


# ================= АДМИН-КОМАНДЫ =================
@dp.message(F.text.lower().startswith(".setbal"))
async def handle_admin_setbal(message: Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():[cite: 1]
        return
    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]
    amount = None[cite: 1]

    if message.reply_to_message and len(parts) >= 2:[cite: 1]
        try:
            amount = float(parts[1].replace(",", "."))[cite: 1]
            target_user = get_user(message.reply_to_message.from_user.id)[cite: 1]
        except ValueError:
            pass
    elif len(parts) >= 3:[cite: 1]
        target_raw = parts[1][cite: 1]
        try:
            amount = float(parts[2].replace(",", "."))[cite: 1]
            target_user = get_user_by_username(target_raw) if target_raw.startswith("@") else get_user(int(target_raw))[cite: 1]
        except ValueError:
            pass

    if target_user and amount is not None:[cite: 1]
        set_balance(target_user["user_id"], amount)[cite: 1]
        t_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]
        await message.reply(f"🔧 Баланс игрока <b>{t_name}</b> установлен на <b>{amount:.2f} TON</b>", parse_mode="HTML")[cite: 1]
    else:
        await message.reply("❌ Формат: <code>.setbal @юзер 1000</code>", parse_mode="HTML")[cite: 1]


@dp.message(F.text.lower().startswith(".freeze") | F.text.lower().startswith(".unfreeze"))
async def handle_admin_freeze(message: Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():[cite: 1]
        return
    is_freeze = message.text.lower().startswith(".freeze")[cite: 1]
    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]

    if message.reply_to_message:[cite: 1]
        target_user = get_user(message.reply_to_message.from_user.id)[cite: 1]
    elif len(parts) >= 2:[cite: 1]
        target_raw = parts[1][cite: 1]
        target_user = get_user_by_username(target_raw) if target_raw.startswith("@") else get_user(int(target_raw))[cite: 1]

    if target_user:[cite: 1]
        set_user_freeze(target_user["user_id"], 1 if is_freeze else 0)[cite: 1]
        t_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]
        status_text = "заморожен ❄️" if is_freeze else "разморожен 🔓"[cite: 1]
        await message.reply(f"👤 Аккаунт <b>{t_name}</b> {status_text}!", parse_mode="HTML")[cite: 1]


# ================= КЭШ / АНКЭШ / РЕНТГЕН =================
@dp.message(F.text.lower().startswith("+рентген") | F.text.lower().startswith("-рентген"))
async def handle_manage_xray(message: Message):
    user_name = (message.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        return

    is_grant = message.text.lower().startswith("+рентген")[cite: 1]
    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]

    if message.reply_to_message:[cite: 1]
        target_user = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )[cite: 1]
    elif len(parts) >= 2:[cite: 1]
        target_raw = parts[1][cite: 1]
        if target_raw.startswith("@"):[cite: 1]
            target_user = get_user_by_username(target_raw)[cite: 1]
        elif target_raw.isdigit():[cite: 1]
            target_user = get_user(int(target_raw))[cite: 1]

    if not target_user:[cite: 1]
        await message.reply("❌ Укажите игрока ответом на сообщение или через <code>+рентген @username</code>", parse_mode="HTML")[cite: 1]
        return

    set_user_xray(target_user["user_id"], 1 if is_grant else 0)[cite: 1]
    display_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]

    if is_grant:[cite: 1]
        await message.reply(f"👁✅ Игроку <b>{display_name}</b> выдан доступ к рентгену!", parse_mode="HTML")[cite: 1]
    else:
        await message.reply(f"👁❌ У игрока <b>{display_name}</b> отозван доступ к рентгену.", parse_mode="HTML")[cite: 1]


@dp.callback_query(F.data.startswith("xray:"))
async def handle_xray_alert(callback: CallbackQuery):
    if not has_xray_access(callback.from_user.id, callback.from_user.username):[cite: 1]
        await callback.answer("Это не твоя игра!", show_alert=True)[cite: 1]
        return

    game_id = callback.data.split(":", 1)[1][cite: 1]
    if game_id not in games:[cite: 1]
        await callback.answer("Игра уже завершена.", show_alert=True)[cite: 1]
        return

    game = games[game_id][cite: 1]
    grid_lines = [][cite: 1]
    for r in range(5):[cite: 1]
        row_str = ""[cite: 1]
        for c in range(5):[cite: 1]
            idx = r * 5 + c[cite: 1]
            if idx in game["opened"]:[cite: 1]
                row_str += "✅ "[cite: 1]
            elif idx in game["mines_pos"]:[cite: 1]
                row_str += "💣 "[cite: 1]
            else:
                row_str += "💎 "[cite: 1]
        grid_lines.append(row_str)[cite: 1]

    board_text = "\n".join(grid_lines)[cite: 1]
    await callback.answer(
        f"ID: {game['user_id']}\n"
        f"Баланс: {float(get_user(game['user_id']).get('balance') or 0.0):.2f} TON\n"
        f"Выбрано мин: {game['mines_count']} шт.\n\n"
        f"{board_text}",
        show_alert=True
    )[cite: 1]


@dp.message(F.text.lower() == "литр")
async def handle_admin_xray(message: Message):
    if not has_xray_access(message.from_user.id, message.from_user.username):[cite: 1]
        return

    if not message.reply_to_message:[cite: 1]
        await message.reply("❌ Ответьте этой командой реплаем на сообщение с активной игрой.")[cite: 1]
        return

    target_msg = message.reply_to_message[cite: 1]
    game_id = f"{target_msg.chat.id}_{target_msg.message_id}"[cite: 1]
    if game_id not in games:[cite: 1]
        await message.reply("❌ В этом сообщении нет активной игры.")[cite: 1]
        return

    game = games[game_id][cite: 1]
    grid_lines = [][cite: 1]
    for r in range(5):[cite: 1]
        row_str = ""[cite: 1]
        for c in range(5):[cite: 1]
            idx = r * 5 + c[cite: 1]
            if idx in game["opened"]:[cite: 1]
                row_str += "✅ "[cite: 1]
            elif idx in game["mines_pos"]:[cite: 1]
                row_str += "💣 "[cite: 1]
            else:
                row_str += "💎 "[cite: 1]
        grid_lines.append(row_str)[cite: 1]

    board_text = "\n".join(grid_lines)[cite: 1]
    await message.reply(
        f"👁 <b>Рентген игрового поля:</b>\n"
        f"👤 Игрок: <b>{html.escape(game['first_name'])}</b> | 🧨 Мин: <b>{game['mines_count']}</b>\n\n"
        f"{board_text}\n\n"
        "💣 — мина | 💎 — алмаз | ✅ — открыто",
        parse_mode="HTML"
    )[cite: 1]


@dp.message(F.text.lower().in_(["б", "баланс"]))
async def handle_show_balance(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    if message.reply_to_message:[cite: 1]
        target = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )[cite: 1]
        name = get_name(target["user_id"], target.get("username"), message.reply_to_message.from_user.first_name)[cite: 1]
    else:
        target = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
        name = get_name(target["user_id"], target.get("username"), message.from_user.first_name)[cite: 1]

    current_balance = float(target.get("balance") or 0.0)[cite: 1]
    await message.reply(f"💳 Баланс игрока <b>{name}</b>: <code>{current_balance:.2f} TON</code> 💎", parse_mode="HTML")[cite: 1]


@dp.message(F.text.lower().startswith("кэш"))
async def handle_admin_cash(message: Message):
    user_name = (message.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await message.reply("⛔ У вас нет прав на использование этой команды.")[cite: 1]
        return

    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]
    amount = None[cite: 1]

    if message.reply_to_message and len(parts) >= 2:[cite: 1]
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
            target_user = get_user(
                message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username,
                message.reply_to_message.from_user.first_name
            )[cite: 1]
        except ValueError:
            pass
    elif len(parts) >= 3:[cite: 1]
        target_raw = parts[1][cite: 1]
        try:
            amount = round(float(parts[2].replace(",", ".")), 2)[cite: 1]
            if target_raw.startswith("@"):[cite: 1]
                target_user = get_user_by_username(target_raw)[cite: 1]
            elif target_raw.isdigit():[cite: 1]
                target_user = get_user(int(target_raw))[cite: 1]
        except ValueError:
            pass
    elif len(parts) == 2:[cite: 1]
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
            target_user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
        except ValueError:
            pass

    if amount is None or amount <= 0:[cite: 1]
        await message.reply("❌ Формат: <code>кэш 500</code> или <code>кэш @username 500</code>", parse_mode="HTML")[cite: 1]
        return

    if amount < MIN_DEPOSIT:
        await message.reply(f"❌ Минимальный депозит — <b>{MIN_DEPOSIT:.2f} TON</b>", parse_mode="HTML")
        return

    if not target_user:[cite: 1]
        await message.reply("❌ Пользователь не найден в базе данных.")[cite: 1]
        return

    add_deposit_amount(target_user["user_id"], amount)[cite: 1]
    updated = get_user(target_user["user_id"])[cite: 1]
    target_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]
    bal = float(updated.get("balance") or 0.0)[cite: 1]

    await message.reply(
        f"⚡ <b>Успешная выдача кэша!</b>\n\n"
        f"👤 Получатель: <b>{target_name}</b>\n"
        f"💰 Начислено: <b>+{amount:.2f} TON</b>\n"
        f"💳 Новый баланс: <b>{bal:.2f} TON</b>",
        parse_mode="HTML"
    )[cite: 1]


@dp.message(F.text.lower().startswith("анкэш"))
async def handle_admin_uncash(message: Message):
    user_name = (message.from_user.username or "").lower()[cite: 1]
    if user_name != ADMIN_USERNAME.lower():[cite: 1]
        await message.reply("⛔ У вас нет прав на использование этой команды.")[cite: 1]
        return

    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]
    is_full = False[cite: 1]
    amount = 0.0[cite: 1]

    if message.reply_to_message:[cite: 1]
        target_user = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )[cite: 1]
        if len(parts) >= 2:[cite: 1]
            if parts[1].lower() == "фулл":[cite: 1]
                is_full = True[cite: 1]
            else:
                try:
                    amount = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
                except ValueError:
                    pass
    elif len(parts) >= 3:[cite: 1]
        target_raw = parts[1][cite: 1]
        if target_raw.startswith("@"):[cite: 1]
            target_user = get_user_by_username(target_raw)[cite: 1]
        elif target_raw.isdigit():[cite: 1]
            target_user = get_user(int(target_raw))[cite: 1]

        if parts[2].lower() == "фулл":[cite: 1]
            is_full = True[cite: 1]
        else:
            try:
                amount = round(float(parts[2].replace(",", ".")), 2)[cite: 1]
            except ValueError:
                pass
    elif len(parts) == 2:[cite: 1]
        target_user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
        if parts[1].lower() == "фулл":[cite: 1]
            is_full = True[cite: 1]
        else:
            try:
                amount = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
            except ValueError:
                pass

    if not target_user:[cite: 1]
        await message.reply("❌ Пользователь не найден.")[cite: 1]
        return

    target_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]
    old_balance = float(target_user.get("balance") or 0.0)[cite: 1]

    if is_full:[cite: 1]
        set_balance(target_user["user_id"], 0.0)[cite: 1]
        await message.reply(
            f"🧹 <b>Баланс полностью аннулирован!</b>\n\n"
            f"👤 Игрок: <b>{target_name}</b>\n"
            f"📉 Списано: <b>-{old_balance:.2f} TON</b>\n"
            f"💳 Новый баланс: <b>0.00 TON</b>",
            parse_mode="HTML"
        )[cite: 1]
        return

    if amount <= 0:[cite: 1]
        await message.reply("❌ Формат: <code>анкэш фулл</code> или <code>анкэш 100</code>", parse_mode="HTML")[cite: 1]
        return

    new_bal = max(0.0, round(old_balance - amount, 2))[cite: 1]
    set_balance(target_user["user_id"], new_bal)[cite: 1]
    await message.reply(
        f"📉 <b>Средства списаны!</b>\n\n"
        f"👤 Игрок: <b>{target_name}</b>\n"
        f"💸 Списано: <b>-{amount:.2f} TON</b>\n"
        f"💳 Новый баланс: <b>{new_bal:.2f} TON</b>",
        parse_mode="HTML"
    )[cite: 1]


# ================= ТОП (/top) =================
def render_top_text(chat_id: int = None, is_admin: bool = False) -> str:
    with get_db() as conn:
        if chat_id and chat_id < 0:[cite: 1]
            rows = conn.execute("""
                SELECT u.user_id, u.username, u.first_name, u.balance 
                FROM users u
                INNER JOIN chat_members cm ON u.user_id = cm.user_id
                WHERE cm.chat_id = ? AND COALESCE(u.balance, 0) <= ?
                ORDER BY COALESCE(u.balance, 0) DESC 
                LIMIT 10
            """, (chat_id, MAX_SAFE_VALUE)).fetchall()[cite: 1]
            header = "🏆 <b>Топ-10 богачей этого чата по балансу TON:</b>\n\n"[cite: 1]
        else:
            if not is_admin:[cite: 1]
                return "ℹ️ Команда <code>/top</code> работает в группе для просмотра топа участников этого чата!"[cite: 1]

            rows = conn.execute("""
                SELECT user_id, username, first_name, balance 
                FROM users 
                WHERE COALESCE(balance, 0) <= ?
                ORDER BY COALESCE(balance, 0) DESC 
                LIMIT 10
            """, (MAX_SAFE_VALUE,)).fetchall()[cite: 1]
            header = "🏆 <b>Глобальный топ-10 богачей по балансу TON:</b>\n\n"[cite: 1]

    if not rows:[cite: 1]
        return header + "<i>В этом чате пока нет игроков!</i>"[cite: 1]

    text = header[cite: 1]
    for idx, row in enumerate(rows, start=1):[cite: 1]
        nickname = get_name(row["user_id"], row["username"], row["first_name"])[cite: 1]
        bal = float(row["balance"] or 0.0)[cite: 1]
        text += f"{idx}. <b>{nickname}</b> — <code>{bal:.2f} TON</code>\n"[cite: 1]
    return text


@dp.message(Command("top"))
async def cmd_top(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    is_admin = (message.from_user.username or "").lower() == ADMIN_USERNAME.lower()[cite: 1]
    await message.answer(render_top_text(message.chat.id, is_admin), parse_mode="HTML")[cite: 1]


@dp.callback_query(F.data == "show_top")
async def cb_show_top(callback: CallbackQuery):
    chat_id = callback.message.chat.id[cite: 1]
    register_chat_member(chat_id, callback.from_user.id)[cite: 1]
    is_admin = (callback.from_user.username or "").lower() == ADMIN_USERNAME.lower()[cite: 1]
    await callback.message.answer(render_top_text(chat_id, is_admin), parse_mode="HTML")[cite: 1]
    await callback.answer()[cite: 1]


# ================= БОНУС 5000 TON =================
def claim_bonus_logic(user_id: int, username: str = None, first_name: str = None):
    user = get_user(user_id, username, first_name)[cite: 1]
    now = datetime.datetime.now()[cite: 1]

    if user["last_bonus"]:[cite: 1]
        last_bonus_time = datetime.datetime.fromisoformat(user["last_bonus"])[cite: 1]
        cooldown = datetime.timedelta(days=1)[cite: 1]
        if now - last_bonus_time < cooldown:[cite: 1]
            remaining = cooldown - (now - last_bonus_time)[cite: 1]
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)[cite: 1]
            minutes, _ = divmod(remainder, 60)[cite: 1]
            return False, f"⏳ Рано! Бонус будет доступен через {hours} ч. {minutes} мин.", 0.0[cite: 1]

    update_balance(user_id, BONUS_AMOUNT)[cite: 1]
    with get_db() as conn:[cite: 1]
        conn.execute("UPDATE users SET last_bonus = ? WHERE user_id = ?", (now.isoformat(), user_id))[cite: 1]
        conn.commit()[cite: 1]

    updated = get_user(user_id)[cite: 1]
    return True, f"🎉 Вы забрали бонус: +{BONUS_AMOUNT:.2f} TON!", float(updated.get("balance") or 0.0)[cite: 1]


@dp.message(F.text.lower().in_(["бонус", "/bonus"]))
async def handle_bonus_command(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    success, msg, new_balance = claim_bonus_logic(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
    if not success:[cite: 1]
        await message.reply(msg)[cite: 1]
    else:
        user_name = get_name(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
        await message.reply(
            f"🎁 <b>Бонус получен!</b>\n\n"
            f"👤 Игрок: <b>{user_name}</b>\n"
            f"💰 Начислено: <b>+{BONUS_AMOUNT:.2f} TON</b>\n"
            f"💳 Баланс: <b>{new_balance:.2f} TON</b>",
            parse_mode="HTML"
        )[cite: 1]


@dp.callback_query(F.data == "get_bonus")
async def cb_get_bonus(callback: CallbackQuery):
    success, msg, new_balance = claim_bonus_logic(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)[cite: 1]
    if not success:[cite: 1]
        await callback.answer(msg, show_alert=True)[cite: 1]
    else:
        await callback.answer(msg, show_alert=True)[cite: 1]
        await callback.message.answer(
            f"🎁 <b>Бонус получен!</b>\n"
            f"Начислено: <b>+{BONUS_AMOUNT:.2f} TON</b>\n"
            f"Текущий баланс: <b>{new_balance:.2f} TON</b>",
            parse_mode="HTML"
        )[cite: 1]


# ================= ПЕРЕВОДЫ =================
@dp.message(F.text.lower().startswith("п "))
async def handle_transfer(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    sender = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)[cite: 1]
    sender_bal = float(sender.get("balance") or 0.0)[cite: 1]
    parts = message.text.strip().split()[cite: 1]
    target_user = None[cite: 1]
    amount = 0.0[cite: 1]

    if message.reply_to_message and len(parts) == 2:[cite: 1]
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)[cite: 1]
            target_user = get_user(
                message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username,
                message.reply_to_message.from_user.first_name
            )[cite: 1]
            register_chat_member(message.chat.id, message.reply_to_message.from_user.id)[cite: 1]
        except ValueError:
            await message.reply("❌ Неверный формат суммы. Пример: <code>п 50</code>", parse_mode="HTML")[cite: 1]
            return
    elif len(parts) >= 3:[cite: 1]
        target_raw = parts[1][cite: 1]
        try:
            amount = round(float(parts[2].replace(",", ".")), 2)[cite: 1]
        except ValueError:
            await message.reply("❌ Неверный формат суммы. Пример: <code>п @юзер 50</code>", parse_mode="HTML")[cite: 1]
            return

        if target_raw.startswith("@"):[cite: 1]
            target_user = get_user_by_username(target_raw)[cite: 1]
        elif target_raw.isdigit():[cite: 1]
            target_user = get_user(int(target_raw))[cite: 1]
    else:
        await message.reply("❌ Формат: <code>п @username сумма</code> или ответом: <code>п сумма</code>", parse_mode="HTML")[cite: 1]
        return

    if not target_user:[cite: 1]
        await message.reply("❌ Получатель не найден в базе.")[cite: 1]
        return

    if target_user["user_id"] == sender["user_id"]:[cite: 1]
        await message.reply("❌ Нельзя переводить TON самому себе.")[cite: 1]
        return

    if amount <= 0 or sender_bal < amount:[cite: 1]
        await message.reply(f"❌ Не хватает TON! Твой баланс: <b>{sender_bal:.2f} TON</b>", parse_mode="HTML")[cite: 1]
        return

    update_balance(sender["user_id"], -amount)[cite: 1]
    update_balance(target_user["user_id"], amount)[cite: 1]

    sender_name = get_name(sender["user_id"], message.from_user.username, message.from_user.first_name)[cite: 1]
    recipient_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))[cite: 1]

    await message.reply(
        f"✅ <b>Перевод выполнен!</b>\n\n"
        f"От: <b>{sender_name}</b>\n"
        f"Кому: <b>{recipient_name}</b>\n"
        f"Сумма: <b>{amount:.2f} TON</b>",
        parse_mode="HTML"
    )[cite: 1]


# ================= ПАСХАЛКИ =================
@dp.message(F.text.func(lambda text: text and any(phrase in text.lower().replace("  ", " ").strip() for phrase in ["lift syka", "лифт сука"])))
async def handle_lift_syka(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)[cite: 1]
    file_path = os.path.join(BASE_DIR, "lift.png")
    if not os.path.exists(file_path):
        await message.reply("ЭТА ЖИ LIFT SYKA (файл lift.png не найден рядом со скриптом)")
        return

    try:
        photo = FSInputFile(file_path)
        await message.reply_photo(photo=photo, caption="ЭТА ЖИ LIFT SYKA")
    except Exception as e:
        await message.reply(f"ЭТА ЖИ LIFT SYKA (ошибка отправки: {e})")[cite: 1]


GROSS_KEYWORDS = [
    "гросс", "гроссштур", "грос", "гросштур", "гросс штур", 
    "грос штур", "grossshtyr", "@grossshtyr"
]

@dp.message(F.text.func(lambda text: text and any(k in text.lower() for k in GROSS_KEYWORDS)))
async def handle_gross_easter_egg(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    file_path = os.path.join(BASE_DIR, "gross.png")
    if not os.path.exists(file_path):
        return

    try:
        photo = FSInputFile(file_path)
        await message.reply_photo(photo=photo)
    except Exception:
        pass


SARMOLAEV_KEYWORDS = [
    "сарделька лаев", "сармалаев", "сарделька", "sarmolaev"
]

@dp.message(F.text.func(lambda text: text and any(k in text.lower() for k in SARMOLAEV_KEYWORDS)))
async def handle_sarmolaev_easter_egg(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    file_path = os.path.join(BASE_DIR, "sarmolaev.png")
    if not os.path.exists(file_path):
        return

    try:
        photo = FSInputFile(file_path)
        await message.reply_photo(photo=photo)
    except Exception:
        pass


@dp.callback_query(F.data == "ignore")
async def handle_ignore(callback: CallbackQuery):
    await callback.answer()[cite: 1]


# ================= ЗАПУСК =================
async def main():
    init_db()[cite: 1]
    print("Casino Bot (Mines, Joker, 21) успешно запущен!")[cite: 1]
    await dp.start_polling(bot)[cite: 1]


if __name__ == "__main__":
    asyncio.run(main())[cite: 1]