"""
bot.py — Telegram Bot: обробка черги, AI фільтрація, кнопки, адмін-команди.
Працює разом з main.py (user client) в одному процесі.
"""

import asyncio
import os
import re
import random
import string
import logging
from datetime import datetime, timedelta
from pathlib import Path
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    GetParticipantRequest, EditAdminRequest,
)
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    ChatAdminRights, BotCommand, BotCommandScopePeerUser,
    BotCommandScopeDefault,
)

log = logging.getLogger("bot")

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    OPENAI_AVAILABLE = False

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ──────────────────────────────────────────────────────────────
# OpenAI синглтон
# ──────────────────────────────────────────────────────────────
_openai_client = None
_openai_key_used: str = ""


def get_openai_client(api_key: str):
    global _openai_client, _openai_key_used
    if not OPENAI_AVAILABLE:
        return None
    if _openai_client is None or _openai_key_used != api_key:
        _openai_client = OpenAI(api_key=api_key)
        _openai_key_used = api_key
    return _openai_client


# ──────────────────────────────────────────────────────────────
# Статистика AI
# ──────────────────────────────────────────────────────────────
ai_stats = {"checked": 0, "passed": 0, "filtered": 0}


# ──────────────────────────────────────────────────────────────
# AI фільтрація
# ──────────────────────────────────────────────────────────────
async def ai_filter_message(text: str, keyword: str, chat_name: str, config: dict) -> bool:
    """True = цільове (пропустити), False = спам/реклама (блокувати)."""
    if not config.get("ai_filter_enabled", False):
        return True

    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return True

    oc = get_openai_client(OPENAI_API_KEY)
    if oc is None:
        return True

    try:
        keywords_str = ", ".join(config.get("keywords", [])[:100])
        minus_words_str = ", ".join(config.get("minus_words", [])[:100])
        ai_main_filter_role = config.get("ai_main_filter_role", "")
        ai_tagret_filter_criteria = config.get("ai_tagret_filter_criteria", "")
        ai_spam_filter_criteria = config.get("ai_spam_filter_criteria", "")

        prompt = (
            f"Повідомлення з Telegram-групи «{chat_name}», знайдене за ключовим словом «{keyword}».\n\n"
            f"Текст повідомлення:\n«{text[:500]}»\n\n"
            f"Ключові слова для моніторингу: {keywords_str}\n"
            f"Стоп-слова (спам-індикатори): {minus_words_str}\n\n"
            f"Критерії ЦІЛЬОВОГО (пропустити):\n{ai_tagret_filter_criteria}\n\n"
            f"Критерії СПАМУ (заблокувати):\n{ai_spam_filter_criteria}\n\n"
            "Визначи: це ЦІЛЬОВЕ повідомлення чи СПАМ?\n"
            "Відповідай одним словом: TARGET або SPAM."
        )

        response = oc.responses.create(
            model=config.get("openai_model", "gpt-4o-mini"),
            instructions=ai_main_filter_role,
            input=prompt,
        )

        result = response.output_text.upper()
        ai_stats["checked"] += 1

        if "TARGET" in result:
            ai_stats["passed"] += 1
            log.info(f"🤖 AI ПРОПУСТИВ: {text[:60]}…")
            return True
        else:
            ai_stats["filtered"] += 1
            log.info(f"🤖 AI ЗАБЛОКУВАВ: {text[:60]}…")
            return False

    except Exception as exc:
        log.error(f"Помилка AI фільтрації: {exc}")
        return True


# ──────────────────────────────────────────────────────────────
# AI: витягування стоп-слів
# ──────────────────────────────────────────────────────────────
async def ai_extract_stop_words(text: str, config: dict) -> list[str]:
    """
    Просить OpenAI витягнути спам-індикатори із заблокованого повідомлення.
    Повертає список нових стоп-слів (уже дедуплікованих).
    """
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return []

    oc = get_openai_client(OPENAI_API_KEY)
    if oc is None:
        return []

    try:
        existing_mw = {w.lower() for w in config.get("minus_words", [])}
        existing_skip = {w.lower() for w in config.get("skip_words", [])}
        forbidden = existing_mw | existing_skip

        prompt = (
            "З наведеного спам-повідомлення витягни 1–3 стоп-слова/фрази, "
            "які є характерними індикаторами спаму/реклами.\n\n"
            "Правила:\n"
            "- Тільки слова/фрази що вказують на комерційний або рекламний характер\n"
            "- НЕ додавай загальні слова (артиклі, прийменники, поширені дієслова)\n"
            "- НЕ додавай слова коротші 3 символів\n"
            "- Малі літери, без лапок\n"
            "- Кожне слово/фразу на новому рядку\n"
            "- Якщо неможливо виділити — відповідай NONE\n"
            "- Відповідай ТІЛЬКИ словами, без нумерації і пояснень\n\n"
            f"Повідомлення:\n{text[:500]}"
        )

        response = oc.responses.create(
            model=config.get("openai_model", "gpt-4o-mini"),
            instructions="Ти — аналітик спам-контенту.",
            input=prompt,
        )

        raw = response.output_text.strip()
        if not raw or "NONE" in raw.upper():
            return []

        candidates = [
            line.strip().lower().strip('"').strip("'").strip('- ')
            for line in raw.splitlines()
            if line.strip()
        ]

        new_words = []
        for word in candidates:
            if len(word) < 3 or len(word) > 60:
                continue
            if word in forbidden:
                continue
            if word not in {w.lower() for w in new_words}:
                new_words.append(word)

        return new_words[:3]

    except Exception as exc:
        log.error(f"Помилка витягування стоп-слів: {exc}")
        return []


# ──────────────────────────────────────────────────────────────
# AI: витягування ключових слів
# ──────────────────────────────────────────────────────────────
async def ai_extract_keywords(text: str, config: dict) -> list[str]:
    """
    Просить OpenAI витягнути цільові ключові слова з повідомлення.
    Повертає список нових ключових слів (вже дедуплікованих).
    """
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return []

    oc = get_openai_client(OPENAI_API_KEY)
    if oc is None:
        return []

    try:
        existing_kw = {w.lower() for w in config.get("keywords", [])}
        existing_skip = {w.lower() for w in config.get("skip_words", [])}
        forbidden = existing_kw | existing_skip

        prompt = (
            "З наведеного цільового повідомлення витягни 1–3 ключові фрази/слова, "
            "які допоможуть знаходити подібні повідомлення в майбутньому.\n\n"
            "Правила:\n"
            "- Тільки слова/фрази що вказують на тему повідомлення\n"
            "- НЕ додавай загальні слова (артиклі, прийменники, поширені дієслова)\n"
            "- НЕ додавай слова коротші 3 символів\n"
            "- Малі літери, без лапок\n"
            "- Кожне слово/фразу на новому рядку\n"
            "- Якщо неможливо виділити — відповідай NONE\n"
            "- Відповідай ТІЛЬКИ словами, без нумерації і пояснень\n\n"
            f"Повідомлення:\n{text[:500]}"
        )

        response = oc.responses.create(
            model=config.get("openai_model", "gpt-4o-mini"),
            instructions="Ти — аналітик цільового контенту.",
            input=prompt,
        )

        raw = response.output_text.strip()
        if not raw or "NONE" in raw.upper():
            return []

        candidates = [
            line.strip().lower().strip('"').strip("'").strip('- ')
            for line in raw.splitlines()
            if line.strip()
        ]

        new_words = []
        for word in candidates:
            if len(word) < 3 or len(word) > 60:
                continue
            if word in forbidden:
                continue
            if word not in {w.lower() for w in new_words}:
                new_words.append(word)

        return new_words[:3]

    except Exception as exc:
        log.error(f"Помилка витягування ключових слів: {exc}")
        return []


# ──────────────────────────────────────────────────────────────
# AI: консолідація списку (дедуплікація + апроксимація до 100)
# ──────────────────────────────────────────────────────────────
async def ai_consolidate_list(words: list[str], list_type: str, config: dict) -> list[str]:
    """
    Консолідує список слів до ≤100 записів за допомогою AI.
    list_type: 'keywords' або 'minus_words'
    """
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return words[:100]

    oc = get_openai_client(OPENAI_API_KEY)
    if oc is None:
        return words[:100]

    try:
        words_str = "\n".join(words)

        if list_type == "keywords":
            task_desc = (
                "Це список КЛЮЧОВИХ СЛІВ для моніторингу повідомлень.\n"
                "Консолідуй його до максимум 100 найважливіших записів."
            )
        else:
            task_desc = (
                "Це список СТОП-СЛІВ (спам-індикаторів) для фільтрації спаму.\n"
                "Консолідуй його до максимум 100 найважливіших записів."
            )

        prompt = (
            f"{task_desc}\n\n"
            "Правила:\n"
            "- Видали контекстні дублікати (напр. однакові слова різними мовами)\n"
            "- Об'єднай схожі фрази\n"
            "- Видали слова що вже покриваються іншими\n"
            "- Зберігай найважливіші та унікальні записи\n"
            "- Малі літери, кожне слово/фразу на окремому рядку\n"
            "- Максимум 100 записів\n"
            "- Відповідай ТІЛЬКИ списком слів, без нумерації і пояснень\n\n"
            f"Поточний список ({len(words)} записів):\n{words_str}"
        )

        response = oc.responses.create(
            model=config.get("openai_model", "gpt-4o-mini"),
            instructions="Ти — асистент для оптимізації списків слів.",
            input=prompt,
        )

        raw = response.output_text.strip()
        if not raw:
            return words[:100]

        consolidated = [
            line.strip().lower().strip('"').strip("'").strip('- ')
            for line in raw.splitlines()
            if line.strip() and len(line.strip()) >= 3
        ]

        seen: set[str] = set()
        result: list[str] = []
        for w in consolidated:
            if w.lower() not in seen:
                result.append(w)
                seen.add(w.lower())

        log.info(f"🧠 Консолідація {list_type}: {len(words)} → {len(result)}")
        return result[:100]

    except Exception as exc:
        log.error(f"Помилка консолідації списку: {exc}")
        return words[:100]


# ──────────────────────────────────────────────────────────────
# Безпечна відправка
# ──────────────────────────────────────────────────────────────
async def safe_send(bot_client, destination, text: str, max_retries: int = 5) -> None:
    """Надсилає з автоматичним FloodWait retry."""
    for attempt in range(max_retries):
        try:
            await bot_client.send_message(destination, text)
            return
        except FloodWaitError as exc:
            wait = exc.seconds + 5
            log.warning(f"FloodWait: чекаю {wait}с… (спроба {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
        except Exception as exc:
            log.error(f"Помилка відправки в {destination}: {exc}")
            return
    log.error(f"safe_send: не вдалося після {max_retries} спроб у {destination}")


async def send_long_message(bot_client, destination, text: str, max_length: int = 4000) -> None:
    """Розбиває довге повідомлення на частини."""
    if len(text) <= max_length:
        await safe_send(bot_client, destination, text)
        return

    parts: list[str] = []
    current = ""
    for line in text.split('\n'):
        chunk = line + '\n'
        if len(current) + len(chunk) <= max_length:
            current += chunk
        else:
            if current:
                parts.append(current)
            current = chunk
    if current:
        parts.append(current)

    for i, part in enumerate(parts, 1):
        header = f"📄 Частина {i}/{len(parts)}\n\n" if len(parts) > 1 else ""
        await safe_send(bot_client, destination, header + part)
        await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────
# Фонова пересилка
# ──────────────────────────────────────────────────────────────
async def background_forwarder(bot_client, pending_messages, get_config_fn, load_config_fn, update_config_fn) -> None:
    log.info("🔄 Запущено фонову пересилку повідомлень (бот)")
    while True:
        try:
            msg_data = await pending_messages.get()
            config = await get_config_fn()
            fwd_ch = config.get("forward_channel")

            if not fwd_ch:
                log.warning("Канал для пересилки не налаштовано!")
                pending_messages.task_done()
                continue

            forward_text = (
                f"🔔 Знайдено: **{msg_data['keyword']}**\n"
                f"📢 Чат: {msg_data['chat']}\n"
                f"👤 Від: {msg_data['sender']}\n\n"
                f"💬 {msg_data['text']}\n\n"
                f"🔗 {msg_data.get('link', '')}"
            )

            # AI фільтрація
            if not await ai_filter_message(
                msg_data['text'], msg_data['keyword'], msg_data['chat'], config
            ):
                log.info(f"🚫 AI відфільтрував повідомлення з {msg_data['chat']}")
                pending_messages.task_done()
                continue

            if config.get("ai_filter_enabled", False):
                buttons = [
                    [Button.inline("✅ Цільове", data=b"target"),
                     Button.inline("🚫 Спам", data=b"spam")]
                ]
                for attempt in range(5):
                    try:
                        sent = await bot_client.send_message(fwd_ch, forward_text, buttons=buttons)
                        break
                    except FloodWaitError as exc:
                        wait = exc.seconds + 5
                        log.warning(f"FloodWait: чекаю {wait}с… (спроба {attempt + 1}/5)")
                        await asyncio.sleep(wait)
                    except Exception as exc:
                        log.error(f"Помилка відправки в {fwd_ch}: {exc}")
                        break
            else:
                await safe_send(bot_client, fwd_ch, forward_text)

            log.info(f"✅ Переслано в {fwd_ch} з {msg_data['chat']}")
            await asyncio.sleep(3)
            pending_messages.task_done()

        except Exception as exc:
            log.error(f"Помилка в фоновій пересилці: {exc}")
            try:
                pending_messages.task_done()
            except ValueError:
                pass
            await asyncio.sleep(5)

# ──────────────────────────────────────────────────────────────
# Авто-створення бота через @BotFather
# ──────────────────────────────────────────────────────────────
async def auto_create_bot(user_client: TelegramClient) -> str:
    """
    Створює нового бота через @BotFather і повертає токен.
    Зберігає токен у .env.
    """
    BOTFATHER = "@BotFather"
    log.info("🤖 BOT_TOKEN не знайдено — створюю бота автоматично через @BotFather…")

    async def send_and_wait(text: str, wait_sec: float = 3.0) -> str:
        await user_client.send_message(BOTFATHER, text)
        await asyncio.sleep(wait_sec)
        messages = await user_client.get_messages(BOTFATHER, limit=1)
        if messages:
            return messages[0].text or ""
        return ""

    # 1. Скасувати можливий незавершений діалог
    await send_and_wait("/cancel", 1.5)

    # 2. /newbot
    resp = await send_and_wait("/newbot", 3)
    if "name" not in resp.lower() and "ім'я" not in resp.lower():
        log.error(f"Неочікувана відповідь від BotFather: {resp[:200]}")
        raise RuntimeError("Не вдалося почати створення бота")

    # 3. Ім'я бота
    resp = await send_and_wait("TGM Monitor Bot", 3)

    # 4. Username (унікальний)
    suffix = ''.join(random.choices(string.digits, k=5))
    bot_username = f"tgm_monitor_{suffix}_bot"

    for attempt in range(5):
        resp = await send_and_wait(bot_username, 4)
        if "token" in resp.lower() or "t.me/" in resp:
            break
        suffix = ''.join(random.choices(string.digits, k=6))
        bot_username = f"tgm_mon_{suffix}_bot"
        log.info(f"Username зайнято, пробую: {bot_username}")
    else:
        raise RuntimeError("Не вдалося підібрати вільний username для бота")

    # 5. Витягнути токен
    token_match = re.search(r"(\d+:[A-Za-z0-9_-]{30,})", resp)
    if not token_match:
        log.error(f"Токен не знайдено у відповіді: {resp[:300]}")
        raise RuntimeError("BotFather не повернув токен")

    token = token_match.group(1)
    log.info(f"✅ Бот створено: @{bot_username}")
    log.info(f"🔑 Токен: {token[:15]}…")

    # 6. Зберегти в .env
    env_path = Path(".env")
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if "BOT_TOKEN" in content:
            lines = content.splitlines()
            new_lines = []
            for line in lines:
                if line.strip().startswith("BOT_TOKEN"):
                    new_lines.append(f"BOT_TOKEN='{token}'")
                else:
                    new_lines.append(line)
            env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        else:
            with env_path.open("a", encoding="utf-8") as f:
                f.write(f"\nBOT_TOKEN='{token}'\n")
    else:
        env_path.write_text(f"BOT_TOKEN='{token}'\n", encoding="utf-8")

    return token


# ──────────────────────────────────────────────────────────────
# Авто-додавання бота адміном у канал
# ──────────────────────────────────────────────────────────────
async def auto_promote_bot_in_channel(
    user_cl: TelegramClient,
    bot_cl: TelegramClient,
    channel: str,
) -> bool:
    """
    Перевіряє чи user є адміном з правом add_admins у каналі.
    Якщо так — додає бота адміном з правами на публікацію.
    Повертає True якщо бот вже адмін або успішно додано.
    """
    try:
        ch_entity = await user_cl.get_entity(channel)
    except Exception as exc:
        log.warning(f"⚠️ Не вдалося отримати канал {channel}: {exc}")
        return False

    # Перевірка прав користувача
    try:
        me = await user_cl.get_me()
        participant = await user_cl(GetParticipantRequest(ch_entity, me.id))
        admin_rights = getattr(participant.participant, "admin_rights", None)

        if admin_rights is None:
            log.warning(f"⚠️ Користувач не є адміном у {channel}")
            return False

        is_creator = "Creator" in type(participant.participant).__name__

        if not is_creator and not admin_rights.add_admins:
            log.warning(f"⚠️ Користувач не має права додавати адмінів у {channel}")
            return False

    except Exception as exc:
        log.warning(f"⚠️ Не вдалося перевірити права у {channel}: {exc}")
        return False

    # Отримати бота і перевірити чи він вже адмін
    try:
        bot_me = await bot_cl.get_me()
        bot_username = bot_me.username
        if not bot_username:
            log.warning("⚠️ У бота немає username")
            return False
        bot_entity = await user_cl.get_entity(f"@{bot_username}")
        try:
            bot_participant = await user_cl(GetParticipantRequest(ch_entity, bot_entity.id))
            bot_admin = getattr(bot_participant.participant, "admin_rights", None)
            if bot_admin is not None:
                log.info(f"✅ Бот вже адмін у {channel}")
                return True
        except Exception:
            pass
    except Exception as exc:
        log.warning(f"⚠️ Не вдалося отримати інфо про бота: {exc}")
        return False

    # Додаємо бота адміном
    try:
        bot_rights = ChatAdminRights(
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            invite_users=True,
        )
        await user_cl(EditAdminRequest(
            channel=ch_entity,
            user_id=bot_entity,
            admin_rights=bot_rights,
            rank="Bot",
        ))
        log.info(f"✅ Бот додано адміном у {channel}")
        return True
    except Exception as exc:
        log.warning(f"⚠️ Не вдалося додати бота адміном у {channel}: {exc}")
        return False


# ──────────────────────────────────────────────────────────────
# Парсинг лог-файлів для статистики
# ──────────────────────────────────────────────────────────────
LOGS_DIR = Path("logs")


def _collect_log_stats(days: int = 1) -> dict:
    """Збирає статистику з лог-файлів за останні N днів."""
    stats = {"queued": 0, "forwarded": 0, "local_blocked": 0, "ai_blocked": 0}
    cutoff = datetime.now() - timedelta(days=days)

    for d in range(days + 1):
        date_str = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")

        # User logs
        user_log = LOGS_DIR / f"user_{date_str}.log"
        if user_log.exists():
            for line in user_log.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "📥 Додано в чергу" in line:
                    stats["queued"] += 1
                elif "🛑 Локальний фільтр заблокував" in line:
                    stats["local_blocked"] += 1

        # Bot logs
        bot_log = LOGS_DIR / f"bot_{date_str}.log"
        if bot_log.exists():
            for line in bot_log.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "✅ Переслано" in line:
                    stats["forwarded"] += 1
                elif "🤖 AI ЗАБЛОКУВАВ" in line:
                    stats["ai_blocked"] += 1

    return stats


def _collect_blocked_messages(days: int = 1, limit: int = 30) -> list[str]:
    """Збирає список заблокованих повідомлень з логів."""
    blocked: list[str] = []

    for d in range(days + 1):
        date_str = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")

        # Локальний фільтр
        user_log = LOGS_DIR / f"user_{date_str}.log"
        if user_log.exists():
            for line in user_log.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "🛑 Локальний фільтр заблокував" in line:
                    blocked.append(f"🛑 [{date_str}] {line.split('Локальний фільтр заблокував: ', 1)[-1]}")

        # AI фільтр
        bot_log = LOGS_DIR / f"bot_{date_str}.log"
        if bot_log.exists():
            for line in bot_log.read_text(encoding="utf-8", errors="ignore").splitlines():
                if "🤖 AI ЗАБЛОКУВАВ" in line:
                    blocked.append(f"🤖 [{date_str}] {line.split('AI ЗАБЛОКУВАВ: ', 1)[-1]}")

    return blocked[-limit:]  # останні N


# ──────────────────────────────────────────────────────────────
# Реєстрація хендлерів бота
# ──────────────────────────────────────────────────────────────
def register_bot_handlers(
    bot_client: TelegramClient,
    user_client: TelegramClient,
    pending_messages,
    get_config_fn,
    load_config_fn,
    update_config_fn,
    is_admin_fn,
    clean_minus_words_fn,
):
    """Реєструє всі хендлери на bot_client."""

    # Повний список команд (для адмінів)
    _admin_cmds = [
        BotCommand(command="start", description="👋 Привітання"),
        BotCommand(command="help", description="📖 Довідка по командах"),
        BotCommand(command="list", description="📋 Всі налаштування"),
        BotCommand(command="ai_status", description="🤖 Статус AI фільтрації"),
        BotCommand(command="ai_enable", description="🟢 Увімкнути AI фільтр"),
        BotCommand(command="ai_disable", description="🔴 Вимкнути AI фільтр"),
        BotCommand(command="ai_set_key", description="🔑 Встановити OpenAI ключ"),
        BotCommand(command="ai_set_model", description="🧠 Модель OpenAI"),
        BotCommand(command="ai_test", description="🧪 Тестувати AI фільтр"),
        BotCommand(command="set_channel", description="📢 Встановити канал"),
        BotCommand(command="get_channel", description="📢 Поточний канал"),
        BotCommand(command="add_word", description="🔍 Додати ключове слово"),
        BotCommand(command="del_word", description="🗑 Видалити ключове слово"),
        BotCommand(command="add_minus", description="🚫 Додати мінус-слово"),
        BotCommand(command="del_minus", description="🗑 Видалити мінус-слово"),
        BotCommand(command="add_skip", description="⏭ Додати skip-слово"),
        BotCommand(command="del_skip", description="🗑 Видалити skip-слово"),
        BotCommand(command="clean_minus", description="🧹 Очистити мінус-слова"),
        BotCommand(command="spam_triggers", description="🛡 Показати спам-тригери"),
        BotCommand(command="add_trigger", description="➕ Додати спам-тригер"),
        BotCommand(command="del_trigger", description="🗑 Видалити спам-тригер"),
        BotCommand(command="spam_services", description="🛡 Показати спам-сервіси"),
        BotCommand(command="add_service", description="➕ Додати спам-сервіс"),
        BotCommand(command="del_service", description="🗑 Видалити спам-сервіс"),
        BotCommand(command="spam_emojis", description="🛡 Показати/задати спам-емодзі"),
        BotCommand(command="spam_threshold", description="🎯 Поріг спам-фільтру"),
        BotCommand(command="queue_status", description="📊 Статус черги"),
        BotCommand(command="add_admin", description="👤 Додати адміна"),
        BotCommand(command="del_admin", description="🗑 Видалити адміна"),
        BotCommand(command="join_add", description="📥 Додати групи в чергу"),
        BotCommand(command="join_list", description="📋 Черга груп"),
        BotCommand(command="join_all", description="🚀 Вступити у всі"),
        BotCommand(command="groups", description="📋 Список груп"),
        BotCommand(command="stats", description="📊 Статистика фільтрації"),
        BotCommand(command="blocked", description="🚫 Список заблокованих"),
    ]
    _admins_with_menu: set[str] = set()

    # Реєстрація меню команд (мінімальне для всіх)
    async def set_bot_commands():
        try:
            # Для не-адмінів — тільки /start та /help
            await bot_client(SetBotCommandsRequest(
                scope=BotCommandScopeDefault(),
                lang_code="uk",
                commands=[
                    BotCommand(command="start", description="👋 Привітання"),
                    BotCommand(command="help", description="📖 Довідка"),
                ],
            ))
            log.info("✅ Базове меню команд зареєстровано")
        except Exception as exc:
            log.warning(f"⚠️ Не вдалося зареєструвати команди: {exc}")

    async def _ensure_admin_menu(event):
        """Встановлює повне меню для адміна при першій взаємодії."""
        sender = await event.get_sender()
        username = getattr(sender, "username", "") or ""
        if not username or username.lower() in _admins_with_menu:
            return
        try:
            from telethon.tl.types import BotCommandScopePeer
            peer = await event.get_input_sender()
            await bot_client(SetBotCommandsRequest(
                scope=BotCommandScopePeer(peer=peer),
                lang_code="uk",
                commands=_admin_cmds,
            ))
            _admins_with_menu.add(username.lower())
            log.info(f"✅ Повне меню встановлено для @{username}")
        except Exception as exc:
            log.warning(f"⚠️ Меню для @{username}: {exc}")

    asyncio.ensure_future(set_bot_commands())

    # ──────────────────────────────────────────────────────────
    # Callback-хендлер для кнопок Цільове/Спам/Відмінити
    # ──────────────────────────────────────────────────────────
    # {msg_id: {"action": "target"|"spam", "words": [...]}}
    _undo_data: dict[int, dict] = {}

    @bot_client.on(events.CallbackQuery())
    async def on_feedback_button(event):
        data = event.data
        if data not in (b"target", b"spam", b"undo_target", b"undo_spam"):
            return

        msg_id = event.message_id
        msg = await event.get_message()
        msg_text = msg.text or "" if msg else ""

        # ── Відмінити ──
        if data in (b"undo_target", b"undo_spam"):
            undo = _undo_data.get(msg_id)
            if not undo:
                await event.answer("⚠️ Немає що відміняти", alert=True)
                return

            action = undo["action"]
            words = undo["words"]

            if words:
                fresh = load_config_fn()
                if action == "target":
                    kw = fresh.get("keywords", [])
                    words_lower = {w.lower() for w in words}
                    fresh["keywords"] = [w for w in kw if w.lower() not in words_lower]
                    log.info(f"↩️ Відмінено ключові слова: {words}")
                else:
                    mw = fresh.get("minus_words", [])
                    words_lower = {w.lower() for w in words}
                    fresh["minus_words"] = [w for w in mw if w.lower() not in words_lower]
                    log.info(f"↩️ Відмінено стоп-слова: {words}")
                await update_config_fn(fresh)
                await event.answer("↩️ Відмінено! Слова видалено з конфігу", alert=False)
            else:
                await event.answer("↩️ Відмінено!", alert=False)

            _undo_data.pop(msg_id, None)

            # Відновити оригінальний текст + кнопки
            try:
                import re as _re
                clean = _re.split(r"\n\n[✅🚫↩️]", msg_text, maxsplit=1)[0]
                buttons = [
                    [Button.inline("✅ Цільове", data=b"target"),
                     Button.inline("🚫 Спам", data=b"spam")]
                ]
                await event.edit(clean, buttons=buttons)
            except Exception as exc:
                log.error(f"Помилка відновлення кнопок: {exc}")
            return

        # ── Цільове / Спам ──
        # Витягуємо оригінальний текст з повідомлення бота (між 💬 та 🔗)
        import re as _re2
        m = _re2.search(r"💬\s*(.+?)(?:\n\n🔗|$)", msg_text, _re2.DOTALL)
        original_text = m.group(1).strip() if m else ""
        if not original_text:
            await event.answer("⚠️ Текст повідомлення не знайдено", alert=True)
            return

        config = await get_config_fn()

        if data == b"target":
            # Миттєва реакція: прибрати кнопки, показати статус
            await event.answer("⏳ Аналізую ключові слова…")
            try:
                await event.edit(msg_text + "\n\n⏳ **Аналізую ключові слова…**", buttons=None)
            except Exception:
                pass

            new_words = await ai_extract_keywords(original_text, config)
            added = []
            if new_words:
                fresh = load_config_fn()
                kw = fresh.get("keywords", [])
                kw_lower = {w.lower() for w in kw}
                for w in new_words:
                    if w.lower() not in kw_lower:
                        kw.append(w)
                        kw_lower.add(w.lower())
                        added.append(w)
                if added:
                    if len(kw) > 100:
                        kw = await ai_consolidate_list(kw, "keywords", fresh)
                    fresh["keywords"] = kw
                    await update_config_fn(fresh)
                    added_str = ", ".join(f'"{w}"' for w in added)
                    log.info(f"✅ Додано ключові слова: {added_str}")
                    result_text = f"\n\n✅ **Додано ключові слова:** {added_str}"
                else:
                    result_text = "\n\n✅ Нових ключових слів не знайдено (всі вже є)"
            else:
                result_text = "\n\n✅ AI не зміг виділити нових ключових слів"

            _undo_data[msg_id] = {"action": "target", "words": added}
            undo_btn = [[Button.inline("↩️ Відмінити", data=b"undo_target")]]

        else:  # spam
            # Миттєва реакція: прибрати кнопки, показати статус
            await event.answer("⏳ Аналізую стоп-слова…")
            try:
                await event.edit(msg_text + "\n\n⏳ **Аналізую стоп-слова…**", buttons=None)
            except Exception:
                pass

            new_words = await ai_extract_stop_words(original_text, config)
            added = []
            if new_words:
                fresh = load_config_fn()
                mw = fresh.get("minus_words", [])
                mw_lower = {w.lower() for w in mw}
                for w in new_words:
                    if w.lower() not in mw_lower:
                        mw.append(w)
                        mw_lower.add(w.lower())
                        added.append(w)
                if added:
                    if len(mw) > 100:
                        mw = await ai_consolidate_list(mw, "minus_words", fresh)
                    fresh["minus_words"] = mw
                    await update_config_fn(fresh)
                    added_str = ", ".join(f'"{w}"' for w in added)
                    log.info(f"🚫 Додано стоп-слова: {added_str}")
                    result_text = f"\n\n🚫 **Додано стоп-слова:** {added_str}"
                else:
                    result_text = "\n\n🚫 Нових стоп-слів не знайдено (всі вже є)"
            else:
                result_text = "\n\n🚫 AI не зміг виділити нових стоп-слів"

            _undo_data[msg_id] = {"action": "spam", "words": added}
            undo_btn = [[Button.inline("↩️ Відмінити", data=b"undo_spam")]]

        # Замінити статус ⏳ на результат + кнопка Відмінити
        try:
            # Прибрати рядок ⏳ і додати результат
            clean_base = _re2.split(r"\n\n⏳", msg_text, maxsplit=1)[0]
            await event.edit(clean_base + result_text, buttons=undo_btn)
        except Exception as exc:
            log.error(f"Помилка редагування повідомлення: {exc}")



    # ──────────────────────────────────────────────────────────
    # Команди адміністратора (через бота)
    # ──────────────────────────────────────────────────────────
    @bot_client.on(events.NewMessage(pattern=r'^/'))
    async def commands(event):
        global OPENAI_API_KEY
        config = await get_config_fn()
        sender = await event.get_sender()
        chat_username = getattr(sender, "username", "") or ""

        if not is_admin_fn(chat_username, config.get("admins", [])):
            return

        # Встановити повне меню при першій взаємодії адміна
        await _ensure_admin_menu(event)

        text = event.message.text.strip()
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # /cmd@bot_name -> /cmd
        arg = parts[1].strip() if len(parts) > 1 else ""

        # === AI ===
        if cmd == "/ai_enable":
            if not OPENAI_AVAILABLE:
                await event.reply("❌ OpenAI не встановлено: pip install openai")
                return
            if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY":
                await event.reply("❌ Спочатку задай ключ: /ai_set_key sk-…")
                return
            config["ai_filter_enabled"] = True
            await update_config_fn(config)
            await event.reply("✅ AI фільтрація УВІМКНЕНА")

        elif cmd == "/ai_disable":
            config["ai_filter_enabled"] = False
            await update_config_fn(config)
            await event.reply("🔴 AI фільтрація ВИМКНЕНА")

        elif cmd == "/ai_set_key":
            if not arg:
                await event.reply("❌ /ai_set_key sk-…")
                return
            OPENAI_API_KEY = arg
            os.environ["OPENAI_API_KEY"] = arg
            await event.reply(f"✅ Ключ збережено: {arg[:10]}…{arg[-4:]}\nВикористай /ai_enable")

        elif cmd == "/ai_set_model":
            if not arg:
                await event.reply(
                    "❌ Вкажи модель:\n"
                    "/ai_set_model gpt-4o-mini (швидко+дешево)\n"
                    "/ai_set_model gpt-4o (точніше)\n"
                    "/ai_set_model gpt-4.1-mini (найточніше)"
                )
                return
            config["openai_model"] = arg
            await update_config_fn(config)
            await event.reply(f"✅ Модель: {arg}")

        elif cmd == "/ai_status":
            enabled = config.get("ai_filter_enabled", False)
            key_ok = bool(OPENAI_API_KEY) and OPENAI_API_KEY != "YOUR_OPENAI_API_KEY"

            # Статистика з логів
            today = _collect_log_stats(1)
            week = _collect_log_stats(7)

            await event.reply(
                f"🤖 **AI фільтрація (OpenAI):**\n"
                f"{'🟢 УВІМКНЕНА' if enabled else '🔴 ВИМКНЕНА'}\n"
                f"🔑 Ключ: {'✅' if key_ok else '❌ не налаштовано'}\n"
                f"🧠 Модель: {config.get('openai_model', 'gpt-4o-mini')}\n"
                f"🎭 Роль: {'✅' if config.get('ai_main_filter_role') else '❌ не задано'}\n"
                f"🎯 Критерії цільового: {'✅' if config.get('ai_tagret_filter_criteria') else '❌ не задано'}\n"
                f"🛡 Критерії спаму: {'✅' if config.get('ai_spam_filter_criteria') else '❌ не задано'}\n\n"
                f"📊 **Статистика сьогодні:**\n"
                f"  📥 В чергу: {today['queued']} | ✅ Переслано: {today['forwarded']}\n"
                f"  🛑 Локальний: {today['local_blocked']} | 🤖 AI: {today['ai_blocked']}\n\n"
                f"📊 **За тиждень:**\n"
                f"  📥 В чергу: {week['queued']} | ✅ Переслано: {week['forwarded']}\n"
                f"  🛑 Локальний: {week['local_blocked']} | 🤖 AI: {week['ai_blocked']}"
            )

        elif cmd == "/ai_test":
            if not arg:
                await event.reply("❌ /ai_test <текст>")
                return
            await event.reply("🤖 Тестую…")
            result = await ai_filter_message(arg, "ситу", "test_chat", config)
            await event.reply("✅ AI ПРОПУСТИВ (цільове)" if result else "🚫 AI ЗАБЛОКУВАВ (спам)")

        elif cmd == "/ai_set_role":
            if not arg:
                await event.reply("❌ /ai_set_role <текст ролі AI>")
                return
            config["ai_main_filter_role"] = arg
            await update_config_fn(config)
            await event.reply(f"✅ AI роль встановлено:\n{arg[:200]}")

        elif cmd == "/ai_get_role":
            role = config.get("ai_main_filter_role", "")
            await event.reply(f"🎭 **AI роль:**\n{role}" if role else "❌ AI роль не налаштовано")

        elif cmd == "/ai_set_target":
            if not arg:
                await event.reply("❌ /ai_set_target <критерії цільового повідомлення>")
                return
            config["ai_tagret_filter_criteria"] = arg
            await update_config_fn(config)
            await event.reply(f"✅ Критерії ЦІЛЬОВОГО встановлено:\n{arg[:200]}")

        elif cmd == "/ai_get_target":
            criteria = config.get("ai_tagret_filter_criteria", "")
            await event.reply(f"🎯 **Критерії ЦІЛЬОВОГО:**\n{criteria}" if criteria else "❌ Критерії цільового не налаштовано")

        elif cmd == "/ai_set_spam":
            if not arg:
                await event.reply("❌ /ai_set_spam <критерії спаму>")
                return
            config["ai_spam_filter_criteria"] = arg
            await update_config_fn(config)
            await event.reply(f"✅ Критерії СПАМУ встановлено:\n{arg[:200]}")

        elif cmd == "/ai_get_spam":
            criteria = config.get("ai_spam_filter_criteria", "")
            await event.reply(f"🚫 **Критерії СПАМУ:**\n{criteria}" if criteria else "❌ Критерії спаму не налаштовано")

        # === Канал ===
        elif cmd == "/set_channel":
            if not arg:
                await event.reply("❌ /set_channel @канал")
                return
            try:
                entity = await bot_client.get_entity(arg)
                config["forward_channel"] = arg
                await update_config_fn(config)
                await event.reply(
                    f"✅ Канал: **{arg}**\n"
                    f"Назва: {getattr(entity, 'title', '?')}\n"
                    f"⚠️ Переконайся що бот є адміном каналу!"
                )
            except Exception as exc:
                await event.reply(f"❌ Помилка доступу до каналу: {exc}")

        elif cmd == "/get_channel":
            ch = config.get("forward_channel")
            await event.reply(f"📢 Канал: **{ch}**" if ch else "❌ Канал не налаштовано")

        # === Адміни ===
        elif cmd == "/add_admin":
            if not arg:
                await event.reply("❌ /add_admin @username")
                return
            admins = config.get("admins", [])
            if arg.lower() in {a.lower() for a in admins}:
                await event.reply("⚠️ Адмін вже є")
            else:
                admins.append(arg)
                config["admins"] = admins
                await update_config_fn(config)
                await event.reply(f"✅ Додано адміна: **{arg}**")

        elif cmd == "/del_admin":
            if "@" + chat_username.lower() == arg.lower():
                await event.reply("❌ Не можна видалити себе")
                return
            admins = config.get("admins", [])
            new_admins = [a for a in admins if a.lower() != arg.lower()]
            if len(new_admins) < len(admins):
                config["admins"] = new_admins
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{arg}**")
            else:
                await event.reply("❌ Адміна не знайдено")

        # === Ключові слова ===
        elif cmd == "/add_word":
            if not arg:
                await event.reply("❌ /add_word <слово>")
                return
            kw = config.get("keywords", [])
            if arg.lower() in {w.lower() for w in kw}:
                await event.reply("⚠️ Вже є")
            else:
                kw.append(arg)
                config["keywords"] = kw
                await update_config_fn(config)
                await event.reply(f"✅ Додано: **{arg}**")

        elif cmd == "/del_word":
            kw = config.get("keywords", [])
            new_kw = [w for w in kw if w.lower() != arg.lower()]
            if len(new_kw) < len(kw):
                config["keywords"] = new_kw
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{arg}**")
            else:
                await event.reply("❌ Не знайдено")

        # === Мінус-слова ===
        elif cmd == "/add_minus":
            if not arg:
                await event.reply("❌ /add_minus <слово>")
                return
            mw = config.get("minus_words", [])
            if arg.lower() in {w.lower() for w in mw}:
                await event.reply("⚠️ Вже є")
            else:
                mw.append(arg)
                config["minus_words"] = mw
                await update_config_fn(config)
                await event.reply(f"✅ Додано мінус-слово: **{arg}**")

        elif cmd == "/del_minus":
            mw = config.get("minus_words", [])
            new_mw = [w for w in mw if w.lower() != arg.lower()]
            if len(new_mw) < len(mw):
                config["minus_words"] = new_mw
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{arg}**")
            else:
                await event.reply("❌ Не знайдено")

        # === Skip-слова ===
        elif cmd == "/add_skip":
            if not arg:
                await event.reply("❌ /add_skip <слово>")
                return
            sw = config.get("skip_words", [])
            if arg.lower() in {w.lower() for w in sw}:
                await event.reply("⚠️ Вже є")
            else:
                sw.append(arg)
                config["skip_words"] = sw
                await update_config_fn(config)
                await event.reply(f"✅ Додано skip: **{arg}**")

        elif cmd == "/del_skip":
            if not arg:
                await event.reply("❌ /del_skip <слово>")
                return
            sw = config.get("skip_words", [])
            new_sw = [w for w in sw if w.lower() != arg.lower()]
            if len(new_sw) < len(sw):
                config["skip_words"] = new_sw
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{arg}**")
            else:
                await event.reply("❌ Не знайдено")

        # === Статус черги ===
        elif cmd == "/queue_status":
            await event.reply(
                f"📊 **Черга пересилки:**\n"
                f"📥 У черзі: {pending_messages.qsize()} повідомлень\n"
                f"📢 Канал: {config.get('forward_channel', 'не встановлено')}\n"
                f"⏱ Затримка: 3 сек"
            )

        # === Очищення minus_words ===
        elif cmd == "/clean_minus":
            old = config.get("minus_words", [])
            new = clean_minus_words_fn(old, config.get("skip_words", []), config.get("keywords", []))
            diff = len(old) - len(new)
            config["minus_words"] = new
            await update_config_fn(config)
            await event.reply(
                f"🧹 Очищено minus_words\n"
                f"Було: {len(old)} | Стало: {len(new)} | Видалено: {diff}"
            )

        # === Список налаштувань ===
        elif cmd == "/list":
            kw = "\n".join(f"  • {w}" for w in config.get("keywords", [])) or "  (пусто)"
            mw = "\n".join(f"  • {w}" for w in config.get("minus_words", [])) or "  (пусто)"
            sw = "\n".join(f"  • {w}" for w in config.get("skip_words", [])) or "  (пусто)"
            jq = "\n".join(f"  • {g}" for g in config.get("join_queue", [])) or "  (пусто)"
            adm = "\n".join(f"  • {a}" for a in config.get("admins", [])) or "  (пусто)"
            ch = config.get("forward_channel", "не встановлено")
            ai_st = "🟢 УВІМКНЕНА" if config.get("ai_filter_enabled") else "🔴 ВИМКНЕНА"

            # Евристичний фільтр
            triggers = "\n".join(f"  • {t}" for t in config.get("spam_commercial_triggers", [])) or "  (пусто)"
            services = "\n".join(f"  • {s}" for s in config.get("spam_services", [])) or "  (пусто)"
            emojis = config.get("spam_emojis", "") or "(пусто)"
            threshold = config.get("spam_score_threshold", 4)

            text_out = (
                f"📋 **Поточні налаштування:**\n\n"
                f"👤 Адміни:\n{adm}\n\n"
                f"📢 Канал пересилки: {ch}\n\n"
                f"🤖 AI фільтрація: {ai_st}\n\n"
                f"🔍 Ключові слова:\n{kw}\n\n"
                f"🚫 Мінус-слова:\n{mw}\n\n"
                f"⏭️ Skip-слова:\n{sw}\n\n"
                f"🛡 **Евристичний фільтр** (поріг: {threshold}):\n\n"
                f"📍 Спам-тригери:\n{triggers}\n\n"
                f"💼 Спам-сервіси:\n{services}\n\n"
                f"🎭 Спам-емодзі: {emojis}\n\n"
                f"📥 Черга груп:\n{jq}"
            )
            await send_long_message(bot_client, event.chat_id, text_out)

        # === Евристичний фільтр ===
        elif cmd == "/spam_triggers":
            triggers = config.get("spam_commercial_triggers", [])
            if not triggers:
                await event.reply("🛡 Спам-тригери: (пусто)")
            else:
                lines = "\n".join(f"  {i+1}. `{t}`" for i, t in enumerate(triggers))
                await send_long_message(bot_client, event.chat_id, f"🛡 **Спам-тригери ({len(triggers)}):**\n\n{lines}")

        elif cmd == "/add_trigger":
            if not arg:
                await event.reply("❌ /add_trigger <regex патерн>")
                return
            triggers = config.get("spam_commercial_triggers", [])
            if arg in triggers:
                await event.reply("⚠️ Вже є")
            else:
                triggers.append(arg)
                config["spam_commercial_triggers"] = triggers
                await update_config_fn(config)
                await event.reply(f"✅ Додано тригер: `{arg}`")

        elif cmd == "/del_trigger":
            if not arg:
                await event.reply("❌ /del_trigger <номер або текст>")
                return
            triggers = config.get("spam_commercial_triggers", [])
            # Дозволити видалення за номером або текстом
            if arg.isdigit() and 1 <= int(arg) <= len(triggers):
                removed = triggers.pop(int(arg) - 1)
                config["spam_commercial_triggers"] = triggers
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено тригер: `{removed}`")
            else:
                new_t = [t for t in triggers if t != arg]
                if len(new_t) < len(triggers):
                    config["spam_commercial_triggers"] = new_t
                    await update_config_fn(config)
                    await event.reply(f"🗑 Видалено: `{arg}`")
                else:
                    await event.reply("❌ Не знайдено")

        elif cmd == "/spam_services":
            services = config.get("spam_services", [])
            if not services:
                await event.reply("🛡 Спам-сервіси: (пусто)")
            else:
                lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(services))
                await send_long_message(bot_client, event.chat_id, f"🛡 **Спам-сервіси ({len(services)}):**\n\n{lines}")

        elif cmd == "/add_service":
            if not arg:
                await event.reply("❌ /add_service <назва>")
                return
            services = config.get("spam_services", [])
            if arg.lower() in {s.lower() for s in services}:
                await event.reply("⚠️ Вже є")
            else:
                services.append(arg.lower())
                config["spam_services"] = services
                await update_config_fn(config)
                await event.reply(f"✅ Додано сервіс: **{arg}**")

        elif cmd == "/del_service":
            if not arg:
                await event.reply("❌ /del_service <назва або номер>")
                return
            services = config.get("spam_services", [])
            if arg.isdigit() and 1 <= int(arg) <= len(services):
                removed = services.pop(int(arg) - 1)
                config["spam_services"] = services
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{removed}**")
            else:
                new_s = [s for s in services if s.lower() != arg.lower()]
                if len(new_s) < len(services):
                    config["spam_services"] = new_s
                    await update_config_fn(config)
                    await event.reply(f"🗑 Видалено: **{arg}**")
                else:
                    await event.reply("❌ Не знайдено")

        elif cmd == "/spam_emojis":
            if arg:
                config["spam_emojis"] = arg
                await update_config_fn(config)
                await event.reply(f"✅ Спам-емодзі встановлено: {arg}")
            else:
                emojis = config.get("spam_emojis", "")
                await event.reply(f"🎭 **Спам-емодзі:** {emojis}\n\n/spam_emojis <символи> — змінити" if emojis else "🎭 Спам-емодзі: (пусто)")

        elif cmd == "/spam_threshold":
            if arg:
                try:
                    val = int(arg)
                    config["spam_score_threshold"] = val
                    await update_config_fn(config)
                    await event.reply(f"✅ Поріг спам-фільтру: **{val}**")
                except ValueError:
                    await event.reply("❌ Вкажи число: /spam_threshold 4")
            else:
                val = config.get("spam_score_threshold", 4)
                await event.reply(f"🎯 **Поріг спам-фільтру:** {val}\n\n/spam_threshold <число> — змінити")

        # === Групи (використовує user_client) ===
        elif cmd == "/join":
            if not arg:
                await event.reply("❌ /join @група")
                return
            try:
                from telethon.tl.functions.channels import JoinChannelRequest
                await user_client(JoinChannelRequest(arg))
                await event.reply(f"✅ Вступив: **{arg}**")
            except Exception as exc:
                await event.reply(f"❌ Помилка: {exc}")

        elif cmd == "/leave":
            if not arg:
                await event.reply("❌ /leave @група")
                return
            try:
                from telethon.tl.functions.channels import LeaveChannelRequest
                await user_client(LeaveChannelRequest(arg))
                await event.reply(f"✅ Вийшов: **{arg}**")
            except Exception as exc:
                await event.reply(f"❌ Помилка: {exc}")

        elif cmd == "/join_add":
            if not arg:
                await event.reply("❌ /join_add @г1 @г2 …")
                return
            new_groups = [g.strip() for g in arg.replace("\n", " ").split() if g.startswith("@")]
            if not new_groups:
                await event.reply("❌ Групи мають починатися з @")
                return

            queue = config.get("join_queue", [])
            q_lower = {g.lower() for g in queue}
            added, skipped = [], []
            for g in new_groups:
                if g.lower() not in q_lower:
                    queue.append(g)
                    q_lower.add(g.lower())
                    added.append(g)
                else:
                    skipped.append(g)

            config["join_queue"] = queue
            await update_config_fn(config)

            msg = ""
            if added:
                msg += f"✅ Додано ({len(added)}):\n" + "\n".join(f"  • {g}" for g in added)
            if skipped:
                msg += f"\n\n⚠️ Вже були ({len(skipped)}):\n" + "\n".join(f"  • {g}" for g in skipped)
            msg += f"\n\n📥 Всього: {len(queue)} груп. /join_all — вступити у всі"
            await send_long_message(bot_client, event.chat_id, msg)

        elif cmd == "/join_del":
            if not arg:
                await event.reply("❌ /join_del @група")
                return
            queue = config.get("join_queue", [])
            new_q = [g for g in queue if g.lower() != arg.lower()]
            if len(new_q) < len(queue):
                config["join_queue"] = new_q
                await update_config_fn(config)
                await event.reply(f"🗑 Видалено: **{arg}**")
            else:
                await event.reply("❌ Не знайдено в черзі")

        elif cmd == "/join_list":
            queue = config.get("join_queue", [])
            if not queue:
                await event.reply("📭 Черга порожня. /join_add @г1 @г2")
                return
            lines = "\n".join(f"  {i + 1}. {g}" for i, g in enumerate(queue))
            await send_long_message(bot_client, event.chat_id,
                                    f"📥 **Черга ({len(queue)}):**\n\n{lines}\n\n/join_all — вступити у всі")

        elif cmd == "/join_all":
            queue = config.get("join_queue", [])
            if not queue:
                await event.reply("📭 Черга порожня")
                return
            await event.reply(f"🚀 Вступаю у {len(queue)} груп(и) у фоні…")

            async def _join_bg():
                from telethon.tl.functions.channels import JoinChannelRequest
                success, failed = [], []
                for i, group in enumerate(queue, 1):
                    try:
                        await user_client(JoinChannelRequest(group))
                        success.append(group)
                        await safe_send(bot_client, event.chat_id, f"✅ [{i}/{len(queue)}] Вступив: {group}")
                    except Exception as exc:
                        failed.append(f"{group} — {exc}")
                        await safe_send(bot_client, event.chat_id, f"❌ [{i}/{len(queue)}] Помилка: {group}\n{exc}")
                    await asyncio.sleep(15)
                fresh = load_config_fn()
                fresh["join_queue"] = [g for g in fresh.get("join_queue", []) if g not in success]
                await update_config_fn(fresh)
                msg_result = f"🏁 **Готово!**\n✅ Вступив: {len(success)}\n❌ Помилок: {len(failed)}"
                if failed:
                    msg_result += "\n\n❌ Не вдалось:\n" + "\n".join(f"  • {f}" for f in failed)
                await safe_send(bot_client, event.chat_id, msg_result)

            asyncio.create_task(_join_bg())

        elif cmd == "/groups":
            dialogs = await user_client.get_dialogs()
            groups = [d for d in dialogs if d.is_group or d.is_channel]
            if not groups:
                await event.reply("📭 Немає груп/каналів")
                return
            lines = "\n".join(
                f"  • {g.title} (@{g.entity.username})" if getattr(g.entity, "username", None)
                else f"  • {g.title}"
                for g in groups
            )
            await send_long_message(bot_client, event.chat_id, f"📋 **Групи ({len(groups)}):**\n\n{lines}")

        # === Статистика з логів ===
        elif cmd == "/stats":
            # /stats або /stats 7 або /stats 30
            days = 1
            if arg:
                if arg in ("тиждень", "week", "7"):
                    days = 7
                elif arg in ("місяць", "month", "30"):
                    days = 30
                elif arg.isdigit():
                    days = int(arg)

            await event.reply(f"⏳ Збираю статистику за {days} днів…")
            s = _collect_log_stats(days)
            total = s['queued'] + s['local_blocked']
            total_blocked = s['local_blocked'] + s['ai_blocked']

            period_name = "сьогодні" if days == 1 else f"за {days} днів"
            text_out = (
                f"📊 **Статистика {period_name}:**\n\n"
                f"📥 В чергу (пройшли базовий фільтр): **{s['queued']}**\n"
                f"✅ Переслано в канал: **{s['forwarded']}**\n\n"
                f"❌ **Заблоковано всього: {total_blocked}**\n"
                f"  🛑 Локальний фільтр: {s['local_blocked']}\n"
                f"  🤖 AI фільтр: {s['ai_blocked']}\n\n"
                f"📝 Всього оброблено: {total + s['ai_blocked']}\n\n"
                f"/blocked — список заблокованих"
            )
            await event.reply(text_out)

        elif cmd == "/blocked":
            days = 1
            if arg:
                if arg in ("тиждень", "week", "7"):
                    days = 7
                elif arg in ("місяць", "month", "30"):
                    days = 30
                elif arg.isdigit():
                    days = int(arg)

            blocked = _collect_blocked_messages(days, limit=50)
            if not blocked:
                await event.reply(f"✅ За {days} днів немає заблокованих повідомлень")
            else:
                lines = "\n".join(blocked)
                header = f"🚫 **Заблоковано за {days} днів ({len(blocked)}):**\n\n"
                await send_long_message(bot_client, event.chat_id, header + lines)

        elif cmd == "/help" or cmd == "/start":
            help_text = (
                "📖 **Команди управління:**\n\n"
                "🤖 **AI Фільтрація:**\n"
                "/ai_enable — увімкнути\n"
                "/ai_disable — вимкнути\n"
                "/ai_set_key [ключ] — OpenAI API ключ\n"
                "/ai_set_model [модель] — модель OpenAI\n"
                "/ai_set_role [текст] — задати роль AI\n"
                "/ai_get_role — поточна роль AI\n"
                "/ai_set_target [текст] — критерії цільового\n"
                "/ai_get_target — поточні критерії цільового\n"
                "/ai_set_spam [текст] — критерії спаму\n"
                "/ai_get_spam — поточні критерії спаму\n"
                "/ai_status — статус і статистика\n"
                "/ai_test [текст] — протестувати\n\n"
                "👤 **Адміни:**\n"
                "/add_admin @user — додати\n"
                "/del_admin @user — видалити\n\n"
                "📢 **Канал:**\n"
                "/set_channel @к — встановити\n"
                "/get_channel — поточний\n"
                "/queue_status — статус черги\n\n"
                "🔍 **Ключові слова:**\n"
                "/add_word [слово] — додати\n"
                "/del_word [слово] — видалити\n\n"
                "🚫 **Мінус-слова:**\n"
                "/add_minus [слово] — додати\n"
                "/del_minus [слово] — видалити\n"
                "/clean_minus — очистити дублі/skip\n\n"
                "⏭️ **Skip-слова:**\n"
                "/add_skip [слово] — додати\n"
                "/del_skip [слово] — видалити\n\n"
                "🛡 **Евристичний фільтр:**\n"
                "/spam_triggers — список тригерів\n"
                "/add_trigger [regex] — додати\n"
                "/del_trigger [№|текст] — видалити\n"
                "/spam_services — список сервісів\n"
                "/add_service [назва] — додати\n"
                "/del_service [№|назва] — видалити\n"
                "/spam_emojis [символи] — показати/задати\n"
                "/spam_threshold [число] — поріг\n\n"
                "📥 **Групи:**\n"
                "/join_add @г1 @г2 — додати в чергу\n"
                "/join_del @г — видалити з черги\n"
                "/join_list — показати чергу\n"
                "/join_all — вступити у всі\n"
                "/join @г — вступити в одну\n"
                "/leave @г — вийти\n\n"
                "⚙️ **Інше:**\n"
                "/groups — всі групи\n"
                "/stats [дні] — статистика (сьогодні/7/30)\n"
                "/blocked [дні] — список заблокованих\n"
                "/list — всі налаштування\n"
                "/help — ця довідка"
            )
            
            help_text = "👋 Привітання!" + "\n\n"+ help_text if cmd == "/start" else help_text
            await send_long_message(bot_client, event.chat_id, help_text)
