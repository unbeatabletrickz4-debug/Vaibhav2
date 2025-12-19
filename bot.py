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
from flask import Flask, request, render_template_string, jsonify
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

running_processes = {} 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

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

@app.route('/')
def home(): return "🤖 Bot Host is Alive!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if not script_name: return "Specify script", 400
    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

@app.route('/editor')
def editor_page():
    target_id = request.args.get('id')
    filename = request.args.get('file')
    uid = int(request.args.get('uid', 0))
    
    owner = get_owner(target_id)
    if uid != ADMIN_ID and uid != owner: return "⛔ Access Denied"
    
    work_dir, _, _, _, _ = resolve_paths(target_id)
    file_path = os.path.join(work_dir, filename)
    
    if not os.path.abspath(file_path).startswith(os.path.abspath(work_dir)):
        return "⛔ Security Block."

    content = ""
    if os.path.exists(file_path):
        with open(file_path, 'r') as f: content = f.read()
    
    return render_template_string(EDITOR_HTML, code=content, target_id=target_id, filename=filename)

@app.route('/save_code', methods=['POST'])
def save_code_route():
    data = request.json
    target_id = data.get('target_id')
    filename = data.get('filename')
    code = data.get('code')
    
    work_dir, _, _, _, _ = resolve_paths(target_id)
    file_path = os.path.join(work_dir, filename)

    try:
        with open(file_path, 'w') as f: f.write(code)
        
        # Smart Install Logic
        if filename.endswith(".txt") or filename == "requirements.txt":
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", file_path])
        elif filename == "package.json":
            subprocess.check_call(["npm", "install"], cwd=work_dir)
        
        restart_process_background(target_id)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

def resolve_run_command(script_path):
    ext = script_path.split('.')[-1].lower()
    if ext == 'js': return ["node", script_path]
    if ext == 'sh': return ["bash", script_path]
    return ["python", "-u", script_path] 

def restart_process_background(target_id):
    work_dir, script_path, env_path, _, _ = resolve_paths(target_id)
    if target_id in running_processes:
        try: os.killpg(os.getpgid(running_processes[target_id]['process'].pid), signal.SIGTERM)
        except: pass
    
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=', 1)
                    custom_env[k.strip()] = v.strip().strip('"').strip("'")
    
    log_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_path, "w")
    
    cmd = resolve_run_command(script_path)
    try:
        proc = subprocess.Popen(cmd, env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, cwd=work_dir, preexec_fn=os.setsid)
        running_processes[target_id] = {"process": proc, "log": log_path}
    except Exception as e:
        logger.error(f"Failed to restart: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- UTILS ---
def get_allowed_users():
    if not os.path.exists(USERS_FILE): return []
    try:
        with open(USERS_FILE, 'r') as f: return json.load(f)
    except: return []

def save_allowed_user(uid):
    users = get_allowed_users()
    if uid not in users:
        users.append(uid)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

def remove_allowed_user(uid):
    users = get_allowed_users()
    if uid in users:
        users.remove(uid)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

def load_ownership():
    if not os.path.exists(OWNERSHIP_FILE): return {}
    try:
        with open(OWNERSHIP_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_ownership(target_id, user_id, type_):
    data = {}
    if os.path.exists(OWNERSHIP_FILE):
        with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
    data[target_id] = {"owner": user_id, "type": type_}
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def delete_ownership(target_id):
    if not os.path.exists(OWNERSHIP_FILE): return
    with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
    if target_id in data: del data[target_id]
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def get_owner(target_id):
    data = load_ownership()
    return data.get(target_id, {}).get("owner")

def resolve_paths(target_id):
    if "|" in target_id:
        repo, file = target_id.split("|")
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
    else:
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
            if not msg: msg = await update.message.reply_text("⏳ Installing Python Deps...")
            proc = await asyncio.create_subprocess_exec("pip", "install", "-r", "requirements.txt", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
        
        if os.path.exists(os.path.join(work_dir, "package.json")):
            if not msg: msg = await update.message.reply_text("⏳ Installing Node Deps...")
            else: await msg.edit_text("⏳ Installing Node Deps...")
            proc = await asyncio.create_subprocess_exec("npm", "install", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            
        if msg: await msg.edit_text("✅ Dependencies Installed!")
    except Exception as e:
        if msg: await msg.edit_text(f"❌ Error: {e}")

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
    return ReplyKeyboardMarkup([["📤 Upload File", "🌐 Clone from Git"], ["📂 My Hosted Apps", "📊 Server Stats"], ["🆘 Help"]], resize_keyboard=True)

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
    await update.message.reply_text("📤 Send file (.py, .js, .sh)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    if not fname.endswith(('.py', '.js', '.sh')): return await update.message.reply_text("❌ Invalid type.")
    
    path = os.path.join(UPLOAD_DIR, fname)
    await file.download_to_drive(path)
    save_ownership(fname, uid, "file")
    context.user_data.update({'type': 'file', 'target_id': fname, 'work_dir': UPLOAD_DIR})
    await update.message.reply_text(f"✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW": return await execute_logic(update, context)
    elif txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_ENV_TEXT
    elif "Deps" in txt:
        await update.message.reply_text("📂 Send `requirements.txt`/`package.json`")
        context.user_data['wait'] = 'req'
    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    target_id = context.user_data['target_id']
    _, _, env_path, _, _ = resolve_paths(target_id)
    with open(env_path, "a") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
        f.write(update.message.text)
    if context.user_data.get('type') == 'repo': 
        await update.message.reply_text("✅ Saved.", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('wait'): return WAIT_EXTRAS
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    target_id = context.user_data['target_id']
    
    path = ""
    if fname == "package.json":
        path = os.path.join(UPLOAD_DIR, "package.json")
    elif fname.endswith(".txt"):
        path = os.path.join(UPLOAD_DIR, f"{target_id}_req.txt")
    
    if path:
        await file.download_to_drive(path)
        msg = await update.message.reply_text("⏳ **Installing Dependencies...**")
        try:
            if fname.endswith(".txt"):
                proc = await asyncio.create_subprocess_exec("pip", "install", "-r", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
            elif fname == "package.json":
                proc = await asyncio.create_subprocess_exec("npm", "install", cwd=UPLOAD_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
            await msg.edit_text("✅ **Installed!**")
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}")
        
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 **Git URL**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url == "🔙 Cancel": return await cancel(update, context)
    repo_name = url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(UPLOAD_DIR, repo_name)
    if os.path.exists(repo_path): shutil.rmtree(repo_path)
    try:
        subprocess.check_call(["git", "clone", url, repo_path])
        await install_dependencies(repo_path, update)
        context.user_data.update({'repo_path': repo_path, 'repo_name': repo_name, 'target_id': f"{repo_name}|PLACEHOLDER", 'type': 'repo', 'work_dir': repo_path})
        await update.message.reply_text("⚙️ **Setup**", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def receive_git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_GIT_ENV_TEXT
    elif txt == "📂 Select File to Run": return await show_file_selection(update, context)
    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data['repo_path']
    files = []
    for root, _, fs in os.walk(repo_path):
        for f in fs:
            if f.endswith(('.py', '.js', '.sh')): files.append(os.path.relpath(os.path.join(root, f), repo_path))
    if not files: return await update.message.reply_text("❌ No executable files.")
    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_py_{f}")] for f in files[:15]]
    await update.message.reply_text("👇 **Select:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_SELECT_FILE

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filename = query.data.split("sel_py_")[1]
    repo_name = context.user_data['repo_name']
    unique_id = f"{repo_name}|{filename}"
    save_ownership(unique_id, update.effective_user.id, "repo")
    context.user_data['target_id'] = unique_id
    await query.edit_message_text(f"✅ Selected `{filename}`")
    return await execute_logic(query, context)

async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    restart_process_background(target_id)
    url = f"{BASE_URL}/status?script={target_id}"
    await msg_func(f"🚀 **Launched!**\n🔗 `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- MANAGE HANDLER (UPDATED with Admin Info) ---
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE): return await update.message.reply_text("📂 Empty.")
    with open(OWNERSHIP_FILE) as f: ownership = json.load(f)
    
    keyboard = []
    for tid, meta in ownership.items():
        owner_id = meta.get("owner")
        # Logic: Admin sees all (with ID info), Users see theirs
        if uid == ADMIN_ID or uid == owner_id:
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            
            # --- FEATURE ADDITION: Admin Info in Label ---
            label = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner_id:
                label += f" (👤 {owner_id})"
                
            keyboard.append([InlineKeyboardButton(label, callback_data=f"man_{tid}")])
    
    if not keyboard: return await update.message.reply_text("📂 No apps.")
    await update.message.reply_text("📂 **Select App:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("man_"):
        tid = data.split("man_")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner: return await query.message.reply_text("⛔ Not yours.")
        
        work_dir, script_path, env_path, req_path, _ = resolve_paths(tid)
        is_running = tid in running_processes and running_processes[tid]['process'].poll() is None
        status = "🟢 Running" if is_running else "🔴 Stopped"
        
        # --- FEATURE ADDITION: Admin Info in Text ---
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
        
        editable_files = []
        if os.path.exists(os.path.join(work_dir, script_path)): editable_files.append(script_path)
        if "|" not in tid:
            if os.path.exists(env_path): editable_files.append(os.path.basename(env_path))
            if os.path.exists(req_path): editable_files.append(os.path.basename(req_path))
        else:
            common_files = [".env", "requirements.txt", "package.json", "Dockerfile", "docker-compose.yml"]
            for f in common_files:
                if os.path.exists(os.path.join(work_dir, f)): editable_files.append(f)
        
        file_btns = []
        for f in editable_files:
            label = "✏️ Main Code" if f == script_path else f"✏️ {f}"
            url = f"{BASE_URL}/editor?id={tid}&file={f}&uid={uid}"
            file_btns.append(InlineKeyboardButton(label, web_app=WebAppInfo(url=url)))
        
        for i in range(0, len(file_btns), 2):
            btns.append(file_btns[i:i+2])

        btns.append([InlineKeyboardButton("📜 Logs", callback_data=f"log_{tid}"), InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{tid}")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        tid = data.split("stop_")[1]
        if tid in running_processes:
            os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            await query.edit_message_text(f"🛑 Stopped `{tid}`")
    
    elif data.startswith("rerun_"):
        context.user_data['fallback_id'] = data.split("rerun_")[1]
        await query.delete_message()
        await execute_logic(update, context)

    elif data.startswith("del_"):
        tid = data.split("del_")[1]
        if tid in running_processes:
            try: os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except: pass
            del running_processes[tid]
        delete_ownership(tid)
        work_dir, _, _, _, _ = resolve_paths(tid)
        if "|" in tid: shutil.rmtree(work_dir, ignore_errors=True)
        else: 
             try: os.remove(os.path.join(UPLOAD_DIR, tid))
             except: pass
        await query.edit_message_text(f"🗑️ Deleted `{tid}`")

    elif data.startswith("log_"):
        tid = data.split("log_")[1]
        path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(path): await context.bot.send_document(chat_id=update.effective_chat.id, document=open(path, 'rb'))
        else: await query.message.reply_text("❌ No logs.")

    elif data.startswith("url_"):
        tid = data.split("url_")[1]
        await query.message.reply_text(f"🔗 `{BASE_URL}/status?script={tid}`", parse_mode="Markdown")

@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if save_allowed_user(int(context.args[0])): await update.message.reply_text("✅ Added.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if remove_allowed_user(int(context.args[0])): await update.message.reply_text("🗑️ Removed.")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Running: {len(running_processes)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **Help**\nContact: @platoonleaderr", parse_mode="Markdown")

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    conv_file = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload File$"), upload_start)],
        states={
            WAIT_FILE: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Document.ALL, receive_file)],
            WAIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(🚀|➕|📝)"), receive_extras), MessageHandler(filters.Document.ALL, receive_extra_files)],
            WAIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)]
        }, fallbacks=[CommandHandler('cancel', cancel)], per_message=False
    )

    conv_git = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🌐 Clone from Git$"), git_start)],
        states={
            WAIT_URL: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_git_url)],
            WAIT_GIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(📝|📂)"), receive_git_extras)],
            WAIT_GIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)],
            WAIT_SELECT_FILE: [CallbackQueryHandler(select_git_file)]
        }, fallbacks=[CommandHandler('cancel', cancel)], per_message=False
    )
    
    app_bot.add_handler(CommandHandler('add', add_user))
    app_bot.add_handler(CommandHandler('remove', remove_user))
    app_bot.add_handler(conv_file)
    app_bot.add_handler(conv_git)
    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Hosted Apps$"), list_hosted))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(MessageHandler(filters.Regex("^🆘 Help$"), help_command))
    app_bot.add_handler(CallbackQueryHandler(manage_callback))
    app_bot.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    app_bot.run_polling()