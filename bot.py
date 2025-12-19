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
OWNERSHIP_FILE = "ownership.json"

running_processes = {} 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER ---
app = Flask(__name__)

# Editor with "User-Isolated" support
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
        .hdr h4{margin:0;font-family:sans-serif;font-size:14px;color:#a6e22e; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; width: 60%;}
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
def home(): return "🤖 Bot is Running!", 200

@app.route('/editor')
def editor_route():
    try:
        req_uid = int(request.args.get('uid'))
        file_name = request.args.get('file')
        # Correctly look in USER folder
        user_folder = os.path.join(BASE_UPLOAD_DIR, str(req_uid))
        file_path = os.path.join(user_folder, file_name)
        
        # Security
        if not os.path.abspath(file_path).startswith(os.path.abspath(user_folder)):
            return "⛔ Path Error"
            
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
        if not os.path.exists(user_dir): os.makedirs(user_dir)
        
        fpath = os.path.join(user_dir, fname)
        
        with open(fpath, 'w', encoding='utf-8') as f: f.write(code)
        
        # Auto install if requirements changed
        if fname == "requirements.txt":
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", fpath])
        elif fname == "package.json":
            subprocess.call(["npm", "install"], cwd=user_dir)
            
        # Trigger Restart
        key = get_run_key(uid, os.path.basename(fpath)) 
        # Attempt to restart related process. 
        # Since files are split, finding main script is complex if editing subfile.
        # But for single file projects, filename is the script.
        threading.Thread(target=run_script_process, args=(uid, fname)).start()

        return jsonify({"ok": True})
    except Exception as e: return jsonify({"ok": False, "msg": str(e)})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- CORE ---
def get_user_dir(uid):
    path = os.path.join(BASE_UPLOAD_DIR, str(uid))
    if not os.path.exists(path): os.makedirs(path)
    return path

def get_run_key(uid, filename): return f"{uid}_{filename}"

def run_script_process(uid, filename):
    # Logic to run script INSIDE user's unique folder
    work_dir = get_user_dir(uid)
    key = get_run_key(uid, filename)
    
    # Kill old
    if key in running_processes:
        try: os.killpg(os.getpgid(running_processes[key]['proc'].pid), signal.SIGTERM)
        except: pass
    
    env_vars = os.environ.copy()
    
    # 1. General .env in user dir
    base_env = os.path.join(work_dir, ".env")
    if os.path.exists(base_env):
        with open(base_env) as f:
            for l in f: 
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=',1)
                    env_vars[k.strip()] = v.strip().strip('"')

    log_file = os.path.join(work_dir, f"{filename}.log")
    log_fp = open(log_file, "w")
    
    cmd = ["python", "-u", filename]
    if filename.endswith(".js"): cmd = ["node", filename]
    elif filename.endswith(".sh"): cmd = ["bash", filename]
    
    try:
        proc = subprocess.Popen(
            cmd, env=env_vars, stdout=log_fp, stderr=subprocess.STDOUT, 
            cwd=work_dir, preexec_fn=os.setsid
        )
        running_processes[key] = {"proc": proc, "log": log_file, "start": datetime.now()}
        return True, "Started"
    except Exception as e: return False, str(e)

# Watchdog function (Crash Detector)
async def process_watchdog(context: ContextTypes.DEFAULT_TYPE):
    # This checks processes and alerts on crash
    to_del = []
    for key, data in running_processes.items():
        if data['proc'].poll() is not None: # Crashed/Exited
            uid = int(key.split('_')[0])
            fname = key.split('_', 1)[1]
            try:
                # Read last lines of log
                log_content = "No logs"
                if os.path.exists(data['log']):
                    with open(data['log'], 'r') as f: log_content = f.read()[-1000:] # Last 1000 chars
                
                await context.bot.send_message(
                    chat_id=uid, 
                    text=f"⚠️ **Crash Alert:** `{fname}` stopped!\n\n📜 Logs:\n`{log_content}`",
                    parse_mode="Markdown"
                )
            except Exception as e: 
                print(f"Failed to alert {uid}: {e}")
            to_del.append(key)
    
    for k in to_del: del running_processes[k]

# --- HELPERS ---
def check_auth(uid):
    if uid == ADMIN_ID: return True
    if not os.path.exists(USERS_FILE): return False
    try: 
        with open(USERS_FILE) as f: return uid in json.load(f)
    except: return False

def load_ownership():
    if not os.path.exists(OWNERSHIP_FILE): return {}
    with open(OWNERSHIP_FILE) as f: return json.load(f)

async def cancel(update, context):
    await update.message.reply_text("🚫 Cancelled", reply_markup=main_menu_kb())
    return ConversationHandler.END

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update.effective_user.id): return
    await update.message.reply_text("🚀 **Hosting Bot Active**\n(Folder Isolation Mode)", reply_markup=main_menu_kb())

def main_menu_kb(): return ReplyKeyboardMarkup([["📤 Upload App", "📂 Dashboard"], ["📊 Stats", "🆘 Help"]], resize_keyboard=True)

# 1. UPLOAD (With Conflict Fix)
WAIT_FILE = 0
async def upload_init(update, context):
    if not check_auth(update.effective_user.id): return
    await update.message.reply_text("📤 Upload File", reply_markup=ReplyKeyboardMarkup([['Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def upload_handle(update, context):
    if update.message.text == 'Cancel': return await cancel(update, context)
    
    doc = update.message.document
    fname = doc.file_name
    uid = update.effective_user.id
    
    # SAVE TO UNIQUE FOLDER (FIX FOR OVERWRITING)
    user_path = get_user_dir(uid) 
    fpath = os.path.join(user_path, fname)
    
    await doc.get_file().download_to_drive(fpath)
    
    # Save Metadata with Ownership
    rec_key = f"{uid}_{fname}"
    meta = load_ownership()
    meta[rec_key] = {"owner": uid, "file": fname}
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(meta, f)
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start", callback_data=f"start_{rec_key}")],
        [InlineKeyboardButton("✏️ Edit Env", web_app=WebAppInfo(url=f"{BASE_URL}/editor?uid={uid}&file=.env"))]
    ])
    await update.message.reply_text(f"✅ **{fname}** Saved in your private workspace.", reply_markup=kb)
    return ConversationHandler.END

# 2. DASHBOARD
async def dashboard(update, context):
    uid = update.effective_user.id
    if not check_auth(uid): return
    
    data = load_ownership()
    kb = []
    
    for key, info in data.items():
        owner = info['owner']
        fname = info['file']
        
        # Admin sees all, User sees theirs
        if uid == ADMIN_ID or uid == owner:
            status = "🟢" if f"{owner}_{fname}" in running_processes else "🔴"
            lbl = f"{status} {fname}"
            if uid == ADMIN_ID and uid != owner: lbl += f" (User: {owner})"
            kb.append([InlineKeyboardButton(lbl, callback_data=f"m_{key}")])
            
    if not kb: return await update.message.reply_text("No apps.")
    await update.message.reply_text("🎛 **Hosted Apps:**", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id
    
    if data.startswith("start_"):
        key = data.split("start_")[1]
        oid, fname = key.split('_', 1)
        if uid != ADMIN_ID and uid != int(oid): return
        
        # Check Req
        u_dir = get_user_dir(oid)
        req = os.path.join(u_dir, "requirements.txt")
        if os.path.exists(req): subprocess.call([sys.executable, "-m", "pip", "install", "-r", req])
        
        success, msg = run_script_process(oid, fname)
        await query.message.reply_text(f"Run Status: {msg}")

    elif data.startswith("m_"):
        key = data.split("m_")[1]
        info = load_ownership().get(key)
        if not info: return await query.message.reply_text("Gone.")
        
        oid, fname = info['owner'], info['file']
        if uid != ADMIN_ID and uid != oid: return 
        
        run_key = f"{oid}_{fname}"
        is_run = run_key in running_processes
        
        btns = []
        if is_run:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{run_key}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Start", callback_data=f"start_{key}")])
            
        # Editors (POINT TO USER DIR)
        url = f"{BASE_URL}/editor?uid={oid}&file={fname}"
        btns.append([InlineKeyboardButton("✏️ Live Edit Code", web_app=WebAppInfo(url=url))])
        
        btns.append([InlineKeyboardButton("🗑 Delete", callback_data=f"del_{key}")])
        
        txt = f"⚙️ **{fname}**"
        if uid == ADMIN_ID: txt += f"\n👤 User: {oid}"
        
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        rk = data.split("stop_")[1]
        if rk in running_processes:
            try: os.killpg(os.getpgid(running_processes[rk]['proc'].pid), signal.SIGTERM)
            except: pass
            del running_processes[rk]
            await query.edit_message_text("🛑 Stopped.")

    elif data.startswith("del_"):
        key = data.split("del_")[1]
        info = load_ownership().get(key)
        if info:
            # Stop first
            rk = f"{info['owner']}_{info['file']}"
            if rk in running_processes:
                try: os.killpg(os.getpgid(running_processes[rk]['proc'].pid), signal.SIGTERM)
                except: pass
                del running_processes[rk]
            
            # Delete file from User Dir
            p = os.path.join(get_user_dir(info['owner']), info['file'])
            if os.path.exists(p): os.remove(p)
            
            d = load_ownership()
            if key in d: del d[key]
            with open(OWNERSHIP_FILE,'w') as f: json.dump(d,f)
            await query.edit_message_text("🗑 Deleted.")

async def admin_add(update, context):
    if update.effective_user.id != ADMIN_ID: return
    try:
        new_id = int(context.args[0])
        u = []
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f: u = json.load(f)
        if new_id not in u: u.append(new_id)
        with open(USERS_FILE,'w') as f: json.dump(u,f)
        await update.message.reply_text(f"Added {new_id}")
    except: pass

async def stats(update, context):
    if not check_auth(update.effective_user.id): return
    await update.message.reply_text(f"Running Apps: {len(running_processes)}")

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # SAFETY: Add JobQueue safely
    if app_bot.job_queue:
        app_bot.job_queue.run_repeating(process_watchdog, interval=60, first=10)
    else:
        print("⚠️ JobQueue failed to load. Watchdog disabled. (Check requirements.txt)")

    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Upload App"), upload_init)],
        states={WAIT_FILE: [MessageHandler(filters.Document.ALL, upload_handle)]},
        fallbacks=[MessageHandler(filters.Regex("Cancel"), cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', admin_add))
    app_bot.add_handler(conv)
    app_bot.add_handler(MessageHandler(filters.Regex("Dashboard"), dashboard))
    app_bot.add_handler(MessageHandler(filters.Regex("Stats"), stats))
    app_bot.add_handler(CallbackQueryHandler(handle_callback))
    app_bot.add_handler(CommandHandler('start', start))
    
    print("🚀 Ultra Bot 2.0 Running...")
    app_bot.run_polling()