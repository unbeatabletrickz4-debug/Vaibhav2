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

# Global State
running_processes = {} 

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER ---
app = Flask(__name__)

@app.route('/')
def home(): return "🤖 Python Host Bot is Alive!", 200

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

# --- DATA MANAGEMENT ---
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

# --- DECORATORS ---
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if uid != ADMIN_ID and uid not in get_allowed_users():
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
    return ReplyKeyboardMarkup([
        ["📤 Upload File", "🌐 Clone from Git"],
        ["📂 My Hosted Apps", "📊 Server Stats"],
        ["🆘 Help"]
    ], resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Add reqs", "📝 Type Env Vars"], 
        ["🚀 RUN NOW", "🔙 Cancel"]
    ], resize_keyboard=True)

def git_extras_keyboard():
    return ReplyKeyboardMarkup([
        ["📝 Type Env Vars"],
        ["📂 Select File to Run", "🔙 Cancel"]
    ], resize_keyboard=True)

# --- HELPERS ---
def smart_fix_requirements(req_path):
    try:
        with open(req_path, 'r') as f: lines = f.readlines()
        clean = []
        for line in lines:
            line = line.strip()
            if not line: continue
            if line.lower().startswith("pip install"):
                clean.extend(line[11:].strip().split())
            else:
                clean.append(line)
        with open(req_path, 'w') as f: f.write('\n'.join(clean))
        return True
    except: return False

async def install_requirements(req_path, update):
    msg = await update.message.reply_text("⏳ **Installing requirements...**")
    smart_fix_requirements(req_path)
    try:
        proc = await asyncio.create_subprocess_exec("pip", "install", "-r", req_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode == 0: await msg.edit_text("✅ **Installed!**")
        else: await msg.edit_text(f"❌ Failed:\n```\n{stderr.decode()[-1000:]}\n```", parse_mode="Markdown")
    except Exception as e: await msg.edit_text(f"❌ Error: {e}")

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

# --- EXECUTION LOGIC ---
async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
    
    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    work_dir, script_path, env_path, _, _ = resolve_paths(target_id)

    # Stop if running
    if target_id in running_processes:
        try:
            os.killpg(os.getpgid(running_processes[target_id]['process'].pid), signal.SIGTERM)
            running_processes[target_id]['process'].wait()
        except: pass
        del running_processes[target_id]

    # Load Env
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=', 1)
                    v = v.strip().strip('"').strip("'")
                    custom_env[k.strip()] = v

    log_file_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_file_path, "w")
    
    try:
        proc = subprocess.Popen(
            ["python", "-u", script_path], 
            env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, 
            cwd=work_dir, preexec_fn=os.setsid
        )
        running_processes[target_id] = {"process": proc, "log": log_file_path}
        
        await msg_func(f"🚀 **Started!**\nID: `{target_id}`\nPID: {proc.pid}")
        await asyncio.sleep(3)
        if proc.poll() is not None:
            log_file.close()
            with open(log_file_path) as f: log = f.read()[-2000:]
            await msg_func(f"❌ **Crashed:**\n`{log}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            url = f"{BASE_URL}/status?script={target_id}"
            await msg_func(f"🟢 **Running!**\n🔗 URL: `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())

    except Exception as e:
        await msg_func(f"❌ Error: {e}", reply_markup=main_menu_keyboard())
        
    return ConversationHandler.END

# --- EDIT CONVERSATION ---
WAIT_EDIT_FILE, WAIT_EDIT_ENV_TXT = range(2)

async def start_edit_src(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target_id = query.data.split("edit_src_")[1]
    context.user_data['edit_target_id'] = target_id
    context.user_data['edit_type'] = 'src'
    
    _, _, _, _, full_path = resolve_paths(target_id)
    
    if os.path.exists(full_path):
        await context.bot.send_document(chat_id=update.effective_chat.id, document=open(full_path, 'rb'), caption="📄 **Current Source File**")
    
    await query.message.reply_text(
        "✏️ **Update Source Code**\n"
        "Please send the **New .py File** to overwrite the existing one.\n"
        "It will be automatically redeployed.",
        reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True)
    )
    return WAIT_EDIT_FILE

async def start_edit_req(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target_id = query.data.split("edit_req_")[1]
    context.user_data['edit_target_id'] = target_id
    context.user_data['edit_type'] = 'req'
    
    _, _, _, req_path, _ = resolve_paths(target_id)
    
    if os.path.exists(req_path):
         await context.bot.send_document(chat_id=update.effective_chat.id, document=open(req_path, 'rb'), caption="📦 **Current Requirements**")
    
    await query.message.reply_text(
        "📦 **Update Requirements**\n"
        "Send a new `requirements.txt` file.",
        reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True)
    )
    return WAIT_EDIT_FILE

async def start_edit_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    target_id = query.data.split("edit_env_")[1]
    context.user_data['edit_target_id'] = target_id
    
    _, _, env_path, _, _ = resolve_paths(target_id)
    
    current_vars = "No variables set."
    if os.path.exists(env_path):
        with open(env_path, 'r') as f: current_vars = f.read()[-1000:]
    
    await query.message.reply_text(
        f"🔐 **Current Env Vars:**\n```\n{current_vars}\n```\n\n"
        "**Options:**\n"
        "1. Type new variables to **Append**.\n"
        "2. Type `/clear` to delete all variables.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True)
    )
    return WAIT_EDIT_ENV_TXT

async def receive_edit_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    
    target_id = context.user_data['edit_target_id']
    edit_type = context.user_data['edit_type']
    file = await update.message.document.get_file()
    
    work_dir, _, _, req_path, full_script_path = resolve_paths(target_id)
    
    if edit_type == 'src':
        if not update.message.document.file_name.endswith('.py'):
            await update.message.reply_text("❌ Send a .py file.")
            return WAIT_EDIT_FILE
        await file.download_to_drive(full_script_path)
        await update.message.reply_text("✅ **Source Updated!** Redeploying...")
        
    elif edit_type == 'req':
        if not update.message.document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Send a .txt file.")
            return WAIT_EDIT_FILE
        await file.download_to_drive(req_path)
        await install_requirements(req_path, update)
        await update.message.reply_text("✅ **Requirements Updated!** Redeploying...")
    
    # Trigger Redeploy
    context.user_data['target_id'] = target_id
    await execute_logic(update, context)
    return ConversationHandler.END

async def receive_edit_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔙 Cancel": return await cancel(update, context)
    
    target_id = context.user_data['edit_target_id']
    _, _, env_path, _, _ = resolve_paths(target_id)
    
    if text == "/clear":
        if os.path.exists(env_path): os.remove(env_path)
        await update.message.reply_text("🗑️ **Env Vars Cleared!** Redeploying...")
    else:
        with open(env_path, "a") as f:
            if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
            f.write(text)
        await update.message.reply_text("✅ **Env Vars Updated!** Redeploying...")

    context.user_data['target_id'] = target_id
    await execute_logic(update, context)
    return ConversationHandler.END


# --- UPLOAD & GIT HANDLERS (Standard) ---
WAIT_PY, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send `.py` file.", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_PY

async def receive_py(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    if not fname.endswith(".py"): return await update.message.reply_text("❌ Needs .py")
    owner = get_owner(fname)
    if os.path.exists(os.path.join(UPLOAD_DIR, fname)) and owner and owner != uid and uid != ADMIN_ID:
        return await update.message.reply_text("❌ Taken.")
    
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
        await update.message.reply_text("📝 **Type Env Vars:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_ENV_TEXT
    elif "reqs" in txt:
        await update.message.reply_text("📂 Send `requirements.txt`.")
        context.user_data['wait'] = 'req'
        return WAIT_EXTRAS
    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    target_id = context.user_data['target_id']
    _, _, env_path, _, _ = resolve_paths(target_id)
    with open(env_path, "a") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
        f.write(update.message.text)
    
    # Check return state
    if context.user_data.get('type') == 'repo': return WAIT_GIT_EXTRAS
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('wait'): return WAIT_EXTRAS
    file = await update.message.document.get_file()
    target_id = context.user_data['target_id']
    _, _, _, req_path, _ = resolve_paths(target_id)
    if context.user_data['wait'] == 'req':
        await file.download_to_drive(req_path)
        await install_requirements(req_path, update)
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# --- GIT HANDLERS ---
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)

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
        req_path = os.path.join(repo_path, "requirements.txt")
        if os.path.exists(req_path): await install_requirements(req_path, update)
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
        await update.message.reply_text("📝 **Type Env Vars:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_GIT_ENV_TEXT
    elif txt == "📂 Select File to Run": return await show_file_selection(update, context)
    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data['repo_path']
    py_files = []
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py"): py_files.append(os.path.relpath(os.path.join(root, file), repo_path))
    if not py_files: return await update.message.reply_text("❌ No .py files.")
    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_py_{f}")] for f in py_files[:10]]
    await update.message.reply_text("👇 **Select Main File:**", reply_markup=InlineKeyboardMarkup(keyboard))
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

# --- MANAGE CALLBACKS ---
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ownership = load_ownership()
    keyboard = []
    for tid, meta in ownership.items():
        if uid == ADMIN_ID or uid == meta.get("owner"):
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status} {tid}", callback_data=f"man_{tid}")])
    if not keyboard: return await update.message.reply_text("📂 No files.", reply_markup=main_menu_keyboard())
    await update.message.reply_text("📂 **Your Apps:**", reply_markup=InlineKeyboardMarkup(keyboard))

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
        
        text = f"⚙️ **Manage:** `{tid}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        btns = []
        if is_running:
            # THIS WAS THE PROBLEMATIC LINE, NOW FIXED AND SPLIT
            btns.append([
                InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{tid}"), 
                InlineKeyboardButton("🔗 URL", callback_data=f"url_{tid}")
            ])
        else:
            btns.append([InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{tid}")])
        
        btns.append([InlineKeyboardButton("✏️ Edit & Deploy", callback_data=f"edit_menu_{tid}")])
        btns.append([
            InlineKeyboardButton("📜 Logs", callback_data=f"log_{tid}"), 
            InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{tid}")
        ])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("edit_menu_"):
        tid = data.split("edit_menu_")[1]
        text = f"✏️ **Edit & Redeploy:** `{tid}`\nChanges will restart the app."
        btns = [
            [InlineKeyboardButton("📝 Update .py", callback_data=f"edit_src_{tid}")],
            [InlineKeyboardButton("📦 Update Reqs", callback_data=f"edit_req_{tid}")],
            [InlineKeyboardButton("🔐 Env Vars", callback_data=f"edit_env_{tid}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"man_{tid}")]
        ]
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

# --- ADMIN & SYSTEM ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 **Help & Support**\n\n"
        "Contact the dev: @platoonleaderr\n\n"
        "• **Upload File:** Host a single .py file.\n"
        "• **Git Clone:** Host a repo from a public URL.\n"
        "• **Env Vars:** You can now type them directly!\n"
        "• **Manage:** Edit files, env vars, and restart scripts.",
        parse_mode="Markdown"
    )

@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if save_allowed_user(int(context.args[0])): await update.message.reply_text("✅ Added.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if remove_allowed_user(int(context.args[0])): await update.message.reply_text("🗑️ Removed.")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Running: {len(running_processes)}")

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    # Edit Conversation
    conv_edit = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_edit_src, pattern="^edit_src_"),
            CallbackQueryHandler(start_edit_req, pattern="^edit_req_"),
            CallbackQueryHandler(start_edit_env, pattern="^edit_env_")
        ],
        states={
            WAIT_EDIT_FILE: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Document.ALL, receive_edit_file)],
            WAIT_EDIT_ENV_TXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_edit_env)]
        },
        fallbacks=[CommandHandler('cancel', cancel), MessageHandler(filters.Regex("^🔙 Cancel$"), cancel)]
    )

    conv_file = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload File$"), upload_start)],
        states={
            WAIT_PY: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Document.FileExtension("py"), receive_py)],
            WAIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(🚀|➕|📝)"), receive_extras), MessageHandler(filters.Document.ALL, receive_extra_files)],
            WAIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    conv_git = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🌐 Clone from Git$"), git_start)],
        states={
            WAIT_URL: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_git_url)],
            WAIT_GIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(📝|📂)"), receive_git_extras)],
            WAIT_GIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)],
            WAIT_SELECT_FILE: [CallbackQueryHandler(select_git_file)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', add_user))
    app_bot.add_handler(CommandHandler('remove', remove_user))
    app_bot.add_handler(conv_edit) # NEW
    app_bot.add_handler(conv_file)
    app_bot.add_handler(conv_git)
    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Hosted Apps$"), list_hosted))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(MessageHandler(filters.Regex("^🆘 Help$"), help_command))
    app_bot.add_handler(CallbackQueryHandler(manage_callback))
    app_bot.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    app_bot.run_polling()