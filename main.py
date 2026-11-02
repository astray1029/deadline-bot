import datetime as dt
import os
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
# на хостинге с постоянной папкой /data база там, иначе рядом с файлом
DB = "/data/reminders.db" if os.path.isdir("/data") else "reminders.db"
DEFAULT_TZ = "Europe/Moscow"

UNITS = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}
DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# за сколько секунд до дедлайна напоминать
PRE_ALERTS = [(86400, "1 день"), (10800, "3 часа"), (3600, "1 час")]
ICONS = {"deadline": "📅", "in": "⏰", "every": "🔁", "daily": "🔁",
         "weekly": "🔁", "digest": "☀️"}


# ---------- база данных ----------
def db_exec(query, params=(), fetch=False):
    with sqlite3.connect(DB) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if fetch else cur.lastrowid


def init_db():
    db_exec(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            text TEXT,
            kind TEXT,
            interval_sec INTEGER,
            run_at REAL,
            hhmm TEXT,
            days TEXT
        )"""
    )
    db_exec("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, tz TEXT)")


def get_tz(chat_id):
    rows = db_exec("SELECT tz FROM users WHERE chat_id = ?", (chat_id,), fetch=True)
    return ZoneInfo(rows[0][0] if rows else DEFAULT_TZ)


# ---------- разбор ввода ----------
def parse_interval(s):
    m = re.fullmatch(r"(\d+)([mhdмчд])", s.lower())
    return int(m.group(1)) * UNITS[m.group(2)] if m else None


def parse_hhmm(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if h < 24 and mi < 60 else None


def parse_date(s, tz):
    today = dt.datetime.now(tz).date()
    low = s.lower()
    if low == "сегодня":
        return today
    if low == "завтра":
        return today + dt.timedelta(days=1)
    if low == "послезавтра":
        return today + dt.timedelta(days=2)
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", s)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            year = int(year)
            return dt.date(year + 2000 if year < 100 else year, month, day)
        date = dt.date(today.year, month, day)
        if date < today:  # без года и дата уже прошла -> следующий год
            date = dt.date(today.year + 1, month, day)
        return date
    except ValueError:
        return None


def parse_days(s):
    low = s.lower()
    if low == "будни":
        return "0,1,2,3,4"
    if low == "выходные":
        return "5,6"
    parts = low.split(",")
    if all(p in DAYS for p in parts):
        return ",".join(str(n) for n in sorted({DAYS[p] for p in parts}))
    return None


def days_label(days):
    return ",".join(DAY_NAMES[int(x)] for x in days.split(","))


def human(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m and not d:
        parts.append(f"{m} мин")
    return " ".join(parts) or "меньше минуты"


# ---------- задачи и планировщик ----------
def remove_jobs(jq, rid):
    for job in jq.get_jobs_by_name(f"t{rid}"):
        job.schedule_removal()


async def fire(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    d = job.data
    text = f"⏰ {d['text']}"
    if d.get("label"):
        text += f"\n{d['label']}"
    markup = None
    if d["kind"] in ("deadline", "in"):
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Готово", callback_data=f"done:{d['id']}"),
            InlineKeyboardButton("⏰ +1 час", callback_data=f"snooze:{d['id']}"),
        ]])
    await context.bot.send_message(job.chat_id, text, reply_markup=markup)
    if d.get("final"):
        db_exec("DELETE FROM tasks WHERE id = ?", (d["id"],))


def deadlines_text(chat_id, days=7):
    now = time.time()
    tz = get_tz(chat_id)
    rows = db_exec(
        "SELECT id, text, run_at FROM tasks WHERE chat_id = ? AND kind = 'deadline' "
        "AND run_at > ? AND run_at <= ? ORDER BY run_at",
        (chat_id, now, now + days * 86400),
        fetch=True,
    )
    if not rows:
        return None
    return "\n".join(
        f"{rid}. {text} — {dt.datetime.fromtimestamp(run_at, tz):%d.%m %H:%M} "
        f"(через {human(run_at - now)})"
        for rid, text, run_at in rows
    )


async def digest(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    body = deadlines_text(chat_id, 7)
    text = "☀️ Доброе утро!\n\n" + (
        f"Дедлайны на 7 дней:\n{body}" if body else "Ближайшие 7 дней без дедлайнов 🎉"
    )
    await context.bot.send_message(chat_id, text)


def schedule(jq, row):
    rid, chat_id, text, kind, sec, run_at, hhmm, days = row
    name = f"t{rid}"
    base = {"id": rid, "text": text, "kind": kind}
    now = time.time()
    if kind == "every":
        jq.run_repeating(fire, interval=sec, first=sec,
                         chat_id=chat_id, name=name, data=base)
    elif kind == "in":
        jq.run_once(fire, when=max(run_at - now, 1), chat_id=chat_id, name=name,
                    data={**base, "final": True})
    elif kind == "deadline":
        for before, label in PRE_ALERTS:
            if run_at - before > now:
                jq.run_once(fire, when=run_at - before - now, chat_id=chat_id,
                            name=name, data={**base, "label": f"📅 Осталось {label}"})
        jq.run_once(fire, when=max(run_at - now, 1), chat_id=chat_id, name=name,
                    data={**base, "label": "🚨 Дедлайн наступил!", "final": True})
    elif kind in ("daily", "weekly", "digest"):
        h, m = map(int, hhmm.split(":"))
        t = dt.time(h, m, tzinfo=get_tz(chat_id))
        day_tuple = tuple(int(x) for x in days.split(",")) if days else tuple(range(7))
        jq.run_daily(digest if kind == "digest" else fire, time=t, days=day_tuple,
                     chat_id=chat_id, name=name, data=base)


def add_task(jq, chat_id, text, kind, sec=None, run_at=None, hhmm=None, days=None):
    rid = db_exec(
        "INSERT INTO tasks (chat_id, text, kind, interval_sec, run_at, hhmm, days) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, text, kind, sec, run_at, hhmm, days),
    )
    schedule(jq, (rid, chat_id, text, kind, sec, run_at, hhmm, days))
    return rid


async def restore(app: Application):
    rows = db_exec(
        "SELECT id, chat_id, text, kind, interval_sec, run_at, hhmm, days FROM tasks",
        fetch=True,
    )
    for row in rows:
        schedule(app.job_queue, row)
    await app.bot.set_my_commands([
        BotCommand("deadline", "дедлайн: 05.11 18:00 текст"),
        BotCommand("week", "дедлайны на 7 дней"),
        BotCommand("morning", "утренняя сводка: 07:30"),
        BotCommand("daily", "каждый день: 07:30 текст"),
        BotCommand("weekly", "по дням: пн,ср 09:00 текст"),
        BotCommand("every", "повтор: 1h текст"),
        BotCommand("in", "один раз: 30m текст"),
        BotCommand("list", "все напоминания"),
        BotCommand("del", "удалить по номеру"),
        BotCommand("tz", "часовой пояс"),
    ])


# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот-напоминалка.\n\n"
        "📅 Дедлайны\n"
        "/deadline 05.11 18:00 Сдать лабу\n"
        "/deadline завтра 12:00 Отчёт\n"
        "Напомню за день, за 3 часа и за час.\n"
        "/week — дедлайны на 7 дней\n"
        "/morning 07:30 — утренняя сводка (/morning off — выключить)\n\n"
        "🔁 Повторы\n"
        "/daily 07:30 Выпить воды — каждый день\n"
        "/weekly пн,ср 09:00 Python — по дням (пн вт ср чт пт сб вс, будни, выходные)\n"
        "/every 1h Размяться — каждый час\n"
        "/in 30m Позвонить — один раз через время\n\n"
        "⚙️ /list, /del 3, /tz Europe/Moscow\n"
        f"Часовой пояс по умолчанию: {DEFAULT_TZ}"
    )


async def deadline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    chat_id = update.effective_chat.id
    tz = get_tz(chat_id)
    d = parse_date(a[0], tz) if len(a) >= 3 else None
    t = parse_hhmm(a[1]) if len(a) >= 3 else None
    if not d or not t:
        await update.message.reply_text(
            "Пример: /deadline 05.11 18:00 Сдать лабу\n"
            "или: /deadline завтра 12:00 Отчёт"
        )
        return
    when = dt.datetime(d.year, d.month, d.day, t[0], t[1], tzinfo=tz)
    ts = when.timestamp()
    if ts <= time.time():
        await update.message.reply_text("Это время уже прошло 🙂")
        return
    text = " ".join(a[2:])
    rid = add_task(context.job_queue, chat_id, text, "deadline", run_at=ts)
    await update.message.reply_text(
        f"✅ №{rid}: «{text}»\n"
        f"📅 {when:%d.%m.%Y %H:%M} (через {human(ts - time.time())})\n"
        "Напомню заранее: за день, за 3 часа и за час (если успею)."
    )


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    body = deadlines_text(update.effective_chat.id, 7)
    await update.message.reply_text(
        f"📅 Дедлайны на 7 дней:\n{body}" if body else "Ближайшие 7 дней свободны 🎉"
    )


async def every(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    sec = parse_interval(a[0]) if len(a) >= 2 else None
    if not sec:
        await update.message.reply_text("Пример: /every 1h Попить воды")
        return
    text = " ".join(a[1:])
    rid = add_task(context.job_queue, update.effective_chat.id, text, "every", sec=sec)
    await update.message.reply_text(f"✅ №{rid}: «{text}» — каждые {human(sec)}")


async def once(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    sec = parse_interval(a[0]) if len(a) >= 2 else None
    if not sec:
        await update.message.reply_text("Пример: /in 30m Позвонить")
        return
    text = " ".join(a[1:])
    rid = add_task(context.job_queue, update.effective_chat.id, text, "in",
                   run_at=time.time() + sec)
    await update.message.reply_text(f"✅ №{rid}: «{text}» — через {human(sec)}")


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    t = parse_hhmm(a[0]) if len(a) >= 2 else None
    if not t:
        await update.message.reply_text("Пример: /daily 07:30 Выпить воды")
        return
    text = " ".join(a[1:])
    hhmm = f"{t[0]:02d}:{t[1]:02d}"
    rid = add_task(context.job_queue, update.effective_chat.id, text, "daily", hhmm=hhmm)
    await update.message.reply_text(f"✅ №{rid}: «{text}» — каждый день в {hhmm}")


async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    days = parse_days(a[0]) if len(a) >= 3 else None
    t = parse_hhmm(a[1]) if len(a) >= 3 else None
    if not days or not t:
        await update.message.reply_text(
            "Пример: /weekly пн,ср 09:00 Python\n"
            "Дни: пн вт ср чт пт сб вс, будни, выходные"
        )
        return
    text = " ".join(a[2:])
    hhmm = f"{t[0]:02d}:{t[1]:02d}"
    rid = add_task(context.job_queue, update.effective_chat.id, text, "weekly",
                   hhmm=hhmm, days=days)
    await update.message.reply_text(
        f"✅ №{rid}: «{text}» — {days_label(days)} в {hhmm}"
    )


def remove_digest(jq, chat_id):
    rows = db_exec("SELECT id FROM tasks WHERE chat_id = ? AND kind = 'digest'",
                   (chat_id,), fetch=True)
    for (rid,) in rows:
        remove_jobs(jq, rid)
        db_exec("DELETE FROM tasks WHERE id = ?", (rid,))


async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    a = context.args
    if a and a[0].lower() in ("off", "выкл"):
        remove_digest(context.job_queue, chat_id)
        await update.message.reply_text("Утренняя сводка выключена.")
        return
    t = parse_hhmm(a[0]) if a else None
    if not t:
        await update.message.reply_text("Пример: /morning 07:30  (или /morning off)")
        return
    remove_digest(context.job_queue, chat_id)
    hhmm = f"{t[0]:02d}:{t[1]:02d}"
    add_task(context.job_queue, chat_id, "Утренняя сводка", "digest", hhmm=hhmm)
    await update.message.reply_text(
        f"☀️ Буду присылать сводку дедлайнов каждый день в {hhmm}"
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tz = get_tz(chat_id)
    rows = db_exec(
        "SELECT id, text, kind, interval_sec, run_at, hhmm, days FROM tasks "
        "WHERE chat_id = ? ORDER BY id",
        (chat_id,),
        fetch=True,
    )
    if not rows:
        await update.message.reply_text("Пока пусто.")
        return
    lines = []
    for rid, text, kind, sec, run_at, hhmm, days in rows:
        if kind == "every":
            when = f"каждые {human(sec)}"
        elif kind in ("in", "deadline"):
            when = f"{dt.datetime.fromtimestamp(run_at, tz):%d.%m %H:%M}"
        elif kind == "weekly":
            when = f"{days_label(days)} в {hhmm}"
        else:
            when = f"каждый день в {hhmm}"
        lines.append(f"{ICONS[kind]} {rid}. {text} — {when}")
    await update.message.reply_text("\n".join(lines))


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Пример: /del 3")
        return
    rid = int(context.args[0])
    found = db_exec("SELECT id FROM tasks WHERE id = ? AND chat_id = ?",
                    (rid, update.effective_chat.id), fetch=True)
    if not found:
        await update.message.reply_text("Такого напоминания нет.")
        return
    db_exec("DELETE FROM tasks WHERE id = ?", (rid,))
    remove_jobs(context.job_queue, rid)
    await update.message.reply_text(f"🗑 №{rid} удалено")


async def tz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            f"Сейчас: {get_tz(chat_id)}\n"
            "Сменить: /tz Europe/Moscow (Europe/Kyiv, Asia/Almaty, Asia/Yekaterinburg...)"
        )
        return
    try:
        ZoneInfo(context.args[0])
    except Exception:
        await update.message.reply_text("Не знаю такой пояс. Пример: /tz Europe/Moscow")
        return
    db_exec("INSERT OR REPLACE INTO users (chat_id, tz) VALUES (?, ?)",
            (chat_id, context.args[0]))
    # ежедневные напоминания нужно пересоздать в новом поясе
    rows = db_exec(
        "SELECT id, chat_id, text, kind, interval_sec, run_at, hhmm, days FROM tasks "
        "WHERE chat_id = ? AND kind IN ('daily', 'weekly', 'digest')",
        (chat_id,), fetch=True,
    )
    for row in rows:
        remove_jobs(context.job_queue, row[0])
        schedule(context.job_queue, row)
    await update.message.reply_text(f"✅ Часовой пояс: {context.args[0]}")


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, _, rid = q.data.partition(":")
    chat_id = q.message.chat_id
    text = q.message.text.split("\n")[0].removeprefix("⏰ ")
    if action == "done":
        if rid.isdigit() and db_exec(
            "SELECT id FROM tasks WHERE id = ? AND chat_id = ?",
            (int(rid), chat_id), fetch=True,
        ):
            db_exec("DELETE FROM tasks WHERE id = ?", (int(rid),))
            remove_jobs(context.job_queue, int(rid))
        await q.edit_message_text(f"✅ {text} — готово")
    elif action == "snooze":
        add_task(context.job_queue, chat_id, text, "in", run_at=time.time() + 3600)
        await q.edit_message_text(f"⏰ {text} — напомню через час")


def main():
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    init_db()
    app = Application.builder().token(TOKEN).post_init(restore).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("deadline", deadline))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("every", every))
    app.add_handler(CommandHandler("in", once))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("weekly", weekly))
    app.add_handler(CommandHandler("morning", morning))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", delete))
    app.add_handler(CommandHandler("tz", tz_cmd))
    app.add_handler(CallbackQueryHandler(buttons))
    app.run_polling()


if __name__ == "__main__":
    main()
