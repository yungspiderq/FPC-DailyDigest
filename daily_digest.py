"""
Утренний дайджест (DailyDigest) — плагин для FunPay Cardinal v0.1.17+

🌅 Каждое утро в заданное время присылает одну сводку со всей картиной магазина:
продажи за вчера (и сравнение с позавчера), активные заказы, проблемные остатки
товаров и баланс аккаунта.

Возможности:
 • 💰 Продажи за вчера: количество, сумма, возвраты + сравнение с позавчерашним
   днем (📈 +19% / 📉 −12%).
 • 📋 Активные заказы: сколько ждут подтверждения и сколько висит старейший.
 • 📦 Остатки: сколько товарных файлов пустые и сколько ниже порога.
 • 💳 Баланс аккаунта (рубли/доллары/евро, если есть).
 • 🕐 Время отправки настраивается (по часам сервера), границы «вчера» — по
   московскому времени FunPay.
 • 🔄 Кнопка «Отправить сейчас» — получить сводку в любой момент.
 • Дайджест отправляется ровно один раз в день (состояние переживает рестарт).

Команды Telegram:
 • /digest — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.common.enums import Currency, OrderStatuses
from Utils import cardinal_tools

logger = logging.getLogger("FPC.DailyDigest")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "Утренний дайджест"
VERSION = "1.0.0"
DESCRIPTION = ("Ежедневная сводка магазина в Telegram: продажи за вчера со сравнением, "
               "активные заказы, проблемные остатки товаров, баланс. Отправляется в заданное "
               "время или вручную. Команда: /digest")
CREDITS = "Qwen (для FunPay Cardinal)"
UUID = "fd7ec5f7-3100-4a86-a79d-0176a60dd4c6"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "daily_digest")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "enabled": True,
    "time": "09:00",        # время отправки (по часам сервера)
    "last_date": "",        # дата последней отправки (ГГГГ-ММ-ДД, по часам сервера)
    "stock_threshold": 5,   # порог «низких» остатков
}
THRESHOLD_RANGE = (1, 100000)
MAX_PAGES = 15             # ограничение пагинации истории (с запасом покрывает 2 дня)
MAX_ACTIVE_PAGES = 10

MSK = timezone(timedelta(hours=3))
CURRENCY_SIGNS = {Currency.USD: "$", Currency.RUB: "₽", Currency.EUR: "€", Currency.UNKNOWN: ""}

CB_HOME = "dig:home"
CB_NOW = "dig:now"
CB_TOG = "dig:tog"
CB_TIME = "dig:time"
CB_THRESH = "dig:thresh"
CB_NOOP = "dig:noop"
ST_TIME = "dig:st:time"
ST_THRESH = "dig:st:thresh"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_busy = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("DailyDigest: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        if _parse_hhmm(settings["time"]) is None:
            settings["time"] = DEFAULT_SETTINGS["time"]
        settings["last_date"] = str(settings.get("last_date") or "")
        try:
            settings["stock_threshold"] = _clamp(int(settings["stock_threshold"]), *THRESHOLD_RANGE)
        except (TypeError, ValueError):
            settings["stock_threshold"] = DEFAULT_SETTINGS["stock_threshold"]
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("DailyDigest: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    text = str(text or "").strip().replace(".", ":")
    parts = text.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    except (ValueError, IndexError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _sign(currency) -> str:
    try:
        return CURRENCY_SIGNS.get(currency, "")
    except Exception:
        return ""


def _human_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d} дн. {h} ч."
    if h:
        return f"{h} ч. {m} мин."
    return f"{m} мин."


def _notify(crd: "Cardinal", text: str) -> None:
    if not crd.telegram:
        return
    kwargs = {"notification_type": tg_utils.NotificationTypes.other} if tg_utils else {}
    try:
        crd.telegram.send_notification(text, **kwargs)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)


def _fmt_sums(sums: dict) -> str:
    """{Currency: сумма} → «3 450 ₽» или «100 ₽ + 20 $»."""
    parts = [f"{v:g} {_sign(c)}".strip() for c, v in sums.items() if v]
    return ", ".join(parts) if parts else "0"


# ══════════════════════════════ ПОСТРОЕНИЕ ДАЙДЖЕСТА ════════════════════════════
def _build_digest(crd: "Cardinal", now_msk: datetime | None = None) -> str:
    """Собирает текст сводки. now_msk — «текущее» время по Москве (для тестируемости)."""
    settings = _load_settings()
    now_msk = now_msk or datetime.now(MSK)
    if now_msk.tzinfo is None:
        now_msk = now_msk.replace(tzinfo=MSK)
    yesterday = (now_msk - timedelta(days=1)).date()
    day_before = (now_msk - timedelta(days=2)).date()
    day_before_start = datetime(day_before.year, day_before.month, day_before.day)

    y_count = y_refunds = db_count = 0
    y_sums: dict = {}
    db_sums: dict = {}

    # ── продажи за вчера и позавчера ──
    try:
        start_from = None
        for _ in range(MAX_PAGES):
            result = crd.account.get_sales(start_from=start_from, include_paid=True,
                                           include_closed=True, include_refunded=True)
            next_id, orders = result[0], result[1]
            if not orders:
                break
            oldest = None
            for o in orders:
                if o.date is None:
                    continue
                if oldest is None or o.date < oldest:
                    oldest = o.date
                d = o.date.date()
                if d == yesterday:
                    if o.status in (OrderStatuses.REFUNDED, OrderStatuses.PARTIALLY_REFUNDED):
                        y_refunds += 1
                        continue
                    if o.status not in (OrderStatuses.PAID, OrderStatuses.CLOSED):
                        continue
                    y_count += 1
                    y_sums[o.currency] = y_sums.get(o.currency, 0.0) + float(o.price or 0)
                elif d == day_before:
                    if o.status in (OrderStatuses.PAID, OrderStatuses.CLOSED):
                        db_count += 1
                        db_sums[o.currency] = db_sums.get(o.currency, 0.0) + float(o.price or 0)
            if oldest is not None and oldest < day_before_start:
                break  # дальше только старше позавчера — не нужно
            if not next_id:
                break
            start_from = next_id
    except Exception:
        logger.error("DailyDigest: не удалось получить историю продаж.")
        logger.debug("TRACEBACK", exc_info=True)

    # ── активные заказы ──
    active_count = 0
    oldest_active_age = None
    try:
        start_from = None
        now_naive_msk = now_msk.replace(tzinfo=None)
        for _ in range(MAX_ACTIVE_PAGES):
            result = crd.account.get_sales(start_from=start_from, include_paid=True,
                                           include_closed=False, include_refunded=False)
            next_id, orders = result[0], result[1]
            if not orders:
                break
            for o in orders:
                if o.status is not OrderStatuses.PAID:
                    continue
                active_count += 1
                if o.date is not None:
                    age = (now_naive_msk - o.date).total_seconds()
                    if oldest_active_age is None or age > oldest_active_age:
                        oldest_active_age = age
            if not next_id:
                break
            start_from = next_id
    except Exception:
        logger.error("DailyDigest: не удалось получить активные заказы.")
        logger.debug("TRACEBACK", exc_info=True)

    # ── остатки товаров ──
    empty_files = low_files = total_files = 0
    try:
        seen_files = set()
        for title in crd.AD_CFG.sections():
            try:
                fname = crd.AD_CFG[title].get("productsFileName")
            except Exception:
                continue
            if not fname or fname in seen_files:
                continue
            seen_files.add(fname)
            total_files += 1
            try:
                count = cardinal_tools.count_products(os.path.join("storage", "products", fname))
            except Exception:
                continue
            if count == 0:
                empty_files += 1
            elif count <= settings["stock_threshold"]:
                low_files += 1
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)

    # ── баланс ──
    balance_line = None
    try:
        balance = getattr(crd, "balance", None)
        if balance is not None:
            parts = []
            if getattr(balance, "total_rub", 0):
                parts.append(f"{balance.total_rub:g} ₽ (к выводу: {balance.available_rub:g})")
            if getattr(balance, "total_usd", 0):
                parts.append(f"{balance.total_usd:g} $ (к выводу: {balance.available_usd:g})")
            if getattr(balance, "total_eur", 0):
                parts.append(f"{balance.total_eur:g} € (к выводу: {balance.available_eur:g})")
            if parts:
                balance_line = "; ".join(parts)
    except Exception:
        logger.debug("TRACEBACK", exc_info=True)

    # ── сборка текста ──
    text = (f"🌅 <b>Утренняя сводка</b> ({now_msk.strftime('%d.%m.%Y')})\n\n"
            f"💰 <b>Продажи за вчера ({yesterday.strftime('%d.%m')}):</b>\n")
    if y_count:
        text += f"   {y_count} заказ(ов) на <b>{_fmt_sums(y_sums)}</b>\n"
    else:
        text += "   продаж не было\n"
    if db_count:
        trend = ""
        y_total = sum(y_sums.values())
        db_total = sum(db_sums.values())
        if db_total > 0:
            pct = round((y_total - db_total) / db_total * 100)
            trend = f" ({'📈 +' if pct >= 0 else '📉 '}{pct}%)"
        text += f"   Позавчера: {db_count} на {_fmt_sums(db_sums)}{trend}\n"
    text += f"↩️ Возвраты за вчера: <b>{y_refunds}</b>\n\n"

    text += f"📋 <b>Активные заказы:</b> {active_count}\n"
    if active_count and oldest_active_age is not None:
        text += f"   Старейший ждёт подтверждения: {_human_delta(oldest_active_age)}\n"
    text += "\n"

    if total_files:
        text += f"📦 <b>Остатки товаров</b> (файлов: {total_files}):\n"
        text += f"   🚨 Пустые: {empty_files} | ⚠️ Ниже {settings['stock_threshold']} шт.: {low_files}\n\n"

    if balance_line:
        text += f"💳 <b>Баланс:</b> {balance_line}\n\n"

    text += "Хорошего дня и побольше продаж! 🍀"
    return text


def _send_digest_now(crd: "Cardinal") -> None:
    if not _busy.acquire(blocking=False):
        return  # сборка уже идет
    try:
        text = _build_digest(crd)
        _notify(crd, text)
        logger.info("DailyDigest: сводка отправлена.")
    except Exception:
        logger.error("DailyDigest: не удалось собрать/отправить сводку.")
        logger.debug("TRACEBACK", exc_info=True)
    finally:
        _busy.release()


def _digest_loop(crd: "Cardinal") -> None:
    logger.info("DailyDigest: планировщик сводки запущен.")
    while not _stop_event.wait(30):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                continue
            settings = _load_settings()
            if not settings["enabled"]:
                continue
            now_local = datetime.now()
            hhmm = _parse_hhmm(settings["time"])
            if not hhmm:
                continue
            today = now_local.strftime("%Y-%m-%d")
            if settings["last_date"] == today:
                continue
            if (now_local.hour, now_local.minute) < hhmm:
                continue
            settings["last_date"] = today
            _save_settings(settings)
            _send_digest_now(crd)
        except Exception:
            logger.error("DailyDigest: ошибка в планировщике сводки.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    state = "ВКЛ 🟢" if settings["enabled"] else "ВЫКЛ 🔴"
    last = settings["last_date"] or "еще не отправлялся"
    return (f"🌅 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"Ежедневная сводка магазина: продажи за вчера, активные заказы, "
            f"остатки товаров и баланс.\n\n"
            f"Дайджест: <b>{state}</b>\n"
            f"🕐 Время отправки: <b>{settings['time']}</b> <i>(по часам сервера)</i>\n"
            f"Последняя отправка: <b>{_escape(last)}</b>\n"
            f"⚠️ Порог «низких» остатков: <b>{settings['stock_threshold']}</b> шт.")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B("🔄 Отправить сейчас", None, CB_NOW))
    kb.add(B(f"{'🟢 Дайджест: ВКЛ' if settings['enabled'] else '🔴 Дайджест: ВЫКЛ'}", None, CB_TOG))
    kb.add(B(f"🕐 Время: {settings['time']}", None, CB_TIME))
    kb.add(B(f"⚠️ Порог остатков: {settings['stock_threshold']}", None, CB_THRESH))
    kb.add(B("🔃 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def send_now(c: CallbackQuery) -> None:
            if not crd.account.is_initiated:
                _answer(bot, c, "Аккаунт FunPay еще не инициализирован. Попробуйте позже.", alert=True)
                return
            _answer(bot, c, "🌅 Собираю сводку — она придет уведомлением через несколько секунд...")
            threading.Thread(target=_send_digest_now, args=(crd,), daemon=True).start()

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"🌅 Дайджест: {'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_time(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"🕐 Введите время ежедневной сводки в формате ЧЧ:ММ (по часам сервера).\n\n"
                f"Текущее значение: <b>{s['time']}</b>.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_TIME)
            _answer(bot, c)

        def act_threshold(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⚠️ Введите порог «низких» остатков для сводки (штук).\n\n"
                f"Текущее значение: <b>{s['stock_threshold']}</b>.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_THRESH)
            _answer(bot, c)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_time(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            hhmm = _parse_hhmm(str(m.text or ""))
            if hhmm is None:
                bot.reply_to(m, "❌ Не понял время. Введите в формате ЧЧ:ММ, например <code>09:00</code>.",
                             parse_mode="HTML")
                return
            settings = _load_settings()
            settings["time"] = f"{hhmm[0]:02d}:{hhmm[1]:02d}"
            _save_settings(settings)
            bot.reply_to(m, f"🕐 Время сводки: <b>{settings['time']}</b>.", parse_mode="HTML")

        def edit_threshold(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *THRESHOLD_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число.")
                return
            settings = _load_settings()
            settings["stock_threshold"] = value
            _save_settings(settings)
            bot.reply_to(m, f"⚠️ Порог остатков: <b>{value}</b> шт.", parse_mode="HTML")

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(send_now, lambda c: c.data == CB_NOW)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(act_time, lambda c: c.data == CB_TIME)
        tg.cbq_handler(act_threshold, lambda c: c.data == CB_THRESH)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_time, func=lambda m: _state_eq(m, ST_TIME))
        tg.msg_handler(edit_threshold, func=lambda m: _state_eq(m, ST_THRESH))
        tg.msg_handler(cmd_open, commands=["digest"])

        try:
            crd.add_telegram_commands(UUID, [("digest", "Утренний дайджест магазина.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"DailyDigest v{VERSION}: Telegram-меню зарегистрировано (команда /digest).")
    else:
        logger.info(f"DailyDigest v{VERSION}: Telegram выключен — дайджест не отправляется.")

    threading.Thread(target=_digest_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = on_delete
