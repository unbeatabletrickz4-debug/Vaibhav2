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

# Simplified HTML to ensure it loads on phones
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
    
    <style>
        body { margin: 0; padding: 0; background: #282a36; color: #f8f8f2; font-family: monospace; display: flex; flex-direction: column; height: 100vh; }
        .header { padding: 10px; background: #44475a; display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #6272a4; }
        .header h3 { margin: 0; font-size: 14px; color: #8be9fd; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .btn { background: #50fa7b; color: #282a36; border: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 14px; }
        .CodeMirror { flex-grow: 1; font-size: 14px; }
    </style>
</head>
<body>
    <div class="header">
        <h3>📄 {{ filename }}</h3>
        <button class="btn" onclick="saveCode()">💾 SAVE</button>
    </div>
    <textarea id="code_area">{{ code }}</textarea>
    <script>
        var tg = window.Telegram.WebApp;
        tg.expand(); 
        
        var editor = CodeMirror.fromTextArea(document.getElementById("code_area"), {
            mode: "python", 
            theme: "dracula", 
            lineNumbers: true
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
                    tg.showAlert("✅ Saved & Redeploying...");
                    setTimeout(() => tg.close(), 1500);
                } else {
                    tg.showAlert("❌ Error: " + data.message);
                }
            })
            .catch(err => tg.showAlert("Error: " + err));
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return "🤖 Bot is Running!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} running", 200
    return f"❌ {script_name} stopped", 404

# --- EDITOR BACKEND (FIXED AUTH) ---
@app.route('/editor')
def editor_page():
    # 1. Grab Params safely
    target_id = request.args.get('id', '')
    filename = request.args.get('file', '')
    raw_uid = request.args.get('uid', '0')
    
    print(f"DEBUG EDITOR: User:{raw_uid} trying to edit {target_id}")

    try:
        req_uid = int(raw_uid)
    except:
        return "❌ Error: Invalid User ID format."

    owner = get_owner(target_id)
    if owner is None:
        return "❌ Error: File owner not found in database."
        
    owner_id = int(owner)

    # 2. Strict Auth Check
    if req_uid != ADMIN_ID and req_uid != owner_id:
        return f"⛔ Access Denied. This file belongs to {owner_id}."

    # 3. Path Resolution (Simpler logic)
    work_dir, _, _, _, full_script_path = resolve_paths(target_id)
    
    # Decide path based on request
    actual_path = os.path.join(work_dir, filename)
    
    if not os.path.exists(actual_path):
        return f"❌ File not found on server: {filename}"

    # 4. Read File safely
    try:
        with open(actual_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return f"❌ Failed to read file: {e}"

    return render_template_string(EDITOR_HTML, code=content, target_id=target_id, filename=filename)

@app.route('/save_code', methods=['POST'])
def save_code_route():
    try:
        data = request.json
        target_id = data.get('target_id')
        filename = data.get('filename')
        code = data.get('code')
        
        work_dir, _, _, _, _ = resolve_paths(target_id)
        actual_path = os.path.join(work_dir, filename)
        
        with open(actual_path, 'w', encoding='utf-8') as f:
            f.write(code)
            
        # Re-install reqs if edited
        if "requirements" in filename or "_req" in filename:
             subprocess.call([sys.executable, "-m", "pip", "install", "-r", actual_path])
        
        # Trigger Restart
        threading.Thread(target=restart_process_background, args=(target_id,)).start()
        
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Save Error: {e}")
        return jsonify({"status": "error", "message": str(e)})

# --- UTILS (RE-ORDERED) ---
def get_owner(target_id):
    if not os.path.exists(OWNERSHIP_FILE): return None
    try:
        with open(OWNERSHIP_FILE, 'r') as f: return json.load(f).get(target_id, {}).get("owner")
    except: return None

def save_ownership(target_id, user_id, type_):
    data = {}
    if os.path.exists(OWNERSHIP_FILE):
        try:
            with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
        except: pass
    data[target_id] = {"owner": int(user_id), "type": type_} # Save as INT
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def delete_ownership(target_id):
    if not os.path.exists(OWNERSHIP_FILE): return
    with open(OWNERSHIP_FILE, 'r') as f: data = json.load(f)
    if target_id in data: del data[target_id]
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def resolve_paths(target_id):
    # Repo format: repo_name|main.py
    if "|" in target_id:
        repo, file = target_id.split("|")
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
    # Single file format: myscript.py
    else:
        work_dir = UPLOAD_DIR
        script_path = target_id
        env_path = os.path.join(work_dir, f"{target_id}.env")
        req_path = os.path.join(work_dir, f"{target_id}_req.txt")
        full_script_path = os.path.join(work_dir, target_id)
        
    return work_dir, script_path, env_path, req_path, full_script_path

# --- PROCESS MANAGEMENT ---
def resolve_run_command(script_path):
    ext = script_path.split('.')[-1].lower()
    if ext == 'js': return ["node", script_path]
    if ext == 'sh': return ["bash", script_path]
    return ["python", "-u", script_path]

def restart_process_background(target_id):
    time.sleep(1) # Small buffer
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
        print(f"Error starting: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- TELEGRAM SETUP ---
# Decorators
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID: # Simple check for Admin commands
            # Check user list if file action
            if not is_allowed(update.effective_user.id):
               return await update.message.reply_text("⛔ Access Denied.")
        return await func(update, context, *args, **kwargs)
    return wrapped

# Access Helper
def is_allowed(uid):
    if uid == ADMIN_ID: return True
    if not os.path.exists(USERS_FILE): return False
    with open(USERS_FILE, 'r') as f: allowed = json.load(f)
    return uid in allowed

# Keyboards
def main_menu(): return ReplyKeyboardMarkup([["📤 Upload File", "🌐 Clone from Git"], ["📂 My Hosted Apps", "📊 Stats"]], resize_keyboard=True)
def extra_keys(): return ReplyKeyboardMarkup([["➕ Add Deps", "📝 Env Vars"], ["🚀 RUN NOW", "🔙 Cancel"]], resize_keyboard=True)
def git_keys(): return ReplyKeyboardMarkup([["📝 Env Vars"], ["📂 Select File", "🔙 Cancel"]], resize_keyboard=True)

# Conversation States
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV, WAIT_SELECT = range(3, 7)

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Hosting Bot Online**", reply_markup=main_menu())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled", reply_markup=main_menu())
    return ConversationHandler.END

# 1. Upload Logic
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text("📤 Send your file (.py .js .sh)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    
    # Ownership Check
    prev_owner = get_owner(fname)
    if prev_owner and int(prev_owner) != uid and uid != ADMIN_ID:
        return await update.message.reply_text("❌ Filename taken.")
        
    path = os.path.join(UPLOAD_DIR, fname)
    await file.download_to_drive(path)
    save_ownership(fname, uid, "file")
    
    context.user_data.update({'type': 'file', 'target_id': fname})
    await update.message.reply_text("✅ File Saved.", reply_markup=extra_keys())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW": return await execute_script(update, context)
    if txt == "🔙 Cancel": return await cancel(update, context)
    if "Env" in txt: 
        await update.message.reply_text("📝 Type vars:", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_ENV_TEXT
    if "Deps" in txt:
        await update.message.reply_text("📂 Send requirements/package.json")
        context.user_data['wait_doc'] = True
        return WAIT_EXTRAS # Loop back for file
    return WAIT_EXTRAS

async def receive_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    tid = context.user_data['target_id']
    _, _, env_path, _, _ = resolve_paths(tid)
    with open(env_path, "a") as f: f.write(update.message.text + "\n")
    
    kb = git_keys() if context.user_data.get('type') == 'repo' else extra_keys()
    nxt = WAIT_GIT_EXTRAS if context.user_data.get('type') == 'repo' else WAIT_EXTRAS
    
    await update.message.reply_text("✅ Env Saved.", reply_markup=kb)
    return nxt

async def receive_dep_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('wait_doc'): return WAIT_EXTRAS
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    tid = context.user_data['target_id']
    
    path = os.path.join(UPLOAD_DIR, f"{tid}_req.txt") if "txt" in fname else os.path.join(UPLOAD_DIR, "package.json")
    await file.download_to_drive(path)
    
    # Auto install for single file
    if "txt" in fname:
        subprocess.call([sys.executable, "-m", "pip", "install", "-r", path])
    elif "json" in fname:
        subprocess.call(["npm", "install"], cwd=UPLOAD_DIR)
        
    context.user_data['wait_doc'] = False
    await update.message.reply_text("✅ Installed.", reply_markup=extra_keys())
    return WAIT_EXTRAS

# 2. Git Logic
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id): return
    await update.message.reply_text("🌐 Send Git URL", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_URL

async def git_clone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    url = update.message.text
    name = url.split('/')[-1].replace('.git','')
    path = os.path.join(UPLOAD_DIR, name)
    if os.path.exists(path): shutil.rmtree(path)
    
    msg = await update.message.reply_text("⏳ Cloning...")
    try:
        subprocess.check_call(["git", "clone", url, path])
        # Auto install reqs if present
        if os.path.exists(os.path.join(path, "requirements.txt")):
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=path)
        await msg.edit_text("✅ Ready.")
        context.user_data.update({'repo_name': name, 'type': 'repo', 'target_id': f"{name}|TMP"})
        await update.message.reply_text("⚙️ Setup:", reply_markup=git_keys())
        return WAIT_GIT_EXTRAS
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel": return await cancel(update, context)
    if "Env" in txt: 
        await update.message.reply_text("📝 Type vars:", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_GIT_ENV
    if "Select" in txt:
        repo = context.user_data['repo_name']
        rpath = os.path.join(UPLOAD_DIR, repo)
        files = []
        for r, _, f in os.walk(rpath):
            for x in f:
                if x.endswith(('.py','.js','.sh')): files.append(os.path.relpath(os.path.join(r,x), rpath))
        if not files: 
            await update.message.reply_text("❌ No scripts.")
            return ConversationHandler.END
        
        # Paginate logic here normally, simplying for now
        keys = [[InlineKeyboardButton(f, callback_data=f"sel_{f}")] for f in files[:20]]
        await update.message.reply_text("👇 Choose:", reply_markup=InlineKeyboardMarkup(keys))
        return WAIT_SELECT
    return WAIT_GIT_EXTRAS

async def git_sel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    file = query.data.split("sel_")[1]
    repo = context.user_data['repo_name']
    
    uid = update.effective_user.id
    target_id = f"{repo}|{file}"
    save_ownership(target_id, uid, "repo")
    
    context.user_data['target_id'] = target_id
    await query.message.delete()
    return await execute_script(update, context, True)

# 3. Execution
async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE, is_git=False):
    # Resolve msg object
    if update.callback_query: msg = update.callback_query.message
    else: msg = update.message
    
    tid = context.user_data.get('target_id') or context.user_data.get('fallback_id')
    restart_process_background(tid)
    await msg.reply_text(f"🚀 **Launched** `{tid}`", parse_mode="Markdown", reply_markup=main_menu())
    return ConversationHandler.END

# 4. Management & Admin Info (User List)
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): return
    
    if not os.path.exists(OWNERSHIP_FILE): return await update.message.reply_text("Empty.")
    with open(OWNERSHIP_FILE) as f: owners = json.load(f)
    
    keys = []
    for tid, meta in owners.items():
        owner_id = int(meta.get("owner", 0))
        # Admin sees All + IDs. User sees Own.
        if uid == ADMIN_ID or uid == owner_id:
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            
            lbl = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner_id:
                lbl += f" (👤 {owner_id})" # <-- ADMIN INFO ADDED HERE
                
            keys.append([InlineKeyboardButton(lbl, callback_data=f"mng_{tid}")])
            
    await update.message.reply_text("📂 **Apps:**", reply_markup=InlineKeyboardMarkup(keys))

async def manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id
    
    if data.startswith("mng_"):
        tid = data.split("mng_")[1]
        oid = int(get_owner(tid) or 0)
        
        if uid != ADMIN_ID and uid != oid:
            return await query.message.reply_text("⛔ Not yours.")
            
        work_dir, script, _, _, _ = resolve_paths(tid)
        
        # Prepare Info
        run_st = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
        
        # Admin sees ID inside manage menu too
        uinfo = f"\n👤 User: `{oid}`" if uid == ADMIN_ID else ""
        
        txt = f"⚙️ **{tid}**\nStatus: {run_st}{uinfo}"
        
        # Editor buttons
        web_link = f"{BASE_URL}/editor?id={tid}&file={script}&uid={uid}"
        env_link = f"{BASE_URL}/editor?id={tid}&file=.env&uid={uid}" # Simplification for menu
        
        bts = [
            [InlineKeyboardButton("✏️ Edit Code", web_app=WebAppInfo(url=web_link)), InlineKeyboardButton("✏️ Env", web_app=WebAppInfo(url=env_link))],
            [InlineKeyboardButton("📜 Logs", callback_data=f"lg_{tid}"), InlineKeyboardButton("🔗 URL", callback_data=f"ul_{tid}")]
        ]
        
        # Control Buttons
        if run_st == "🟢":
            bts.append([InlineKeyboardButton("🛑 Stop", callback_data=f"st_{tid}")])
        else:
            bts.append([InlineKeyboardButton("🚀 Run", callback_data=f"rn_{tid}")])
            
        bts.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"dl_{tid}")])
        
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(bts), parse_mode="Markdown")

    elif data.startswith("st_"): # Stop
        tid = data.split("st_")[1]
        if tid in running_processes:
            try: os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except: pass
        await query.edit_message_text(f"🛑 Stopped {tid}")

    elif data.startswith("rn_"): # Run
        context.user_data['fallback_id'] = data.split("rn_")[1]
        await execute_script(update, context)

    elif data.startswith("dl_"): # Delete
        tid = data.split("dl_")[1]
        # Kill
        if tid in running_processes:
            try: os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except: pass
            del running_processes[tid]
        
        delete_ownership(tid)
        work_dir, _, _, _, _ = resolve_paths(tid)
        if "|" in tid: shutil.rmtree(work_dir, ignore_errors=True)
        else: 
            try: os.remove(os.path.join(work_dir, tid))
            except: pass
        await query.edit_message_text("🗑️ Deleted.")

    elif data.startswith("lg_"): # Logs
        tid = data.split("lg_")[1]
        lpath = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(lpath): 
            await context.bot.send_document(uid, open(lpath,'rb'))
        else:
            await query.message.reply_text("No logs.")

    elif data.startswith("ul_"): # URL
        tid = data.split("ul_")[1]
        await query.message.reply_text(f"🔗 `{BASE_URL}/status?script={tid}`", parse_mode="Markdown")

# --- ADMIN CMDS ---
async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        if not os.path.exists(USERS_FILE):
            with open(USERS_FILE,'w') as f: json.dump([],f)
        with open(USERS_FILE,'r') as f: u = json.load(f)
        uid = int(context.args[0])
        if uid not in u: u.append(uid)
        with open(USERS_FILE,'w') as f: json.dump(u,f)
        await update.message.reply_text(f"✅ Added {uid}")
    except: await update.message.reply_text("Error. Use /add 1234")

# --- START ---
if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # 1. Files
    cf = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Upload File"), upload_start)],
        states={
            WAIT_FILE: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Document.ALL, receive_file)],
            WAIT_EXTRAS: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Regex("(Run|Env|Deps)"), receive_extras), MessageHandler(filters.Document.ALL, receive_dep_file)],
            WAIT_ENV_TEXT: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, receive_env)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    # 2. Git
    cg = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Git"), git_start)],
        states={
            WAIT_URL: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, git_clone)],
            WAIT_GIT_EXTRAS: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Regex("(Env|Select)"), git_extras)],
            WAIT_GIT_ENV: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, receive_env)],
            WAIT_SELECT: [CallbackQueryHandler(git_sel)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', admin_add))
    app_bot.add_handler(cf)
    app_bot.add_handler(cg)
    app_bot.add_handler(MessageHandler(filters.Regex("My Hosted"), list_hosted))
    app_bot.add_handler(CallbackQueryHandler(manager))
    app_bot.add_handler(CommandHandler('start', start))
    
    print("Running...")
    app_bot.run_polling()