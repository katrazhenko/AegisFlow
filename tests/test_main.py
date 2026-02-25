"""
Тести для рефакторингу monitor bot.
Запуск: python -m pytest tests.py -v
"""

import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch


# ── Імпортуємо лише чисті функції (без Telethon/asyncio startup) ──
import importlib, sys, types

# Мінімальний stub щоб обійти запуск asyncio.run(main()) при імпорті
telethon_stub = types.ModuleType("telethon")
telethon_stub.TelegramClient = MagicMock()
telethon_stub.events = MagicMock()
telethon_stub.Button = MagicMock()
telethon_events = types.ModuleType("telethon.events")
telethon_events.NewMessage = MagicMock(return_value=lambda f: f)
telethon_events.CallbackQuery = MagicMock(return_value=lambda f: f)
telethon_stub.events = telethon_events
telethon_tl = types.ModuleType("telethon.tl")
telethon_tl_funcs = types.ModuleType("telethon.tl.functions")
telethon_tl_channels = types.ModuleType("telethon.tl.functions.channels")
telethon_tl_channels.JoinChannelRequest  = MagicMock()
telethon_tl_channels.LeaveChannelRequest = MagicMock()
telethon_errors = types.ModuleType("telethon.errors")
telethon_errors.FloodWaitError = Exception
telethon_tl_types = types.ModuleType("telethon.tl.types")
telethon_tl_types.InputChannel = MagicMock()

sys.modules.setdefault("telethon",                          telethon_stub)
sys.modules.setdefault("telethon.events",                   telethon_events)
sys.modules.setdefault("telethon.tl",                       telethon_tl)
sys.modules.setdefault("telethon.tl.functions",             telethon_tl_funcs)
sys.modules.setdefault("telethon.tl.functions.channels",    telethon_tl_channels)
sys.modules.setdefault("telethon.errors",                   telethon_errors)
sys.modules.setdefault("telethon.tl.types",                 telethon_tl_types)

# Задаємо env щоб не впав при перевірці
import os
os.environ.setdefault("TG_API_ID",   "12345678")
os.environ.setdefault("TG_API_HASH", "deadbeef")
os.environ.setdefault("TG_PHONE",    "+34600000000")

# Патчимо asyncio.run щоб main() не запустилась при імпорті
with patch("asyncio.run"):
    # Патчимо LOCK_FILE щоб не створювати файл
    with patch("pathlib.Path.exists", return_value=False), \
         patch("pathlib.Path.write_text"):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "main_module",
            pathlib.Path(__file__).parent.parent / "main.py"
        )
        main_module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(main_module)
        except SystemExit:
            pass

clean_minus_words = main_module.clean_minus_words
has_minus_word    = main_module.has_minus_word
find_keyword      = main_module.find_keyword
format_sender     = main_module.format_sender
format_chat       = main_module.format_chat
is_admin          = main_module.is_admin


# ════════════════════════════════════════════════════════════════
# clean_minus_words
# ════════════════════════════════════════════════════════════════
class TestCleanMinusWords:
    def test_removes_skip_words_from_minus(self):
        minus  = ["buy usdt now"]
        skip   = ["buy", "now"]
        result = clean_minus_words(minus, skip, [])
        assert result == ["usdt"]

    def test_removes_keyword_from_minus(self):
        minus    = ["keyword spam"]
        keywords = ["keyword"]
        result   = clean_minus_words(minus, [], keywords)
        assert result == ["spam"]

    def test_deduplication(self):
        minus  = ["spam", "SPAM", "Spam"]
        result = clean_minus_words(minus, [], [])
        assert len(result) == 1

    def test_removes_empty_after_cleaning(self):
        minus  = ["the"]
        skip   = ["the"]
        result = clean_minus_words(minus, skip, [])
        assert result == []

    def test_preserves_untouched_entries(self):
        minus  = ["garantizado", "скидка"]
        result = clean_minus_words(minus, ["the"], [])
        assert result == ["garantizado", "скидка"]

    def test_empty_lists(self):
        assert clean_minus_words([], [], []) == []

    def test_multiword_minus_partial_removal(self):
        minus  = ["buy usdt p2p"]
        skip   = ["buy", "p2p"]
        result = clean_minus_words(minus, skip, [])
        assert result == ["usdt"]


# ════════════════════════════════════════════════════════════════
# has_minus_word
# ════════════════════════════════════════════════════════════════
class TestHasMinusWord:
    def test_detects_phrase(self):
        assert has_minus_word("Помогите получить сиру garantizado!", ["garantizado"])

    def test_detects_multi_word_phrase(self):
        assert has_minus_word("comprar usdt p2p deal", ["usdt p2p"])

    def test_no_match(self):
        assert not has_minus_word("Помогите с ситой пожалуйста", ["garantizado", "bitcoin"])

    def test_case_insensitive(self):
        assert has_minus_word("GARANTIZADO resultado", ["garantizado"])

    def test_empty_minus(self):
        assert not has_minus_word("будь-яке повідомлення", [])

    def test_empty_text(self):
        assert not has_minus_word("", ["spam"])


# ════════════════════════════════════════════════════════════════
# find_keyword
# ════════════════════════════════════════════════════════════════
class TestFindKeyword:
    KEYWORDS = ["urgent request", "delivery", "refund", "повернення коштів"]

    def test_finds_exact_keyword(self):
        assert find_keyword("I need an urgent request please", self.KEYWORDS) == "urgent request"

    def test_finds_single_word_keyword(self):
        assert find_keyword("My delivery arrived", self.KEYWORDS) == "delivery"

    def test_returns_none_when_not_found(self):
        assert find_keyword("Hola, ¿cómo estás?", self.KEYWORDS) is None

    def test_case_insensitive_match(self):
        assert find_keyword("Need a REFUND now", self.KEYWORDS) == "refund"

    def test_cyrillic_keyword(self):
        assert find_keyword("хочу повернення коштів будь ласка", self.KEYWORDS) == "повернення коштів"

    def test_partial_word_no_match(self):
        # "deliveries" — not full word "delivery" by word-boundary
        # Testing \b behavior
        result = find_keyword("my deliveries arrived", ["delivery"])
        # \b should not match partial word
        assert result is None

    def test_returns_first_match(self):
        kw  = ["refund", "refund request"]
        res = find_keyword("I want refund request", kw)
        assert res in kw   # будь-яке зі збігів

    def test_empty_text(self):
        assert find_keyword("", self.KEYWORDS) is None

    def test_empty_keywords(self):
        assert find_keyword("urgent request", []) is None


# ════════════════════════════════════════════════════════════════
# format_sender
# ════════════════════════════════════════════════════════════════
class TestFormatSender:
    def _make_sender(self, first="", last="", username=None, uid=None):
        s = MagicMock()
        s.first_name = first
        s.last_name  = last
        s.username   = username
        s.id         = uid
        return s

    def test_full_name_with_username(self):
        s = self._make_sender("John", "Doe", username="johnd")
        assert "John Doe" in format_sender(s)
        assert "@johnd" in format_sender(s)

    def test_only_first_name_no_username(self):
        s = self._make_sender("Jane", uid=123456)
        result = format_sender(s)
        assert "Jane" in result
        assert "123456" in result

    def test_no_name_no_username_has_id(self):
        s = self._make_sender(uid=9999)
        assert "9999" in format_sender(s)

    def test_all_empty(self):
        s = self._make_sender()
        result = format_sender(s)
        assert result == ""  # нічого не заповнено


# ════════════════════════════════════════════════════════════════
# format_chat
# ════════════════════════════════════════════════════════════════
class TestFormatChat:
    def _make_chat(self, title="", username=None):
        c = MagicMock()
        c.title    = title
        c.username = username
        return c

    def test_title_with_username(self):
        c = self._make_chat("Tech Community", "tech_comm")
        result = format_chat(c)
        assert "Tech Community" in result
        assert "@tech_comm" in result

    def test_title_without_username(self):
        c = self._make_chat("Private Group")
        result = format_chat(c)
        assert "Private Group" in result
        assert "@" not in result


# ════════════════════════════════════════════════════════════════
# is_admin
# ════════════════════════════════════════════════════════════════
class TestIsAdmin:
    ADMINS = ["@admin_user1", "@admin_user2"]

    def test_valid_admin(self):
        assert is_admin("admin_user1", self.ADMINS)

    def test_case_insensitive(self):
        assert is_admin("ADMIN_USER2", self.ADMINS)

    def test_not_admin(self):
        assert not is_admin("randomuser", self.ADMINS)

    def test_empty_admins(self):
        assert not is_admin("admin_user1", [])


# ════════════════════════════════════════════════════════════════
# Інтеграційні: clean_minus + has_minus
# ════════════════════════════════════════════════════════════════
class TestIntegration:
    def test_after_cleaning_minus_no_false_positive(self):
        """After cleaning 'keyword spam' -> 'spam',
           message with 'keyword' is not blocked by minus word."""
        raw_minus = ["keyword spam"]
        keywords  = ["keyword"]
        cleaned   = clean_minus_words(raw_minus, [], keywords)
        # cleaned == ["spam"]
        msg = "I need keyword help"
        assert not has_minus_word(msg, cleaned), (
            "After cleaning, message with keyword should not be blocked"
        )

    def test_spam_still_blocked_after_cleaning(self):
        raw_minus = ["guaranteed money fast"]
        skip      = ["money"]
        cleaned   = clean_minus_words(raw_minus, skip, [])
        # cleaned == ["guaranteed fast"]
        msg = "Get guaranteed fast results today"
        assert has_minus_word(msg, cleaned)

send_long_message = main_module.send_long_message


# ════════════════════════════════════════════════════════════════
# Edge cases: has_minus_word — unicode & punctuation
# ════════════════════════════════════════════════════════════════
class TestHasMinusWordEdgeCases:
    def test_cyrillic_minus_word(self):
        assert has_minus_word("Buy USDT fast", ["buy usdt"])

    def test_minus_word_with_punctuation(self):
        assert has_minus_word("Guaranteed! your result", ["guaranteed"])

    def test_substring_match(self):
        """minus_words uses substring match, not word-boundary."""
        assert has_minus_word("cryptocurrency trading", ["crypto"])

    def test_multiline_text(self):
        text = "Первая строка\nвторая строка con bitcoin\nтретья"
        assert has_minus_word(text, ["bitcoin"])


# ════════════════════════════════════════════════════════════════
# Edge cases: find_keyword — special chars & unicode
# ════════════════════════════════════════════════════════════════
class TestFindKeywordEdgeCases:
    def test_keyword_with_accent(self):
        assert find_keyword("I need cancellation urgently", ["cancellation"]) == "cancellation"

    def test_keyword_not_as_substring(self):
        """Word boundary prevents matching 'cat' inside 'category'."""
        assert find_keyword("select category now", ["cat"]) is None

    def test_numbers_in_keyword(self):
        assert find_keyword("please use model 500 today", ["model 500"]) == "model 500"


# ════════════════════════════════════════════════════════════════
# Edge cases: format_sender & format_chat
# ════════════════════════════════════════════════════════════════
class TestFormatEdgeCases:
    def test_sender_with_none_first_name(self):
        s = MagicMock()
        s.first_name = None
        s.last_name = None
        s.username = "testuser"
        s.id = 42
        result = format_sender(s)
        assert "@testuser" in result

    def test_chat_with_none_title(self):
        c = MagicMock()
        c.title = None
        c.username = "channel_name"
        result = format_chat(c)
        assert "@channel_name" in result


# ════════════════════════════════════════════════════════════════
# is_admin edge cases
# ════════════════════════════════════════════════════════════════
class TestIsAdminEdgeCases:
    def test_admin_with_empty_username(self):
        assert not is_admin("", ["@admin"])

    def test_admin_list_with_no_at_prefix(self):
        """Admins in config should include @, but if they don't — no match."""
        assert not is_admin("user123", ["user123"])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
