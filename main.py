import os
import re
import sqlite3
import time

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")  # токен от @BotFather
# на Amvera постоянная папка /data, локально - рядом с файлом
DB = "/data/reminders.db" if os.path.isdir("/data") else "reminders.db"

UNITS = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}


# ---------- база данных ----------
def db_exec(query, params=(), fetch=False):
    with sqlite3.connect(DB) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if fetch else cur.lastrowid


def init_db():
    db_exec(
        """CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            text TEXT,
            interval_sec INTEGER,
            repeat INTEGER,
            run_at REAL
        )"""
    )


# ---------- вспомогательное ----------
def parse_interval(s: str):
    """'30m' / '1h' / '2d' (или м, ч, д) -> секунды"""
    m = re.fullmatch(r"(\d+)([mhdмчд])", s.lower())
    if not m:
        return None
    return int(m.group(1)) * UNITS[m.group(2)]


def human(sec: int) -> str:
    if sec % 86400 == 0:
        return f"{sec // 86400} д"
    if sec % 3600 == 0:
        return f"{sec // 3600} ч"
    return f"{sec // 60} мин"


async def fire(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await context.bot.send_message(context.job.chat_id, f"⏰ {data['text']}")
    if not data["repeat"]:
        db_exec("DELETE FROM reminders WHERE id = ?", (data["id"],))


def schedule(job_queue, rid, chat_id, text, interval_sec, repeat, run_at):
    data = {"id": rid, "text": text, "repeat": bool(repeat)}
    name = f"r{rid}"
    if repeat:
        job_queue.run_repeating(
            fire, interval=interval_sec, first=interval_sec,
            chat_id=chat_id, name=name, data=data,
        )
    else:
        job_queue.run_once(
            fire, when=max(run_at - time.time(), 1),
            chat_id=chat_id, name=name, data=data,
        )


async def restore(app: Application):
    """После перезапуска бота поднимаем все напоминания из базы."""
    rows = db_exec(
        "SELECT id, chat_id, text, interval_sec, repeat, run_at FROM reminders",
        fetch=True,
    )
    for row in rows:
        schedule(app.job_queue, *row)


# ---------- команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот-напоминалка.\n\n"
        "/every 1h Попить воды — повторять каждый час\n"
        "/in 30m Сдать лабу — напомнить один раз через 30 минут\n"
        "/list — все напоминания\n"
        "/del 3 — удалить напоминание №3\n\n"
        "Единицы: m (мин), h (час), d (день)"
    )


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE, repeat: bool):
    args = context.args
    cmd = "every" if repeat else "in"
    if len(args) < 2 or parse_interval(args[0]) is None:
        await update.message.reply_text(f"Пример: /{cmd} 1h Попить воды")
        return

    sec = parse_interval(args[0])
    text = " ".join(args[1:])
    chat_id = update.effective_chat.id
    run_at = time.time() + sec

    rid = db_exec(
        "INSERT INTO reminders (chat_id, text, interval_sec, repeat, run_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, text, sec, int(repeat), run_at),
    )
    schedule(context.job_queue, rid, chat_id, text, sec, int(repeat), run_at)

    kind = f"каждые {human(sec)}" if repeat else f"через {human(sec)}"
    await update.message.reply_text(f"✅ №{rid}: «{text}» — {kind}")


async def every(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add(update, context, repeat=True)


async def once(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await add(update, context, repeat=False)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_exec(
        "SELECT id, text, interval_sec, repeat FROM reminders WHERE chat_id = ?",
        (update.effective_chat.id,),
        fetch=True,
    )
    if not rows:
        await update.message.reply_text("Пока пусто.")
        return
    lines = [
        f"{rid}. {text} — " + (f"каждые {human(sec)}" if rep else "один раз")
        for rid, text, sec, rep in rows
    ]
    await update.message.reply_text("\n".join(lines))


async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Пример: /del 3")
        return
    rid = int(context.args[0])
    chat_id = update.effective_chat.id

    found = db_exec(
        "SELECT id FROM reminders WHERE id = ? AND chat_id = ?",
        (rid, chat_id),
        fetch=True,
    )
    if not found:
        await update.message.reply_text("Такого напоминания нет.")
        return

    db_exec("DELETE FROM reminders WHERE id = ?", (rid,))
    for job in context.job_queue.get_jobs_by_name(f"r{rid}"):
        job.schedule_removal()
    await update.message.reply_text(f"🗑 №{rid} удалено")


def main():
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    init_db()
    app = Application.builder().token(TOKEN).post_init(restore).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("every", every))
    app.add_handler(CommandHandler("in", once))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("del", delete))
    app.run_polling()


if __name__ == "__main__":
    main()
