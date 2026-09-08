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
ASSETS_DIR = "///"
MIN_DEPOSIT = 10.0

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Игровые сессии
games = {}          # Мины
joker_games = {}    # Башня Джокер
bj_games = {}       # 21 Очко

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
    register_chat_member(message.chat.id, message.from_user.id)
    user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    bal = float(user.get("balance") or 0.0)
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
    user = get_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    bal = float(user.get("balance") or 0.0)
    max_bal = float(user.get("max_balance") or bal)
    max_dep = float(user.get("max_deposit") or 0.0)
    total_games = int(user.get("total_deposits") or 0)
    games_won = int(user.get("games_won") or 0)
    winrate = (games_won / total_games * 100) if total_games > 0 else 0.0
    xray_status = "Есть ✅" if (user.get("has_xray") or (callback.from_user.username or "").lower() == ADMIN_USERNAME.lower()) else "Нет ❌"

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
    register_chat_member(message.chat.id, message.from_user.id)
    if message.reply_to_message:
        target = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )
    else:
        target = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

    max_bal = float(target.get("max_balance") or 0.0)
    max_dep = float(target.get("max_deposit") or 0.0)
    total_games = int(target.get("total_deposits") or 0)
    games_won = int(target.get("games_won") or 0)
    winrate = (games_won / total_games * 100) if total_games > 0 else 0.0

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
    register_chat_member(message.chat.id, message.from_user.id)
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)
    if user.get("is_frozen"):
        await message.reply("⛔ Ваш аккаунт заморожен администратором.")
        return

    user_bal = float(user.get("balance") or 0.0)
    text = message.text.lower().strip()

    args_text = text[4:].strip()
    if not args_text:
        await message.reply("❌ Введите сумму ставки. Пример: <code>мины 100</code> или <code>мины 100 24 шт</code>", parse_mode="HTML")
        return

    allowed_mines = [1, 5, 10, 15, 20, 24]
    mines_count = None
    bet = None

    mines_match = re.search(r"\b(\d+)\s*(?:шт|мин|м)\b", args_text)
    if mines_match:
        val = int(mines_match.group(1))
        if val in allowed_mines:
            mines_count = val
            set_user_mines(user_id, mines_count)
            args_text = args_text[:mines_match.start()] + " " + args_text[mines_match.end():]
        else:
            await message.reply(f"❌ Недопустимое число мин. Доступны: {', '.join(f'{x}шт' for x in allowed_mines)}")
            return

    tokens = args_text.replace(",", ".").split()
    numbers = []
    for t in tokens:
        try:
            numbers.append(float(t))
        except ValueError:
            pass

    if not numbers:
        await message.reply("❌ Не указана сумма ставки. Пример: <code>мины 50 10 шт</code>", parse_mode="HTML")
        return

    if mines_count is None and len(numbers) >= 2:
        if int(numbers[1]) in allowed_mines:
            bet = numbers[0]
            mines_count = int(numbers[1])
            set_user_mines(user_id, mines_count)
        elif int(numbers[0]) in allowed_mines:
            mines_count = int(numbers[0])
            bet = numbers[1]
            set_user_mines(user_id, mines_count)

    if bet is None:
        bet = numbers[0]
    if mines_count is None:
        mines_count = user.get("default_mines") or 5

    bet = round(bet, 2)
    if bet <= 0 or user_bal < bet:
        await message.reply(f"❌ Недостаточно средств! Баланс: <b>{user_bal:.2f} TON</b>", parse_mode="HTML")
        return

    update_balance(user_id, -bet)
    player_name = get_name(user_id, message.from_user.username, message.from_user.first_name)

    sent_msg = await message.answer(
        f"💣 <b>Игра началась!</b>\n\n"
        f"👤 Игрок: <b>{player_name}</b>\n"
        f"💵 Ставка: <b>{bet:.2f} TON</b>\n"
        f"🧨 Мин: <b>{mines_count} шт.</b> | 💎 Безопасных клеток: <b>{TOTAL_TILES - mines_count}</b>\n\n"
        "Открывайте клетки:",
        parse_mode="HTML"
    )

    game_id = f"{sent_msg.chat.id}_{sent_msg.message_id}"
    if user_id in custom_next_mines and len(custom_next_mines[user_id]) == mines_count:
        mines_positions = custom_next_mines.pop(user_id)
    else:
        mines_positions = set(random.sample(range(TOTAL_TILES), mines_count))

    games[game_id] = {
        "user_id": user_id,
        "first_name": message.from_user.first_name,
        "username": message.from_user.username,
        "bet": bet,
        "mines_count": mines_count,
        "mines_pos": mines_positions,
        "opened": set(),
        "last_click": 0.0
    }

    await sent_msg.edit_reply_markup(reply_markup=generate_game_keyboard(game_id))


@dp.callback_query(F.data.startswith("tile:"))
async def handle_click_tile(callback: CallbackQuery):
    _, game_id, tile_str = callback.data.split(":", 2)
    tile_idx = int(tile_str)

    if game_id not in games:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    game = games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return

    now = time.time()
    if now - game.get("last_click", 0) < 0.35:
        await callback.answer("⏳ Не спешите!", show_alert=False)
        return
    game["last_click"] = now

    if tile_idx in game["opened"]:
        await callback.answer()
        return

    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))

    if tile_idx in game["mines_pos"]:
        bet = game["bet"]
        kb = generate_game_keyboard(game_id, reveal_all=True)
        user_id = game["user_id"]
        del games[game_id]
        record_game_result(user_id, won=False)
        await callback.answer()
        asyncio.create_task(animate_loss(callback.message, player_name, bet, kb))
        return

    game["opened"].add(tile_idx)
    opened_len = len(game["opened"])
    safe_total = TOTAL_TILES - game["mines_count"]

    if opened_len == safe_total:
        mult = calculate_multiplier(game["mines_count"], opened_len)
        win_sum = round(game["bet"] * mult, 2)
        update_balance(game["user_id"], win_sum)
        kb = generate_game_keyboard(game_id, reveal_all=True)
        user_id = game["user_id"]
        del games[game_id]
        record_game_result(user_id, won=True)

        try:
            await callback.message.edit_text(
                f"🏆 <b>Идеальная зачистка!</b>\n\n"
                f"👤 Игрок: <b>{player_name}</b>\n"
                f"Множитель: <b>x{mult}</b>\n"
                f"Выигрыш: <b>+{win_sum:.2f} TON</b> 💎",
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    cur_mult = calculate_multiplier(game["mines_count"], opened_len)
    cur_win = round(game["bet"] * cur_mult, 2)

    try:
        await callback.message.edit_text(
            f"🎮 <b>Мины ({game['mines_count']} шт.)</b>\n"
            f"👤 Игрок: <b>{player_name}</b>\n"
            f"💎 Найдено: <b>{opened_len}/{safe_total}</b>\n"
            f"Куш: <b>{cur_win:.2f} TON</b> (x{cur_mult})\n\n"
            "Открывайте следующую клетку или забирайте TON:",
            reply_markup=generate_game_keyboard(game_id),
            parse_mode="HTML"
        )
    except TelegramRetryAfter as e:
        await callback.answer(f"⏳ Слишком быстро! Подождите {e.retry_after} сек.", show_alert=True)
        return
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def handle_cancel_game(callback: CallbackQuery):
    game_id = callback.data.split(":", 1)[1]
    if game_id not in games:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    game = games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return
    if len(game["opened"]) > 0:
        await callback.answer("⚠️ Вы уже открыли ячейку! Отмена невозможна.", show_alert=True)
        return

    update_balance(game["user_id"], game["bet"])
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))
    del games[game_id]

    await callback.message.edit_text(
        f"🚫 <b>Игра отменена!</b>\n\n👤 Игрок: <b>{player_name}</b>\n💵 Ставка <b>{game['bet']:.2f} TON</b> возвращена.",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Игра отменена!")


@dp.callback_query(F.data.startswith("cashout:"))
async def handle_cashout(callback: CallbackQuery):
    game_id = callback.data.split(":", 1)[1]
    if game_id not in games:
        await callback.answer("Игра завершена.", show_alert=True)
        return
    game = games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return
    if len(game["opened"]) == 0:
        await callback.answer("Откройте хотя бы одну ячейку!", show_alert=True)
        return

    final_mult = calculate_multiplier(game["mines_count"], len(game["opened"]))
    win_sum = round(game["bet"] * final_mult, 2)
    update_balance(game["user_id"], win_sum)
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))

    kb = generate_game_keyboard(game_id, reveal_all=True)
    user_id = game["user_id"]
    del games[game_id]
    record_game_result(user_id, won=True)

    try:
        await callback.message.edit_text(
            f"💰 <b>{player_name} забрал куш!</b>\n\n"
            f"Множитель: <b>x{final_mult}</b>\n"
            f"Выигрыш: <b>+{win_sum:.2f} TON</b> 💎\n\nКарта раунда:",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.answer()


# ================= 2. БАШНЯ ДЖОКЕР =================
@dp.message(F.text.lower().startswith("джокер"))
async def handle_joker_command(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    user_id = message.from_user.id
    user = get_user(user_id, message.from_user.username, message.from_user.first_name)
    if user.get("is_frozen"):
        await message.reply("⛔ Ваш аккаунт заморожен.")
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("❌ Введите ставку. Пример: <code>джокер 100</code>", parse_mode="HTML")
        return

    try:
        bet = round(float(parts[1].replace(",", ".")), 2)
    except ValueError:
        return

    if bet <= 0 or float(user["balance"]) < bet:
        await message.reply("❌ Недостаточно средств!")
        return

    update_balance(user_id, -bet)
    player_name = get_name(user_id, message.from_user.username, message.from_user.first_name)
    display_title = f"{html.escape(message.from_user.first_name)} {player_name}"

    sent_msg = await message.answer(
        f"<b>{display_title}</b>, вы начали игру джокер!\n"
        f"💰 Ставка: {int(bet) if bet.is_integer() else bet} TON\n\n"
        "Выберите карту:",
        parse_mode="HTML"
    )

    game_id = f"{sent_msg.chat.id}_{sent_msg.message_id}"
    joker_games[game_id] = {
        "user_id": user_id,
        "first_name": message.from_user.first_name,
        "username": message.from_user.username,
        "bet": bet,
        "history": [],
        "current_skull": random.randint(0, 2),
        "last_click": 0.0
    }
    await sent_msg.edit_reply_markup(reply_markup=generate_joker_keyboard(game_id))


@dp.callback_query(F.data.startswith("jk_pick:"))
async def handle_joker_pick(callback: CallbackQuery):
    _, game_id, col_str = callback.data.split(":")
    col_idx = int(col_str)

    if game_id not in joker_games:
        await callback.answer("Игра завершена.", show_alert=True)
        return

    game = joker_games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return

    now = time.time()
    if now - game.get("last_click", 0) < 0.35:
        await callback.answer("⏳ Не спешите!", show_alert=False)
        return
    game["last_click"] = now

    skull_col = game["current_skull"]
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))
    display_title = f"{html.escape(game['first_name'])} {player_name}"

    if col_idx == skull_col:
        game["history"].append({"chosen": col_idx, "skull": skull_col})
        kb = generate_joker_keyboard(game_id, reveal_all=True)
        user_id = game["user_id"]
        bet = game["bet"]
        del joker_games[game_id]
        record_game_result(user_id, won=False)

        try:
            await callback.message.edit_text(
                f"<b>{display_title}</b>, вы проиграли!\n"
                f"💰 Ставка: {int(bet) if bet.is_integer() else bet} TON",
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer()
        return

    game["history"].append({"chosen": col_idx, "skull": skull_col})
    curr_level = len(game["history"])
    mult = JOKER_MULTS[curr_level - 1]
    curr_win = round(game["bet"] * mult, 2)

    if curr_level >= len(JOKER_MULTS):
        update_balance(game["user_id"], curr_win)
        kb = generate_joker_keyboard(game_id, reveal_all=True)
        user_id = game["user_id"]
        del joker_games[game_id]
        record_game_result(user_id, won=True)

        await callback.message.edit_text(
            f"<b>{display_title}</b>, максимальный выигрыш!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {curr_win:.2f} TON",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
        return

    game["current_skull"] = random.randint(0, 2)
    try:
        await callback.message.edit_text(
            f"<b>{display_title}</b>, вы начали игру джокер!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {curr_win:.2f} TON",
            reply_markup=generate_joker_keyboard(game_id),
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("jk_cash:"))
async def handle_joker_cashout(callback: CallbackQuery):
    game_id = callback.data.split(":")[1]
    if game_id not in joker_games:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return
    game = joker_games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return

    mult = JOKER_MULTS[len(game["history"]) - 1]
    win_sum = round(game["bet"] * mult, 2)
    update_balance(game["user_id"], win_sum)
    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))
    display_title = f"{html.escape(game['first_name'])} {player_name}"

    kb = generate_joker_keyboard(game_id, reveal_all=True)
    user_id = game["user_id"]
    del joker_games[game_id]
    record_game_result(user_id, won=True)

    try:
        await callback.message.edit_text(
            f"<b>{display_title}</b>, вы забрали выигрыш!\n"
            f"💰 Ставка: {int(game['bet']) if game['bet'].is_integer() else game['bet']} TON\n"
            f"💵 Выигрыш: x{str(mult).replace('.', ',')} | {win_sum:.2f} TON",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception:
        pass
    await callback.answer("Зачислено на баланс!")


# ================= 3. 21 ОЧКО (BLACKJACK) =================
@dp.message(F.text.lower().startswith("21"))
async def handle_blackjack(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if user.get("is_frozen"):
        await message.reply("⛔ Ваш аккаунт заморожен.")
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("❌ Введите сумму ставки. Пример: <code>21 100</code>", parse_mode="HTML")
        return

    try:
        bet = round(float(parts[1].replace(",", ".")), 2)
    except ValueError:
        return

    if bet <= 0 or float(user["balance"]) < bet:
        await message.reply("❌ Недостаточно средств!")
        return

    update_balance(user["user_id"], -bet)
    deck = DECK.copy()
    random.shuffle(deck)

    player_cards = [deck.pop(), deck.pop()]
    dealer_cards = [deck.pop(), deck.pop()]

    player_pts = calc_hand(player_cards)
    player_name = get_name(user["user_id"], user["username"], message.from_user.first_name)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🃏 Взять карту", callback_data="bj_hit"),
        InlineKeyboardButton(text="🛑 Вскрыться", callback_data="bj_stand")
    ]])

    msg = await message.answer(
        f"♠️ <b>21 Очко</b>\n\n"
        f"👤 Игрок: <b>{player_name}</b> | Ставка: <b>{bet:.2f} TON</b>\n"
        f"Твои карты: {format_hand(player_cards)} (<b>{player_pts}</b>)\n"
        f"Карта дилера: [{dealer_cards[0][0]}] [❓]",
        reply_markup=kb,
        parse_mode="HTML"
    )

    bj_games[f"{msg.chat.id}_{msg.message_id}"] = {
        "user_id": user["user_id"],
        "username": user["username"],
        "first_name": message.from_user.first_name,
        "bet": bet,
        "deck": deck,
        "player": player_cards,
        "dealer": dealer_cards
    }


@dp.callback_query(F.data.in_(["bj_hit", "bj_stand"]))
async def handle_bj_actions(callback: CallbackQuery):
    game_id = f"{callback.message.chat.id}_{callback.message.message_id}"
    if game_id not in bj_games:
        await callback.answer("Раунд завершен.", show_alert=True)
        return

    game = bj_games[game_id]
    if callback.from_user.id != game["user_id"]:
        await callback.answer("Это не твоя игра!", show_alert=True)
        return

    player_name = get_name(game["user_id"], game.get("username"), game.get("first_name"))

    if callback.data == "bj_hit":
        game["player"].append(game["deck"].pop())
        pts = calc_hand(game["player"])

        if pts > 21:
            del bj_games[game_id]
            record_game_result(game["user_id"], won=False)
            await callback.message.edit_text(
                f"💥 <b>Перебор! ({pts} очков)</b>\n\n"
                f"👤 {player_name} проиграл <b>{game['bet']:.2f} TON</b>\n"
                f"Карты: {format_hand(game['player'])}",
                reply_markup=None,
                parse_mode="HTML"
            )
            await callback.answer()
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🃏 Взять карту", callback_data="bj_hit"),
            InlineKeyboardButton(text="🛑 Вскрыться", callback_data="bj_stand")
        ]])
        await callback.message.edit_text(
            f"♠️ <b>21 Очко</b>\n\n"
            f"👤 {player_name} | Ставка: <b>{game['bet']:.2f} TON</b>\n"
            f"Твои карты: {format_hand(game['player'])} (<b>{pts}</b>)\n"
            f"Карта дилера: [{game['dealer'][0][0]}] [❓]",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await callback.answer()
        return

    if callback.data == "bj_stand":
        del bj_games[game_id]
        player_pts = calc_hand(game["player"])
        dealer_pts = calc_hand(game["dealer"])

        while dealer_pts < 17:
            game["dealer"].append(game["deck"].pop())
            dealer_pts = calc_hand(game["dealer"])

        if dealer_pts > 21 or player_pts > dealer_pts:
            win = round(game["bet"] * 2.0, 2)
            update_balance(game["user_id"], win)
            record_game_result(game["user_id"], won=True)
            res_text = f"🏆 <b>ПОБЕДА!</b>\nВыигрыш: <b>+{win:.2f} TON</b> 💎"
        elif player_pts == dealer_pts:
            update_balance(game["user_id"], game["bet"])
            res_text = "🤝 <b>Ничья!</b> Ставка возвращена."
        else:
            record_game_result(game["user_id"], won=False)
            res_text = f"💀 <b>Дилер победил!</b> Потеряно: <b>{game['bet']:.2f} TON</b>"

        await callback.message.edit_text(
            f"♠️ <b>Итог игры 21 Очко</b>\n\n"
            f"👤 Твои карты: {format_hand(game['player'])} (<b>{player_pts}</b>)\n"
            f"🤖 Карты дилера: {format_hand(game['dealer'])} (<b>{dealer_pts}</b>)\n\n"
            f"{res_text}",
            reply_markup=None,
            parse_mode="HTML"
        )
        await callback.answer()


# ================= .nextgame =================
@dp.message(F.text.lower().startswith(".nextgame"))
async def handle_nextgame(message: Message):
    user_name = (message.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await message.reply("⛔ Команда доступна только администратору @lithromantov.")
        return

    if message.chat.type != "private":
        await message.reply("❌ Настройку .nextgame можно вызывать только в ЛС с ботом.")
        return

    user_id = message.from_user.id
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply("❌ Укажите количество мин. Пример: <code>.nextgame 15шт</code>", parse_mode="HTML")
        return

    match = re.search(r"(\d+)", parts[1])
    if not match:
        await message.reply("❌ Число мин не распознано. Пример: <code>.nextgame 15шт</code>", parse_mode="HTML")
        return

    total_mines = int(match.group(1))
    if not (1 <= total_mines <= 24):
        await message.reply("❌ Допустимое количество мин от 1 до 24.")
        return

    nextgame_setups[user_id] = {
        "total_mines": total_mines,
        "chosen_tiles": set()
    }

    await message.answer(
        f"🛠 <b>Настройка следующей игры ({total_mines} мин)</b>\n\n"
        f"Кликайте по ячейкам, чтобы расставить мины (нужно выбрать ровно {total_mines} шт.):",
        reply_markup=generate_nextgame_keyboard(user_id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("ng_tile:"))
async def handle_ng_tile(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id not in nextgame_setups:
        await callback.answer("Сессия настройки истекла.", show_alert=True)
        return

    tile_idx = int(callback.data.split(":", 1)[1])
    setup = nextgame_setups[user_id]

    if tile_idx in setup["chosen_tiles"]:
        setup["chosen_tiles"].remove(tile_idx)
    else:
        if len(setup["chosen_tiles"]) >= setup["total_mines"]:
            await callback.answer(f"⚠️ Уже выбрано максимум мин ({setup['total_mines']} шт.)!", show_alert=True)
            return
        setup["chosen_tiles"].add(tile_idx)

    await callback.message.edit_reply_markup(reply_markup=generate_nextgame_keyboard(user_id))
    await callback.answer()


@dp.callback_query(F.data == "ng_save")
async def handle_ng_save(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    user_id = callback.from_user.id
    setup = nextgame_setups.get(user_id)
    if not setup or len(setup["chosen_tiles"]) != setup["total_mines"]:
        await callback.answer(f"Выберите ровно {setup['total_mines']} мин!", show_alert=True)
        return

    custom_next_mines[user_id] = set(setup["chosen_tiles"])
    mines_count = setup["total_mines"]
    del nextgame_setups[user_id]

    await callback.message.edit_text(
        f"✅ <b>Расстановка сохранена!</b>\n\n"
        f"В твоей следующей игре на <b>{mines_count} мин</b> мины окажутся точно на выбранных позициях.",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Сохранено!")


@dp.callback_query(F.data == "ng_cancel")
async def handle_ng_cancel(callback: CallbackQuery):
    user_name = (callback.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id in nextgame_setups:
        del nextgame_setups[user_id]
    await callback.message.edit_text("❌ Настройка следующей игры отменена.", reply_markup=None)
    await callback.answer("Отменено!")


# ================= АДМИН-КОМАНДЫ =================
@dp.message(F.text.lower().startswith(".setbal"))
async def handle_admin_setbal(message: Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    parts = message.text.strip().split()
    target_user = None
    amount = None

    if message.reply_to_message and len(parts) >= 2:
        try:
            amount = float(parts[1].replace(",", "."))
            target_user = get_user(message.reply_to_message.from_user.id)
        except ValueError:
            pass
    elif len(parts) >= 3:
        target_raw = parts[1]
        try:
            amount = float(parts[2].replace(",", "."))
            target_user = get_user_by_username(target_raw) if target_raw.startswith("@") else get_user(int(target_raw))
        except ValueError:
            pass

    if target_user and amount is not None:
        set_balance(target_user["user_id"], amount)
        t_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))
        await message.reply(f"🔧 Баланс игрока <b>{t_name}</b> установлен на <b>{amount:.2f} TON</b>", parse_mode="HTML")
    else:
        await message.reply("❌ Формат: <code>.setbal @юзер 1000</code>", parse_mode="HTML")


@dp.message(F.text.lower().startswith(".freeze") | F.text.lower().startswith(".unfreeze"))
async def handle_admin_freeze(message: Message):
    if (message.from_user.username or "").lower() != ADMIN_USERNAME.lower():
        return
    is_freeze = message.text.lower().startswith(".freeze")
    parts = message.text.strip().split()
    target_user = None

    if message.reply_to_message:
        target_user = get_user(message.reply_to_message.from_user.id)
    elif len(parts) >= 2:
        target_raw = parts[1]
        target_user = get_user_by_username(target_raw) if target_raw.startswith("@") else get_user(int(target_raw))

    if target_user:
        set_user_freeze(target_user["user_id"], 1 if is_freeze else 0)
        t_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))
        status_text = "заморожен ❄️" if is_freeze else "разморожен 🔓"
        await message.reply(f"👤 Аккаунт <b>{t_name}</b> {status_text}!", parse_mode="HTML")


# ================= КЭШ / АНКЭШ / РЕНТГЕН =================
@dp.message(F.text.lower().startswith("+рентген") | F.text.lower().startswith("-рентген"))
async def handle_manage_xray(message: Message):
    user_name = (message.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        return

    is_grant = message.text.lower().startswith("+рентген")
    parts = message.text.strip().split()
    target_user = None

    if message.reply_to_message:
        target_user = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )
    elif len(parts) >= 2:
        target_raw = parts[1]
        if target_raw.startswith("@"):
            target_user = get_user_by_username(target_raw)
        elif target_raw.isdigit():
            target_user = get_user(int(target_raw))

    if not target_user:
        await message.reply("❌ Укажите игрока ответом на сообщение или через <code>+рентген @username</code>", parse_mode="HTML")
        return

    set_user_xray(target_user["user_id"], 1 if is_grant else 0)
    display_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))

    if is_grant:
        await message.reply(f"👁✅ Игроку <b>{display_name}</b> выдан доступ к рентгену!", parse_mode="HTML")
    else:
        await message.reply(f"👁❌ У игрока <b>{display_name}</b> отозван доступ к рентгену.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("xray:"))
async def handle_xray_alert(callback: CallbackQuery):
    if not has_xray_access(callback.from_user.id, callback.from_user.username):
        await callback.answer("Это не твоя игра!", show_alert=True)
        return

    game_id = callback.data.split(":", 1)[1]
    if game_id not in games:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    game = games[game_id]
    grid_lines = []
    for r in range(5):
        row_str = ""
        for c in range(5):
            idx = r * 5 + c
            if idx in game["opened"]:
                row_str += "✅ "
            elif idx in game["mines_pos"]:
                row_str += "💣 "
            else:
                row_str += "💎 "
        grid_lines.append(row_str)

    board_text = "\n".join(grid_lines)
    await callback.answer(
        f"ID: {game['user_id']}\n"
        f"Баланс: {float(get_user(game['user_id']).get('balance') or 0.0):.2f} TON\n"
        f"Выбрано мин: {game['mines_count']} шт.\n\n"
        f"{board_text}",
        show_alert=True
    )


@dp.message(F.text.lower() == "литр")
async def handle_admin_xray(message: Message):
    if not has_xray_access(message.from_user.id, message.from_user.username):
        return

    if not message.reply_to_message:
        await message.reply("❌ Ответьте этой командой реплаем на сообщение с активной игрой.")
        return

    target_msg = message.reply_to_message
    game_id = f"{target_msg.chat.id}_{target_msg.message_id}"
    if game_id not in games:
        await message.reply("❌ В этом сообщении нет активной игры.")
        return

    game = games[game_id]
    grid_lines = []
    for r in range(5):
        row_str = ""
        for c in range(5):
            idx = r * 5 + c
            if idx in game["opened"]:
                row_str += "✅ "
            elif idx in game["mines_pos"]:
                row_str += "💣 "
            else:
                row_str += "💎 "
        grid_lines.append(row_str)

    board_text = "\n".join(grid_lines)
    await message.reply(
        f"👁 <b>Рентген игрового поля:</b>\n"
        f"👤 Игрок: <b>{html.escape(game['first_name'])}</b> | 🧨 Мин: <b>{game['mines_count']}</b>\n\n"
        f"{board_text}\n\n"
        "💣 — мина | 💎 — алмаз | ✅ — открыто",
        parse_mode="HTML"
    )


@dp.message(F.text.lower().in_(["б", "баланс"]))
async def handle_show_balance(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    if message.reply_to_message:
        target = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )
        name = get_name(target["user_id"], target.get("username"), message.reply_to_message.from_user.first_name)
    else:
        target = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        name = get_name(target["user_id"], target.get("username"), message.from_user.first_name)

    current_balance = float(target.get("balance") or 0.0)
    await message.reply(f"💳 Баланс игрока <b>{name}</b>: <code>{current_balance:.2f} TON</code> 💎", parse_mode="HTML")


@dp.message(F.text.lower().startswith("кэш"))
async def handle_admin_cash(message: Message):
    user_name = (message.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await message.reply("⛔ У вас нет прав на использование этой команды.")
        return

    parts = message.text.strip().split()
    target_user = None
    amount = None

    if message.reply_to_message and len(parts) >= 2:
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)
            target_user = get_user(
                message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username,
                message.reply_to_message.from_user.first_name
            )
        except ValueError:
            pass
    elif len(parts) >= 3:
        target_raw = parts[1]
        try:
            amount = round(float(parts[2].replace(",", ".")), 2)
            if target_raw.startswith("@"):
                target_user = get_user_by_username(target_raw)
            elif target_raw.isdigit():
                target_user = get_user(int(target_raw))
        except ValueError:
            pass
    elif len(parts) == 2:
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)
            target_user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        except ValueError:
            pass

    if amount is None or amount <= 0:
        await message.reply("❌ Формат: <code>кэш 500</code> или <code>кэш @username 500</code>", parse_mode="HTML")
        return

    if amount < MIN_DEPOSIT:
        await message.reply(f"❌ Минимальный депозит — <b>{MIN_DEPOSIT:.2f} TON</b>", parse_mode="HTML")
        return

    if not target_user:
        await message.reply("❌ Пользователь не найден в базе данных.")
        return

    add_deposit_amount(target_user["user_id"], amount)
    updated = get_user(target_user["user_id"])
    target_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))
    bal = float(updated.get("balance") or 0.0)

    await message.reply(
        f"⚡ <b>Успешная выдача кэша!</b>\n\n"
        f"👤 Получатель: <b>{target_name}</b>\n"
        f"💰 Начислено: <b>+{amount:.2f} TON</b>\n"
        f"💳 Новый баланс: <b>{bal:.2f} TON</b>",
        parse_mode="HTML"
    )


@dp.message(F.text.lower().startswith("анкэш"))
async def handle_admin_uncash(message: Message):
    user_name = (message.from_user.username or "").lower()
    if user_name != ADMIN_USERNAME.lower():
        await message.reply("⛔ У вас нет прав на использование этой команды.")
        return

    parts = message.text.strip().split()
    target_user = None
    is_full = False
    amount = 0.0

    if message.reply_to_message:
        target_user = get_user(
            message.reply_to_message.from_user.id,
            message.reply_to_message.from_user.username,
            message.reply_to_message.from_user.first_name
        )
        if len(parts) >= 2:
            if parts[1].lower() == "фулл":
                is_full = True
            else:
                try:
                    amount = round(float(parts[1].replace(",", ".")), 2)
                except ValueError:
                    pass
    elif len(parts) >= 3:
        target_raw = parts[1]
        if target_raw.startswith("@"):
            target_user = get_user_by_username(target_raw)
        elif target_raw.isdigit():
            target_user = get_user(int(target_raw))

        if parts[2].lower() == "фулл":
            is_full = True
        else:
            try:
                amount = round(float(parts[2].replace(",", ".")), 2)
            except ValueError:
                pass
    elif len(parts) == 2:
        target_user = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        if parts[1].lower() == "фулл":
            is_full = True
        else:
            try:
                amount = round(float(parts[1].replace(",", ".")), 2)
            except ValueError:
                pass

    if not target_user:
        await message.reply("❌ Пользователь не найден.")
        return

    target_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))
    old_balance = float(target_user.get("balance") or 0.0)

    if is_full:
        set_balance(target_user["user_id"], 0.0)
        await message.reply(
            f"🧹 <b>Баланс полностью аннулирован!</b>\n\n"
            f"👤 Игрок: <b>{target_name}</b>\n"
            f"📉 Списано: <b>-{old_balance:.2f} TON</b>\n"
            f"💳 Новый баланс: <b>0.00 TON</b>",
            parse_mode="HTML"
        )
        return

    if amount <= 0:
        await message.reply("❌ Формат: <code>анкэш фулл</code> или <code>анкэш 100</code>", parse_mode="HTML")
        return

    new_bal = max(0.0, round(old_balance - amount, 2))
    set_balance(target_user["user_id"], new_bal)
    await message.reply(
        f"📉 <b>Средства списаны!</b>\n\n"
        f"👤 Игрок: <b>{target_name}</b>\n"
        f"💸 Списано: <b>-{amount:.2f} TON</b>\n"
        f"💳 Новый баланс: <b>{new_bal:.2f} TON</b>",
        parse_mode="HTML"
    )


# ================= ТОП (/top) =================
def render_top_text(chat_id: int = None, is_admin: bool = False) -> str:
    with get_db() as conn:
        if chat_id and chat_id < 0:
            rows = conn.execute("""
                SELECT u.user_id, u.username, u.first_name, u.balance 
                FROM users u
                INNER JOIN chat_members cm ON u.user_id = cm.user_id
                WHERE cm.chat_id = ? AND COALESCE(u.balance, 0) <= ?
                ORDER BY COALESCE(u.balance, 0) DESC 
                LIMIT 10
            """, (chat_id, MAX_SAFE_VALUE)).fetchall()
            header = "🏆 <b>Топ-10 богачей этого чата по балансу TON:</b>\n\n"
        else:
            if not is_admin:
                return "ℹ️ Команда <code>/top</code> работает в группе для просмотра топа участников этого чата!"

            rows = conn.execute("""
                SELECT user_id, username, first_name, balance 
                FROM users 
                WHERE COALESCE(balance, 0) <= ?
                ORDER BY COALESCE(balance, 0) DESC 
                LIMIT 10
            """, (MAX_SAFE_VALUE,)).fetchall()
            header = "🏆 <b>Глобальный топ-10 богачей по балансу TON:</b>\n\n"

    if not rows:
        return header + "<i>В этом чате пока нет игроков!</i>"

    text = header
    for idx, row in enumerate(rows, start=1):
        nickname = get_name(row["user_id"], row["username"], row["first_name"])
        bal = float(row["balance"] or 0.0)
        text += f"{idx}. <b>{nickname}</b> — <code>{bal:.2f} TON</code>\n"
    return text


@dp.message(Command("top"))
async def cmd_top(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    is_admin = (message.from_user.username or "").lower() == ADMIN_USERNAME.lower()
    await message.answer(render_top_text(message.chat.id, is_admin), parse_mode="HTML")


@dp.callback_query(F.data == "show_top")
async def cb_show_top(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    register_chat_member(chat_id, callback.from_user.id)
    is_admin = (callback.from_user.username or "").lower() == ADMIN_USERNAME.lower()
    await callback.message.answer(render_top_text(chat_id, is_admin), parse_mode="HTML")
    await callback.answer()


# ================= БОНУС 5000 TON =================
def claim_bonus_logic(user_id: int, username: str = None, first_name: str = None):
    user = get_user(user_id, username, first_name)
    now = datetime.datetime.now()

    if user["last_bonus"]:
        last_bonus_time = datetime.datetime.fromisoformat(user["last_bonus"])
        cooldown = datetime.timedelta(days=1)
        if now - last_bonus_time < cooldown:
            remaining = cooldown - (now - last_bonus_time)
            hours, remainder = divmod(int(remaining.total_seconds()), 3600)
            minutes, _ = divmod(remainder, 60)
            return False, f"⏳ Рано! Бонус будет доступен через {hours} ч. {minutes} мин.", 0.0

    update_balance(user_id, BONUS_AMOUNT)
    with get_db() as conn:
        conn.execute("UPDATE users SET last_bonus = ? WHERE user_id = ?", (now.isoformat(), user_id))
        conn.commit()

    updated = get_user(user_id)
    return True, f"🎉 Вы забрали бонус: +{BONUS_AMOUNT:.2f} TON!", float(updated.get("balance") or 0.0)


@dp.message(F.text.lower().in_(["бонус", "/bonus"]))
async def handle_bonus_command(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    success, msg, new_balance = claim_bonus_logic(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not success:
        await message.reply(msg)
    else:
        user_name = get_name(message.from_user.id, message.from_user.username, message.from_user.first_name)
        await message.reply(
            f"🎁 <b>Бонус получен!</b>\n\n"
            f"👤 Игрок: <b>{user_name}</b>\n"
            f"💰 Начислено: <b>+{BONUS_AMOUNT:.2f} TON</b>\n"
            f"💳 Баланс: <b>{new_balance:.2f} TON</b>",
            parse_mode="HTML"
        )


@dp.callback_query(F.data == "get_bonus")
async def cb_get_bonus(callback: CallbackQuery):
    success, msg, new_balance = claim_bonus_logic(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    if not success:
        await callback.answer(msg, show_alert=True)
    else:
        await callback.answer(msg, show_alert=True)
        await callback.message.answer(
            f"🎁 <b>Бонус получен!</b>\n"
            f"Начислено: <b>+{BONUS_AMOUNT:.2f} TON</b>\n"
            f"Текущий баланс: <b>{new_balance:.2f} TON</b>",
            parse_mode="HTML"
        )


# ================= ПЕРЕВОДЫ =================
@dp.message(F.text.lower().startswith("п "))
async def handle_transfer(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    sender = get_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    sender_bal = float(sender.get("balance") or 0.0)
    parts = message.text.strip().split()
    target_user = None
    amount = 0.0

    if message.reply_to_message and len(parts) == 2:
        try:
            amount = round(float(parts[1].replace(",", ".")), 2)
            target_user = get_user(
                message.reply_to_message.from_user.id,
                message.reply_to_message.from_user.username,
                message.reply_to_message.from_user.first_name
            )
            register_chat_member(message.chat.id, message.reply_to_message.from_user.id)
        except ValueError:
            await message.reply("❌ Неверный формат суммы. Пример: <code>п 50</code>", parse_mode="HTML")
            return
    elif len(parts) >= 3:
        target_raw = parts[1]
        try:
            amount = round(float(parts[2].replace(",", ".")), 2)
        except ValueError:
            await message.reply("❌ Неверный формат суммы. Пример: <code>п @юзер 50</code>", parse_mode="HTML")
            return

        if target_raw.startswith("@"):
            target_user = get_user_by_username(target_raw)
        elif target_raw.isdigit():
            target_user = get_user(int(target_raw))
    else:
        await message.reply("❌ Формат: <code>п @username сумма</code> или ответом: <code>п сумма</code>", parse_mode="HTML")
        return

    if not target_user:
        await message.reply("❌ Получатель не найден в базе.")
        return

    if target_user["user_id"] == sender["user_id"]:
        await message.reply("❌ Нельзя переводить TON самому себе.")
        return

    if amount <= 0 or sender_bal < amount:
        await message.reply(f"❌ Не хватает TON! Твой баланс: <b>{sender_bal:.2f} TON</b>", parse_mode="HTML")
        return

    update_balance(sender["user_id"], -amount)
    update_balance(target_user["user_id"], amount)

    sender_name = get_name(sender["user_id"], message.from_user.username, message.from_user.first_name)
    recipient_name = get_name(target_user["user_id"], target_user.get("username"), target_user.get("first_name"))

    await message.reply(
        f"✅ <b>Перевод выполнен!</b>\n\n"
        f"От: <b>{sender_name}</b>\n"
        f"Кому: <b>{recipient_name}</b>\n"
        f"Сумма: <b>{amount:.2f} TON</b>",
        parse_mode="HTML"
    )


# ================= ПАСХАЛКИ =================
@dp.message(F.text.func(lambda text: text and any(phrase in text.lower().replace("  ", " ").strip() for phrase in ["lift syka", "лифт сука"])))
async def handle_lift_syka(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    file_path = os.path.join(ASSETS_DIR, "lift.png")
    if not os.path.exists(file_path):
        await message.reply(f"ЭТА ЖИ LIFT SYKA (файл не найден по пути: {file_path})")
        return

    try:
        photo = FSInputFile(file_path)
        await message.reply_photo(photo=photo, caption="ЭТА ЖИ LIFT SYKA")
    except Exception as e:
        await message.reply(f"ЭТА ЖИ LIFT SYKA (ошибка отправки: {e})")


GROSS_KEYWORDS = [
    "гросс", "гроссштур", "грос", "гросштур", "гросс штур", 
    "грос штур", "grossshtyr", "@grossshtyr"
]

@dp.message(F.text.func(lambda text: text and any(k in text.lower() for k in GROSS_KEYWORDS)))
async def handle_gross_easter_egg(message: Message):
    register_chat_member(message.chat.id, message.from_user.id)
    file_path = os.path.join(ASSETS_DIR, "gross.png")
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
    file_path = os.path.join(ASSETS_DIR, "sarmolaev.png")
    if not os.path.exists(file_path):
        return

    try:
        photo = FSInputFile(file_path)
        await message.reply_photo(photo=photo)
    except Exception:
        pass


@dp.callback_query(F.data == "ignore")
async def handle_ignore(callback: CallbackQuery):
    await callback.answer()


# ================= ЗАПУСК =================
async def main():
    init_db()
    print("Casino Bot (Mines, Joker, 21) успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())