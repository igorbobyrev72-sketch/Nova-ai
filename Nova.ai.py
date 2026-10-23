import os
import sqlite3
import threading
import requests
import telebot
import io
import base64
import asyncio
import edge_tts
import time
from PyPDF2 import PdfReader
import pandas as pd
import pdf2docx
from PIL import Image
from fpdf import FPDF

from telebot import types
from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

if not BOT_TOKEN or not GROQ_KEY:
    raise SystemExit("Ошибка: не заданы BOT_TOKEN или GROQ_KEY")

bot = telebot.TeleBot(BOT_TOKEN)
user_files = {}
find_error_mode = {}

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, role TEXT DEFAULT 'Обычный', model TEXT, requests INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, role TEXT, content TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS favorites (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, text TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS core_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, task_text TEXT, agent_type TEXT, result TEXT, status TEXT DEFAULT 'pending')''')
    conn.commit()
    conn.close()

init_db()

def get_user(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT role, model, requests FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (chat_id, model) VALUES (?, ?)", (chat_id, MODEL))
        conn.commit()
        row = ("Обычный", MODEL, 0)
    conn.close()
    return row

def update_user(chat_id, role=None, model=None):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    if role: c.execute("UPDATE users SET role = ? WHERE chat_id = ?", (role, chat_id))
    if model: c.execute("UPDATE users SET model = ? WHERE chat_id = ?", (model, chat_id))
    conn.commit()
    conn.close()

def inc_requests(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("UPDATE users SET requests = requests + 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def get_history(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT role, content FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 10", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

def add_history(chat_id, role, content):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
    conn.commit()
    conn.close()

def clear_history(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

# --- HEALTH CHECK ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args): pass

def run_health():
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()

# --- ЛОГИКА ---
def is_allowed(m):
    if ADMIN_ID != 0 and m.chat.id != ADMIN_ID:
        bot.reply_to(m, "Иди нахуй, доступ закрыт.")
        return False
    return True

def get_main_keyboard(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    role, _, _ = get_user(chat_id)
    err_mode = "ВКЛ" if find_error_mode.get(chat_id, False) else "ВЫКЛ"
    btn1 = types.KeyboardButton(f"Роль: {role}")
    btn2 = types.KeyboardButton(f"Найди ошибку: {err_mode}")
    btn3 = types.KeyboardButton("Сменить модель")
    btn4 = types.KeyboardButton("Профиль")
    btn5 = types.KeyboardButton("Очистить историю")
    btn6 = types.KeyboardButton("Поиск")
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    markup.add(btn5, btn6)
    return markup

def get_main_inline_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("💬 ИИ-Чат", callback_data="menu_chat"), types.InlineKeyboardButton("🧠 Режимы", callback_data="menu_modes"))
    markup.add(types.InlineKeyboardButton("📜 История", callback_data="menu_history"), types.InlineKeyboardButton("👤 Профиль", callback_data="menu_profile"))
    markup.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"), types.InlineKeyboardButton("❓ Помощь", callback_data="menu_help"))
    return markup

def ask_groq(user_id, text, system_prompt=None):
    history = get_history(user_id)
    history.append({"role": "user", "content": text})
    if len(history) > 10: history = history[-10:]
    
    role, _, _ = get_user(user_id)
    
    if system_prompt:
        sys_prompt = system_prompt
    elif role == "Дебаггер": sys_prompt = "Ты — сеньор-разработчик. Найди ошибку, объясни и выдай исправленный код."
    elif role == "Эксперт": sys_prompt = "Ты — строгий эксперт. Отвечай максимально точно, без воды."
    elif role == "Юморист": sys_prompt = "Ты — веселый юморист. Отвечай с шутками и прибаутками."
    elif role == "Программист": sys_prompt = "Ты — опытный программист. Пиши код и объясняй его."
    elif role == "Дружелюбный": sys_prompt = "Ты — дружелюбный помощник. Общайся тепло и поддерживай."
    else: sys_prompt = "Ты полезный помощник. Отвечай без Markdown."

    messages = [{"role": "system", "content": sys_prompt}] + history
    _, current_model, _ = get_user(user_id)
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    data = {"model": current_model, "messages": messages}
    
    r = requests.post(url, headers=headers, json=data, timeout=60)
    r.raise_for_status()
    ans = r.json()["choices"][0]["message"]["content"]
    
    add_history(user_id, "user", text)
    add_history(user_id, "assistant", ans)
    inc_requests(user_id)
    return ans

# --- NOVA CORE AUTO ---
AGENT_PROMPTS = {
    "planner": "Ты — Planner (Планировщик) в системе Nova Core. Твоя задача — взять идею пользователя и разбить её на четкие, пошаговые инструкции. Формат ответа: нумерованный список шагов. Никакой воды.",
    "builder": "Ты — Builder (Разработчик). Твоя задача — написать код или техническое решение по плану. Пиши чистый Python-код. Отвечай без лишних слов, только код.",
    "checker": "Ты — Checker (Проверяющий). Твоя задача — проанализировать предложенное решение. Найди баги, уязвимости, логические ошибки. Будь строгим."
}

@bot.message_handler(commands=["auto"])
def cmd_auto(m):
    if not is_allowed(m): return
    msg = bot.reply_to(m, "🚀 Nova Core Auto активирован.\n\nНапиши задачу, и агенты (План -> Код -> Проверка) выполнят её по цепочке:")
    bot.register_next_step_handler(msg, process_auto_task)

def process_auto_task(m):
    if not is_allowed(m): return
    task_text = m.text
    if not task_text: return

    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("INSERT INTO core_tasks (chat_id, task_text) VALUES (?, ?)", (m.chat.id, task_text))
    task_id = c.lastrowid
    conn.commit()
    conn.close()

    bot.reply_to(m, f"Задача: {task_text[:50]}...\n\nЗапускаю цепочку. Из-за лимитов Groq это займет несколько минут.")

    def safe_groq(prompt, sys_prompt):
        safe_prompt = prompt[:4000] 
        for attempt in range(3):
            try:
                return ask_groq(m.chat.id, safe_prompt, system_prompt=sys_prompt)
            except Exception as e:
                if "429" in str(e):
                    bot.send_message(m.chat.id, f"⏳ Groq забанил (429). Жду 60 секунд...")
                    time.sleep(60)
                elif "413" in str(e):
                    bot.send_message(m.chat.id, f"❌ Текст слишком длинный (413). Обрезаю и пробую снова...")
                    safe_prompt = safe_prompt[:2000]
                else:
                    raise e
        raise Exception("Groq не отпустил после 3 попыток.")

    try:
        msg1 = bot.send_message(m.chat.id, "⏳ Шаг 1: Planner составляет план...")
        plan = safe_groq(task_text, AGENT_PROMPTS["planner"])
        bot.edit_message_text(f"✅ План готов.\n\nЖду 15 секунд...", m.chat.id, msg1.message_id)
        time.sleep(15)

        msg2 = bot.send_message(m.chat.id, "⏳ Шаг 2: Builder пишет код...")
        builder_input = f"Вот задача: {task_text}\n\nВот план действий:\n{plan[:3000]}\n\nНапиши код по этому плану."
        code_result = safe_groq(builder_input, AGENT_PROMPTS["builder"])
        bot.edit_message_text(f"✅ Код написан.\n\nЖду 15 секунд...", m.chat.id, msg2.message_id)
        time.sleep(15)

        msg3 = bot.send_message(m.chat.id, "⏳ Шаг 3: Checker проверяет код...")
        checker_input = f"Вот задача: {task_text}\n\nВот код:\n{code_result[:3000]}\n\nПроверь его на ошибки."
        review = safe_groq(checker_input, AGENT_PROMPTS["checker"])
        
        final_result = f"ПЛАН:\n{plan}\n\nКОД:\n{code_result}\n\nРЕВЬЮ:\n{review}"
        conn = sqlite3.connect("bot.db")
        c = conn.cursor()
        c.execute("UPDATE core_tasks SET agent_type = 'auto', result = ?, status = 'done' WHERE id = ?", (final_result, task_id))
        conn.commit()
        conn.close()

        bot.edit_message_text(f"✅ Проверка завершена!", m.chat.id, msg3.message_id)
        
        final_text = f"🔥 РЕЗУЛЬТАТ NOVA CORE AUTO:\n\n📋 ПЛАН:\n{plan}\n\n💻 КОД:\n{code_result}\n\n🔍 ПРОВЕРКА:\n{review}"
        for i in range(0, len(final_text), 4000):
            bot.send_message(m.chat.id, final_text[i:i+4000])

    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Ошибка в цепочке: {e}")

@bot.message_handler(commands=["core"])
def cmd_core(m):
    if not is_allowed(m): return
    msg = bot.reply_to(m, "🧠 Nova Core активирован.\n\nНапиши задачу:")
    bot.register_next_step_handler(msg, process_core_task)

def process_core_task(m):
    if not is_allowed(m): return
    task_text = m.text
    if not task_text: return
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("INSERT INTO core_tasks (chat_id, task_text) VALUES (?, ?)", (m.chat.id, task_text))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📋 Planner", callback_data=f"core_planner_{task_id}"))
    markup.add(types.InlineKeyboardButton("💻 Builder", callback_data=f"core_builder_{task_id}"))
    markup.add(types.InlineKeyboardButton("🔍 Checker", callback_data=f"core_checker_{task_id}"))
    bot.reply_to(m, f"Задача: {task_text[:50]}...\n\nВыбери агента:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("core_"))
def handle_core_callback(call):
    if not is_allowed(call.message): return
    parts = call.data.split("_")
    agent_type = parts[1]
    task_id = parts[2]
    bot.answer_callback_query(call.id, f"Агент {agent_type} работает...")
    
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("SELECT task_text FROM core_tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    conn.close()
    if not row: return
    
    task_text = row[0]
    bot.edit_message_text(f"⏳ Агент {agent_type.capitalize()} думает...", call.message.chat.id, call.message.message_id)
    
    try:
        ans = ask_groq(call.message.chat.id, task_text[:4000], system_prompt=AGENT_PROMPTS.get(agent_type, "Ты полезный помощник."))
        conn = sqlite3.connect("bot.db")
        c = conn.cursor()
        c.execute("UPDATE core_tasks SET agent_type = ?, result = ?, status = 'done' WHERE id = ?", (agent_type, ans, task_id))
        conn.commit()
        conn.close()
        
        full_text = f"🤖 Ответ от {agent_type.capitalize()}:\n\n{ans}"
        for i in range(0, len(full_text), 4000):
            if i == 0: bot.edit_message_text(full_text[i:i+4000], call.message.chat.id, call.message.message_id)
            else: bot.send_message(call.message.chat.id, full_text[i:i+4000])
    except Exception as e:
        bot.edit_message_text(f"❌ Ошибка: {e}", call.message.chat.id, call.message.message_id)

# --- КОМАНДЫ ---
@bot.message_handler(commands=["start"])
def start(m):
    if not is_allowed(m): return
    get_user(m.chat.id)
    welcome_text = f"👋 Привет, {m.from_user.first_name}!\n\nЯ — Nova SI. Выбери раздел ниже:"
    bot.send_message(m.chat.id, welcome_text, reply_markup=get_main_inline_menu())
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Очистить чат"), types.KeyboardButton("Сменить модель"))
    bot.send_message(m.chat.id, "Кнопки быстрого доступа:", reply_markup=markup)

@bot.message_handler(commands=["help"])
def cmd_help(m):
    if not is_allowed(m): return
    help_text = (
        "🤖 Мои возможности:\n\n"
        "💬 ИИ-Чат — пиши мне, и я отвечу.\n"
        "📎 Файлы — кидай PDF, TXT или картинки.\n"
        "🖼 Картинки — /image твой_запрос.\n"
        "🎙 Голос — отправь голосовое.\n"
        "🌐 Поиск — кнопка Поиск.\n"
        "🚀 Nova Core Auto — /auto для запуска цепочки агентов.\n\n"
        "Команды:\n/start — Меню\n/clear — Очистить историю\n/profile — Профиль\n/help — Помощь"
    )
    bot.send_message(m.chat.id, help_text)

@bot.message_handler(commands=["clear"])
def cmd_clear(m):
    if not is_allowed(m): return
    clear_history(m.chat.id)
    bot.reply_to(m, "🧹 История очищена.", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(commands=["profile"])
def cmd_profile(m):
    if not is_allowed(m): return
    role, model, reqs = get_user(m.chat.id)
    text = f"👤 Профиль:\n\nРоль: {role}\nМодель: {model}\nЗапросов: {reqs}"
    bot.reply_to(m, text, reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(commands=["image"])
def gen_image(m):
    if not is_allowed(m): return
    prompt = m.text.replace("/image", "").strip()
    if not prompt:
        bot.reply_to(m, "А промпт где?")
        return
    tmp = bot.reply_to(m, "Рисую...")
    try:
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"
        img_data = requests.get(url, timeout=60).content
        bot.delete_message(m.chat.id, tmp.message_id)
        bot.send_photo(m.chat.id, img_data, caption=f"Запрос: {prompt}")
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(content_types=['voice'])
def handle_voice(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Слушаю...")
    try:
        file_info = bot.get_file(m.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)
        url = "https://api.groq.com/openai/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {GROQ_KEY}"}
        files = {"file": ("voice.ogg", downloaded)}
        data = {"model": "whisper-large-v3-turbo"}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        r.raise_for_status()
        text = r.json().get("text", "")
        bot.edit_message_text(f"Ты сказал: {text}\n\nДумаю...", m.chat.id, tmp.message_id)
        ans = ask_groq(m.chat.id, text)
        audio_file = f"/tmp/ans_{m.chat.id}.mp3"
        async def tts():
            communicate = edge_tts.Communicate(ans, "ru-RU-DmitryNeural")
            await communicate.save(audio_file)
        asyncio.run(tts())
        with open(audio_file, "rb") as audio:
            bot.send_voice(m.chat.id, audio, caption=ans[:1000])
        os.remove(audio_file)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(func=lambda message: message.text == "Поиск")
def search_web(m):
    if not is_allowed(m): return
    msg = bot.reply_to(m, "Введи поисковый запрос:")
    bot.register_next_step_handler(msg, process_search)

def process_search(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Ищу в интернете...")
    
    instances = [
        "https://searx.tiekoetter.com",
        "https://searx.be",
        "https://search.bus-hit.me",
        "https://searxng.site",
        "https://priv.au"
    ]
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    results = None
    
    for base_url in instances:
        try:
            url = f"{base_url}/search"
            params = {"q": m.text, "format": "json"}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            
            if r.status_code == 200:
                try:
                    data = r.json()
                    if data.get("results"):
                        results = data.get("results", [])[:3]
                        break 
                except:
                    continue 
        except Exception:
            continue 
            
    if not results:
        bot.edit_message_text("Все поисковики сейчас перегружены. Попробуй позже.", m.chat.id, tmp.message_id)
        return

    context = ""
    for res in results:
        context += f"Источник: {res.get('title')} ({res.get('url')})\n{res.get('content')}\n\n"
        
    try:
        ans = ask_groq(m.chat.id, f"Ответь на запрос '{m.text}', используя эту инфу:\n\n{context[:4000]}")
        bot.edit_message_text(ans[:4000], m.chat.id, tmp.message_id)
    except Exception as e:
        bot.edit_message_text(f"Ошибка ИИ: {e}", m.chat.id, tmp.message_id)

# --- ОБРАБОТКА ИНЛАЙН ---
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def handle_menu(call):
    if not is_allowed(call.message): return
    bot.answer_callback_query(call.id)
    data = call.data
    
    if data == "menu_chat":
        bot.edit_message_text("💬 ИИ-Чат активирован.", call.message.chat.id, call.message.message_id, reply_markup=get_main_inline_menu())
    elif data == "menu_modes":
        markup = types.InlineKeyboardMarkup(row_width=2)
        for r in ["Обычный", "Дебаггер", "Эксперт", "Юморист", "Программист", "Дружелюбный"]:
            markup.add(types.InlineKeyboardButton(r, callback_data=f"role_{r}"))
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="menu_back"))
        bot.edit_message_text("🧠 Выбери режим:", call.message.chat.id, call.message.message_id, reply_markup=markup)
    elif data == "menu_history":
        bot.edit_message_text("📜 История диалогов (последние 10 сообщений).", call.message.chat.id, call.message.message_id, reply_markup=get_main_inline_menu())
    elif data == "menu_profile":
        role, model, reqs = get_user(call.message.chat.id)
        bot.edit_message_text(f"👤 Профиль\n\nРоль: {role}\nМодель: {model}\nЗапросов: {reqs}", call.message.chat.id, call.message.message_id, reply_markup=get_main_inline_menu())
    elif data == "menu_settings":
        bot.edit_message_text("⚙️ Настройки появятся позже.", call.message.chat.id, call.message.message_id, reply_markup=get_main_inline_menu())
    elif data == "menu_help":
        cmd_help(call.message)
    elif data == "menu_back":
        bot.edit_message_text("Выбери раздел:", call.message.chat.id, call.message.message_id, reply_markup=get_main_inline_menu())

@bot.message_handler(func=lambda message: message.text.startswith("Роль:"))
def role_menu(m):
    if not is_allowed(m): return
    markup = types.InlineKeyboardMarkup()
    for r in ["Обычный", "Дебаггер", "Эксперт", "Юморист", "Программист", "Дружелюбный"]:
        markup.add(types.InlineKeyboardButton(r, callback_data=f"role_{r}"))
    bot.reply_to(m, "Выбери роль:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("role_"))
def set_role(call):
    if not is_allowed(call.message): return
    update_user(call.message.chat.id, role=call.data.replace("role_", ""))
    bot.answer_callback_query(call.id, f"Роль: {call.data.replace('role_', '')}")
    bot.edit_message_text(f"Теперь моя роль: {call.data.replace('role_', '')}", call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Меню обновлено.", reply_markup=get_main_keyboard(call.message.chat.id))

@bot.message_handler(func=lambda message: message.text == "Сменить модель")
def model_menu(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Смотрю модели...")
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=20)
        r.raise_for_status()
        valid = [d["id"] for d in r.json().get("data", []) if not any(x in d["id"] for x in ["whisper", "guard", "tts"])]
        if not valid:
            bot.edit_message_text("Нет моделей.", m.chat.id, tmp.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for mid in valid[:10]: markup.add(types.InlineKeyboardButton(mid, callback_data=f"model_{mid}"))
        bot.edit_message_text("Выбери модель:", m.chat.id, tmp.message_id, reply_markup=markup)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("model_"))
def set_model(call):
    if not is_allowed(call.message): return
    update_user(call.message.chat.id, model=call.data.replace("model_", ""))
    bot.answer_callback_query(call.id, f"Модель: {call.data.replace('model_', '')}")
    bot.edit_message_text(f"Теперь я работаю на модели: {call.data.replace('model_', '')}", call.message.chat.id, call.message.message_id)

@bot.message_handler(func=lambda message: message.text == "Профиль")
def profile_btn(m): cmd_profile(m)

@bot.message_handler(func=lambda message: message.text in ["Очистить историю", "Очистить чат"])
def clear_btn(m): cmd_clear(m)

@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    if not is_allowed(m): return
    if not OPENROUTER_KEY:
        bot.reply_to(m, "Нет ключа OPENROUTER_KEY.")
        return
    tmp = bot.reply_to(m, "Смотрю на фото...")
    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        base64_image = base64.b64encode(bot.download_file(file_info.file_path)).decode('utf-8')
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
        task_text = None
        for model in ["google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-nano-12b-v2-vl:free"]:
            payload = {"model": model, "messages": [{"role": "user", "content": [{"type": "text", "text": "Выпиши текст с картинки."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}]}
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=40)
                if r.status_code == 200:
                    task_text = r.json()["choices"][0]["message"]["content"]
                    break
            except: pass
        if not task_text:
            bot.edit_message_text("Модели заняты.", m.chat.id, tmp.message_id)
            return
        bot.edit_message_text("Распознал. Решаю...", m.chat.id, tmp.message_id)
        prompt = f"Найди ошибку в решении:\n{task_text}" if find_error_mode.get(m.chat.id) else f"Реши пошагово:\n{task_text}"
        ans = ask_groq(m.chat.id, prompt)
        for i in range(0, len(ans), 4000):
            if i == 0: bot.edit_message_text(ans[i:i+4000], m.chat.id, tmp.message_id)
            else: bot.send_message(m.chat.id, ans[i:i+4000])
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(func=lambda message: message.text.startswith("Найди ошибку:"))
def toggle_error_mode(m):
    if not is_allowed(m): return
    current = find_error_mode.get(m.chat.id, False)
    find_error_mode[m.chat.id] = not current
    status = "ВКЛ" if not current else "ВЫКЛ"
    bot.reply_to(m, f"Режим: {status}", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not is_allowed(m): return
    file_name = m.document.file_name
    ext = file_name.split('.')[-1].lower()
    file_info = bot.get_file(m.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    user_files[m.chat.id] = {"name": file_name, "data": downloaded, "ext": ext}
    if ext in ["pdf", "txt"]:
        tmp = bot.reply_to(m, "Читаю...")
        try:
            text = ""
            if ext == "pdf":
                for page in PdfReader(io.BytesIO(downloaded)).pages: text += page.extract_text() or ""
            else:
                text = downloaded.decode('utf-8', errors='ignore')
            if not text.strip():
                bot.edit_message_text("Файл пустой.", m.chat.id, tmp.message_id)
                return
            ans = ask_groq(m.chat.id, f"Ответь по тексту (или сделай выжимку):\n\n{text[:4000]}")
            bot.edit_message_text(ans[:4000], m.chat.id, tmp.message_id)
            del user_files[m.chat.id]
        except Exception as e:
            bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)
        return
    markup = types.InlineKeyboardMarkup()
    if ext == "pdf": markup.add(types.InlineKeyboardButton("В DOCX", callback_data="conv_pdf_docx"))
    elif ext in ["jpg", "jpeg", "png", "webp"]: markup.add(types.InlineKeyboardButton("В PDF", callback_data="conv_img_pdf"))
    elif ext == "csv": markup.add(types.InlineKeyboardButton("В Excel", callback_data="conv_csv_xlsx"))
    elif ext in ["md", "markdown"]: markup.add(types.InlineKeyboardButton("В PDF", callback_data="conv_md_pdf"))
    elif ext in ["mp3", "ogg", "wav", "m4a"]: markup.add(types.InlineKeyboardButton("В текст", callback_data="conv_audio_txt"))
    else:
        bot.reply_to(m, f"Формат .{ext} не знаю.")
        return
    bot.reply_to(m, f"Файл {file_name}. Че делаем?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("conv_"))
def process_conversion(call):
    if not is_allowed(call.message): return
    chat_id = call.message.chat.id
    if chat_id not in user_files:
        bot.answer_callback_query(call.id, "Файл проебался.")
        return
    fd = user_files[chat_id]
    tmp = bot.send_message(chat_id, "Конвертирую...")
    try:
        if call.data == "conv_pdf_docx":
            docx_path = f"/tmp/{fd['name']}.docx"
            with open("/tmp/temp.pdf", "wb") as f: f.write(fd["data"])
            cv = pdf2docx.Converter("/tmp/temp.pdf")
            cv.convert(docx_path)
            cv.close()
            with open(docx_path, "rb") as f: bot.send_document(chat_id, f, caption="Держи DOCX.")
            os.remove(docx_path)
        elif call.data == "conv_img_pdf":
            img = Image.open(io.BytesIO(fd["data"]))
            pdf_bytes = io.BytesIO()
            img.convert('RGB').save(pdf_bytes, format='PDF')
            pdf_bytes.name = "image.pdf"
            bot.send_document(chat_id, pdf_bytes, caption="Держи PDF.")
        elif call.data == "conv_csv_xlsx":
            df = pd.read_csv(io.BytesIO(fd["data"]))
            xlsx_bytes = io.BytesIO()
            df.to_excel(xlsx_bytes, index=False)
            xlsx_bytes.name = "data.xlsx"
            bot.send_document(chat_id, xlsx_bytes, caption="Держи Excel.")
        elif call.data == "conv_md_pdf":
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)
            pdf.multi_cell(0, 10, fd["data"].decode("utf-8"))
            pdf_bytes = io.BytesIO(pdf.output(dest='S').encode('latin-1'))
            pdf_bytes.name = "markdown.pdf"
            bot.send_document(chat_id, pdf_bytes, caption="Держи PDF.")
        elif call.data == "conv_audio_txt":
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {GROQ_KEY}"}, files={"file": (fd["name"], fd["data"])}, data={"model": "whisper-large-v3-turbo"}, timeout=60)
            r.raise_for_status()
            txt_bytes = io.BytesIO(r.json().get("text", "Пусто.").encode('utf-8'))
            txt_bytes.name = "transcript.txt"
            bot.send_document(chat_id, txt_bytes, caption="Держи текст.")
        bot.delete_message(chat_id, tmp.message_id)
        del user_files[chat_id]
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", chat_id, tmp.message_id)

@bot.message_handler(content_types=["text"])
def on_text(m):
    if not is_allowed(m): return
    if m.text.startswith("Роль:"): return role_menu(m)
    if m.text.startswith("Найди ошибку:"): return toggle_error_mode(m)
    if m.text == "Сменить модель": return model_menu(m)
    if m.text == "Профиль": return profile_btn(m)
    if m.text in ["Очистить историю", "Очистить чат"]: return clear_btn(m)
    if m.text == "Поиск": return search_web(m)
    
    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = ask_groq(m.chat.id, m.text)
        for i in range(0, len(ans), 4000):
            if i == 0: bot.edit_message_text(ans[i:i+4000], m.chat.id, tmp.message_id)
            else: bot.send_message(m.chat.id, ans[i:i+4000])
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

if __name__ == "__main__":
    t = threading.Thread(target=run_health, daemon=True)
    t.start()
    bot.infinity_polling()
