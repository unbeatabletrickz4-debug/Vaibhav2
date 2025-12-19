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
import time
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
    <title>Live Editor</title>
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
        body { margin: 0; padding: 0; background: #282a36; color: #f8f8f2; font-family: monospace; display: flex; flex-direction: column; height: 100vh; }
        .header { padding: 12px; background: #44475a; display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #6272a4; }
        .header h3 { margin: 0; font-size: 14px; color: #8be9fd; text-overflow: ellipsis; white-space: nowrap; overflow: hidden; max-width: 60%; }
        .btn { background: #50fa7b; color: #282a36; border: none; padding: 8px 15px; border-radius: 6px; font-weight: bold; cursor: pointer; }
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
                    tg.showAlert("✅ Code Saved & Script Restarting...");
                    setTimeout(() => tg.close(), 1000);
                } else {
                    tg.showAlert("❌ Error: " + data.message);
                }
            })
            .catch(err => {
                tg.showAlert("❌ Connection Error: " + err);
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return "🤖 Bot is Active & Running!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if not script_name: return "Specify script", 400
    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

@app.route('/editor')
def editor_page():
    try:
        target_id = request.args.get('id')
        filename = request.args.get('file')
        # FIX 1: TYPE CASTING AND DEFAULT HANDLING
        uid_arg = request.args.get('uid', '0')
        uid = int(uid_arg) if uid_arg.isdigit() else 0
    except Exception as e:
        return f"❌ Invalid Request Parameters: {e}"
    
    owner = get_owner(target_id)
    
    # FIX 2: ROBUST OWNER CHECK (Compare Integers)
    # Check if user is Admin OR User is Owner
    if uid != ADMIN_ID and uid != int(owner or 0):
        return f"⛔ Access Denied. \nYour ID: {uid}\nOwner ID: {owner}"
    
    work_dir, _, _, _, _ = resolve_paths(target_id)
    file_path = os.path.join(work_dir, filename)
    
    # Security: Directory Traversal Check
    if not os.path.abspath(file_path).startswith(os.path.abspath(UPLOAD_DIR)):
        return "⛔ Security Alert: File path manipulation detected."

    content = ""
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f: 
                content = f.read()
        except UnicodeDecodeError:
            return "❌ Cannot edit binary files."
    else:
        return f"❌ File not found: {filename}"
    
    return render_template_string(EDITOR_HTML, code=content, target_id=target_id, filename=filename)

@app.route('/save_code', methods=['POST'])
def save_code_route():
    try:
        data = request.json
        target_id = data.get('target_id')
        filename = data.get('filename')
        code = data.get('code')
        
        work_dir, _, _, _, _ = resolve_paths(target_id)
        file_path = os.path.join(work_dir, filename)

        with open(file_path, 'w', encoding='utf-8') as f: 
            f.write(code)
        
        # Smart Install Reqs if file modified
        if filename.endswith("requirements.txt") or filename.endswith("_req.txt"):
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", file_path])
        elif filename == "package.json":
            subprocess.call(["npm", "install"], cwd=work_dir)
        
        # Restart Process
        threading.Thread(target=restart_process_background, args=(target_id,)).start()
        
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f"Save Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# --- RUNNER ENGINE ---
def resolve_run_command(script_path):
    # Ensure correct extension detection
    ext = script_path.split('.')[-1].lower()
    if ext == 'js': return ["node", script_path]
    if ext == 'sh': return ["bash", script_path]
    # Default Python, -u is critical for logging
    return ["python", "-u", script_path]

def restart_process_background(target_id):
    # Short delay to ensure write operations complete
    time.sleep(1)
    
    work_dir, script_path, env_path, _, full_script_path = resolve_paths(target_id)
    
    # Validation: Ensure script exists before running
    if not os.path.exists(full_script_path):
        logger.error(f"Cannot restart: File missing {full_script_path}")
        return

    # Kill existing
    if target_id in running_processes:
        try: 
            proc = running_processes[target_id]['process']
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except: pass
    
    # Env Prep
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k, v = l.strip().split('=', 1)
                    custom_env[k.strip()] = v.strip().strip('"').strip("'")
    
    # Logs
    log_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_path, "w")
    
    cmd = resolve_run_command(script_path)
    
    try:
        proc = subprocess.Popen(
            cmd, 
            env=custom_env, 
            stdout=log_file, 
            stderr=subprocess.STDOUT, 
            cwd=work_dir, 
            preexec_fn=os.setsid
        )
        running_processes[target_id] = {"process": proc, "log": log_path}
    except Exception as e:
        logger.error(f"Process Launch Error: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- HELPER FUNCS ---
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
        try:
            with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
        except: pass # Corrupt file reset
    data[target_id] = {"owner": int(user_id), "type": type_} # Save ID as INT
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def delete_ownership(target_id):
    if not os.path.exists(OWNERSHIP_FILE): return
    with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
    if target_id in data: del data[target_id]
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def get_owner(target_id):
    data = load_ownership()
    val = data.get(target_id, {}).get("owner")
    return int(val) if val is not None else None

def resolve_paths(target_id):
    # Logic to distinguish between Repo Mode and Single File Mode
    if "|" in target_id:
        # Repo Mode: ID is "reponame|main.py"
        repo, file = target_id.split("|")
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file # Relative to repo root
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
    else:
        # Single File Mode: ID is "script.py"
        work_dir = UPLOAD_DIR
        script_path = target_id
        env_path = os.path.join(work_dir, f"{target_id}.env")
        req_path = os.path.join(work_dir, f"{target_id}_req.txt")
        full_script_path = os.path.join(work_dir, target_id)
        
    return work_dir, script_path, env_path, req_path, full_script_path

async def install_dependencies(work_dir, update):
    # Manual helper to run installs inside Conversation Handler
    msg = None
    try:
        # Generic requirements
        reqs = os.path.join(work_dir, "requirements.txt")
        pkg = os.path.join(work_dir, "package.json")
        
        installed = False
        if os.path.exists(reqs):
            if not msg: msg = await update.message.reply_text("⏳ Installing Python libraries...")
            proc = await asyncio.create_subprocess_exec("pip", "install", "-r", reqs, cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            installed = True
            
        if os.path.exists(pkg):
            if not msg: msg = await update.message.reply_text("⏳ Installing Node packages...")
            else: await msg.edit_text("⏳ Installing Node packages...")
            proc = await asyncio.create_subprocess_exec("npm", "install", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            installed = True
            
        if installed and msg: await msg.edit_text("✅ Dependencies Installed!")
    except Exception as e:
        if msg: await msg.edit_text(f"❌ Install Error: {e}")

# --- AUTH DECORATORS ---
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID and user_id not in get_allowed_users():
            await update.message.reply_text("⛔ **Access Denied.**\nContact Admin.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ **Admin Only.**")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- TELEGRAM HANDLERS ---
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)

def main_menu_keyboard():
    return ReplyKeyboardMarkup([["📤 Upload File", "🌐 Clone from Git"], ["📂 My Hosted Apps", "📊 Server Stats"], ["🆘 Help"]], resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup([["➕ Add Deps", "📝 Type Env Vars"], ["🚀 RUN NOW", "🔙 Cancel"]], resize_keyboard=True)

def git_extras_keyboard():
    return ReplyKeyboardMarkup([["📝 Type Env Vars"], ["📂 Select File to Run", "🔙 Cancel"]], resize_keyboard=True)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Mega Host Bot**\nI support: `.py`, `.js`, `.sh` files.\nPlus full Git & Web Editing.", reply_markup=main_menu_keyboard())

# --- FILE UPLOAD LOGIC ---
@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send file (.py, .js, .sh)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    
    if not fname.endswith(('.py', '.js', '.sh')): 
        return await update.message.reply_text("❌ Invalid file type.")
    
    path = os.path.join(UPLOAD_DIR, fname)
    # File ownership conflict check
    owner = get_owner(fname)
    if os.path.exists(path) and owner and int(owner) != uid and uid != ADMIN_ID:
        return await update.message.reply_text(f"❌ Filename '{fname}' is owned by another user.")

    await file.download_to_drive(path)
    save_ownership(fname, uid, "file")
    context.user_data.update({'type': 'file', 'target_id': fname, 'work_dir': UPLOAD_DIR})
    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW": return await execute_logic(update, context)
    elif txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env Variables**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_ENV_TEXT
    elif "Deps" in txt:
        await update.message.reply_text("📂 Send `requirements.txt` or `package.json`.")
        context.user_data['wait'] = 'req'
        # Stay in same state waiting for document
        return WAIT_EXTRAS
    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    target_id = context.user_data['target_id']
    _, _, env_path, _, _ = resolve_paths(target_id)
    
    try:
        with open(env_path, "a") as f:
            if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
            f.write(update.message.text)
        await update.message.reply_text("✅ Variables Saved.")
    except:
        await update.message.reply_text("❌ Error saving variables.")

    if context.user_data.get('type') == 'repo': 
        await update.message.reply_text("Options:", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    await update.message.reply_text("Options:", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only if we are waiting for a specific file type (Deps)
    if not context.user_data.get('wait'): return WAIT_EXTRAS
    
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    target_id = context.user_data['target_id']
    
    save_path = ""
    # Smart naming for Single File Mode
    if fname == "package.json": save_path = os.path.join(UPLOAD_DIR, "package.json")
    elif fname.endswith(".txt"): save_path = os.path.join(UPLOAD_DIR, f"{target_id}_req.txt")
    
    if save_path:
        await file.download_to_drive(save_path)
        msg = await update.message.reply_text("⏳ Installing...")
        try:
            # Install now so it's ready for manual single run
            if fname.endswith(".txt"):
                subprocess.call([sys.executable, "-m", "pip", "install", "-r", save_path])
            elif fname == "package.json":
                subprocess.call(["npm", "install"], cwd=UPLOAD_DIR)
            await msg.edit_text("✅ Installed!")
        except Exception as e:
            await msg.edit_text(f"❌ Install Error: {e}")
            
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# --- GIT HANDLERS ---
@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 **Git URL**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url == "🔙 Cancel": return await cancel(update, context)
    repo_name = url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(UPLOAD_DIR, repo_name)
    
    msg = await update.message.reply_text("⏳ Cloning...")
    if os.path.exists(repo_path): shutil.rmtree(repo_path)
    try:
        subprocess.check_call(["git", "clone", url, repo_path])
        await msg.edit_text("✅ Cloned. Auto-installing deps...")
        # Auto install found reqs
        await install_dependencies(repo_path, update)
        
        context.user_data.update({'repo_path': repo_path, 'repo_name': repo_name, 'target_id': f"{repo_name}|PLACEHOLDER", 'type': 'repo', 'work_dir': repo_path})
        await update.message.reply_text("⚙️ **Repo Setup**", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    except Exception as e:
        await msg.edit_text(f"❌ Clone Failed: {e}")
        return ConversationHandler.END

async def receive_git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env Variables**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_GIT_ENV_TEXT
    elif txt == "📂 Select File to Run": return await show_file_selection(update, context)
    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data['repo_path']
    found = []
    for root, _, fs in os.walk(repo_path):
        for f in fs:
            if f.endswith(('.py', '.js', '.sh')): found.append(os.path.relpath(os.path.join(root, f), repo_path))
    if not found: return await update.message.reply_text("❌ No code files found.")
    
    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_py_{f}")] for f in found[:20]]
    await update.message.reply_text("👇 **Select File:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_SELECT_FILE

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filename = query.data.split("sel_py_")[1]
    repo_name = context.user_data['repo_name']
    unique_id = f"{repo_name}|{filename}"
    
    # Store accurate ownership
    save_ownership(unique_id, update.effective_user.id, "repo")
    
    context.user_data['target_id'] = unique_id
    await query.edit_message_text(f"✅ Selected `{filename}`")
    return await execute_logic(query, context)

# --- EXECUTION & MANAGEMENT ---
async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function handles the final start of any script
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
    
    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    if not target_id: 
        await msg_func("⚠️ Error finding script ID.")
        return ConversationHandler.END

    restart_process_background(target_id)
    
    # Generate Link
    url = f"{BASE_URL}/status?script={target_id}"
    await msg_func(f"🚀 **Started!**\nMonitor: `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE): return await update.message.reply_text("📂 Empty.")
    with open(OWNERSHIP_FILE) as f: ownership = json.load(f)
    
    keyboard = []
    count = 0
    for tid, meta in ownership.items():
        owner_id = meta.get("owner")
        
        # LOGIC: Admins see everything + User ID. Users see only theirs.
        if uid == ADMIN_ID or uid == owner_id:
            count += 1
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            
            label = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner_id:
                label += f" (👤 {owner_id})" # Show ID to Admin
            
            keyboard.append([InlineKeyboardButton(label, callback_data=f"man_{tid}")])
    
    if count == 0: return await update.message.reply_text("📂 No apps found.")
    await update.message.reply_text("📂 **Your Hosted Apps:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("man_"):
        tid = data.split("man_")[1]
        owner = get_owner(tid)
        
        # Security: Allow if Admin or Owner
        if uid != ADMIN_ID and uid != int(owner or 0): 
            return await query.message.reply_text("⛔ Not yours.")
        
        work_dir, script_path, env_path, req_path, _ = resolve_paths(tid)
        is_running = tid in running_processes and running_processes[tid]['process'].poll() is None
        status = "🟢 Running" if is_running else "🔴 Stopped"
        
        admin_info = ""
        if uid == ADMIN_ID:
            admin_info = f"\n👤 Owner ID: `{owner}`"

        text = f"⚙️ **{tid}**\n{admin_info}\nStatus: {status}"
        btns = []
        
        # Control Buttons
        if is_running:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{tid}"), InlineKeyboardButton("🔗 URL", callback_data=f"url_{tid}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{tid}")])
        
        # Live Editor Buttons (The complex part handled simply)
        editable_files = []
        if os.path.exists(os.path.join(work_dir, script_path)): editable_files.append(script_path)
        if "|" not in tid:
            if os.path.exists(env_path): editable_files.append(os.path.basename(env_path))
            if os.path.exists(req_path): editable_files.append(os.path.basename(req_path))
        else:
            # Check for Git config files
            for f in [".env", "requirements.txt", "package.json", "Dockerfile", "Procfile"]:
                if os.path.exists(os.path.join(work_dir, f)): editable_files.append(f)
        
        editor_row = []
        for f in editable_files:
            label = "Main" if f == script_path else f
            url = f"{BASE_URL}/editor?id={tid}&file={f}&uid={uid}" # Pass UID for checking in Flask
            editor_row.append(InlineKeyboardButton(f"✏️ {label}", web_app=WebAppInfo(url=url)))
            
            # Chunk buttons if too many files
            if len(editor_row) == 2:
                btns.append(editor_row)
                editor_row = []
        if editor_row: btns.append(editor_row)

        btns.append([InlineKeyboardButton("📜 Logs", callback_data=f"log_{tid}"), InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{tid}")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        tid = data.split("stop_")[1]
        if tid in running_processes:
            try: os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except: pass
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

# --- ADMIN CMDS ---
@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: /add 1234")
    if save_allowed_user(int(context.args[0])): await update.message.reply_text("✅ Added.")
    else: await update.message.reply_text("⚠️ Exists.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: /remove 1234")
    if remove_allowed_user(int(context.args[0])): await update.message.reply_text("🗑️ Removed.")
    else: await update.message.reply_text("⚠️ Not found.")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Running Processes: {len(running_processes)}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **Help**\nAdmin: @platoonleaderr", parse_mode="Markdown")

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Conversations
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