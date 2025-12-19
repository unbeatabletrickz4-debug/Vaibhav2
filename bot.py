import os
import logging
import asyncio
import subprocess
import signal
import sys
import psutil
import json
import threading
import shutil
from urllib.parse import quote

from flask import Flask, request, render_template_string, jsonify
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
os.makedirs(UPLOAD_DIR, exist_ok=True)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

running_processes = {}  # {target_id: {"process": Popen, "log": log_path}}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HELPERS FOR ID TYPES ---
def is_user_file_id(tid: str) -> bool:
    # Format: u<uid>|filename.ext  (exactly one pipe)
    return (
        isinstance(tid, str)
        and tid.startswith("u")
        and ("|" in tid)
        and tid.count("|") == 1
        and tid.split("|", 1)[0][1:].isdigit()
    )

def is_repo_id(tid: str) -> bool:
    # Existing repo id format: repoName|path/to/file.ext  (can contain more pipes? usually 1)
    # We'll treat as repo when it has '|' but NOT user-file id.
    return ("|" in tid) and (not is_user_file_id(tid))

def safe_status_url(tid: str) -> str:
    # Make pipe etc safe for querystring
    return f"{BASE_URL}/status?script={quote(tid, safe='')}"

# --- FLASK SERVER & EDITOR ---
app = Flask(__name__)

EDITOR_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Universal Editor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/theme/dracula.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/python/python.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/javascript/javascript.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/shell/shell.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/dockerfile/dockerfile.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/properties/properties.min.js"></script>
    <style>
        body { margin: 0; padding: 0; background: #282a36; color: #f8f8f2; font-family: sans-serif; display: flex; flex-direction: column; height: 100vh; }
        .header { padding: 10px; background: #44475a; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #6272a4; }
        .header h3 { margin: 0; font-size: 14px; color: #8be9fd; }
        .btn { background: #50fa7b; color: #282a36; border: none; padding: 8px 15px; border-radius: 5px; font-weight: bold; cursor: pointer; }
        .CodeMirror { flex-grow: 1; font-size: 13px; }
    </style>
</head>
<body>
    <div class="header">
        <h3>📄 {{ filename }}</h3>
        <button class="btn" onclick="saveCode()">💾 Save & Restart</button>
    </div>
    <textarea id="code_area">{{ code }}</textarea>
    <script>
        var tg = window.Telegram.WebApp;
        tg.expand(); 
        
        var fname = "{{ filename }}".toLowerCase();
        var mode = "python";
        if(fname.endsWith(".js") || fname.endsWith(".json")) mode = "javascript";
        if(fname.endsWith(".sh")) mode = "shell";
        if(fname.includes("dockerfile")) mode = "dockerfile";
        if(fname.endsWith(".env") || fname.endsWith(".txt")) mode = "properties";

        var editor = CodeMirror.fromTextArea(document.getElementById("code_area"), {
            mode: mode, theme: "dracula", lineNumbers: true
        });

        function saveCode() {
            fetch('/save_code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ 
                    target_id: "{{ target_id }}", 
                    filename: "{{ filename }}",
                    code: editor.getValue()
                })
            })
            .then(r => r.json())
            .then(data => {
                if(data.status === 'success') {
                    tg.showAlert("✅ Saved & Restarting...");
                    tg.close();
                } else {
                    tg.showAlert("❌ Error: " + data.message);
                }
            });
        }
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return "🤖 Bot Host is Alive!", 200

@app.route("/status")
def script_status():
    script_name = request.args.get("script")
    if not script_name:
        return "Specify script", 400

    # Note: Flask already decodes querystring; keys should match running_processes
    if (
        script_name in running_processes
        and running_processes[script_name]["process"].poll() is None
    ):
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

@app.route("/editor")
def editor_page():
    target_id = request.args.get("id")
    filename = request.args.get("file")
    uid = int(request.args.get("uid", 0))

    owner = get_owner(target_id)
    if uid != ADMIN_ID and uid != owner:
        return "⛔ Access Denied"

    work_dir, _, _, _, _ = resolve_paths(target_id)
    file_path = os.path.join(work_dir, filename)

    if not os.path.abspath(file_path).startswith(os.path.abspath(work_dir)):
        return "⛔ Security Block."

    content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

    return render_template_string(
        EDITOR_HTML, code=content, target_id=target_id, filename=filename
    )

@app.route("/save_code", methods=["POST"])
def save_code_route():
    data = request.json or {}
    target_id = data.get("target_id")
    filename = data.get("filename")
    code = data.get("code", "")

    if not target_id or not filename:
        return jsonify({"status": "error", "message": "Missing target_id/filename"})

    work_dir, _, _, _, _ = resolve_paths(target_id)
    file_path = os.path.join(work_dir, filename)

    # Security: block escaping work_dir
    if not os.path.abspath(file_path).startswith(os.path.abspath(work_dir)):
        return jsonify({"status": "error", "message": "Security Block."})

    try:
        os.makedirs(work_dir, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Smart install for edited files
        if filename.endswith(".txt") or filename == "requirements.txt":
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", file_path])
        elif filename == "package.json":
            subprocess.check_call(["npm", "install"], cwd=work_dir)

        restart_process_background(target_id)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

def resolve_run_command(script_path: str):
    ext = script_path.split(".")[-1].lower()
    if ext == "js":
        return ["node", script_path]
    if ext == "sh":
        return ["bash", script_path]
    return ["python", "-u", script_path]

def restart_process_background(target_id: str):
    work_dir, script_path, env_path, _, _ = resolve_paths(target_id)

    # Stop previous
    if target_id in running_processes:
        try:
            os.killpg(os.getpgid(running_processes[target_id]["process"].pid), signal.SIGTERM)
        except Exception:
            pass

    # Build env
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8", errors="ignore") as f:
            for l in f:
                if "=" in l and not l.strip().startswith("#"):
                    k, v = l.strip().split("=", 1)
                    custom_env[k.strip()] = v.strip().strip('"').strip("'")

    # Log path
    log_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_path, "w", encoding="utf-8")

    cmd = resolve_run_command(script_path)
    try:
        os.makedirs(work_dir, exist_ok=True)
        proc = subprocess.Popen(
            cmd,
            env=custom_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            preexec_fn=os.setsid,
        )
        running_processes[target_id] = {"process": proc, "log": log_path}
    except Exception as e:
        logger.error(f"Failed to restart: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- UTILS & DATA ---
def get_allowed_users():
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_allowed_user(uid):
    users = get_allowed_users()
    if uid not in users:
        users.append(uid)
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f)
        return True
    return False

def remove_allowed_user(uid):
    users = get_allowed_users()
    if uid in users:
        users.remove(uid)
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f)
        return True
    return False

def load_ownership():
    if not os.path.exists(OWNERSHIP_FILE):
        return {}
    try:
        with open(OWNERSHIP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_ownership(target_id, user_id, type_):
    data = {}
    if os.path.exists(OWNERSHIP_FILE):
        with open(OWNERSHIP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    data[target_id] = {"owner": user_id, "type": type_}
    with open(OWNERSHIP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def delete_ownership(target_id):
    if not os.path.exists(OWNERSHIP_FILE):
        return
    with open(OWNERSHIP_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if target_id in data:
        del data[target_id]
    with open(OWNERSHIP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def get_owner(target_id):
    data = load_ownership()
    return data.get(target_id, {}).get("owner")

def resolve_paths(target_id):
    """
    Supported target_id formats:
    1) User upload file:   u<uid>|filename.py
       Workdir: scripts/<uid>/
       Env:    scripts/<uid>/.env
       Req:    scripts/<uid>/requirements.txt
    2) Repo mode:         repoName|path/to/main.py
       Workdir: scripts/<repoName>/
       Env:    scripts/<repoName>/.env
       Req:    scripts/<repoName>/requirements.txt
    3) Legacy:            filename.py (stored directly in scripts/)
    """
    # NEW: per-user uploaded file mode
    if is_user_file_id(target_id):
        u, filename = target_id.split("|", 1)  # u123|test.py
        uid = u[1:]
        work_dir = os.path.join(UPLOAD_DIR, uid)
        script_path = filename
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
        return work_dir, script_path, env_path, req_path, full_script_path

    # Existing repo mode
    if is_repo_id(target_id):
        repo, file = target_id.split("|", 1)
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
        return work_dir, script_path, env_path, req_path, full_script_path

    # Legacy single file in scripts/
    work_dir = UPLOAD_DIR
    script_path = target_id
    env_path = os.path.join(work_dir, f"{target_id}.env")
    req_path = os.path.join(work_dir, f"{target_id}_req.txt")
    full_script_path = os.path.join(work_dir, target_id)
    return work_dir, script_path, env_path, req_path, full_script_path

async def install_dependencies(work_dir, update):
    msg = None
    try:
        if os.path.exists(os.path.join(work_dir, "requirements.txt")):
            if not msg:
                msg = await update.message.reply_text("⏳ Installing Python Deps...")
            proc = await asyncio.create_subprocess_exec(
                "pip",
                "install",
                "-r",
                "requirements.txt",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        if os.path.exists(os.path.join(work_dir, "package.json")):
            if not msg:
                msg = await update.message.reply_text("⏳ Installing Node Deps...")
            else:
                await msg.edit_text("⏳ Installing Node Deps...")
            proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        if msg:
            await msg.edit_text("✅ Dependencies Installed!")
    except Exception as e:
        if msg:
            await msg.edit_text(f"❌ Error: {e}")

# --- DECORATORS ---
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID and update.effective_user.id not in get_allowed_users():
            await update.message.reply_text("⛔ Access Denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Super Admin Only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- KEYBOARDS ---
def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [["📤 Upload File", "🌐 Clone from Git"], ["📂 My Hosted Apps", "📊 Server Stats"], ["🆘 Help"]],
        resize_keyboard=True,
    )

def extras_keyboard():
    return ReplyKeyboardMarkup([["➕ Add Deps", "📝 Type Env Vars"], ["🚀 RUN NOW", "🔙 Cancel"]], resize_keyboard=True)

def git_extras_keyboard():
    return ReplyKeyboardMarkup([["📝 Type Env Vars"], ["📂 Select File to Run", "🔙 Cancel"]], resize_keyboard=True)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- HANDLERS ---
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Mega Hosting Bot**", reply_markup=main_menu_keyboard())

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📤 Send file (.py, .js, .sh)",
        reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True),
    )
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel":
        return await cancel(update, context)

    doc = update.message.document
    if not doc:
        return WAIT_FILE

    file = await doc.get_file()
    fname = doc.file_name
    uid = update.effective_user.id

    if not fname.endswith((".py", ".js", ".sh")):
        await update.message.reply_text("❌ Invalid type. Only .py/.js/.sh")
        return WAIT_FILE

    # ✅ NEW: Per-user folder (no overwrite between users)
    user_dir = os.path.join(UPLOAD_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)

    path = os.path.join(user_dir, fname)
    await file.download_to_drive(path)

    # ✅ NEW: Unique ID per user
    unique_id = f"u{uid}|{fname}"

    save_ownership(unique_id, uid, "file")
    context.user_data.update(
        {"type": "file", "target_id": unique_id, "work_dir": user_dir}
    )

    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text

    if txt == "🚀 RUN NOW":
        return await execute_logic(update, context)
    if txt == "🔙 Cancel":
        return await cancel(update, context)

    if txt == "📝 Type Env Vars":
        await update.message.reply_text(
            "📝 **Type Env (KEY=VALUE per line):**",
            reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True),
            parse_mode="Markdown",
        )
        return WAIT_ENV_TEXT

    if "Deps" in txt:
        await update.message.reply_text("📂 Send `requirements.txt` or `package.json`")
        context.user_data["wait"] = "deps"
        return WAIT_EXTRAS

    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel":
        return await cancel(update, context)

    target_id = context.user_data.get("target_id")
    if not target_id:
        await update.message.reply_text("❌ No target selected.")
        return ConversationHandler.END

    _, _, env_path, _, _ = resolve_paths(target_id)

    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "a", encoding="utf-8") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0:
            f.write("\n")
        f.write(update.message.text.strip())

    if context.user_data.get("type") == "repo":
        await update.message.reply_text("✅ Saved.", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS

    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("wait") != "deps":
        return WAIT_EXTRAS

    doc = update.message.document
    if not doc:
        return WAIT_EXTRAS

    file = await doc.get_file()
    fname = doc.file_name

    # ✅ NEW: install deps inside the correct work_dir (per-user or repo)
    target_id = context.user_data.get("target_id")
    work_dir = context.user_data.get("work_dir")
    if not work_dir and target_id:
        work_dir, _, _, _, _ = resolve_paths(target_id)

    if not work_dir:
        await update.message.reply_text("❌ Work directory not found.")
        context.user_data["wait"] = None
        return WAIT_EXTRAS

    os.makedirs(work_dir, exist_ok=True)

    if fname == "requirements.txt":
        save_path = os.path.join(work_dir, "requirements.txt")
    elif fname == "package.json":
        save_path = os.path.join(work_dir, "package.json")
    else:
        await update.message.reply_text("❌ Only requirements.txt or package.json allowed.")
        return WAIT_EXTRAS

    await file.download_to_drive(save_path)

    msg = await update.message.reply_text("⏳ Installing Dependencies...")
    try:
        if fname == "requirements.txt":
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        else:
            proc = await asyncio.create_subprocess_exec(
                "npm", "install",
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

        await msg.edit_text("✅ Installed!")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

    context.user_data["wait"] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌐 **Git URL**",
        reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True),
        parse_mode="Markdown",
    )
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url == "🔙 Cancel":
        return await cancel(update, context)

    uid = update.effective_user.id

    # ✅ NEW: per-user unique repo folder name to avoid collisions
    base_repo = url.split("/")[-1].replace(".git", "")
    repo_name = f"{base_repo}_u{uid}"
    repo_path = os.path.join(UPLOAD_DIR, repo_name)

    if os.path.exists(repo_path):
        shutil.rmtree(repo_path)

    try:
        subprocess.check_call(["git", "clone", url, repo_path])
        await install_dependencies(repo_path, update)
        context.user_data.update(
            {
                "repo_path": repo_path,
                "repo_name": repo_name,
                "target_id": f"{repo_name}|PLACEHOLDER",
                "type": "repo",
                "work_dir": repo_path,
            }
        )
        await update.message.reply_text("⚙️ **Setup**", reply_markup=git_extras_keyboard(), parse_mode="Markdown")
        return WAIT_GIT_EXTRAS
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def receive_git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel":
        return await cancel(update, context)

    if txt == "📝 Type Env Vars":
        await update.message.reply_text(
            "📝 **Type Env (KEY=VALUE per line):**",
            reply_markup=ReplyKeyboardMarkup([["🔙 Cancel"]], resize_keyboard=True),
            parse_mode="Markdown",
        )
        return WAIT_GIT_ENV_TEXT

    if txt == "📂 Select File to Run":
        return await show_file_selection(update, context)

    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data.get("repo_path")
    if not repo_path:
        return await update.message.reply_text("❌ Repo not found.")

    files = []
    for root, _, fs in os.walk(repo_path):
        for f in fs:
            if f.endswith((".py", ".js", ".sh")):
                files.append(os.path.relpath(os.path.join(root, f), repo_path))

    if not files:
        return await update.message.reply_text("❌ No executable files.")

    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_py_{f}")] for f in files[:15]]
    await update.message.reply_text("👇 **Select:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return WAIT_SELECT_FILE

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    filename = query.data.split("sel_py_")[1]
    repo_name = context.user_data.get("repo_name")
    unique_id = f"{repo_name}|{filename}"

    save_ownership(unique_id, update.effective_user.id, "repo")
    context.user_data["target_id"] = unique_id

    await query.edit_message_text(f"✅ Selected `{filename}`", parse_mode="Markdown")
    return await execute_logic(query, context)

async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if getattr(update, "message", None) else update.callback_query.message.reply_text

    target_id = context.user_data.get("target_id", context.user_data.get("fallback_id"))
    if not target_id:
        await msg_func("❌ No target selected.")
        return ConversationHandler.END

    restart_process_background(target_id)

    url = safe_status_url(target_id)
    await msg_func(f"🚀 **Launched!**\n🔗 `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- MANAGE HANDLER ---
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE):
        return await update.message.reply_text("📂 Empty.")

    with open(OWNERSHIP_FILE, "r", encoding="utf-8") as f:
        ownership = json.load(f)

    keyboard = []
    for tid, meta in ownership.items():
        owner_id = meta.get("owner")
        if uid == ADMIN_ID or uid == owner_id:
            is_running = tid in running_processes and running_processes[tid]["process"].poll() is None
            status = "🟢" if is_running else "🔴"

            label = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner_id:
                label += f" (👤 {owner_id})"

            keyboard.append([InlineKeyboardButton(label, callback_data=f"man_{tid}")])

    if not keyboard:
        return await update.message.reply_text("📂 No apps.")

    await update.message.reply_text("📂 **Select App:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("man_"):
        tid = data.split("man_")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner:
            return await query.message.reply_text("⛔ Not yours.")

        work_dir, script_path, env_path, req_path, _ = resolve_paths(tid)
        is_running = tid in running_processes and running_processes[tid]["process"].poll() is None
        status = "🟢 Running" if is_running else "🔴 Stopped"

        admin_info = ""
        if uid == ADMIN_ID:
            admin_info = f"\n👤 **Owner:** `{owner}`"

        text = f"⚙️ **App:** `{tid}`{admin_info}\nStatus: {status}"
        btns = []

        row1 = []
        if is_running:
            row1.append(InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{tid}"))
            row1.append(InlineKeyboardButton("🔗 URL", callback_data=f"url_{tid}"))
        else:
            row1.append(InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{tid}"))
        btns.append(row1)

        # Editable files
        editable_files = []
        if os.path.exists(os.path.join(work_dir, script_path)):
            editable_files.append(script_path)

        # ✅ NEW: Treat user-file ids like "non-repo" (show .env and requirements if present)
        if is_user_file_id(tid):
            common_files = [".env", "requirements.txt", "package.json"]
            for f in common_files:
                if os.path.exists(os.path.join(work_dir, f)):
                    editable_files.append(f)

        elif not is_repo_id(tid):
            # Legacy single-file mode
            if os.path.exists(env_path):
                editable_files.append(os.path.basename(env_path))
            if os.path.exists(req_path):
                editable_files.append(os.path.basename(req_path))

        else:
            # Repo mode
            common_files = [".env", "requirements.txt", "package.json", "Dockerfile", "docker-compose.yml"]
            for f in common_files:
                if os.path.exists(os.path.join(work_dir, f)):
                    editable_files.append(f)

        file_btns = []
        for f in editable_files:
            label = "✏️ Main Code" if f == script_path else f"✏️ {f}"
            url = f"{BASE_URL}/editor?id={quote(tid, safe='')}&file={quote(f, safe='')}&uid={uid}"
            file_btns.append(InlineKeyboardButton(label, web_app=WebAppInfo(url=url)))

        for i in range(0, len(file_btns), 2):
            btns.append(file_btns[i : i + 2])

        btns.append(
            [InlineKeyboardButton("📜 Logs", callback_data=f"log_{tid}"),
             InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{tid}")]
        )

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        tid = data.split("stop_")[1]
        if tid in running_processes:
            try:
                os.killpg(os.getpgid(running_processes[tid]["process"].pid), signal.SIGTERM)
            except Exception:
                pass
            await query.edit_message_text(f"🛑 Stopped `{tid}`", parse_mode="Markdown")

    elif data.startswith("rerun_"):
        context.user_data["fallback_id"] = data.split("rerun_")[1]
        await query.delete_message()
        await execute_logic(update, context)

    elif data.startswith("del_"):
        tid = data.split("del_")[1]

        if tid in running_processes:
            try:
                os.killpg(os.getpgid(running_processes[tid]["process"].pid), signal.SIGTERM)
            except Exception:
                pass
            del running_processes[tid]

        delete_ownership(tid)

        work_dir, script_path, _, _, _ = resolve_paths(tid)

        # ✅ NEW: Delete correct folder/file depending on type
        if is_repo_id(tid):
            shutil.rmtree(work_dir, ignore_errors=True)
        elif is_user_file_id(tid):
            # delete only that one script file (keep user's folder for other scripts)
            try:
                os.remove(os.path.join(work_dir, script_path))
            except Exception:
                pass
        else:
            # legacy
            try:
                os.remove(os.path.join(UPLOAD_DIR, tid))
            except Exception:
                pass

        await query.edit_message_text(f"🗑️ Deleted `{tid}`", parse_mode="Markdown")

    elif data.startswith("log_"):
        tid = data.split("log_")[1]
        path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(path):
            await context.bot.send_document(chat_id=update.effective_chat.id, document=open(path, "rb"))
        else:
            await query.message.reply_text("❌ No logs.")

    elif data.startswith("url_"):
        tid = data.split("url_")[1]
        await query.message.reply_text(f"🔗 `{safe_status_url(tid)}`", parse_mode="Markdown")

@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /add <user_id>")
    if save_allowed_user(int(context.args[0])):
        await update.message.reply_text("✅ Added.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /remove <user_id>")
    if remove_allowed_user(int(context.args[0])):
        await update.message.reply_text("🗑️ Removed.")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Running: {len(running_processes)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **Help**\nContact: @platoonleaderr", parse_mode="Markdown")

if __name__ == "__main__":
    # Start Flask (Render web port)
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

    if not TOKEN:
        print("❌ ERROR: TOKEN env var not set")
        sys.exit(1)

    app_bot = ApplicationBuilder().token(TOKEN).build()

    conv_file = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload File$"), upload_start)],
        states={
            WAIT_FILE: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Document.ALL, receive_file),
            ],
            WAIT_EXTRAS: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Regex("^(🚀 RUN NOW|➕ Add Deps|📝 Type Env Vars)$"), receive_extras),
                MessageHandler(filters.Document.ALL, receive_extra_files),
            ],
            WAIT_ENV_TEXT: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_env_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    conv_git = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🌐 Clone from Git$"), git_start)],
        states={
            WAIT_URL: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_git_url),
            ],
            WAIT_GIT_EXTRAS: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.Regex("^(📝 Type Env Vars|📂 Select File to Run)$"), receive_git_extras),
            ],
            WAIT_GIT_ENV_TEXT: [
                MessageHandler(filters.Regex("^🔙 Cancel$"), cancel),
                MessageHandler(filters.TEXT, receive_env_text),
            ],
            WAIT_SELECT_FILE: [CallbackQueryHandler(select_git_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app_bot.add_handler(CommandHandler("add", add_user))
    app_bot.add_handler(CommandHandler("remove", remove_user))
    app_bot.add_handler(conv_file)
    app_bot.add_handler(conv_git)
    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Hosted Apps$"), list_hosted))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(MessageHandler(filters.Regex("^🆘 Help$"), help_command))
    app_bot.add_handler(CallbackQueryHandler(manage_callback))
    app_bot.add_handler(CommandHandler("start", start))

    print("Bot is up and running!")
    app_bot.run_polling()
