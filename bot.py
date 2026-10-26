import asyncio
import httpx
import re
import json
import os
import sys

# Windows consoles often default to cp1251 and crash on emoji in print().
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Токен берётся из переменной окружения BOT_TOKEN или из файла token.txt.
# token.txt в .gitignore — реальный токен НИКОГДА не попадает в репозиторий.
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    try:
        with open("token.txt", encoding="utf-8") as _f:
            TOKEN = _f.read().strip()
    except OSError:
        pass
DATA_FILE = "users.json"
FREE_LIMIT = 3            # бесплатных поисков
RESULTS = 5              # сколько свободных ников показывать за поиск
TRAP_INTERVAL = 300      # как часто проверять ловушки (сек)
STICKER_SET = "AnimatedEmojies"   # премиум-анимированные стикеры
# Набор кастомных (премиум) эмодзи для декора — любой из t.me/addemoji/<имя>.
# Поменять: тапни любой премиум-эмодзи → «Скопировать ссылку на набор»
# → t.me/addemoji/ИМЯ → впиши ИМЯ сюда (или в переменную окружения CUSTOM_EMOJI_SET).
CUSTOM_EMOJI_SET = os.getenv("CUSTOM_EMOJI_SET", "NameColor")
EMOJI_CFG = "emoji_set.txt"   # сюда сохраняется выбранный через /setemoji набор
CUSTOM_EMOJI_FILE = "custom_emoji.json"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))   # твой Telegram ID (узнать через /profile)
MD = "Markdown"

# Премиум-стикеры: emoji -> file_id ; кастом-эмодзи: emoji -> custom_emoji_id
STICKERS: dict[str, str] = {}
CUSTOM_EMOJI: dict[str, str] = {}
CUSTOM_EMOJI_OK = True   # станет False, если у владельца бота нет Premium

# ─── Storage ──────────────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_user(uid: int) -> dict:
    data = load_data()
    key = str(uid)
    if key not in data:
        data[key] = {"attempts": 0, "premium": False, "traps": []}
        save_data(data)
    return data[key]

def update_user(uid: int, updates: dict):
    data = load_data()
    key = str(uid)
    if key not in data:
        data[key] = {"attempts": 0, "premium": False, "traps": []}
    data[key].update(updates)
    save_data(data)

# ─── Username validation ──────────────────────────────────────────────────────

# Telegram rules: 5-32 chars, a-z / 0-9 / _, must start with a letter,
# can't end with "_", no double "__".
RESERVED = {
    "admin", "administrator", "telegram", "support", "security", "settings",
    "contact", "service", "help", "info", "news", "root", "null", "bot",
    "channel", "group", "user", "me", "you", "home", "auth", "login", "api",
    "web", "app", "test", "start", "stop", "spam", "abuse", "everyone", "all",
}

USERNAME_RE = re.compile(r"[a-z][a-z0-9_]{3,30}[a-z0-9]")

def is_valid_username(name: str) -> bool:
    name = name.lower()
    return bool(USERNAME_RE.fullmatch(name)) and "__" not in name

def is_reserved(name: str) -> bool:
    return name.lower() in RESERVED

# ─── Checkers (t.me + Fragment) ───────────────────────────────────────────────

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}

async def check_username(username: str):
    """t.me check. True = свободен, False = занят/зарезервирован, None = ошибка.

    Любая публичная сущность (юзер/бот/канал/группа) рендерит блок
    `tgme_page_title`; у свободного ника его нет. Проверено эмпирически.
    """
    username = username.lstrip("@").lower()
    if not is_valid_username(username):
        return None
    if is_reserved(username):
        return False
    try:
        async with httpx.AsyncClient(timeout=10, headers=BROWSER) as client:
            resp = await client.get(f"https://t.me/{username}", follow_redirects=True)
            text = resp.text
    except Exception:
        return None
    taken = ("tgme_page_title" in text) or ("tgme_page_extra" in text)
    return not taken


async def check_fragment(username: str):
    """Fragment-статус ника. Возвращает (status, label, price).

    status: 'free'  — ника нет на Fragment (реально свободен),
            'sale'  — продаётся / на аукционе,
            'sold'  — продан кому-то,
            'taken' — занят владельцем через Fragment,
            'unknown' — не удалось определить.
    Сигнатуры проверены эмпирически на fragment.com.
    """
    username = username.lstrip("@").lower()
    try:
        async with httpx.AsyncClient(timeout=12, headers=BROWSER) as client:
            resp = await client.get(f"https://fragment.com/username/{username}",
                                    follow_redirects=True)
            text = resp.text
    except Exception:
        return ("unknown", None, None)

    m_title = re.search(r"<title>(.*?)</title>", text)
    title = (m_title.group(1) if m_title else "").lower()
    if username not in title:
        # на странице нет имени в <title> => ника нет в системе Fragment
        return ("free", None, None)

    m = re.search(r'tm-status-(avail|taken|unavail)[^"]*">([^<]+)</span>', text)
    m_price = re.search(r'icon-before[^>]*>\s*([\d,]+)', text)
    price = m_price.group(1) if m_price else None
    if not m:
        return ("unknown", None, price)
    cls, label = m.group(1), m.group(2).strip()
    status = {"avail": "sale", "taken": "taken", "unavail": "sold"}.get(cls, "unknown")
    return (status, label, price)


async def full_check(name: str) -> dict:
    """Комбинированная проверка t.me + Fragment.

    verdict: free | taken | frag_taken | sold | sale | reserved | invalid | error
    """
    name = name.lstrip("@").lower()
    if not is_valid_username(name):
        return {"verdict": "invalid"}
    if is_reserved(name):
        return {"verdict": "reserved"}
    tme = await check_username(name)
    if tme is None:
        return {"verdict": "error"}
    if tme is False:
        return {"verdict": "taken"}
    # t.me свободен — уточняем по Fragment
    fstatus, flabel, fprice = await check_fragment(name)
    verdict = {
        "free": "free",
        "sale": "sale",
        "sold": "sold",
        "taken": "frag_taken",
    }.get(fstatus, "free")  # unknown -> доверяем t.me (free)
    return {"verdict": verdict, "frag": fstatus, "frag_label": flabel, "price": fprice}

# ─── Rating ───────────────────────────────────────────────────────────────────

def rate_username(username: str) -> int:
    u = username.lower()
    s, n = 0, len(u)
    if n <= 4: s += 5
    elif n == 5: s += 4
    elif n == 6: s += 3
    elif n <= 8: s += 2
    else: s += 1
    if u.isalpha(): s += 3
    elif u.isalnum(): s += 1
    if len(set(u)) == len(u): s += 1            # все символы разные
    if any(ch in "qwxzjkv" for ch in u): s += 1  # редкие буквы
    if re.search(r"(.)\1{2,}", u): s -= 1        # 3+ одинаковых подряд
    return max(1, min(10, s))

def stars(score: int) -> str:
    return "⭐" * max(1, score // 2) + f" {score}/10"

# ─── Generators ───────────────────────────────────────────────────────────────

def gen_candidates(length: int = 6, count: int = 60) -> list:
    """Произносимые ники (согласная/гласная) — валидные и не зарезервированные."""
    import random
    vowels, consonants = "aeiou", "bcdfghjklmnpqrstvwxyz"
    results, seen, tries = [], set(), 0
    while len(results) < count and tries < 4000:
        tries += 1
        start_vowel = random.random() < 0.3
        name = ""
        for i in range(length):
            use_vowel = (i % 2 == 0) if start_vowel else (i % 2 == 1)
            name += random.choice(vowels if use_vowel else consonants)
        if name in seen:
            continue
        seen.add(name)
        if is_valid_username(name) and not is_reserved(name):
            results.append(name)
    return results

def gen_rare(length: int = 5, count: int = 100) -> list:
    """Редкие «некрасивые» ники: кластеры согласных + редкие буквы (x,z,q,j,k,w,v).

    Именно такие Fragment НЕ скупает, поэтому короткие (в т.ч. 5-буквенные)
    ники этого стиля часто реально свободны — как @xznow. Проверено эмпирически.
    """
    import random
    rare = "xzqjkwv"
    cons = "bcdfghjklmnpqrstvwxz"
    vowels = "aeiou"
    patterns = ["CCVCC", "CCCVC", "CVCCC", "CCVCV", "VCCVC", "CCVVC", "CVCVC"]
    results, seen, tries = [], set(), 0
    while len(results) < count and tries < 8000:
        tries += 1
        pat = random.choice(patterns)
        if len(pat) < length:                 # дотягиваем шаблон до нужной длины
            pat = (pat + "CVCVCV")[:length]
        pat = pat[:length]
        name = ""
        for c in pat:
            if c == "C":
                name += random.choice(rare) if random.random() < 0.45 else random.choice(cons)
            else:
                name += random.choice(vowels)
        if name in seen:
            continue
        seen.add(name)
        if is_valid_username(name) and not is_reserved(name):
            results.append(name)
    return results

def gen_from_mask(mask: str, count: int = 80) -> list:
    """Заполняет '?' случайными буквами. a?b?c -> aXbYc."""
    import random, string
    results, seen, tries = [], set(), 0
    while len(results) < count and tries < 3000:
        tries += 1
        name = "".join(random.choice(string.ascii_lowercase) if ch == "?" else ch
                        for ch in mask)
        if name in seen:
            continue
        seen.add(name)
        if is_valid_username(name) and not is_reserved(name):
            results.append(name)
    return results

# ─── Stickers ─────────────────────────────────────────────────────────────────

async def load_stickers(app):
    global STICKERS
    try:
        sset = await app.bot.get_sticker_set(STICKER_SET)
        STICKERS = {s.emoji: s.file_id for s in sset.stickers if s.emoji}
        print(f"🎨 Стикеров загружено: {len(STICKERS)}", flush=True)
    except Exception as e:
        print(f"⚠️ Стикеры не загружены: {e}", flush=True)

async def sticker(message, emoji: str):
    fid = STICKERS.get(emoji)
    if not fid:
        return
    try:
        await message.reply_sticker(fid)
    except Exception:
        pass


def load_saved_custom_emoji() -> dict[str, str]:
    if not os.path.exists(CUSTOM_EMOJI_FILE):
        return {}
    try:
        with open(CUSTOM_EMOJI_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except Exception:
        pass
    return {}


def save_custom_emoji(data: dict[str, str]) -> None:
    with open(CUSTOM_EMOJI_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def load_custom_emoji(app):
    """Загружает кастом (премиум) эмодзи из набора CUSTOM_EMOJI_SET."""
    global CUSTOM_EMOJI, CUSTOM_EMOJI_SET
    saved_emoji = load_saved_custom_emoji()
    try:
        if os.path.exists(EMOJI_CFG):
            saved = open(EMOJI_CFG, encoding="utf-8").read().strip()
            if saved:
                CUSTOM_EMOJI_SET = saved
    except Exception:
        pass
    try:
        cset = await app.bot.get_sticker_set(CUSTOM_EMOJI_SET)
        if cset.sticker_type != "custom_emoji":
            CUSTOM_EMOJI = saved_emoji
            print(f"⚠️ Набор {CUSTOM_EMOJI_SET} не custom_emoji — декор выключен.", flush=True)
            if CUSTOM_EMOJI:
                print(f"✨ Загружено сохранённых эмодзи: {len(CUSTOM_EMOJI)}", flush=True)
            return
        m = {}
        for s in cset.stickers:
            if s.emoji and s.custom_emoji_id and s.emoji not in m:
                m[s.emoji] = s.custom_emoji_id
        m.update(saved_emoji)
        CUSTOM_EMOJI = m
        print(f"✨ Кастом-эмодзи загружено: {len(CUSTOM_EMOJI)} ({CUSTOM_EMOJI_SET})", flush=True)
    except Exception as e:
        CUSTOM_EMOJI = saved_emoji
        print(f"⚠️ Кастом-эмодзи не загружены: {e}", flush=True)
        if CUSTOM_EMOJI:
            print(f"✨ Загружено сохранённых эмодзи: {len(CUSTOM_EMOJI)}", flush=True)


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _utf16_slice(s: str, offset: int, length: int) -> str:
    raw = s.encode("utf-16-le")
    start = offset * 2
    end = start + length * 2
    return raw[start:end].decode("utf-16-le")


def decorate(text: str):
    """Помечает эмодзи из набора как премиум-эмодзи.

    Возвращает (text, entities|None). ВНИМАНИЕ: кастом-эмодзи нельзя совмещать
    с Markdown в одном сообщении — поэтому такой текст шлётся без разметки.
    """
    if not CUSTOM_EMOJI:
        return text, None
    spans = []
    for emo, cid in CUSTOM_EMOJI.items():
        start = 0
        while True:
            idx = text.find(emo, start)
            if idx == -1:
                break
            spans.append((idx, idx + len(emo), cid))
            start = idx + len(emo)
    if not spans:
        return text, None
    spans.sort()
    entities, last_end = [], -1
    for s, e, cid in spans:
        if s < last_end:           # пропускаем пересечения
            continue
        entities.append(MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=_utf16_len(text[:s]),
            length=_utf16_len(text[s:e]),
            custom_emoji_id=cid))
        last_end = e
    return text, entities


def render(template: str):
    """Мини-рендерер: *жирный*, `моно`, и кастом-эмодзи -> (text, entities).
    '_' остаётся обычным символом (чтобы не ломать ники/ссылки с подчёркиванием).
    Обходит запрет Telegram «entities вместо parse_mode»: собираем всё руками.
    """
    parts, entities, out = [], [], 0
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        if ch in ("*", "`"):
            close = template.find(ch, i + 1)
            if close != -1:
                inner = template[i + 1:close]
                ln = _utf16_len(inner)
                if ln:
                    entities.append(MessageEntity(
                        type=(MessageEntity.BOLD if ch == "*" else MessageEntity.CODE),
                        offset=out, length=ln))
                parts.append(inner)
                out += ln
                i = close + 1
                continue
        parts.append(ch)
        out += _utf16_len(ch)
        i += 1
    text = "".join(parts)
    for e in (decorate(text)[1] or []):
        entities.append(e)
    entities.sort(key=lambda e: e.offset)
    return text, entities


async def _send_render(send_func, template, **kwargs):
    """Шлёт/редактирует текст с премиум-эмодзи + форматированием (*жирный*, `моно`).
    Если у владельца бота нет Premium, кастом-эмодзи отклоняются — тогда тот же
    текст уходит с обычным форматированием (запоминаем, чтобы не дёргать зря)."""
    global CUSTOM_EMOJI_OK
    kwargs.pop("parse_mode", None)
    text, entities = render(template)
    has_ce = any(e.type == MessageEntity.CUSTOM_EMOJI for e in entities)
    if has_ce and CUSTOM_EMOJI_OK:
        try:
            return await send_func(text, entities=entities, **kwargs)
        except Exception:
            CUSTOM_EMOJI_OK = False
    safe = [e for e in entities if e.type != MessageEntity.CUSTOM_EMOJI]
    return await send_func(text, entities=(safe or None), **kwargs)


async def reply_render(message, template, **kwargs):
    return await _send_render(message.reply_text, template, **kwargs)


async def edit_render(message, template, **kwargs):
    return await _send_render(message.edit_text, template, **kwargs)


async def reply_deco(message, text: str, **kwargs):
    """Совместимость: декорированный ответ (теперь через render)."""
    return await reply_render(message, text, **kwargs)

# ─── UI helpers ───────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Найти свободные ники", callback_data="fl:6")],
        [InlineKeyboardButton("5️⃣ Свободные 5-букв 💎", callback_data="r5"),
         InlineKeyboardButton("⭐ По рейтингу 💎", callback_data="m:top")],
        [InlineKeyboardButton("🎭 По маске 💎", callback_data="m:mask"),
         InlineKeyboardButton("🪤 Ловушки 💎", callback_data="m:traps")],
        [InlineKeyboardButton("💎 Premium", callback_data="m:premium"),
         InlineKeyboardButton("📊 Профиль", callback_data="m:profile")],
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="m:menu")]])

def again_kb(cb: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Ещё", callback_data=cb),
        InlineKeyboardButton("⬅️ Меню", callback_data="m:menu"),
    ]])

def status_line(user: dict) -> str:
    if user["premium"]:
        return "💎 *Статус:* Premium ♾"
    left = max(0, FREE_LIMIT - user["attempts"])
    return f"🎯 *Попытки:* {left}/{FREE_LIMIT}"

def menu_text(user: dict) -> str:
    return (
        "⚡️ *SenixSearch*\n"
        "поиск свободных редких юзернеймов\n"
        "━━━━━━━━━━━━━━━━━\n\n"
        f"{status_line(user)}\n\n"
        "👇 Выбери действие или просто отправь любой ник для проверки."
    )

def verdict_card(name: str, res: dict) -> str:
    score = rate_username(name)
    v = res["verdict"]
    price = res.get("price") or "—"
    if v == "free":
        head = f"✅ `@{name}` — *СВОБОДЕН!*"
        extra = "🎉 Можно занять прямо в настройках Telegram!"
    elif v == "taken":
        head = f"❌ `@{name}` — занят"
        extra = "Используется как публичный юзернейм."
    elif v == "frag_taken":
        head = f"🔒 `@{name}` — занят (Fragment)"
        extra = "Принадлежит владельцу через Fragment."
    elif v == "sold":
        head = f"💰 `@{name}` — продан на Fragment"
        extra = f"Цена продажи: {price} TON\nfragment.com/username/{name}"
    elif v == "sale":
        head = f"🏷 `@{name}` — продаётся на Fragment"
        extra = (f"Мин. ставка: {price} TON\n"
                 f"Бесплатно занять нельзя, только купить:\n"
                 f"fragment.com/username/{name}")
    elif v == "reserved":
        head = f"🚫 `@{name}` — зарезервирован Telegram"
        extra = "Системный ник, занять нельзя."
    else:
        head = f"⚠️ `@{name}` — не удалось проверить"
        extra = "Попробуй позже."
    return f"{head}\n\n⭐ Рейтинг: {stars(score)}\n📏 Длина: {len(name)}\n{extra}"

VERDICT_STICKER = {
    "free": "🎉", "taken": "😭", "frag_taken": "😭",
    "sold": "🤑", "sale": "🤑", "reserved": "💀",
}

# ─── Premium offer ────────────────────────────────────────────────────────────

async def send_premium(message, locked: bool = False):
    head = "⛔️ *Лимит бесплатных попыток исчерпан!*\n\n" if locked else ""
    text = head + (
        "💎 *SenixSearch Premium*\n"
        "━━━━━━━━━━━━━━━\n"
        "🔓 Открывает:\n\n"
        "5️⃣ Поиск свободных редких 5-буквенных ников\n"
        "🎭 Фильтр по маске (a?b?c → любые буквы)\n"
        "⭐ Поиск по рейтингу (7/10–10/10)\n"
        "🪤 Ловушка — уведомление, когда ник освободится\n"
        "♾ Безлимитный поиск без ограничений"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Активировать (тест — бесплатно)", callback_data="m:activate")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="m:menu")],
    ])
    await reply_render(message, text, reply_markup=kb)
    await sticker(message, "💎")

# ─── Core search ──────────────────────────────────────────────────────────────

async def run_search(message, user, uid, *, length=6, mask=None,
                     min_rating=0, rare=False, label="свободные ники", repeat_cb="fl:6"):
    if not user["premium"] and user["attempts"] >= FREE_LIMIT:
        await send_premium(message, locked=True)
        return
    if mask:
        cands = gen_from_mask(mask, 80)
    else:
        cands = gen_rare(length, 120) if rare else gen_candidates(length=length, count=80)
        if min_rating:
            cands = [c for c in cands if rate_username(c) >= min_rating]
    if not cands:
        await message.reply_text("😔 Не удалось собрать кандидатов. Попробуй другую маску.")
        return

    status = await reply_render(
        message, f"🔍 Ищу: *{label}*\nПроверяю t.me и Fragment…")
    found = []
    for c in cands:
        if len(found) >= RESULTS:
            break
        res = await full_check(c)
        if res["verdict"] == "free":
            found.append(c)
        await asyncio.sleep(0.15)

    if not user["premium"]:
        update_user(uid, {"attempts": user["attempts"] + 1})
    left = "♾" if user["premium"] else max(0, FREE_LIMIT - get_user(uid)["attempts"])

    if not found:
        searched_len = len(mask) if mask else length
        hint = ""
        if searched_len <= 5:
            hint = ("\n\n⚠️ 5-буквенные ники Telegram отдал под Fragment —\n"
                    "бесплатно занять нельзя. Открой «5-букв на Fragment».")
        await edit_render(
            status,
            f"😔 Свободных не нашёл в этой партии.\nЖми «Ещё» 🔄{hint}\n\n🎯 Осталось: {left}",
            reply_markup=again_kb(repeat_cb))
        await sticker(message, "😭")
        return

    found.sort(key=rate_username, reverse=True)
    lines = [f"✨ *Найдено свободных — {label}:*", ""]
    for name in found:
        lines.append(f"✅ `@{name}`   ⭐ {rate_username(name)}/10")
    lines += ["", "📲 Занять: Telegram → Настройки → Имя пользователя",
              "☝️ Нажми на ник, чтобы скопировать.",
              f"🎯 Осталось попыток: {left}"]
    await edit_render(status, "\n".join(lines), reply_markup=again_kb(repeat_cb))
    await sticker(message, "🎉")


async def run_fragment_search(message, user, uid, length=5):
    """Premium: редкие N-буквенные ники, доступные для ПОКУПКИ на Fragment.

    Короткие (особенно 5-буквенные) ники Telegram больше не даёт занимать
    бесплатно — они выставлены/проданы на Fragment. Показываем те, что
    можно купить, с минимальной ставкой.
    """
    if not user["premium"]:
        await send_premium(message)
        return
    status = await reply_render(
        message, f"💎 Ищу редкие {length}-буквенные на Fragment…\nпроверяю аукционы")
    cands = gen_candidates(length=length, count=160)
    found = []  # (name, price)
    for c in cands:
        if len(found) >= RESULTS:
            break
        st, _label, price = await check_fragment(c)
        if st == "sale":
            found.append((c, price))
        await asyncio.sleep(0.12)
    if not found:
        await edit_render(
            status,
            "😔 Сейчас не нашёл выставленных на продажу. Жми «Ещё» 🔄",
            reply_markup=again_kb(f"frag:{length}"))
        return
    lines = [f"💎 *Редкие {length}-буквенные на Fragment:*", "",
             "бесплатно занять нельзя — только купить:", ""]
    for name, price in found:
        lines.append(f"🏷 `@{name}` — от {price or '?'} TON\n    fragment.com/username/{name}")
    lines += ["", "⚠️ Все 5-буквенные ники Telegram перевёл на Fragment."]
    await edit_render(status, "\n".join(lines),
                      reply_markup=again_kb(f"frag:{length}"))
    await sticker(message, "🤑")


async def do_search_username(message, user, uid, raw: str):
    name = raw.lstrip("@").lower()
    if not is_valid_username(name):
        await message.reply_text(
            "❌ Неверный ник.\nПравила: 5–32 символа, a–z 0–9 _, "
            "начинается с буквы, не заканчивается на «_».")
        return
    if is_reserved(name):
        await reply_render(
            message, f"🚫 `@{name}` зарезервирован Telegram — занять нельзя.")
        await sticker(message, "💀")
        return
    if not user["premium"] and user["attempts"] >= FREE_LIMIT:
        await send_premium(message, locked=True)
        return

    status = await reply_render(
        message, f"🔍 Проверяю `@{name}`…\nt.me + Fragment")
    res = await full_check(name)
    if res["verdict"] == "error":
        await status.edit_text("⚠️ Не удалось проверить. Попробуй позже.")
        return

    if not user["premium"]:
        update_user(uid, {"attempts": user["attempts"] + 1})
    left = "♾" if user["premium"] else max(0, FREE_LIMIT - get_user(uid)["attempts"])

    v = res["verdict"]
    rows = []
    if v in ("taken", "frag_taken", "sold", "sale"):
        rows.append([InlineKeyboardButton("🪤 Ловушка на этот ник", callback_data=f"trap:{name}")])
    rows.append([InlineKeyboardButton("⬅️ Меню", callback_data="m:menu")])
    await edit_render(
        status,
        verdict_card(name, res) + f"\n\n🎯 Осталось попыток: {left}",
        reply_markup=InlineKeyboardMarkup(rows))
    emoji = VERDICT_STICKER.get(v)
    if emoji:
        await sticker(message, emoji)

# ─── Mask / Trap input ────────────────────────────────────────────────────────

async def handle_mask(message, user, uid, raw: str):
    if not user["premium"]:
        await send_premium(message)
        return
    mask = raw.lstrip("@").lower()
    if not re.fullmatch(r"[a-z?]{5,12}", mask):
        await reply_render(
            message, "Маска: буквы a–z и `?`, 5–12 символов.\nПример: `a?b?c`")
        return
    if "?" not in mask:
        await do_search_username(message, user, uid, mask)
        return
    await run_search(message, user, uid, mask=mask,
                     label=f"маска {mask}", repeat_cb=f"rm:{mask}")


async def handle_trap_add(message, user, uid, raw: str):
    if not user["premium"]:
        await send_premium(message)
        return
    name = raw.lstrip("@").lower()
    if not is_valid_username(name):
        await message.reply_text("❌ Неверный ник для ловушки.")
        return
    traps = user.get("traps", [])
    if name in traps:
        await reply_render(message, f"🪤 `@{name}` уже в ловушках!")
        return
    if len(traps) >= 20:
        await message.reply_text("Максимум 20 ловушек.")
        return
    traps.append(name)
    update_user(uid, {"traps": traps})
    await reply_render(
        message,
        f"🪤 Ловушка на `@{name}` поставлена!\n"
        "Проверяю каждые 5 минут и пришлю уведомление, как только освободится 🔔")
    await sticker(message, "🔥")


async def show_traps(message, user, uid):
    traps = user.get("traps", [])
    body = "\n".join(f"🪤 `@{t}`" for t in traps) if traps else "пока пусто"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить ловушку", callback_data="m:addtrap")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="m:menu")],
    ])
    await reply_render(
        message,
        "🪤 *Твои ловушки*\n━━━━━━━━━━\n"
        f"{body}\n\n"
        "Проверяю их каждые 5 минут и уведомлю, как только ник освободится 🔔",
        reply_markup=kb)


async def show_profile(message, user, uid):
    st = "💎 Premium ♾" if user["premium"] else f"🎯 {max(0, FREE_LIMIT - user['attempts'])}/{FREE_LIMIT} попыток"
    await reply_render(
        message,
        "📊 *Профиль*\n━━━━━━━━━━\n"
        f"🆔 ID: `{uid}`\n"
        f"Статус: {st}\n"
        f"🪤 Активных ловушек: {len(user.get('traps', []))}",
        reply_markup=back_kb())

# ─── Trap background watcher ──────────────────────────────────────────────────

async def trap_watcher(app):
    while True:
        await asyncio.sleep(TRAP_INTERVAL)
        data = load_data()
        changed = False
        for uid, udata in data.items():
            remaining = []
            for t in udata.get("traps", []):
                res = await full_check(t)
                if res.get("verdict") == "free":
                    changed = True
                    try:
                        await app.bot.send_message(
                            int(uid),
                            f"🎉 *Ловушка сработала!*\n\n"
                            f"`@{t}` теперь *СВОБОДЕН!*\n"
                            f"Занимай скорее → t.me/{t}",
                            parse_mode=MD)
                        fid = STICKERS.get("🎉")
                        if fid:
                            await app.bot.send_sticker(int(uid), fid)
                    except Exception:
                        pass
                else:
                    remaining.append(t)
                await asyncio.sleep(0.3)
            udata["traps"] = remaining
        if changed:
            save_data(data)

# ─── Command handlers ─────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    await sticker(update.message, "👋")
    await reply_render(update.message, menu_text(user), reply_markup=main_menu_kb())


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not ctx.args:
        await update.message.reply_text("Пример: /search durov")
        return
    await do_search_username(update.message, user, update.effective_user.id, ctx.args[0])


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    length = 6
    if ctx.args:
        try:
            length = int(ctx.args[0])
            assert 5 <= length <= 12
        except Exception:
            await update.message.reply_text("Длина 5–12. Пример: /find 6")
            return
    if length <= 5:
        if not user["premium"]:
            await send_premium(update.message)
            return
        await run_search(update.message, user, uid, length=length, rare=True,
                         label=f"свободные {length}-буквенные", repeat_cb=f"fl:{length}")
        return
    await run_search(update.message, user, uid, length=length,
                     label=f"ники ({length} букв)", repeat_cb=f"fl:{length}")


async def cmd_mask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not ctx.args:
        await update.message.reply_text("Пример: /mask a?b?c  (`?` = любая буква)", parse_mode=MD)
        return
    await handle_mask(update.message, user, uid, ctx.args[0])


async def cmd_trap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    if not ctx.args:
        await update.message.reply_text("Пример: /trap durov")
        return
    await handle_trap_add(update.message, user, uid, ctx.args[0])


async def cmd_traps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await show_traps(update.message, get_user(uid), uid)


async def cmd_premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user["premium"]:
        await update.message.reply_text("💎 У тебя уже Premium!", reply_markup=back_kb())
        return
    await send_premium(update.message)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await show_profile(update.message, get_user(uid), uid)


async def cmd_decor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Демо премиум-эмодзи для декора (нужен Premium у владельца бота)."""
    if not CUSTOM_EMOJI:
        await update.message.reply_text(
            "Кастом-эмодзи не загружены.\n"
            f"Текущий набор: {CUSTOM_EMOJI_SET}\n"
            "Укажи свой премиум-набор в CUSTOM_EMOJI_SET (t.me/addemoji/ИМЯ).")
        return
    sample = "".join(list(CUSTOM_EMOJI.keys())[:6])
    await reply_deco(
        update.message,
        f"{sample}\n"
        "Премиум-эмодзи для декора работают! ✨\n"
        f"Набор: {CUSTOM_EMOJI_SET} · {len(CUSTOM_EMOJI)} эмодзи\n"
        f"{sample}")


async def cmd_setemoji(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Подключить свой набор кастом-эмодзи: /setemoji <имя или ссылка addemoji>."""
    global CUSTOM_EMOJI_SET, CUSTOM_EMOJI
    uid = update.effective_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        await update.message.reply_text("⛔️ Менять набор может только владелец бота.")
        return
    if not ctx.args:
        await update.message.reply_text(
            "🎨 Подключить свой набор премиум-эмодзи:\n"
            "`/setemoji ИМЯ`\n\n"
            "ИМЯ — из ссылки `t.me/addemoji/ИМЯ` (или вставь ссылку целиком).\n"
            f"Сейчас активен: *{CUSTOM_EMOJI_SET}* ({len(CUSTOM_EMOJI)} эмодзи)",
            parse_mode=MD)
        return
    name = ctx.args[0].strip()
    if "addemoji/" in name:                       # принимаем и полную ссылку
        name = name.split("addemoji/")[-1].strip("/")
    try:
        cset = await ctx.bot.get_sticker_set(name)
    except Exception:
        await update.message.reply_text(f"❌ Набор «{name}» не найден. Проверь имя.")
        return
    if cset.sticker_type != "custom_emoji":
        await update.message.reply_text(
            f"❌ «{name}» — это не набор кастом-эмодзи.\n"
            "Нужен набор из ссылки t.me/addemoji/… (не addstickers).")
        return
    CUSTOM_EMOJI = {s.emoji: s.custom_emoji_id
                    for s in cset.stickers if s.emoji and s.custom_emoji_id}
    CUSTOM_EMOJI_SET = name
    try:
        with open(EMOJI_CFG, "w", encoding="utf-8") as f:
            f.write(name)
    except Exception:
        pass
    sample = "".join(list(CUSTOM_EMOJI.keys())[:6])
    await reply_deco(
        update.message,
        f"✅ Набор подключён: {name}\n"
        f"Эмодзи: {len(CUSTOM_EMOJI)}\n{sample}\n"
        "Проверь декор: /decor")


def extract_custom_emoji_from_message(message) -> dict[str, str]:
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    found: dict[str, str] = {}
    for entity in entities:
        if entity.type != MessageEntity.CUSTOM_EMOJI or not entity.custom_emoji_id:
            continue
        emoji = _utf16_slice(text, entity.offset, entity.length)
        if emoji:
            found[emoji] = entity.custom_emoji_id
    return found


async def cmd_addemoji(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохранить отдельные Telegram Premium emoji из сообщения."""
    global CUSTOM_EMOJI
    uid = update.effective_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        await update.message.reply_text("⛔️ Добавлять premium emoji может только владелец бота.")
        return

    source = update.message.reply_to_message or update.message
    found = extract_custom_emoji_from_message(source)

    if not found:
        await update.message.reply_text(
            "Отправь premium emoji вместе с командой `/addemoji` "
            "или ответь командой `/addemoji` на сообщение с premium emoji.",
            parse_mode=MD)
        return

    saved = load_saved_custom_emoji()
    saved.update(found)
    save_custom_emoji(saved)
    CUSTOM_EMOJI.update(found)

    sample = "".join(found.keys())
    await reply_deco(
        update.message,
        f"✅ Добавлено premium emoji: {len(found)}\n{sample}\n"
        "Проверь декор: /decor")


async def cmd_rating(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply_render(
        update.message,
        "📊 *Как считается рейтинг ника:*\n\n"
        "⭐ Длина: чем короче — тем выше (5 букв = топ)\n"
        "⭐ Только буквы (без цифр и _)\n"
        "⭐ Все символы разные\n"
        "⭐ Редкие буквы (q, w, x, z, j, k, v)\n\n"
        "9–10 — супер-редкий · 7–8 — редкий · 5–6 — обычный\n\n"
        "💎 «Свободные 5-букв» ищет редкие 5-буквенные (кластеры согласных),\n"
        "которые Fragment не скупил — как @xznow.",
        reply_markup=back_kb())

# ─── Callback router ──────────────────────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    user = get_user(uid)
    data = q.data
    msg = q.message
    await q.answer()

    if data == "m:menu":
        await edit_render(msg, menu_text(user), reply_markup=main_menu_kb())
    elif data == "m:profile":
        await show_profile(msg, user, uid)
    elif data == "m:premium":
        await send_premium(msg)
    elif data == "m:activate":
        update_user(uid, {"premium": True})
        await reply_render(
            msg, "💎 *Premium активирован!*\nВсе функции разблокированы 🎉",
            reply_markup=main_menu_kb())
        await sticker(msg, "🥳")
    elif data.startswith("fl:"):
        length = int(data[3:])
        if length <= 5:
            if not user["premium"]:
                await send_premium(msg)
                return
            await run_search(msg, user, uid, length=length, rare=True,
                             label=f"свободные {length}-буквенные", repeat_cb=data)
            return
        await run_search(msg, user, uid, length=length,
                         label=f"ники ({length} букв)", repeat_cb=data)
    elif data == "r5" or data.startswith("frag:"):
        if not user["premium"]:
            await send_premium(msg)
            return
        await run_search(msg, user, uid, length=5, rare=True,
                         label="свободные 5-буквенные", repeat_cb="r5")
    elif data == "m:top":
        if not user["premium"]:
            await send_premium(msg)
            return
        await run_search(msg, user, uid, length=6, min_rating=7,
                         label="топ по рейтингу", repeat_cb="m:top")
    elif data == "m:mask":
        if not user["premium"]:
            await send_premium(msg)
            return
        ctx.user_data["await"] = "mask"
        await reply_render(msg, "🎭 Отправь маску, например `a?b?c`\n`?` = любая буква.")
    elif data == "m:traps":
        if not user["premium"]:
            await send_premium(msg)
            return
        await show_traps(msg, user, uid)
    elif data == "m:addtrap":
        if not user["premium"]:
            await send_premium(msg)
            return
        ctx.user_data["await"] = "trap"
        await msg.reply_text("🪤 Отправь ник, за которым следить (без @).")
    elif data.startswith("rm:"):
        if not user["premium"]:
            await send_premium(msg)
            return
        mask = data[3:]
        await run_search(msg, user, uid, mask=mask,
                         label=f"маска {mask}", repeat_cb=data)
    elif data.startswith("trap:"):
        name = data.split(":", 1)[1]
        if not user["premium"]:
            await send_premium(msg)
            return
        traps = user.get("traps", [])
        if name not in traps and len(traps) < 20:
            traps.append(name)
            update_user(uid, {"traps": traps})
        await reply_render(msg, f"🪤 Ловушка на `@{name}` поставлена! 🔔")
        await sticker(msg, "🔥")

# ─── Free text ────────────────────────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = get_user(uid)
    txt = update.message.text.strip()
    awaiting = ctx.user_data.pop("await", None)
    if awaiting == "mask":
        await handle_mask(update.message, user, uid, txt)
    elif awaiting == "trap":
        await handle_trap_add(update.message, user, uid, txt)
    elif is_valid_username(txt.lstrip("@")):
        await do_search_username(update.message, user, uid, txt)
    else:
        await update.message.reply_text(
            "🔎 Отправь ник (5–32 символа) для проверки или открой меню: /start")

# ─── Main ─────────────────────────────────────────────────────────────────────

async def _post_init(app: Application):
    await load_stickers(app)
    await load_custom_emoji(app)
    app.bot_data["_trap_task"] = asyncio.create_task(trap_watcher(app))
    print("🔔 Trap watcher запущен.", flush=True)


def main():
    if not TOKEN or ":" not in TOKEN:
        raise SystemExit("❌ BOT_TOKEN не задан. Получи токен у @BotFather.")
    # Python 3.12+ no longer auto-creates an event loop in the main thread,
    # but PTB 21.x's run_polling() calls asyncio.get_event_loop(). Provide one.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("mask", cmd_mask))
    app.add_handler(CommandHandler("trap", cmd_trap))
    app.add_handler(CommandHandler("traps", cmd_traps))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("rating", cmd_rating))
    app.add_handler(CommandHandler("decor", cmd_decor))
    app.add_handler(CommandHandler("setemoji", cmd_setemoji))
    app.add_handler(CommandHandler("addemoji", cmd_addemoji))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("⚡️ SenixSearch запущен! Polling... (Ctrl+C — стоп)", flush=True)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
import os
while True:
    try:
        import re
        import random
        import string
        from base64 import b64decode
        import requests
        import sys
        import time
        from datetime import datetime
        from requests import get
        from base64 import b64decode
        from time import sleep
        from random import choice, randint
        from fake_useragent import UserAgent
        from telethon import TelegramClient, sync, errors, types, functions
        from telethon.tl.functions.account import CheckUsernameRequest, UpdateUsernameRequest
        from telethon.tl.functions.channels import JoinChannelRequest
        break
    except ImportError as e:
        os.system(f'pip install {e.name}')
        os.system('cls' if os.name == 'nt' else 'clear')
        continue
class ChackUserName:
    def __init__(self,ses):
        self.Client=ses
        self.names=set()
        self.generate_username()
    def user_gen(self,pattern):
        fixed_digits = {}
        fixed_letters = {}
        result = []
        repeat_pattern = re.compile(r'(\w|\#|\*)\[(\d+)\]')
        def process_char(char):
            if char == 'q':
                if 'q' not in fixed_digits:
                    fixed_digits['q'] = str(random.randint(0, 9))
                return fixed_digits['q']
            elif char == '&':
                if '&' not in fixed_digits:
                    fixed_digits['&'] = str(random.randint(0, 9))
                return fixed_digits['&']
            elif char == '$':
                if '$' not in fixed_digits:
                    fixed_digits['$'] = str(random.randint(0, 9))
                return fixed_digits['$']
            elif char == '@':
                if '@' not in fixed_letters:
                    fixed_letters['@'] = random.choice(string.ascii_lowercase)
                return fixed_letters['@']
            elif char == '#':
                if '#' not in fixed_letters:
                    fixed_letters['#'] = random.choice(string.ascii_lowercase)
                return fixed_letters['#']
            elif char == '*':
                if '*' not in fixed_letters:
                    fixed_letters['*'] = random.choice(string.ascii_lowercase)
                return fixed_letters['*']
            else:
                return char        
        i = 0
        while i < len(pattern):
            match = repeat_pattern.match(pattern, i)
            if match:
                char = match.group(1)
                repeat_count = int(match.group(2))
                result.extend([process_char(char)] * repeat_count)
                i += len(match.group(0))
            else:
                result.append(process_char(pattern[i]))
                i += 1
        return ''.join(result)
    def generate_username(self):
        numb=0
        while True:
            user=self.user_gen(random.choice(["#*123","#*456","#*567","#*789","#*vip","#*boss","#*trx","##qqqq","##****","#*qqq","trx#*","vip#*","#*pay","#qpay","#8q88","#88q8","#q8888","#qq$$","##q88","svipq&$","##q88","kefuq&$","vipq&$","#888q","pc28#","#1234","#12345","#123456","#1234567","#123456789","#4321","#54321","#3210","#0123","#6789","#2345","#4567","#3456","#5678","xbhwq&","@#520","vipq&","xmwhq&","xmfk&q","xmhvvq&","@#168","vipq&","##***","@@520","xmkfq&","xmkfqq","usdtq&","xmkfq","usdtq","kefuq&","kefuq","usdt#","#usdt"

            ]))
            if user.lower() not in self.names:
                self.names.add(user.lower())
                try:
                    Fragment=self.Chack_UserName_Fragment(user)
                except:
                    self.names.discard(user)
                    continue
                numb += 1
                if Fragment == "taken":
                    print(f"-[{numb}] UserName is Taken [@{user}]")
                elif Fragment == "available":
                    print(f"-[{numb}] UserName is Sold [@{user}]")
                elif Fragment == "Unavailable":
                    print(f"-[{numb}] UserName is Unavailable [@{user}]")
                    self.Chack_UserName_TeleGram(user)
                elif Fragment == "unknown":
                    print(f"-[{numb}] UserName is unknown [@{user}]")
                    self.names.discard(user)
                else:print(f"-[{numb}] Error is [{Fragment}]")
    def Chack_UserName_TeleGram(self,user):
        try:
            tele = self.Client(CheckUsernameRequest(username=user))
            if tele:
                print(f"- UserName is Good (CheckUsernameRequest) [{user}]")
                self.save_username_to_channel(user)
                Fragment=self.Chack_UserName_Fragment(user)
                if Fragment == "taken":
                    st_claim = True
                else:
                    st_claim = False
                text = f"â€¢ New UserName,Claim : {st_claim} .\nâ€¢ UserName : @{user} ."
                self.Client.send_message("viip93",text)
                chack_flood = self.Chack_UserName_Flood(user)
                if chack_flood:
                    print(f"- UserName is Flood [@{user}]")
            else:
                print(f"- UserName is Bad (CheckUsernameRequest) [{user}] Taken.")
        except errors.rpcbaseerrors.BadRequestError:
            print(f"- UserName is Band [@{user}]")
            return
        except errors.FloodWaitError as timer:
            num = int(timer.seconds)
            print(f"- Error Account Flood (CheckUsernameRequest) Time [{num}]\n- UserName [{user}]\n")
            while num > 0:
                print(f"The flood will end after: [{num}]", end="\r")
                time.sleep(1)
                num -= 1
            self.names.discard(user)
            return
        except errors.UsernameInvalidError:
            print(f"- UserName is Invalid [@{user}]")
            return
        
                
    def Chack_UserName_Flood(self,user):
        all_chat = self.Client.get_dialogs()
        for chat in all_chat:
            if chat.name == "aز¯ل´چخ±ة´" and not chat.entity.username:
                self.Client(functions.channels.DeleteChannelRequest(channel=chat.entity))
                return True
        return False
                
    def save_username_to_channel(self,user):
        r = self.Client(functions.channels.CreateChannelRequest(
            title="ًں•¸ï¸ڈلژ¦â´¼ًں•¸ï¸ڈ",
            about="@xsxvxs",
            megagroup=False
        ))
        try:
            self.Client(functions.channels.UpdateUsernameRequest(channel=r.chats[0], username=user))
            messages = self.Client.get_messages(user, limit=1)
            if messages:self.Client.delete_messages(user, messages[0].id)
            video_message_id = random.choice([7,8,9,10,11,12,13,14,15,16,36,18,19,20,21,22,23,24,25,28,37,38,39,40,41,42,43,44,45,46,50,51,52,53,54,55,56,57,58,59,60,61,62])
            video_message = self.Client.get_messages('rmrmrmr', ids=video_message_id)
            if video_message and video_message.media:
                video_path = self.Client.download_media(video_message.media, file='VideoStar.mp4')
                time.sleep(1.5)
            self.Client.send_file(user, file='VideoStar.mp4', caption=(
                f'â€¢ Done Claim UserName â€¢ [@{user}] .\n'
                f'â€¢ Owner UserName : @xsxvxs @b_b8a @userfragmentsell .\n\n'))
            return
        except Exception as e:
            if "You're admin of too many public channels, make some channels private to change the username of this channel" in str(e):
                print(f"- Error (too many public channels) save_username_to_channel, UserName: [@{user}]\n Error : [{e}]")
                return
            elif "A wait" in str(e):
                time_flood=e.seconds
                print(f"- Error Account Flood (caused by UpdateUsernameRequest) Time [{time_flood}]\n- UserName [{user}]\n")
                while time_flood > 0:
                    print(f"The flood will end after: [{time_flood}]", end="\r")
                    time.sleep(1)
                    time_flood -= 1
                time.sleep(2.5)
                try:
                    self.Client(functions.channels.UpdateUsernameRequest(channel=r.chats[0], username=user))
                    return
                except Exception as e:
                    if "You're admin of too many public channels, make some channels private to change the username of this channel" in str(e):
                        print(f"- Error (too many public channels) save_username_to_channel, UserName: [@{user}]\n Error : [{e}]")
                        return
                    else:
                        print(f"- Error save_username_to_channel, UserName: [@{user}]\n Error : [{e}]")
                    return
            else:
                print(f"- Error save_username_to_channel, UserName: [@{user}]\n Error : [{e}]")
                return
                
                
    def Chack_UserName_Fragment(self,user):
        try:
            response = requests.get(f"https://fragment.com/username/{user}",timeout=5).text
            if '<span class="tm-section-header-status tm-status-taken">Taken</span>' in response:return "taken"
            elif '<span class="tm-section-header-status tm-status-unavail">Sold</span>' in response:return "available"
            elif '<div class="table-cell-status-thin thin-only tm-status-unavail">Unavailable</div>' in response:return "Unavailable"
            else:return "unknown"
        except Exception as e:return e
    @staticmethod
    def Get_Session():
        while True:
            phone = '+201064253446'#Your phone number 
            if phone == '':phone = input("> Enter Phone Number Telegram :")
            client = TelegramClient("SG_Client", b64decode("MjUzMjQ1ODE=").decode(), b64decode("MDhmZWVlNWVlYjZmYzBmMzFkNWYyZDIzYmIyYzMxZDA=").decode())
            try:client.start(phone=phone)
            except:continue
            os.system('cls' if os.name == 'nt' else 'clear')
            return client
if __name__ == "__main__":
    Client = ChackUserName.Get_Session()
    ChackUserName(Client)
        
