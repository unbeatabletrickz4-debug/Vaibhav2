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
from flask import Flask, request
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
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

# --- FLASK SERVER (Only for Uptime, No Editor) ---
app = Flask(__name__)

@app.route('/')
def home(): return "🤖 Bot is Alive!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if not script_name: return "Specify script", 400
    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- DATA & UTILS ---
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
    data = load_ownership()
    data[target_id] = {"owner": user_id, "type": type_}
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def delete_ownership(target_id):
    data = load_ownership()
    if target_id in data:
        del data[target_id]
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
    else:
        work_dir = UPLOAD_DIR
        script_path = target_id
        env_path = os.path.join(work_dir, f"{target_id}.env")
        req_path = os.path.join(work_dir, f"{target_id}_req.txt")
    return work_dir, script_path, env_path, req_path

async def install_dependencies(work_dir, update):
    msg = None
    try:
        # Python
        if os.path.exists(os.path.join(work_dir, "requirements.txt")):
            if not msg: msg = await update.message.reply_text("⏳ Installing Dependencies...")
            proc = await asyncio.create_subprocess_exec("pip", "install", "-r", "requirements.txt", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
        
        # Node
        if os.path.exists(os.path.join(work_dir, "package.json")):
            if not msg: msg = await update.message.reply_text("⏳ Installing Dependencies...")
            proc = await asyncio.create_subprocess_exec("npm", "install", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            
        if msg: await msg.edit_text("✅ Installed!")
    except: pass

# --- PERMISSIONS ---
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
            await update.message.reply_text("⛔ Admin Only.")
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

# --- STATES ---
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)

# --- HANDLERS ---
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Hosting Bot Ready**", reply_markup=main_menu_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# ... UPLOAD LOGIC ...
@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send file (.py, .js, .sh)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    path = os.path.join(UPLOAD_DIR, fname)
    
    # Conflict check
    owner = get_owner(fname)
    if os.path.exists(path) and owner and owner != uid and uid != ADMIN_ID:
        return await update.message.reply_text("❌ Filename taken by another user.")

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
        await update.message.reply_text("📂 Send `requirements.txt` or `package.json`")
        context.user_data['wait'] = 'req'
    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    target_id = context.user_data['target_id']
    _, _, env_path, _ = resolve_paths(target_id)
    with open(env_path, "a") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
        f.write(update.message.text)
    
    markup = git_extras_keyboard() if context.user_data.get('type') == 'repo' else extras_keyboard()
    return_state = WAIT_GIT_EXTRAS if context.user_data.get('type') == 'repo' else WAIT_EXTRAS
    
    await update.message.reply_text("✅ Saved.", reply_markup=markup)
    return return_state

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('wait'): return WAIT_EXTRAS
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    target_id = context.user_data['target_id']
    
    path = ""
    if fname == "package.json": path = os.path.join(UPLOAD_DIR, "package.json")
    elif fname.endswith(".txt"): path = os.path.join(UPLOAD_DIR, f"{target_id}_req.txt")
    
    if path:
        await file.download_to_drive(path)
        # Install immediately
        msg = await update.message.reply_text("⏳ Installing...")
        try:
            if fname.endswith(".txt"):
                proc = await asyncio.create_subprocess_exec("pip", "install", "-r", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
            elif fname == "package.json":
                proc = await asyncio.create_subprocess_exec("npm", "install", cwd=UPLOAD_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
            await msg.edit_text("✅ Installed!")
        except: pass
        
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# ... GIT LOGIC ...
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
    except:
        await update.message.reply_text("❌ Clone Failed")
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
    if not files: return await update.message.reply_text("❌ No scripts found.")
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

# ... EXECUTION ...
async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    work_dir, script_path, env_path, _ = resolve_paths(target_id)
    
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
    
    cmd = ["python", "-u", script_path]
    if script_path.endswith(".js"): cmd = ["node", script_path]
    elif script_path.endswith(".sh"): cmd = ["bash", script_path]
    
    try:
        proc = subprocess.Popen(cmd, env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, cwd=work_dir, preexec_fn=os.setsid)
        running_processes[target_id] = {"process": proc, "log": log_path}
        url = f"{BASE_URL}/status?script={target_id}"
        await msg_func(f"🚀 **Running!**\nPID: {proc.pid}\n🔗 `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e: await msg_func(f"❌ Error: {e}")
    return ConversationHandler.END

# --- MANAGE & ADMIN LIST (THE REQUESTED FEATURE) ---
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not os.path.exists(OWNERSHIP_FILE): return await update.message.reply_text("📂 Empty.")
    with open(OWNERSHIP_FILE) as f: ownership = json.load(f)
    
    keyboard = []
    for tid, meta in ownership.items():
        owner_id = meta.get("owner")
        
        # LOGIC: 
        # 1. If Admin: Show ALL scripts + Add (User: ID) info
        # 2. If User: Show ONLY their own scripts
        
        if uid == ADMIN_ID or uid == owner_id:
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            
            # Label Construction
            label = f"{status} {tid}"
            if uid == ADMIN_ID and uid != owner_id:
                label += f" (👤 {owner_id})" # <-- ADMIN SEES THIS
            
            keyboard.append([InlineKeyboardButton(label, callback_data=f"man_{tid}")])
    
    if not keyboard: return await update.message.reply_text("📂 No apps found.")
    await update.message.reply_text("📂 **Hosted Apps:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("man_"):
        tid = data.split("man_")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner: return await query.message.reply_text("⛔ Not yours.")
        
        is_running = tid in running_processes and running_processes[tid]['process'].poll() is None
        status = "🟢 Running" if is_running else "🔴 Stopped"
        
        # Admin gets extra info in the text
        extra_info = ""
        if uid == ADMIN_ID:
            extra_info = f"\n👤 **Owner:** `{owner}`"

        text = f"⚙️ **Manage:** `{tid}`{extra_info}\nStatus: {status}"
        
        btns = []
        if is_running:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{tid}"), InlineKeyboardButton("🔗 URL", callback_data=f"url_{tid}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{tid}")])
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
        work_dir, _, _, _ = resolve_paths(tid)
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
