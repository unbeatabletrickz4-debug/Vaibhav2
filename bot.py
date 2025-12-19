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
import socket
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

# BASE FOLDER
BASE_UPLOAD_DIR = "scripts"
if not os.path.exists(BASE_UPLOAD_DIR):
    os.makedirs(BASE_UPLOAD_DIR)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

running_processes = {} 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER & EDITOR ---
app = Flask(__name__)

# Same Advanced Editor from Polyglot Version
EDITOR_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Code Editor</title>
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
        .header h3 { margin: 0; font-size: 13px; color: #50fa7b; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; max-width: 60%; }
        .btn { background: #ff79c6; color: #282a36; border: none; padding: 8px 16px; border-radius: 6px; font-weight: bold; cursor: pointer; }
        .CodeMirror { flex-grow: 1; font-size: 13px; }
    </style>
</head>
<body>
    <div class="header">
        <h3>✏️ {{ filename }}</h3>
        <button class="btn" onclick="saveCode()">SAVE 💾</button>
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
                    code: editor.getValue(),
                    uid: "{{ uid }}"
                })
            })
            .then(r => r.json())
            .then(data => {
                if(data.status === 'success') {
                    tg.showAlert("✅ Saved & Redeploying...");
                    setTimeout(() => tg.close(), 1000);
                } else {
                    tg.showAlert("❌ Error: " + data.message);
                }
            })
            .catch(e => tg.showAlert("Conn Error"));
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return "🤖 Bot Active", 200

@app.route('/status')
def script_status():
    s = request.args.get('script')
    if s in running_processes and running_processes[s]['proc'].poll() is None: return "Running", 200
    return "Stopped", 404

# --- EDITOR ROUTES (AUTH FIXED) ---
@app.route('/editor')
def editor_page():
    tid = request.args.get('id')
    fname = request.args.get('file')
    uid_str = request.args.get('uid', '0')
    try: uid = int(uid_str)
    except: return "Invalid UID"
    
    # Check Owner via Database
    owner = get_owner(tid)
    if not owner: return "File Record Not Found"
    if uid != ADMIN_ID and uid != int(owner): return "⛔ Access Denied"
    
    # RESOLVE PATH CORRECTLY
    wd, _, _, _, _ = resolve_paths(tid)
    fpath = os.path.join(wd, fname)
    
    if not os.path.exists(fpath): return "File not found on disk."
    
    with open(fpath, 'r', encoding='utf-8', errors='ignore') as f: c = f.read()
    return render_template_string(EDITOR_HTML, code=c, target_id=tid, filename=fname, uid=uid)

@app.route('/save_code', methods=['POST'])
def save_code():
    try:
        d = request.json
        tid = d['target_id']
        fname = d['filename']
        code = d['code']
        
        wd, _, _, _, _ = resolve_paths(tid)
        fpath = os.path.join(wd, fname)
        
        with open(fpath, 'w', encoding='utf-8') as f: f.write(code)
        
        # Trigger install if deps changed
        if "requirements" in fname or "_req" in fname:
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", fpath])
        
        # Trigger Restart in Background
        threading.Thread(target=restart_process_bg, args=(tid,)).start()
        
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- ISOLATION & PATH LOGIC (CRITICAL FIX) ---
# New Format for TargetID: "UID_Filename" or "UID_RepoName|Script.py"

def get_user_dir(uid):
    p = os.path.join(BASE_UPLOAD_DIR, str(uid))
    if not os.path.exists(p): os.makedirs(p)
    return p

def resolve_paths(tid):
    # Split ID: "12345_myscript.py" -> uid=12345, remainder="myscript.py"
    try:
        uid_str, remainder = tid.split('_', 1)
        user_dir = os.path.join(BASE_UPLOAD_DIR, uid_str)
    except: return None, None, None, None, None

    if "|" in remainder:
        # Git Repo Mode: "RepoName|script.py"
        rname, sname = remainder.split('|', 1)
        work_dir = os.path.join(user_dir, rname)
        script_p = sname
    else:
        # Single File Mode: "script.py"
        work_dir = user_dir
        script_p = remainder
        
    full_p = os.path.join(work_dir, script_p)
    
    # Determine config paths
    if "|" in remainder:
        env_p = os.path.join(work_dir, ".env")
        req_p = os.path.join(work_dir, "requirements.txt")
    else:
        env_p = os.path.join(work_dir, f"{remainder}.env")
        req_p = os.path.join(work_dir, f"{remainder}_req.txt")
        
    return work_dir, script_p, env_p, req_p, full_p

def restart_process_bg(tid):
    time.sleep(1)
    wd, script, env_p, _, full_p = resolve_paths(tid)
    
    if tid in running_processes:
        try: os.killpg(os.getpgid(running_processes[tid]['proc'].pid), signal.SIGTERM)
        except: pass
    
    env = os.environ.copy()
    if os.path.exists(env_p):
        with open(env_p) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=',1)
                    env[k.strip()] = v.strip().strip('"')
    
    # Auto command
    cmd = ["python", "-u", script]
    if script.endswith(".js"): cmd = ["node", script]
    elif script.endswith(".sh"): cmd = ["bash", script]
    
    try:
        log = open(os.path.join(BASE_UPLOAD_DIR, f"{tid}.log"), "w")
        proc = subprocess.Popen(cmd, cwd=wd, env=env, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
        running_processes[tid] = {"proc": proc}
    except Exception as e: print(e)

# --- DB & AUTH ---
def get_owner(tid):
    if not os.path.exists(OWNERSHIP_FILE): return None
    try: return json.load(open(OWNERSHIP_FILE)).get(tid, {}).get("owner")
    except: return None

def save_ownership(tid, uid, ftype):
    d = {}
    if os.path.exists(OWNERSHIP_FILE): 
        with open(OWNERSHIP_FILE) as f: d = json.load(f)
    d[tid] = {"owner": int(uid), "type": ftype}
    with open(OWNERSHIP_FILE,'w') as f: json.dump(d, f)

def check_auth(uid):
    if uid == ADMIN_ID: return True
    if not os.path.exists(USERS_FILE): return False
    return uid in json.load(open(USERS_FILE))

# --- HANDLERS ---
# DECORATORS
def restricted(func):
    async def wrapped(up, ctx, *a, **k):
        if not check_auth(up.effective_user.id): return await up.message.reply_text("⛔ Access Denied.")
        return await func(up, ctx, *a, **k)
    return wrapped

def admin_only(func):
    async def wrapped(up, ctx, *a, **k):
        if up.effective_user.id != ADMIN_ID: return
        return await func(up, ctx, *a, **k)
    return wrapped

# STATES
W_FILE, W_EXT, W_ENV = 0, 1, 2
W_URL, W_GEXT, W_GENV, W_SEL = 0, 1, 2, 3 # Git

# Keyboards
def mn_kb(): return ReplyKeyboardMarkup([["📤 Upload File", "🌐 Clone from Git"], ["📂 My Hosted Apps", "📊 Server Stats"], ["🆘 Help"]], resize_keyboard=True)
def ex_kb(): return ReplyKeyboardMarkup([["➕ Deps", "📝 Env Vars"], ["🚀 RUN", "🔙 Cancel"]], resize_keyboard=True)
def g_kb(): return ReplyKeyboardMarkup([["📝 Env Vars"], ["📂 Select File", "🔙 Cancel"]], resize_keyboard=True)

# COMMANDS
@restricted
async def start(u, c): await u.message.reply_text("👋 **Advanced Hosting Bot**", reply_markup=mn_kb())

async def help_cmd(u, c): await u.message.reply_text("🆘 **Support**\nContact: @platoonleaderr", parse_mode="Markdown")

async def cancel(u, c): 
    await u.message.reply_text("🚫 Cancelled", reply_markup=mn_kb())
    return ConversationHandler.END

# 1. FILE UPLOAD (Isolated)
@restricted
async def up_start(u, c):
    await u.message.reply_text("📤 Send file (.py .js .sh)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return W_FILE

async def rx_file(u, c):
    if u.message.text=="🔙 Cancel": return await cancel(u,c)
    doc = u.message.document
    fname = doc.file_name
    uid = u.effective_user.id
    
    # 1. CREATE USER DIR
    udip = get_user_dir(uid)
    save_p = os.path.join(udip, fname)
    
    await doc.get_file().download_to_drive(save_p)
    
    # 2. GENERATE UNIQUE ID: UID_FILENAME
    tid = f"{uid}_{fname}"
    save_ownership(tid, uid, "file")
    
    c.user_data.update({'tid': tid, 'dir': udip})
    await u.message.reply_text("✅ Saved in isolated storage.", reply_markup=ex_kb())
    return W_EXT

async def rx_extras(u, c):
    txt = u.message.text
    if txt == "🚀 RUN": return await launch(u, c)
    if txt == "🔙 Cancel": return await cancel(u, c)
    if "Env" in txt:
        await u.message.reply_text("📝 Type vars:", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return W_ENV
    if "Deps" in txt:
        await u.message.reply_text("📂 Send reqs/package.json")
        c.user_data['wait_d'] = True
        return W_EXT # stay
    return W_EXT

async def rx_env(u, c):
    if u.message.text=="🔙 Cancel": return await cancel(u, c)
    tid = c.user_data['tid']
    _, _, ep, _, _ = resolve_paths(tid)
    with open(ep, "a") as f: f.write(u.message.text + "\n")
    
    kb = g_kb() if "|" in tid else ex_kb()
    st = W_GEXT if "|" in tid else W_EXT
    await u.message.reply_text("✅ Saved.", reply_markup=kb)
    return st

async def rx_deps_file(u, c):
    if not c.user_data.get('wait_d'): return W_EXT
    doc = u.message.document
    uid = u.effective_user.id
    fname = doc.file_name
    udip = get_user_dir(uid)
    tid = c.user_data['tid']
    
    # Correct Path
    if "txt" in fname: path = os.path.join(udip, f"{tid.split('_')[1]}_req.txt")
    else: path = os.path.join(udip, "package.json")
    
    await doc.get_file().download_to_drive(path)
    # Install
    msg = await u.message.reply_text("⏳ Installing...")
    if "txt" in fname: subprocess.call([sys.executable,"-m","pip","install","-r",path])
    else: subprocess.call(["npm","install"], cwd=udip)
    
    c.user_data['wait_d'] = False
    await msg.edit_text("✅ Ready.")
    return W_EXT

# 2. GIT (Restored)
@restricted
async def git_start(u, c):
    await u.message.reply_text("🌐 Send Git URL", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return W_URL

async def rx_git(u, c):
    if u.message.text=="🔙 Cancel": return await cancel(u, c)
    url = u.message.text
    name = url.split('/')[-1].replace('.git','')
    uid = u.effective_user.id
    
    # ISOLATE CLONE
    udip = get_user_dir(uid)
    repo_p = os.path.join(udip, name)
    if os.path.exists(repo_p): shutil.rmtree(repo_p)
    
    msg = await u.message.reply_text("⏳ Cloning...")
    try:
        subprocess.check_call(["git", "clone", url, repo_p])
        reqs = os.path.join(repo_p, "requirements.txt")
        if os.path.exists(reqs): subprocess.call([sys.executable,"-m","pip","install","-r",reqs])
        
        await msg.edit_text("✅ Done.")
        c.user_data.update({'tid': f"{uid}_{name}|TEMP", 'rname': name, 'dir': repo_p})
        await u.message.reply_text("⚙️ Config:", reply_markup=g_kb())
        return W_GEXT
    except:
        await msg.edit_text("❌ Error.")
        return ConversationHandler.END

async def rx_git_extras(u, c):
    txt = u.message.text
    if txt == "🔙 Cancel": return await cancel(u, c)
    if "Env" in txt: 
        await u.message.reply_text("📝 Vars:", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return W_GENV
    if "Select" in txt:
        rp = c.user_data['dir']
        files = []
        for r,_,fs in os.walk(rp):
            for f in fs: 
                if f.endswith(('.py','.js','.sh')): files.append(os.path.relpath(os.path.join(r,f), rp))
        kb = [[InlineKeyboardButton(f, callback_data=f"sel_{f}")] for f in files[:20]]
        await u.message.reply_text("👇 File:", reply_markup=InlineKeyboardMarkup(kb))
        return W_SEL
    return W_GEXT

async def git_sel(u, c):
    q = u.callback_query
    await q.answer()
    fn = q.data.split('sel_')[1]
    uid = u.effective_user.id
    rn = c.user_data['rname']
    
    tid = f"{uid}_{rn}|{fn}" # Correct Unique ID
    save_ownership(tid, uid, "repo")
    c.user_data['tid'] = tid
    return await launch(u, c)

async def launch(u, c):
    mf = u.message.reply_text if u.message else u.callback_query.message.reply_text
    tid = c.user_data['tid']
    restart_process_bg(tid)
    url = f"{BASE_URL}/status?script={tid}"
    await mf(f"🚀 Started!\nMonitor: {url}", reply_markup=mn_kb(), parse_mode=None) # plain text to avoid parse errors
    return ConversationHandler.END

# 3. MANAGE
@restricted
async def list_h(u, c):
    uid = u.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE): return await u.message.reply_text("Empty.")
    
    d = json.load(open(OWNERSHIP_FILE))
    kb = []
    
    for tid, info in d.items():
        oid = int(info['owner'])
        if uid == ADMIN_ID or uid == oid:
            # Clean filename from ID for display
            display_name = tid.split('_', 1)[1] if '_' in tid else tid
            st = "🟢" if tid in running_processes else "🔴"
            
            lbl = f"{st} {display_name}"
            if uid == ADMIN_ID and oid != uid: lbl += f" (👤 {oid})"
            
            kb.append([InlineKeyboardButton(lbl, callback_data=f"m_{tid}")])
            
    await u.message.reply_text("📂 Apps:", reply_markup=InlineKeyboardMarkup(kb))

async def cb(u, c):
    q = u.callback_query
    await q.answer()
    dt = q.data
    uid = u.effective_user.id
    
    if dt.startswith("m_"):
        tid = dt.split("m_")[1]
        owner = int(get_owner(tid) or 0)
        
        if uid != ADMIN_ID and uid != owner: return 
        
        wd, scr, _, _, _ = resolve_paths(tid)
        st = "🟢 Running" if tid in running_processes else "🔴 Stopped"
        
        # Editors (File Lists)
        efiles = []
        if os.path.exists(os.path.join(wd, scr)): efiles.append(scr)
        # Scan dir
        if "|" in tid:
            for f in [".env", "requirements.txt", "package.json"]:
                if os.path.exists(os.path.join(wd, f)): efiles.append(f)
        else: # Single
            b = tid.split('_', 1)[1] # remove UID prefix from view
            # env is stored as script.py.env
            if os.path.exists(os.path.join(wd, f"{b}.env")): efiles.append(f"{b}.env")
            
        r1 = []
        if "Running" in st: r1.append(InlineKeyboardButton("🛑 Stop", callback_data=f"st_{tid}"))
        else: r1.append(InlineKeyboardButton("🚀 Start", callback_data=f"stt_{tid}"))
        
        r2 = []
        for f in efiles:
            l = "Main" if f == scr else f
            # We must pass 'file' name relative to User Dir
            r2.append(InlineKeyboardButton(f"✏️ {l}", web_app=WebAppInfo(url=f"{BASE_URL}/editor?id={tid}&file={f}&uid={uid}")))
            
        r3 = [InlineKeyboardButton("🗑 Del", callback_data=f"rm_{tid}"), InlineKeyboardButton("📜 Logs", callback_data=f"lg_{tid}")]
        
        rows = [r1, r2, r3]
        clean_name = tid.split('_',1)[1]
        info = f"⚙️ {clean_name}\nStatus: {st}"
        if uid==ADMIN_ID: info += f"\nUser: {owner}"
        
        await q.edit_message_text(info, reply_markup=InlineKeyboardMarkup(rows))

    elif dt.startswith("stt_"): # Start
        tid = dt.split("stt_")[1]
        restart_process_bg(tid)
        await q.edit_message_text("🚀 Starting...")
        
    elif dt.startswith("st_"): # Stop
        tid = dt.split("st_")[1]
        if tid in running_processes:
            os.killpg(os.getpgid(running_processes[tid]['proc'].pid), signal.SIGTERM)
            del running_processes[tid]
        await q.edit_message_text("🛑 Stopped")
        
    elif dt.startswith("rm_"): # Del
        tid = dt.split("rm_")[1]
        # cleanup
        wd, _, _, _, fp = resolve_paths(tid)
        if "|" in tid: shutil.rmtree(wd) # Delete repo dir
        else: 
            if os.path.exists(fp): os.remove(fp) # Delete script
            # also cleanup .env/req
            b = tid.split('_',1)[1]
            e = os.path.join(wd, f"{b}.env")
            if os.path.exists(e): os.remove(e)
            
        delete_ownership(tid)
        await q.edit_message_text("🗑 Deleted")
        
    elif dt.startswith("lg_"):
        tid = dt.split("lg_")[1]
        lp = os.path.join(BASE_UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(lp): await context.bot.send_document(uid, open(lp,'rb'))
        else: await q.message.reply_text("No logs")

@admin_only
async def adm_add(u,c):
    try:
        i = int(c.args[0])
        l = []
        if os.path.exists(USERS_FILE): l=json.load(open(USERS_FILE))
        if i not in l: l.append(i)
        json.dump(l, open(USERS_FILE,'w'))
        await u.message.reply_text(f"Add {i}")
    except: pass

@admin_only
async def adm_rm(u,c):
    try:
        i = int(c.args[0])
        l = json.load(open(USERS_FILE))
        if i in l: l.remove(i)
        json.dump(l, open(USERS_FILE,'w'))
        await u.message.reply_text(f"Rem {i}")
    except: pass

@restricted
async def stats(u,c): await u.message.reply_text(f"Running: {len(running_processes)}")

# --- MAIN ---
if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    cf = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Upload File"), up_start)],
        states={
            W_FILE: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Document.ALL, rx_file)],
            W_EXT: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Regex("(RUN|Env|Deps)"), rx_extras), MessageHandler(filters.Document.ALL, rx_deps_file)],
            W_ENV: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, rx_env)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    cg = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("Clone"), git_start)],
        states={
            W_URL: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, rx_git)],
            W_GEXT: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.Regex("(Vars|Select)"), rx_git_extras)],
            W_GENV: [MessageHandler(filters.Regex("Cancel"), cancel), MessageHandler(filters.TEXT, rx_env)],
            W_SEL: [CallbackQueryHandler(git_sel)]
        }, fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', adm_add))
    app_bot.add_handler(CommandHandler('remove', adm_rm))
    app_bot.add_handler(CommandHandler('help', help_cmd))
    app_bot.add_handler(CommandHandler('start', start))
    app_bot.add_handler(cf)
    app_bot.add_handler(cg)
    app_bot.add_handler(MessageHandler(filters.Regex("My Hosted"), list_h))
    app_bot.add_handler(MessageHandler(filters.Regex("Stats"), stats))
    app_bot.add_handler(MessageHandler(filters.Regex("Help"), help_cmd))
    app_bot.add_handler(CallbackQueryHandler(cb))
    
    print("Online...")
    app_bot.run_polling()