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
import socket
from datetime import datetime
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

# FOLDER STRUCTURE: scripts/USER_ID/filename
BASE_UPLOAD_DIR = "scripts"
if not os.path.exists(BASE_UPLOAD_DIR):
    os.makedirs(BASE_UPLOAD_DIR)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json" # Kept for metadata logic

# Stores process objects
# Key format: "USERID_FILENAME" (to handle multi-user separation in runtime)
running_processes = {} 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER (Web App Editor Backend) ---
app = Flask(__name__)

# Minified Editor HTML
EDITOR_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Advance Editor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/theme/monokai.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/python/python.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/javascript/javascript.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/shell/shell.min.js"></script>
    <style>
        body{margin:0;background:#272822;color:#f8f8f2;display:flex;flex-direction:column;height:100vh}
        .hdr{padding:10px;background:#1e1f1c;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #75715e}
        .hdr h4{margin:0;font-family:sans-serif;font-size:14px;color:#a6e22e}
        .btn{background:#f92672;color:#fff;border:none;padding:8px 16px;border-radius:4px;cursor:pointer;font-weight:bold}
        .CodeMirror{flex-grow:1;font-size:14px}
    </style>
</head>
<body>
    <div class="hdr"><h4>📂 {{ filename }}</h4><button class="btn" onclick="sv()">SAVE & RUN 🚀</button></div>
    <textarea id="code">{{ code }}</textarea>
    <script>
        const tg=window.Telegram.WebApp; tg.expand();
        const cm=CodeMirror.fromTextArea(document.getElementById("code"),{mode:"python",theme:"monokai",lineNumbers:true});
        function sv(){
            fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({uid:"{{ uid }}",file:"{{ filename }}",code:cm.getValue()})})
            .then(r=>r.json()).then(d=>{
                d.ok? tg.showAlert("✅ Updated!") : tg.showAlert("❌ Error: "+d.msg);
                if(d.ok) setTimeout(()=>tg.close(),1000);
            }).catch(e=>tg.showAlert("Err: "+e));
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return "🤖 Ultimate Hosting Bot Active", 200

@app.route('/status')
def script_status():
    uid = request.args.get('uid')
    script = request.args.get('script')
    pid_key = f"{uid}_{script}"
    if pid_key in running_processes and running_processes[pid_key]['proc'].poll() is None:
        return f"🟢 Running: {script}", 200
    return f"🔴 Stopped: {script}", 404

@app.route('/editor')
def editor_route():
    try:
        req_uid = int(request.args.get('uid'))
        file_name = request.args.get('file')
        user_folder = os.path.join(BASE_UPLOAD_DIR, str(req_uid))
        file_path = os.path.join(user_folder, file_name)
        
        # Security: Prevent traversing out of user folder
        if not os.path.abspath(file_path).startswith(os.path.abspath(user_folder)):
            return "⛔ Security Block"
            
        content = ""
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
            
        return render_template_string(EDITOR_HTML, code=content, filename=file_name, uid=req_uid)
    except Exception as e: return f"Error: {e}"

@app.route('/save', methods=['POST'])
def save_route():
    try:
        data = request.json
        uid = data.get('uid')
        fname = data.get('file')
        code = data.get('code')
        
        user_dir = os.path.join(BASE_UPLOAD_DIR, str(uid))
        fpath = os.path.join(user_dir, fname)
        
        with open(fpath, 'w', encoding='utf-8') as f: f.write(code)
        
        # Auto install dep logic
        if fname == "requirements.txt":
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", fpath])
        
        # Auto restart if it's the main file associated
        # Finding associated run key
        # For simplicity in this logic, we assume we just save here. 
        # Ideally trigger restart logic via IPC or thread event if complex.
        
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- CORE FUNCTIONS (USER ISOLATION UPGRADE) ---

def get_user_dir(uid):
    path = os.path.join(BASE_UPLOAD_DIR, str(uid))
    if not os.path.exists(path): os.makedirs(path)
    return path

# Unique Run Key: "UID_FILENAME"
def get_run_key(uid, filename): return f"{uid}_{filename}"

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

# Enhanced Runner
def run_script_process(uid, filename):
    work_dir = get_user_dir(uid)
    script_path = os.path.join(work_dir, filename)
    
    key = get_run_key(uid, filename)
    
    # 1. Stop Existing
    if key in running_processes:
        try: os.killpg(os.getpgid(running_processes[key]['proc'].pid), signal.SIGTERM)
        except: pass
    
    # 2. Env Prep (Isolated Env File)
    # Check if a .env exists in user folder
    # We scan for any .env file in that user's folder
    env_vars = os.environ.copy()
    
    # Auto-assign PORT for web apps
    free_port = get_free_port()
    env_vars['PORT'] = str(free_port)
    
    env_file = os.path.join(work_dir, ".env") # General env file
    # Or specific env: script.py.env
    spec_env = os.path.join(work_dir, f"{filename}.env")
    
    target_env = spec_env if os.path.exists(spec_env) else env_file
    
    if os.path.exists(target_env):
        with open(target_env) as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    env_vars[k.strip()] = v.strip().strip('"')

    # 3. Log File
    log_file = os.path.join(work_dir, f"{filename}.log")
    logger_fp = open(log_file, "w")
    
    # 4. Command Resolver
    cmd = ["python", "-u", filename]
    if filename.endswith(".js"): cmd = ["node", filename]
    elif filename.endswith(".sh"): cmd = ["bash", filename]
    
    try:
        proc = subprocess.Popen(
            cmd,
            env=env_vars,
            stdout=logger_fp,
            stderr=subprocess.STDOUT,
            cwd=work_dir, # Run INSIDE user folder
            preexec_fn=os.setsid
        )
        running_processes[key] = {
            "proc": proc, 
            "log": log_file,
            "port": free_port,
            "start_time": datetime.now()
        }
        return True, free_port
    except Exception as e:
        return False, str(e)

# --- WATCHDOG (AUTO-CRASH ALERT) ---
async def process_watchdog(context: ContextTypes.DEFAULT_TYPE):
    # Runs every minute to check crashed scripts
    for key, data in list(running_processes.items()):
        if data['proc'].poll() is not None:
            # It crashed or stopped
            uid, fname = key.split('_', 1)
            # Notify User
            try:
                # Send log snippet
                with open(data['log'], 'r') as f:
                    log_tail = f.read()[-500:]
                
                msg = f"⚠️ **Alert:** `{fname}` has stopped/crashed.\n\n📝 **Last Logs:**\n`{log_tail}`"
                await context.bot.send_message(chat_id=int(uid), text=msg, parse_mode="Markdown")
            except: pass
            
            # Remove from tracking
            del running_processes[key]

# --- PERMISSIONS ---
def check_auth(uid):
    if uid == ADMIN_ID: return True
    if not os.path.exists(USERS_FILE): return False
    try:
        with open(USERS_FILE) as f: return uid in json.load(f)
    except: return False

def admin_only(func):
    async def wrapped(update, context, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID: return
        return await func(update, context, *args, **kwargs)
    return wrapped

def authorized(func):
    async def wrapped(update, context, *args, **kwargs):
        if not check_auth(update.effective_user.id):
            return await update.message.reply_text("⛔ Authorized access only.")
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- TELEGRAM HANDLERS ---

@authorized
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚡ **Pro Hosting Bot**\n"
        "Features:\n"
        "📂 User Isolated Workspace\n"
        "🔄 Auto-Restart & Crash Alerts\n"
        "🌐 Live Web Editor",
        reply_markup=main_menu_kb()
    )

def main_menu_kb():
    return ReplyKeyboardMarkup([["📤 Upload App", "📂 Dashboard"], ["📊 Stats", "🆘 Help"]], resize_keyboard=True)

# 1. UPLOAD
WAIT_FILE, WAIT_CONFIG = range(2)

@authorized
async def upload_init(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Upload your file (py/js/sh/zip)", reply_markup=ReplyKeyboardMarkup([['Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def upload_handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == 'Cancel': return await cancel(update, context)
    
    doc = update.message.document
    fname = doc.file_name
    uid = update.effective_user.id
    user_path = get_user_dir(uid)
    
    fpath = os.path.join(user_path, fname)
    await doc.get_file().download_to_drive(fpath)
    
    context.user_data['upload_file'] = fname
    context.user_data['uid'] = uid
    
    # Save generic ownership record for listing logic (Legacy support)
    rec_key = f"{uid}_{fname}"
    meta = {}
    if os.path.exists(OWNERSHIP_FILE):
        with open(OWNERSHIP_FILE) as f: meta = json.load(f)
    meta[rec_key] = {"owner": uid, "file": fname}
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(meta, f)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Now", callback_data=f"start_{rec_key}")],
        [InlineKeyboardButton("➕ Add Requirements", callback_data=f"req_{rec_key}")],
        [InlineKeyboardButton("✏️ Edit Environment", web_app=WebAppInfo(url=f"{BASE_URL}/editor?uid={uid}&file=.env"))]
    ])
    
    await update.message.reply_text(f"✅ **{fname}** uploaded successfully.", reply_markup=kb)
    return ConversationHandler.END

# 2. DASHBOARD / MANAGE
@authorized
async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE): return await update.message.reply_text("No apps found.")
    
    with open(OWNERSHIP_FILE) as f: data = json.load(f)
    
    # Clean non-existent files logic can go here...
    
    keyboard = []
    count = 0
    
    for key, info in data.items():
        owner = info['owner']
        fname = info['file']
        
        # Isolation: Admin sees all (with ID). User sees only theirs.
        if uid == ADMIN_ID or uid == owner:
            count += 1
            run_key = f"{owner}_{fname}"
            status = "🟢" if run_key in running_processes else "🔴"
            
            label = f"{status} {fname}"
            if uid == ADMIN_ID and uid != owner:
                label += f" (👤 {owner})"
                
            keyboard.append([InlineKeyboardButton(label, callback_data=f"m_{key}")])
            
    if count == 0: return await update.message.reply_text("📂 No apps found.")
    await update.message.reply_text("🎛 **Control Panel:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id
    
    # Generic Handlers
    if data.startswith("start_"):
        key = data.split("start_")[1]
        t_uid, t_file = key.split('_', 1)
        
        # Verify Owner/Admin
        if uid != ADMIN_ID and uid != int(t_uid): return 
        
        # Check requirements install
        u_dir = get_user_dir(t_uid)
        req_path = os.path.join(u_dir, "requirements.txt")
        if os.path.exists(req_path):
            msg = await query.message.reply_text("📦 Installing dependencies first...")
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", req_path], cwd=u_dir)
            await msg.delete()

        success, port_or_err = run_script_process(t_uid, t_file)
        if success:
            await query.message.reply_text(f"✅ Started **{t_file}** on internal port `{port_or_err}`", parse_mode="Markdown")
        else:
            await query.message.reply_text(f"❌ Error: {port_or_err}")

    # Manager Menu
    elif data.startswith("m_"):
        key = data.split("m_")[1]
        
        if key not in load_json(OWNERSHIP_FILE): 
            return await query.message.reply_text("Item gone.")
            
        info = load_json(OWNERSHIP_FILE)[key]
        t_uid = info['owner']
        fname = info['file']
        
        if uid != ADMIN_ID and uid != t_uid: return 
        
        run_key = f"{t_uid}_{fname}"
        is_running = run_key in running_processes
        status = "🟢 Online" if is_running else "🔴 Offline"
        
        admin_note = f"\n🆔 User: `{t_uid}`" if uid == ADMIN_ID else ""
        text = f"🎛 **App:** `{fname}`\n📊 Status: {status}{admin_note}"
        
        btns = []
        if is_running:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{run_key}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Start", callback_data=f"start_{key}")])
            
        # Editors
        web_url = f"{BASE_URL}/editor?uid={t_uid}&file={fname}"
        env_url = f"{BASE_URL}/editor?uid={t_uid}&file=.env"
        btns.append([InlineKeyboardButton("📝 Edit Code", web_app=WebAppInfo(url=web_url)), 
                     InlineKeyboardButton("🔐 Edit Env", web_app=WebAppInfo(url=env_url))])
        
        btns.append([InlineKeyboardButton("🗑 Delete", callback_data=f"del_{key}"), InlineKeyboardButton("📜 Logs", callback_data=f"log_{key}")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        rkey = data.split("stop_")[1]
        if rkey in running_processes:
            try: os.killpg(os.getpgid(running_processes[rkey]['proc'].pid), signal.SIGTERM)
            except: pass
            del running_processes[rkey]
            await query.message.reply_text("🛑 Process Stopped.")
        else:
            await query.message.reply_text("⚠️ Already stopped.")

    elif data.startswith("log_"):
        key = data.split("log_")[1]
        info = load_json(OWNERSHIP_FILE).get(key)
        if info:
            log_p = os.path.join(get_user_dir(info['owner']), f"{info['file']}.log")
            if os.path.exists(log_p):
                await context.bot.send_document(uid, open(log_p, 'rb'))
            else:
                await query.message.reply_text("📭 No logs found.")

    elif data.startswith("del_"):
        key = data.split("del_")[1]
        info = load_json(OWNERSHIP_FILE).get(key)
        if info:
            # Stop if running
            rkey = f"{info['owner']}_{info['file']}"
            if rkey in running_processes:
                try: os.killpg(os.getpgid(running_processes[rkey]['proc'].pid), signal.SIGTERM)
                except: pass
                del running_processes[rkey]
                
            # Delete file
            u_path = os.path.join(get_user_dir(info['owner']), info['file'])
            if os.path.exists(u_path): os.remove(u_path)
            
            # Remove record
            d = load_json(OWNERSHIP_FILE)
            del d[key]
            with open(OWNERSHIP_FILE, 'w') as f: json.dump(d, f)
            
            await query.edit_message_text("🗑 App Deleted.")

# --- UTILS ---
def load_json(path):
    if not os.path.exists(path): return {}
    with open(path) as f: return json.load(f)

async def cancel(update, context):
    await update.message.reply_text("Cancelled", reply_markup=main_menu_kb())
    return ConversationHandler.END

# --- ADMIN USER MANAGEMENT ---
@admin_only
async def add_u(update, context):
    try:
        new_id = int(context.args[0])
        u = []
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f: u = json.load(f)
        if new_id not in u: u.append(new_id)
        with open(USERS_FILE,'w') as f: json.dump(u,f)
        await update.message.reply_text(f"Added {new_id}")
    except: await update.message.reply_text("Usage: /add 1234")

@admin_only
async def rm_u(update, context):
    try:
        old_id = int(context.args[0])
        with open(USERS_FILE) as f: u = json.load(f)
        if old_id in u: u.remove(old_id)
        with open(USERS_FILE,'w') as f: json.dump(u,f)
        await update.message.reply_text(f"Removed {old_id}")
    except: await update.message.reply_text("Err.")

@authorized
async def sys_stats(update, context):
    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    await update.message.reply_text(f"🖥 CPU: {cpu}%\n💾 RAM: {mem}%\n⚡ Apps Running: {len(running_processes)}")

# --- MAIN ---
if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Watchdog Job
    job_queue = app_bot.job_queue
    job_queue.run_repeating(process_watchdog, interval=60, first=10) # Check crashes every 60s

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Upload App"), upload_init)],
        states={WAIT_FILE: [MessageHandler(filters.Document.ALL, upload_handle)]},
        fallbacks=[MessageHandler(filters.Regex("Cancel"), cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', add_u))
    app_bot.add_handler(CommandHandler('remove', rm_u))
    app_bot.add_handler(conv)
    app_bot.add_handler(MessageHandler(filters.Regex("Dashboard"), dashboard))
    app_bot.add_handler(MessageHandler(filters.Regex("Stats"), sys_stats))
    app_bot.add_handler(CallbackQueryHandler(handle_callback))
    app_bot.add_handler(CommandHandler('start', start))
    
    print("🚀 Ultra Bot Started")
    app_bot.run_polling()