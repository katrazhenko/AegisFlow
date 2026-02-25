"""
main.py — User client: моніторинг груп + базова фільтрація.
Збирає повідомлення та складає в чергу для бота (bot.py).
"""

import json
import asyncio
import os
import sys
import re
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Optional
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# ──────────────────────────────────────────────────────────────
# Налаштування логування: logs/user_YYYY-MM-DD.log та logs/bot_YYYY-MM-DD.log
# ──────────────────────────────────────────────────────────────
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(exist_ok=True)

_log_format = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


class DailyFileHandler(logging.FileHandler):
    """Хендлер що автоматично створює новий файл при зміні дати."""

    def __init__(self, prefix: str, logs_dir: Path):
        self._prefix = prefix
        self._logs_dir = logs_dir
        self._current_date = ""
        filepath = self._get_filepath()
        super().__init__(filepath, encoding="utf-8")

    def _get_filepath(self) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        self._current_date = today
        return str(self._logs_dir / f"{self._prefix}_{today}.log")

    def emit(self, record):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._current_date:
            self.close()
            self.baseFilename = self._get_filepath()
            self.stream = self._open()
        super().emit(record)


def _setup_logger(name: str, prefix: str) -> logging.Logger:
    """Створює логер з виводом в консоль + файл logs/{prefix}_YYYY-MM-DD.log."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Консоль
    console = logging.StreamHandler()
    console.setFormatter(_log_format)
    logger.addHandler(console)

    # Файл з міткою дати
    file_h = DailyFileHandler(prefix, LOGS_DIR)
    file_h.setFormatter(_log_format)
    logger.addHandler(file_h)

    return logger


log = _setup_logger("monitor", "user")
_setup_logger("bot", "bot")

EFP = Path(".env")
def _load_env():
    """Завантажує .env якщо він є (без залежності від python-dotenv)."""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")
if not all([API_ID, API_HASH, PHONE, BOT_TOKEN, BOT_USERNAME]):
    log.error(
        "Задай TG_API_ID, TG_API_HASH, TG_PHONE, BOT_TOKEN, BOT_USERNAME у файлі .env або змінних оточення"
    )
    sys.exit(1)

CONFIG_DIR = Path("config")
CONFIG_DIR.mkdir(exist_ok=True)
CONFIG_FILE = CONFIG_DIR / "config.json"

# ──────────────────────────────────────────────────────────────
# Кешований конфіг + lock
# ──────────────────────────────────────────────────────────────
_config_cache: Optional[dict] = None
_config_lock = asyncio.Lock()


def load_config() -> dict:
    """Завжди читає з диска (синхронно). Використовуй усередині lock."""
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Атомарне збереження через тимчасовий файл."""
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    tmp.replace(CONFIG_FILE)


async def get_config() -> dict:
    """Повертає кешований конфіг; при першому виклику читає з диска."""
    global _config_cache
    async with _config_lock:
        if _config_cache is None:
            _config_cache = load_config()
        return dict(_config_cache)  # shallow copy


async def update_config(config: dict) -> None:
    """Зберігає конфіг та оновлює кеш."""
    global _config_cache
    async with _config_lock:
        save_config(config)
        _config_cache = config


def invalidate_config_cache() -> None:
    global _config_cache
    _config_cache = None


# ──────────────────────────────────────────────────────────────
# Черга пересилки (спільна між user та bot)
# ──────────────────────────────────────────────────────────────
pending_messages: asyncio.Queue = asyncio.Queue()

# ──────────────────────────────────────────────────────────────
# Telethon клієнти (сесії зберігаються в data/)
# ──────────────────────────────────────────────────────────────
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
user_client = TelegramClient(str(DATA_DIR / PHONE.replace("+", "")), API_ID, API_HASH)
bot_client = TelegramClient(str(DATA_DIR / BOT_USERNAME.replace("@", "")), API_ID, API_HASH)


# ──────────────────────────────────────────────────────────────
# Утиліти: очищення minus_words
# ──────────────────────────────────────────────────────────────
def clean_minus_words(minus_words: list[str], skip_words: list[str], keywords: list[str]) -> list[str]:
    """
    Видаляє зі списку мінус-слів ті слова, що є в skip_words або keywords.
    Повертає новий (очищений) список. НЕ мутує оригінал.
    """
    skip_lower = {w.lower() for w in skip_words}
    kw_lower = {w.lower() for w in keywords}
    forbidden = skip_lower | kw_lower

    result: list[str] = []
    seen: set[str] = set()

    for phrase in minus_words:
        words = phrase.lower().split()
        cleaned = [w for w in words if w not in forbidden]
        new_phrase = " ".join(cleaned).strip()
        if new_phrase and new_phrase not in seen:
            result.append(new_phrase)
            seen.add(new_phrase)

    return result


def has_minus_word(text: str, minus_words: list[str]) -> bool:
    """True якщо текст містить будь-яке мінус-слово."""
    text_lower = text.lower()
    for phrase in minus_words:
        if phrase.lower() in text_lower:
            return True
    return False


def find_keyword(text: str, keywords: list[str]) -> str | None:
    """
    Повертає перше знайдене ключове слово або None.
    """
    text_lower = text.lower()
    for kw in keywords:
        pattern = r"(?<!\w)" + re.escape(kw.lower()) + r"(?!\w)"
        if re.search(pattern, text_lower):
            return kw
    return None


# ──────────────────────────────────────────────────────────────
# Допоміжна: форматування відправника
# ──────────────────────────────────────────────────────────────
def format_sender(sender) -> str:
    """Повертає читабельний рядок з іменем/username відправника."""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    uname = getattr(sender, "username", None)
    uid = getattr(sender, "id", None)

    name = f"{first} {last}".strip()
    if uname:
        tag = f"[ @{uname} ]"
    elif uid:
        tag = f"[ {uid} ]"
    else:
        tag = ""
    return f"{name} {tag}".strip()


def format_chat(chat) -> str:
    title = getattr(chat, "title", None) or ""
    username = getattr(chat, "username", None)
    suffix = f" [ @{username} ]" if username else ""
    return f"{title}{suffix}"


# ──────────────────────────────────────────────────────────────
# Перевірка прав адміна
# ──────────────────────────────────────────────────────────────
def is_admin(chat_username: str, admins: list[str]) -> bool:
    return ("@" + chat_username.lower()) in {a.lower() for a in admins}


# ──────────────────────────────────────────────────────────────
# Локальний евристичний спам-фільтр
# ──────────────────────────────────────────────────────────────
_compiled_triggers: list[re.Pattern] | None = None
_compiled_triggers_src: list[str] | None = None


def _get_compiled_triggers(patterns: list[str]) -> list[re.Pattern]:
    """Кешує скомпільовані regex-патерни для комерційних тригерів."""
    global _compiled_triggers, _compiled_triggers_src
    if _compiled_triggers is not None and _compiled_triggers_src == patterns:
        return _compiled_triggers
    _compiled_triggers = [re.compile(p, re.IGNORECASE) for p in patterns]
    _compiled_triggers_src = patterns
    return _compiled_triggers


def is_service_spam(text: str, config: dict) -> bool:
    """
    Локальний евристичний фільтр: виявляє комерційний спам.
    True = спам, False = не спам.
    """
    t = text.lower()
    score = 0

    # 1. Комерційні тригери (regex з конфігу)
    triggers = config.get("spam_commercial_triggers", [])
    if triggers:
        compiled = _get_compiled_triggers(triggers)
        triggers_found = sum(1 for p in compiled if p.search(t))
        if triggers_found >= 3:
            score += 4
        elif triggers_found >= 2:
            score += 3
        elif triggers_found == 1:
            score += 1

    # 2. Емодзі прайс-листів
    spam_emojis = config.get("spam_emojis", "")
    if spam_emojis:
        emoji_pattern = f"[{re.escape(spam_emojis)}]"
        emoji_count = len(re.findall(emoji_pattern, text))
        if emoji_count >= 6:
            score += 3
        elif emoji_count >= 3:
            score += 1

    # 3. Перелік послуг
    services = config.get("spam_services", [])
    if services:
        services_found = sum(1 for s in services if s in t)
        if services_found >= 5:
            score += 4
        elif services_found >= 3:
            score += 2
        elif services_found >= 2:
            score += 1

    # 4. Ціни в тексті
    prices = len(re.findall(r"\d+\s*(?:[€$]|eur|usd)\b", t))
    if prices >= 3:
        score += 4
    elif prices >= 2:
        score += 3
    elif prices == 1:
        score += 1

    # 5. Контактні патерни
    if re.search(r"\+?\d[\d\s\-]{8,}", text):
        score += 1
    if re.search(r"contact.{0,10}privado|privado.{0,5}📱|в лс|в личк|telegram.{0,5}@", t):
        score += 2

    # 6. Рядки з маркерами прайс-листів
    bullet_lines = len(re.findall(r"^[✓✔•►▸→]\s*\S", text, re.MULTILINE))
    if bullet_lines >= 4:
        score += 3
    elif bullet_lines >= 2:
        score += 1

    threshold = config.get("spam_score_threshold", 4)
    return score >= threshold


# ──────────────────────────────────────────────────────────────
# Моніторинг повідомлень (user client)
# ──────────────────────────────────────────────────────────────
@user_client.on(events.NewMessage(incoming=True))
async def monitor(event):
    text = event.message.text
    if not text:
        return

    config = await get_config()

    chat = await event.get_chat()
    chat_usernameid = getattr(chat, "username", getattr(chat, "id", False))

    # Виключити канал пересилки
    fwd_ch = config.get("forward_channel", "")
    if fwd_ch and chat_usernameid:
        fwd_clean = fwd_ch.lstrip("@").lower()
        if str(chat_usernameid).lower() == fwd_clean:
            return

    # Виключити чати з адмінами зі списку моніторингу
    if getattr(chat, "username", False) and is_admin(getattr(chat, "username", False), config.get("admins", [])):
        return

    # Перевірка мінус-слів
    if has_minus_word(text, config.get("minus_words", [])):
        return

    # Пошук ключового слова
    found_keyword = find_keyword(text, config.get("keywords", []))
    if not found_keyword:
        return

    sender = await event.get_sender()
    chat_name = format_chat(chat)
    sender_name = format_sender(sender)

    # Посилання на оригінальне повідомлення
    if chat_usernameid:
        msg_link = f"https://t.me/{chat_usernameid}/{event.message.id}"
    else:
        msg_link = ''

    # Локальний спам-фільтр (без API)
    if is_service_spam(text, config):
        log.info(f"🛑 Локальний фільтр заблокував: {text[:60]}… з {chat_name}")
        return

    # Додати в чергу для бота
    await pending_messages.put({
        "keyword": found_keyword,
        "chat": chat_name,
        "sender": sender_name,
        "text": text if len(text) <= 1000 else text[:1000] + "…",
        "link": msg_link,
    })
    log.info(f"📥 Додано в чергу з {chat_name} (черга: {pending_messages.qsize()})")


# ──────────────────────────────────────────────────────────────
# Точка входу
# ──────────────────────────────────────────────────────────────
async def main():
    global BOT_TOKEN

    from bot import (
        register_bot_handlers, background_forwarder,
        auto_create_bot, auto_promote_bot_in_channel,
    )

    # Запуск user client
    await user_client.start()
    log.info("✅ User client запущено (моніторинг)")

    # Авто-створення бота якщо токен відсутній
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_FROM_BOTFATHER":
        try:
            BOT_TOKEN = await auto_create_bot(user_client)
            os.environ["BOT_TOKEN"] = BOT_TOKEN
        except Exception as exc:
            log.error(f"❌ Не вдалося створити бота: {exc}")
            log.error("Створи бота вручну: https://t.me/BotFather → /newbot")
            log.error("Потім додай BOT_TOKEN='...' в .env")
            return

    # Запуск bot client
    await bot_client.start(bot_token=BOT_TOKEN)
    log.info("✅ Bot client запущено (обробка)")

    # Реєструємо хендлери бота
    register_bot_handlers(
        bot_client=bot_client,
        user_client=user_client,
        pending_messages=pending_messages,
        get_config_fn=get_config,
        load_config_fn=load_config,
        update_config_fn=update_config,
        is_admin_fn=is_admin,
        clean_minus_words_fn=clean_minus_words,
    )

    # Фонова пересилка (в контексті бота)
    asyncio.create_task(
        background_forwarder(bot_client, pending_messages, get_config, load_config, update_config)
    )

    log.info("🚀 Обидва клієнти працюють")

    # Авто-додавання бота адміном у канал пересилки
    config = await get_config()
    fwd_ch = config.get("forward_channel")
    if fwd_ch:
        await auto_promote_bot_in_channel(user_client, bot_client, fwd_ch)
    else:
        log.warning("⚠️ Канал пересилки не налаштовано — використай /set_channel @канал")

    # Запускаємо обидва клієнти паралельно
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
