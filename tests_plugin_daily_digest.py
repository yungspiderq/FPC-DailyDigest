"""
Функциональные тесты плагина daily_digest.py на моках.
Запуск: cd FPC && python3 tests_plugin_daily_digest.py
"""
import importlib.util
import json
import os
import sys
import time
from configparser import ConfigParser
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

from FunPayAPI import types as fp_types
from FunPayAPI.common import enums as fp_enums
from FunPayAPI.common.enums import OrderStatuses

spec = importlib.util.spec_from_file_location("plugins.daily_digest", "plugins/daily_digest.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.daily_digest"] = plugin
spec.loader.exec_module(plugin)

plugin._digest_loop = lambda crd: None  # поток тестируем через _build_digest / _send_digest_now

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


PRODUCTS_DIR = "storage/products"
F_EMPTY = "dig_test_empty.txt"
F_LOW = "dig_test_low.txt"
F_OK = "dig_test_ok.txt"


def write_products(fname, n):
    os.makedirs(PRODUCTS_DIR, exist_ok=True)
    with open(os.path.join(PRODUCTS_DIR, fname), "w", encoding="utf-8") as f:
        f.write("\n".join(f"p{i}" for i in range(n)) + ("\n" if n else ""))


def cleanup_files():
    if os.path.isdir(PRODUCTS_DIR):
        for fname in os.listdir(PRODUCTS_DIR):
            if fname.startswith("dig_test_"):
                try:
                    os.remove(os.path.join(PRODUCTS_DIR, fname))
                except OSError:
                    pass


def mk_order(id_, price, days_ago_msk, status=OrderStatuses.CLOSED, now_msk=None, hour=12):
    now_msk = now_msk or datetime(2026, 10, 16, 9, 0, tzinfo=plugin.MSK)
    d = (now_msk - timedelta(days=days_ago_msk)).replace(hour=hour, minute=0,
                                                          tzinfo=None)
    return fp_types.OrderShortcut(id_, "Аккаунт Steam", price, fp_enums.Currency.RUB,
                                  "buyer1", 777, 5555, status, d, "Аккаунты", None, "",
                                  dont_search_amount=True)


class MockAccount:
    """get_sales переключается по include_closed: True — история, False — активные."""

    def __init__(self, history=None, active=None):
        self.is_initiated = True
        self.history_pages = history or {None: (None, [])}
        self.active_pages = active or {None: (None, [])}
        self.calls = []

    def get_sales(self, start_from=None, include_closed=True, **kw):
        self.calls.append(("history" if include_closed else "active", start_from))
        pages = self.history_pages if include_closed else self.active_pages
        if start_from in pages:
            return pages[start_from]
        return None, [], "ru", {}


def make_ad_cfg():
    cfg = ConfigParser(delimiters=(":",), interpolation=None)
    cfg.optionxform = str
    cfg["Lot Empty"] = {"response": "$product", "productsFileName": F_EMPTY}
    cfg["Lot Low"] = {"response": "$product", "productsFileName": F_LOW}
    cfg["Lot OK"] = {"response": "$product", "productsFileName": F_OK}
    return cfg


class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notification_settings, self.notifications = {}, []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, notification_type=None, **kwargs):
        self.notifications.append((text, notification_type))


class MockCardinal:
    def __init__(self, account, telegram, balance=None):
        self.account = account
        self.telegram = telegram
        self.AD_CFG = make_ad_cfg()
        self.balance = balance
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


NOW = datetime(2026, 10, 16, 9, 0, tzinfo=plugin.MSK)  # «сейчас»: 16.10.2026 09:00 МСК

# ═══════════════════════ ТЕСТ 1: метаданные и утилиты ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 _parse_hhmm", plugin._parse_hhmm("09:00") == (9, 0) and plugin._parse_hhmm("9:5") == (9, 5)
      and plugin._parse_hhmm("25:00") is None and plugin._parse_hhmm("abc") is None)

# ═══════════════════════ ТЕСТ 2: построение дайджеста ═══════════════════════
write_products(F_EMPTY, 0)
write_products(F_LOW, 2)
write_products(F_OK, 10)

history = {None: (None, [
    mk_order("Y1", 100.0, 1, OrderStatuses.CLOSED, NOW, hour=18),   # вчера 15.10
    mk_order("Y2", 250.0, 1, OrderStatuses.PAID, NOW, hour=12),      # вчера
    mk_order("Y3", 500.0, 1, OrderStatuses.REFUNDED, NOW, hour=10),  # вчера, возврат
    mk_order("D1", 200.0, 2, OrderStatuses.CLOSED, NOW, hour=15),    # позавчера 14.10
])}
active = {None: (None, [
    mk_order("A1", 300.0, 0, OrderStatuses.PAID, NOW, hour=4),  # висит ~5 часов
])}
balance = SimpleNamespace(total_rub=1000.0, available_rub=800.0,
                          total_usd=0.0, available_usd=0.0, total_eur=0.0, available_eur=0.0)
acc = MockAccount(history, active)
crd = MockCardinal(acc, None, balance)
reset_settings(stock_threshold=5)

text = plugin._build_digest(crd, now_msk=NOW)
check("2.1 продажи за вчера: 2 заказа на 350 ₽", "2 заказ" in text and "350 ₽" in text)
check("2.2 возвраты за вчера учтены отдельно", "Возвраты за вчера: <b>1</b>" in text)
check("2.3 сравнение с позавчера и тренд +75%", "Позавчера: 1 на 200 ₽" in text and "📈 +75%" in text)
check("2.4 активные заказы + возраст старейшего", "Активные заказы:</b> 1" in text and "5 ч." in text)
check("2.5 остатки: пустые и низкие", "Пустые: 1" in text and "Ниже 5 шт.: 1" in text)
check("2.6 баланс с суммой и доступной к выводу", "1000 ₽" in text and "800" in text)
check("2.7 история запрошена, активные — отдельным запросом",
      any(k == "history" for k, _ in acc.calls) and any(k == "active" for k, _ in acc.calls))

# нет продаж вчера
acc2 = MockAccount({None: (None, [mk_order("D1", 200.0, 2, OrderStatuses.CLOSED, NOW)])},
                   {None: (None, [])})
crd2 = MockCardinal(acc2, None, None)
text = plugin._build_digest(crd2, now_msk=NOW)
check("2.8 без продаж вчера — корректный текст", "продаж не было" in text)
check("2.9 без баланса — секция пропускается", "Баланс" not in text)

# ранняя остановка пагинации на старых заказах
old_only = {None: ("NEXT", [mk_order("O1", 50.0, 30, OrderStatuses.CLOSED, NOW)]),
            "NEXT": (None, [mk_order("O2", 60.0, 40, OrderStatuses.CLOSED, NOW)])}
acc3 = MockAccount(old_only, {None: (None, [])})
crd3 = MockCardinal(acc3, None, None)
plugin._build_digest(crd3, now_msk=NOW)
hist_calls = [c for c in acc3.calls if c[0] == "history"]
check("2.10 ранняя остановка: вторая страница истории не запрашивается", len(hist_calls) == 1)

# ═══════════════════════ ТЕСТ 3: отправка ═══════════════════════
tg = MockTelegram()
crd_t = MockCardinal(MockAccount(history, active), tg, balance)
reset_settings()
plugin._send_digest_now(crd_t)
check("3.1 _send_digest_now шлет уведомление", len(tg.notifications) == 1
      and "Утренняя сводка" in tg.notifications[0][0])
# повторный вызов не блокируется (lock освобожден)
tg.notifications.clear()
plugin._send_digest_now(crd_t)
check("3.2 повторная отправка работает (busy-lock освобождается)", len(tg.notifications) == 1)

# ═══════════════════════ ТЕСТ 4: Telegram UI ═══════════════════════
tg = MockTelegram()
crd_u = MockCardinal(MockAccount(history, active), tg, balance)
plugin.init(crd_u)
bot = tg.bot
reset_settings()
check("4.1 команда /digest зарегистрирована",
      any(kw.get("commands") == ["digest"] for _, kw in tg.msg_handlers))


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("4.2 страница настроек открывает меню", name == "open_home" and "Утренний дайджест" in last_text())
check("4.3 в меню статус, время и последняя отправка",
      "Дайджест: <b>ВКЛ" in last_text() and "09:00" in last_text())

name = dispatch_cb(plugin.CB_TOG)
check("4.4 дайджест переключается", name == "toggle_enabled"
      and plugin._load_settings()["enabled"] is False)
dispatch_cb(plugin.CB_TOG)
check("4.5 и включается обратно", plugin._load_settings()["enabled"] is True)

dispatch_cb(plugin.CB_TIME)
st = tg.user_states.get((111, 222), {})
check("4.6 кнопка времени ставит состояние", st.get("state") == plugin.ST_TIME)
dispatch_msg("08:30")
check("4.7 время сохранено", plugin._load_settings()["time"] == "08:30")
dispatch_cb(plugin.CB_TIME)
dispatch_msg("99:99")
check("4.8 некорректное время отклоняется", plugin._load_settings()["time"] == "08:30"
      and "❌" in bot.replies[-1])

dispatch_cb(plugin.CB_THRESH)
dispatch_msg("10")
check("4.9 порог остатков сохранен", plugin._load_settings()["stock_threshold"] == 10)

tg.notifications.clear()
name = dispatch_cb(plugin.CB_NOW)
time.sleep(0.7)
check("4.10 «Отправить сейчас» шлет сводку уведомлением",
      name == "send_now" and any("Утренняя сводка" in t for t, _ in tg.notifications))
dispatch_cb(plugin.CB_NOOP)
check("4.11 noop отвечает", True)

# ═══════════════════════ ТЕСТ 5: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"enabled": 1, "time": "99:99", "last_date": None, "stock_threshold": "abc"}, f)
s = plugin._load_settings()
check("5.1 битые значения нормализуются", s["enabled"] is True and s["time"] == "09:00"
      and s["last_date"] == "" and s["stock_threshold"] == 5)
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("5.2 битый JSON → дефолты", s["time"] == "09:00" and s["enabled"] is True)

cleanup_files()
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
