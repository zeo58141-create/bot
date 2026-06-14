"""
╔══════════════════════════════════════╗
║     PERSONAL HOSTING BOT v5.0        ║
║     Python 3.10+ | PTB v21+          ║
║     ZIP support added!               ║
╚══════════════════════════════════════╝
"""

import os, sys, time, asyncio, subprocess, threading, signal, psutil, zipfile
from pathlib import Path
from datetime import datetime
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN    = "8635653347:AAHfdoO9MvdvR_O0D216WFHAS3DLFm5Cr3g"
OWNER_ID     = 8321630022
BOT_NAME     = "⚡ ZenoHost"
SERVER_NAME  = "🌐 Zeno Cloud"
RAM_TOTAL_MB = 308
BOTS_DIR     = Path("hosted_bots")

# ══════════════════════════════════════════════
#  RUNTIME STATE
# ══════════════════════════════════════════════
hosted_bots:    dict[int, dict[str, dict]] = {}
pending_upload: dict[int, dict] = {}
banned_users:   set[int] = set()

# ══════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════

def user_bots(uid: int) -> dict[str, dict]:
    if uid not in hosted_bots:
        hosted_bots[uid] = {}
    return hosted_bots[uid]

def get_ram_usage() -> tuple[float, float]:
    mem  = psutil.virtual_memory()
    used = mem.used / 1024 / 1024
    pct  = (used / RAM_TOTAL_MB) * 100
    return round(used, 1), round(min(pct, 100), 1)

def get_uptime(started_at: datetime) -> str:
    delta  = datetime.now() - started_at
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def make_bar(pct: float, length: int = 10) -> str:
    f = int(length * pct / 100)
    return "█" * f + "░" * (length - f)

def all_running_count() -> int:
    total = 0
    for ubots in hosted_bots.values():
        total += sum(1 for b in ubots.values() if b["status"] == "running")
    return total

def is_banned(uid: int) -> bool:
    return uid in banned_users

def fmt_terminal(lines: list[str], bot_name: str) -> str:
    import unicodedata

    def vis_len(s: str) -> int:
        n = 0
        for ch in s:
            n += 2 if unicodedata.east_asian_width(ch) in ("W","F") else 1
        return n

    def rpad(s: str, width: int) -> str:
        pad = width - vis_len(s)
        return s + " " * max(pad, 0)

    W = 52
    src = lines[-18:] if lines else ["  No output yet..."]
    display = []
    for raw in src:
        ln = raw.rstrip()
        out, vl = "", 0
        for ch in ln:
            cw = 2 if unicodedata.east_asian_width(ch) in ("W","F") else 1
            if vl + cw > W:
                break
            out += ch
            vl  += cw
        display.append(out)

    top   = "+" + "-" * (W + 2) + "+"
    title = f" OUTPUT [{bot_name}] ".center(W)
    rows  = [top, "|" + title + " |", top]
    rows += ["| " + rpad(ln, W) + " |" for ln in display]
    rows.append(top)
    return "<pre>" + "\n".join(rows) + "</pre>"

def get_user_bots_dir(uid: int) -> Path:
    p = BOTS_DIR / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p

# ══════════════════════════════════════════════
#  PROCESS MANAGEMENT
# ══════════════════════════════════════════════

def _install_req(req_path: Path) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req_path),
         "--quiet", "--no-warn-script-location"],
        capture_output=True, text=True, timeout=180,
    )
    return r.returncode == 0, (r.stdout + r.stderr).strip()

def start_bot_process(uid: int, bot_name: str, file_path: Path, req_path: Path | None = None):
    dq = deque(maxlen=80)
    ubots = user_bots(uid)
    ubots[bot_name] = {
        "process":      None,
        "file":         file_path.name,
        "file_size":    file_path.stat().st_size,
        "req_file":     req_path.name if req_path else None,
        "started_at":   datetime.now(),
        "output_lines": dq,
        "status":       "starting",
        "pid":          None,
        "owner_uid":    uid,
    }

    def _run():
        if req_path and req_path.exists():
            dq.append("📦 Installing requirements...")
            ok, out = _install_req(req_path)
            dq.append("✅ Done!" if ok else "⚠️  pip warnings")
            for ln in out.splitlines()[-3:]:
                if ln.strip():
                    dq.append(f"  pip> {ln.strip()}")

        dq.append(f"🚀 Launching {bot_name}...")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(file_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env={**os.environ},
            )
        except Exception as e:
            dq.append(f"💥 Launch failed: {e}")
            if bot_name in user_bots(uid):
                user_bots(uid)[bot_name]["status"] = "crashed"
            return

        ubots = user_bots(uid)
        if bot_name in ubots:
            ubots[bot_name].update({"process": proc, "pid": proc.pid, "status": "running"})

        for line in iter(proc.stdout.readline, ""):
            dq.append(line.rstrip())
        proc.stdout.close()

        rc = proc.poll()
        if bot_name in user_bots(uid):
            user_bots(uid)[bot_name]["status"] = "stopped" if rc == 0 else "crashed"
            dq.append("✅ Exited cleanly" if rc == 0 else f"💥 Crashed (exit {rc})")

    threading.Thread(target=_run, daemon=True, name=f"bot-{uid}-{bot_name}").start()

def stop_bot_process(uid: int, bot_name: str) -> bool:
    ubots = user_bots(uid)
    if bot_name not in ubots:
        return False
    proc = ubots[bot_name].get("process")
    if proc:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except: pass
    ubots[bot_name]["status"] = "stopped"
    return True

# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Bots",    callback_data="menu_bots"),
         InlineKeyboardButton("👤 Account", callback_data="menu_account")],
        [InlineKeyboardButton("🆘 Support", callback_data="menu_support"),
         InlineKeyboardButton("📊 Status",  callback_data="menu_status")],
    ])

def kb_bots(uid: int) -> InlineKeyboardMarkup:
    ubots = user_bots(uid)
    rows  = [[InlineKeyboardButton("➕ Add New Bot", callback_data="add_file")]]
    if ubots:
        rows.append([InlineKeyboardButton("─────── Your Bots ───────", callback_data="noop")])
        for name, info in ubots.items():
            st  = info["status"]
            em  = {"running": "🟢", "crashed": "🔴", "starting": "🟡", "stopped": "⚪"}.get(st, "⚪")
            kb  = info["file_size"] // 1024
            rows.append([InlineKeyboardButton(f"{em} {name}  [{kb} KB]", callback_data=f"bot:{name}")])
    else:
        rows.append([InlineKeyboardButton("📭 No bots yet", callback_data="noop")])
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)

def kb_bot(uid: int, name: str) -> InlineKeyboardMarkup:
    ubots  = user_bots(uid)
    st     = ubots.get(name, {}).get("status", "stopped")
    toggle = "⏹ Stop Bot" if st in ("running", "starting") else "▶️ Start Bot"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📟 Terminal Output",  callback_data=f"term:{name}")],
        [InlineKeyboardButton(toggle,                callback_data=f"toggle:{name}"),
         InlineKeyboardButton("🗑 Delete",           callback_data=f"del:{name}")],
        [InlineKeyboardButton("🔄 Refresh",          callback_data=f"bot:{name}")],
        [InlineKeyboardButton("🔙 Back to Bots",     callback_data="menu_bots")],
    ])

def kb_back(to: str = "menu_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=to)]])

def kb_admin() -> InlineKeyboardMarkup:
    rows = []
    for uid, ubots in hosted_bots.items():
        for name, info in ubots.items():
            st  = info["status"]
            em  = "🟢" if st == "running" else "🔴"
            act = f"adm_stop:{uid}:{name}" if st == "running" else f"adm_start:{uid}:{name}"
            rows.append([InlineKeyboardButton(
                f"{em} [{uid}] {name} — {'Stop' if st == 'running' else 'Start'}",
                callback_data=act,
            )])
    if not rows:
        rows.append([InlineKeyboardButton("📭 No bots hosted by anyone", callback_data="noop")])
    rows += [
        [InlineKeyboardButton("👥 Manage Users", callback_data="adm_users")],
        [InlineKeyboardButton("🔙 Close",        callback_data="adm_close")],
    ]
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════
#  TEXT BUILDERS
# ══════════════════════════════════════════════

def txt_welcome(name: str) -> str:
    return (
        f"👋 <b>Welcome, {name}!</b>\n\n"
        f"<b>{BOT_NAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Bots</b>    — Manage your hosted bots\n"
        f"👤 <b>Account</b> — Your profile info\n"
        f"🆘 <b>Support</b> — Help & commands\n"
        f"📊 <b>Status</b>  — Service status\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Choose an option below 👇"
    )

def txt_bots(uid: int) -> str:
    ubots = user_bots(uid)
    cnt   = sum(1 for b in ubots.values() if b["status"] == "running")
    total = len(ubots)
    out   = [
        "🤖 <b>Your Hosted Bots</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"✅ Active : <code>{cnt} / {total}</code>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not ubots:
        out.append("📭 You have no bots hosted yet.\nTap <b>➕ Add New Bot</b> to get started!")
    else:
        for nm, info in ubots.items():
            st = info["status"]
            em = {"running": "🟢", "crashed": "🔴", "starting": "🟡", "stopped": "⚪"}.get(st, "⚪")
            up = get_uptime(info["started_at"]) if st == "running" else "—"
            kb = info["file_size"] // 1024
            out.append(
                f"\n{em} <b>{nm}</b>\n"
                f"   📄 <code>{info['file']}</code>  [{kb} KB]\n"
                f"   ⏱ Uptime : <code>{up}</code>"
            )
    return "\n".join(out)

def txt_bot(uid: int, name: str) -> str:
    info = user_bots(uid).get(name)
    if not info:
        return "❌ Bot not found."
    st  = info["status"]
    em  = {"running": "🟢 Running", "crashed": "🔴 Crashed",
           "stopped": "⚪ Stopped", "starting": "🟡 Starting..."}.get(st, st)
    up  = get_uptime(info["started_at"]) if st == "running" else "—"
    sz  = info["file_size"]
    szs = f"{sz // 1024} KB" if sz >= 1024 else f"{sz} B"
    return (
        f"🤖 <b>{name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File   : <code>{info['file']}</code>\n"
        f"📦 Size   : <code>{szs}</code>\n"
        f"📋 Req    : <code>{info.get('req_file') or 'None'}</code>\n"
        f"🔵 Status : <code>{em}</code>\n"
        f"⏱ Uptime : <code>{up}</code>\n"
        f"🔢 PID    : <code>{info.get('pid') or '—'}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )

def txt_status_public() -> str:
    cnt   = all_running_count()
    total = sum(len(u) for u in hosted_bots.values())
    pct   = round((cnt / total * 100) if total else 100, 1)
    return (
        f"📊 <b>Service Status</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 Service : <code>{SERVER_NAME}</code>\n"
        f"🟢 Status  : <code>Operational</code>\n"
        f"🤖 Bots Up : <code>{cnt} / {total}</code>\n"
        f"<code>[{make_bar(pct)}] {pct}%</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ All systems normal"
    )

def txt_status_owner() -> str:
    used, pct = get_ram_usage()
    cpu       = psutil.cpu_percent(interval=0.3)
    cnt       = all_running_count()
    total     = sum(len(u) for u in hosted_bots.values())
    users_cnt = len(hosted_bots)
    return (
        f"📊 <b>Admin — Server Stats</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 Server  : <code>{SERVER_NAME}</code>\n\n"
        f"💾 <b>RAM</b>\n"
        f"<code>[{make_bar(pct)}] {pct}%</code>\n"
        f"<code>{used} MB / {RAM_TOTAL_MB} MB</code>\n\n"
        f"🖥 <b>CPU</b>\n"
        f"<code>[{make_bar(cpu)}] {cpu}%</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Active Bots : <code>{cnt} / {total}</code>\n"
        f"👥 Total Users : <code>{users_cnt}</code>\n"
        f"🚫 Banned      : <code>{len(banned_users)}</code>"
    )

def txt_account(user, is_owner: bool) -> str:
    return (
        f"👤 <b>Account Info</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID       : <code>{user.id}</code>\n"
        f"👤 Name     : <b>{user.full_name}</b>\n"
        f"🔑 Username : @{user.username or 'N/A'}\n"
        f"🛡 Role     : <code>{'👑 Owner' if is_owner else '👤 User'}</code>\n"
        f"📋 Status   : <code>{'🚫 Banned' if is_banned(user.id) else '✅ Active'}</code>\n"
        f"🤖 My Bots  : <code>{len(user_bots(user.id))}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )

# Continue in next command...

# ══════════════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 You have been banned from this service.")
        return
    pending_upload.pop(user.id, None)
    await update.message.reply_text(txt_welcome(user.first_name), reply_markup=kb_main(), parse_mode="HTML")

async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        return
    t1  = time.monotonic()
    msg = await update.message.reply_text("🏓 Pinging...")
    ms  = round((time.monotonic() - t1) * 1000, 2)
    q   = ("⚡ Excellent" if ms < 100 else
           "✅ Good"      if ms < 300 else
           "⚠️ Average"   if ms < 600 else "🔴 Slow")
    await msg.edit_text(
        f"🏓 <b>Pong!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Response : <code>{ms} ms</code>\n"
        f"📶 Quality  : <code>{q}</code>\n"
        f"🌐 Service  : <code>{SERVER_NAME}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML",
    )

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Admins only.")
        return
    await update.message.reply_text(
        f"🔧 <b>Admin Panel</b>\n━━━━━━━━━━━━━━━━━━━━━\n{txt_status_owner()}",
        reply_markup=kb_admin(), parse_mode="HTML",
    )

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not ctx.args:
        await update.message.reply_text("Usage: /ban <user_id>"); return
    try:
        uid = int(ctx.args[0])
        banned_users.add(uid)
        await update.message.reply_text(f"🚫 User <code>{uid}</code> banned.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not ctx.args:
        await update.message.reply_text("Usage: /unban <user_id>"); return
    try:
        uid = int(ctx.args[0])
        banned_users.discard(uid)
        await update.message.reply_text(f"✅ User <code>{uid}</code> unbanned.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")


# ══════════════════════════════════════════════
#  BUTTON HANDLER (with ZIP upload option)
# ══════════════════════════════════════════════

async def btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    data     = q.data
    user     = q.from_user
    uid      = user.id
    is_owner = (uid == OWNER_ID)

    if is_banned(uid):
        await q.answer("🚫 You are banned!", show_alert=True)
        return
    await q.answer()

    if data == "menu_main":
        await q.edit_message_text(txt_welcome(user.first_name), reply_markup=kb_main(), parse_mode="HTML")

    elif data == "menu_bots":
        pending_upload.pop(uid, None)
        await q.edit_message_text(txt_bots(uid), reply_markup=kb_bots(uid), parse_mode="HTML")

    # ── Add bot → Choose method ───────────────
    elif data == "add_file":
        pending_upload[uid] = {"state": "choose"}
        await q.edit_message_text(
            "📤 <b>Upload Your Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Choose how to upload:\n\n"
            "📦 <b>ZIP File</b> — Upload a .zip containing:\n"
            "   • <code>bot.py</code> (or any .py file)\n"
            "   • <code>requirements.txt</code> (optional)\n\n"
            "📄 <b>Manual</b> — Upload files one by one",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Upload ZIP File", callback_data="upload_zip")],
                [InlineKeyboardButton("📄 Manual Upload",   callback_data="upload_manual")],
                [InlineKeyboardButton("❌ Cancel",          callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )

    # ── ZIP upload selected ───────────────────
    elif data == "upload_zip":
        pending_upload[uid] = {"state": "zip"}
        await q.edit_message_text(
            "📦 <b>Send ZIP File</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Send a <code>.zip</code> file containing:\n"
            "• Your bot's <code>.py</code> file\n"
            "• <code>requirements.txt</code> (optional)\n\n"
            "The ZIP will be extracted automatically!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )

    # ── Manual upload selected ────────────────
    elif data == "upload_manual":
        pending_upload[uid] = {"state": "req"}
        await q.edit_message_text(
            "📦 <b>Step 1 of 2 — Requirements</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Send your <code>requirements.txt</code> file.\n\n"
            "No extra packages needed? Tap Skip 👇",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Skip — No Requirements", callback_data="skip_req")],
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )

    # ── Skip requirements ─────────────────────
    elif data == "skip_req":
        pending_upload[uid] = {"state": "py", "req_path": None}
        await q.edit_message_text(
            "🐍 <b>Step 2 of 2 — Bot File</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Send your bot's <code>.py</code> file now.\n"
            "It will be hosted automatically ✅",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )

    elif data == "menu_account":
        await q.edit_message_text(txt_account(user, is_owner), reply_markup=kb_back(), parse_mode="HTML")

    elif data == "menu_support":
        await q.edit_message_text(
            f"🆘 <b>Support</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Welcome to <b>{BOT_NAME}</b>!\n\n"
            f"<b>Available Commands:</b>\n"
            f"• /start — Open main panel\n"
            f"• /ping  — Check response time\n\n"
            f"<b>How to host a bot:</b>\n"
            f"1️⃣ Tap 🤖 <b>Bots</b>\n"
            f"2️⃣ Tap <b>➕ Add New Bot</b>\n"
            f"3️⃣ Choose <b>ZIP</b> or <b>Manual</b> upload\n"
            f"4️⃣ Upload files\n"
            f"5️⃣ Done! View terminal for live output\n\n"
            f"<b>Need help?</b> Contact the bot owner.",
            reply_markup=kb_back(), parse_mode="HTML",
        )

    elif data == "menu_status":
        txt = txt_status_owner() if is_owner else txt_status_public()
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="menu_status")],
            [InlineKeyboardButton("🔙 Back",    callback_data="menu_main")],
        ]), parse_mode="HTML")

    elif data.startswith("bot:"):
        name = data[4:]
        if name not in user_bots(uid):
            await q.answer("Bot not found!", show_alert=True); return
        await q.edit_message_text(txt_bot(uid, name), reply_markup=kb_bot(uid, name), parse_mode="HTML")

    elif data.startswith("term:"):
        name  = data[5:]
        ubots = user_bots(uid)
        if name not in ubots:
            await q.answer("Bot not found!", show_alert=True); return
        lines = list(ubots[name].get("output_lines", []))
        await q.edit_message_text(
            fmt_terminal(lines, name),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"term:{name}")],
                [InlineKeyboardButton("🔙 Back",    callback_data=f"bot:{name}")],
            ]),
            parse_mode="HTML",
        )

    elif data.startswith("toggle:"):
        name  = data[7:]
        ubots = user_bots(uid)
        if name not in ubots:
            await q.answer("Bot not found!", show_alert=True); return
        info = ubots[name]
        if info["status"] in ("running", "starting"):
            stop_bot_process(uid, name)
        else:
            udir = get_user_bots_dir(uid)
            fp   = udir / info["file"]
            rp   = (udir / info["req_file"]) if info.get("req_file") else None
            if fp.exists():
                start_bot_process(uid, name, fp, rp)
            else:
                await q.answer("❌ Bot file missing!", show_alert=True); return
        await asyncio.sleep(0.5)
        await q.edit_message_text(txt_bot(uid, name), reply_markup=kb_bot(uid, name), parse_mode="HTML")

    elif data.startswith("del:"):
        name  = data[4:]
        ubots = user_bots(uid)
        if name in ubots:
            stop_bot_process(uid, name)
            info = ubots[name]
            udir = get_user_bots_dir(uid)
            for fname in [info.get("file"), info.get("req_file")]:
                if fname:
                    try: (udir / fname).unlink(missing_ok=True)
                    except: pass
            del ubots[name]
        await q.edit_message_text(
            f"🗑 <b>{name}</b> deleted successfully.",
            reply_markup=kb_back("menu_bots"), parse_mode="HTML",
        )

    elif data.startswith("adm_stop:"):
        if not is_owner: return
        _, target_uid_str, name = data.split(":", 2)
        target_uid = int(target_uid_str)
        stop_bot_process(target_uid, name)
        await q.edit_message_text(
            f"⏹ <b>{name}</b> stopped.\n\n{txt_status_owner()}",
            reply_markup=kb_admin(), parse_mode="HTML",
        )

    elif data.startswith("adm_start:"):
        if not is_owner: return
        _, target_uid_str, name = data.split(":", 2)
        target_uid = int(target_uid_str)
        info = user_bots(target_uid).get(name)
        if info:
            udir = get_user_bots_dir(target_uid)
            fp   = udir / info["file"]
            rp   = (udir / info["req_file"]) if info.get("req_file") else None
            if fp.exists():
                start_bot_process(target_uid, name, fp, rp)
                await asyncio.sleep(0.5)
        await q.edit_message_text(
            f"▶️ <b>{name}</b> started.\n\n{txt_status_owner()}",
            reply_markup=kb_admin(), parse_mode="HTML",
        )

    elif data == "adm_users":
        if not is_owner: return
        ban_list = ", ".join(str(u) for u in banned_users) if banned_users else "None"
        user_lines = []
        for u_id, ubots in hosted_bots.items():
            cnt = sum(1 for b in ubots.values() if b["status"] == "running")
            user_lines.append(f"• <code>{u_id}</code> — {len(ubots)} bots ({cnt} running)")
        users_text = "\n".join(user_lines) if user_lines else "None yet"
        await q.edit_message_text(
            f"👥 <b>User Management</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚫 <b>Banned IDs:</b>\n<code>{ban_list}</code>\n\n"
            f"👤 <b>Active Users:</b>\n{users_text}\n\n"
            f"<b>Commands:</b>\n"
            f"• /ban &lt;user_id&gt;\n"
            f"• /unban &lt;user_id&gt;",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Admin", callback_data="adm_back")],
            ]),
            parse_mode="HTML",
        )

    elif data == "adm_back":
        if not is_owner: return
        await q.edit_message_text(
            f"🔧 <b>Admin Panel</b>\n━━━━━━━━━━━━━━━━━━━━━\n{txt_status_owner()}",
            reply_markup=kb_admin(), parse_mode="HTML",
        )

    elif data == "adm_close":
        if not is_owner: return
        await q.edit_message_text("✅ Admin panel closed. Use /admin to reopen.")

    elif data == "noop":
        pass


# ══════════════════════════════════════════════
#  DOCUMENT HANDLER (with ZIP support!)
# ══════════════════════════════════════════════

async def doc_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    uid   = user.id

    if is_banned(uid):
        return

    doc   = update.message.document
    state = pending_upload.get(uid, {}).get("state")
    if not state:
        return   # not in upload flow

    udir = get_user_bots_dir(uid)

    # ══════════════════════════════════════════
    # STATE: "zip" — ZIP file upload
    # ══════════════════════════════════════════
    if state == "zip":
        if not doc or not doc.file_name.endswith(".zip"):
            await update.message.reply_text(
                "❌ Please send a <b>.zip</b> file.\nOr tap Cancel to go back.",
                parse_mode="HTML",
            )
            return

        msg = await update.message.reply_text("📦 <b>Extracting ZIP...</b>", parse_mode="HTML")

        # Download ZIP
        zip_path = udir / doc.file_name
        await (await doc.get_file()).download_to_drive(str(zip_path))

        # Extract
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(udir)
        except Exception as e:
            await msg.edit_text(f"❌ <b>ZIP extraction failed!</b>\n<code>{e}</code>", parse_mode="HTML")
            zip_path.unlink(missing_ok=True)
            return

        zip_path.unlink()  # delete zip after extraction

        # Find .py files and requirements.txt
        py_files = list(udir.glob("*.py"))
        req_file = udir / "requirements.txt"

        if not py_files:
            await msg.edit_text(
                "❌ <b>No .py file found in ZIP!</b>\n"
                "Make sure your ZIP contains a Python bot file.",
                parse_mode="HTML",
            )
            return

        # Use first .py file as bot
        bot_file = py_files[0]
        bot_name = bot_file.stem
        size_kb  = bot_file.stat().st_size // 1024
        req_path = req_file if req_file.exists() else None

        # Stop old instance
        ubots = user_bots(uid)
        if bot_name in ubots:
            stop_bot_process(uid, bot_name)
            await asyncio.sleep(0.5)

        await msg.edit_text("⏳ <b>Hosting bot...</b>", parse_mode="HTML")

        # Launch
        start_bot_process(uid, bot_name, bot_file, req_path)
        pending_upload.pop(uid, None)
        await asyncio.sleep(1.5)

        req_line = f"\n📋 Req    : <code>{req_path.name}</code>" if req_path else ""
        await msg.edit_text(
            f"🚀 <b>Bot Hosted from ZIP!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Name   : <code>{bot_name}</code>\n"
            f"📄 File   : <code>{bot_file.name}</code>  [{size_kb} KB]"
            f"{req_line}\n"
            f"🟡 Status  : <code>Starting...</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📟 Terminal Output", callback_data=f"term:{bot_name}")],
                [InlineKeyboardButton("🤖 View My Bots",   callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )
        return

    # ══════════════════════════════════════════
    # STATE: "req" — requirements.txt
    # ══════════════════════════════════════════
    if state == "req":
        if not doc or not doc.file_name.endswith(".txt"):
            await update.message.reply_text(
                "❌ Please send a <b>.txt</b> file (requirements.txt).\n"
                "Or tap <b>Skip</b> if no packages needed.",
                parse_mode="HTML",
            )
            return
        req_path = udir / doc.file_name
        await (await doc.get_file()).download_to_drive(str(req_path))
        pending_upload[uid] = {"state": "py", "req_path": req_path}
        await update.message.reply_text(
            f"✅ <b>Requirements received!</b> <code>{doc.file_name}</code>\n\n"
            f"🐍 <b>Step 2 of 2</b> — Now send your bot's <code>.py</code> file.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )
        return

    # ══════════════════════════════════════════
    # STATE: "py" — bot .py file
    # ══════════════════════════════════════════
    if state == "py":
        if not doc or not doc.file_name.endswith(".py"):
            await update.message.reply_text(
                "❌ Please send a <b>.py</b> Python file.",
                parse_mode="HTML",
            )
            return

        file_path = udir / doc.file_name
        await (await doc.get_file()).download_to_drive(str(file_path))

        req_path = pending_upload[uid].get("req_path")
        bot_name = file_path.stem
        size_kb  = file_path.stat().st_size // 1024

        ubots = user_bots(uid)
        if bot_name in ubots:
            stop_bot_process(uid, bot_name)
            await asyncio.sleep(0.5)

        msg = await update.message.reply_text(
            f"⏳ <b>Setting up {bot_name}...</b>",
            parse_mode="HTML",
        )

        start_bot_process(uid, bot_name, file_path, req_path)
        pending_upload.pop(uid, None)
        await asyncio.sleep(1.5)

        req_line = f"\n📋 Req    : <code>{req_path.name}</code>" if req_path else ""
        await msg.edit_text(
            f"🚀 <b>Bot Hosted!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Name   : <code>{bot_name}</code>\n"
            f"📄 File   : <code>{doc.file_name}</code>  [{size_kb} KB]"
            f"{req_line}\n"
            f"🟡 Status  : <code>Starting...</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📟 Terminal Output", callback_data=f"term:{bot_name}")],
                [InlineKeyboardButton("🤖 View My Bots",   callback_data="menu_bots")],
            ]),
            parse_mode="HTML",
        )


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════

async def main():
    BOTS_DIR.mkdir(exist_ok=True)
    print("╔══════════════════════════════════╗")
    print("║   HOSTING BOT v5 — STARTING UP   ║")
    print(f"║  Brand  : {BOT_NAME:<23}║")
    print(f"║  Server : {SERVER_NAME:<23}║")
    print("╚══════════════════════════════════╝")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping",  cmd_ping))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("ban",   cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))

    print("✅ Online! Send /start on Telegram.\n")

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            print("\n⏹ Shutting down...")
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
