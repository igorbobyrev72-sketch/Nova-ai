import os
import threading
import requests
import telebot
import io
import re

from telebot import types
from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer
)
from duckduckgo_search import DDGS
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

if not BOT_TOKEN or not GROQ_KEY:
    msg = "Ошибка: не заданы переменные"
    raise SystemExit(msg)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
user_histories = {}
user_models = {}
user_roles = {}
send_code_files = {}

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass

def run_health():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), Health)
    server.serve_forever()

def get_main_keyboard(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    role = user_roles.get(chat_id, "Обычный")
    file_status = "ВКЛ" if send_code_files.get(chat_id, False) else "ВЫКЛ"
    btn1 = types.KeyboardButton(f"Роль: {role}")
    btn2 = types.KeyboardButton(f"Код файлами: {file_status}")
    btn3 = types.KeyboardButton("Сменить модель")
    btn4 = types.KeyboardButton("Очистить историю")
    markup.add(btn1, btn2)
    markup.add(btn3, btn4)
    return markup

def is_allowed(m):
    if ADMIN_ID != 0 and m.chat.id != ADMIN_ID:
        bot.reply_to(m, "Иди нахуй, доступ закрыт.")
        return False
    return True

def search_and_fetch(query):
    try:
        results = DDGS().text(query, max_results=3, region='ru-ru')
        context = ""
        for res in results:
            url = res.get('href')
            title = res.get('title')
            if not url: continue
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                page = requests.get(url, headers=headers, timeout=5)
                soup = BeautifulSoup(page.text, 'html.parser')
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(separator=' ', strip=True)
                context += f"--- {title} ({url}) ---\n{text[:3000]}\n\n"
            except Exception:
                pass
        return context if context else None
    except Exception as e:
        print(f"Поиск сдох: {e}")
        return None

def ask_groq(user_id, text, context=""):
    history = user_histories.get(user_id, [])
    
    if context:
        prompt = f"Запрос: {text}\n\nКонтекст из интернета:\n{context}\n\nНа основе этого напиши ответ или рабочий код. Без воды."
        history.append({"role": "user", "content": prompt})
    else:
        history.append({"role": "user", "content": text})
        
    if len(history) > 10:
        history = history[-10:]
        
    role = user_roles.get(user_id, "Обычный")
    if role == "Дебаггер":
        sys_prompt = "Ты — сеньор-разработчик. Найди ошибку, объясни и выдай исправленный код."
    else:
        sys_prompt = (
            "Ты полезный помощник. Отвечай кратко и по делу. "
            "Если пользователь просит создать файл или спрашивает, умеешь ли ты это делать, "
            "отвечай, что для создания .txt файла нужно использовать команду /txt [твой текст или тема]. "
            "Не пиши код в блоке ```, если тебя об этом не просят напрямую!"
        )

    messages = [{"role": "system", "content": sys_prompt}] + history
    current_model = user_models.get(user_id, MODEL)
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    data = {"model": current_model, "messages": messages}
    
    r = requests.post(url, headers=headers, json=data, timeout=60)
    r.raise_for_status()
    ans = r.json()["choices"][0]["message"]["content"]
    
    history.append({"role": "assistant", "content": ans})
    user_histories[user_id] = history
    return ans

def extract_and_send_files(m, text):
    chat_id = m.chat.id
    pattern = r"```(\w+)?\n(.*?)```"
    matches = re.findall(pattern, text, re.DOTALL)
    if not matches:
        return False
    ext_map = {"python": "py", "javascript": "js", "html": "html", "css": "css", "json": "json", "go": "go", "rust": "rs"}
    sent = False
    for lang, code in matches:
        lang = lang.lower() if lang else "txt"
        ext = ext_map.get(lang, "txt")
        filename = f"code.{ext}"
        doc = io.BytesIO(code.strip().encode('utf-8'))
        doc.name = filename
        try:
            bot.send_document(chat_id, doc, caption=f"Файл: {filename}")
            sent = True
        except Exception as e:
            bot.send_message(chat_id, f"Не смог отправить файл: {e}")
    return sent

@bot.message_handler(commands=["start"])
def start(m):
    if not is_allowed(m): return
    user_histories[m.chat.id] = []
    user_models[m.chat.id] = MODEL
    send_code_files[m.chat.id] = False
    user_roles[m.chat.id] = "Обычный"
    bot.reply_to(m, "Здарова. Скинь задачу или юзай `/web` для поиска в инете.\n\nУмею делать `.txt` файлы через команду `/txt [тема или текст]`.", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(commands=["txt"])
def gen_txt(m):
    if not is_allowed(m): return
    prompt = m.text.replace("/txt", "").strip()
    if not prompt:
        bot.reply_to(m, "А текст или тема где, хули?")
        return

    tmp = bot.reply_to(m, "Пишу файл...")
    try:
        ans = ask_groq(m.chat.id, prompt)
        file_data = ans.encode('utf-8')
        doc = io.BytesIO(file_data)
        doc.name = "response.txt"
        
        bot.delete_message(m.chat.id, tmp.message_id)
        bot.send_document(m.chat.id, doc, caption="Держи файл.")
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(commands=["web"])
def web_search(m):
    if not is_allowed(m): return
    query = m.text.replace("/web", "").strip()
    if not query:
        bot.reply_to(m, "А запрос где?")
        return
    tmp = bot.reply_to(m, "Ищу...")
    
    context = search_and_fetch(query)
    if not context:
        bot.edit_message_text("Поисковик забанил запрос, сука. Попробуй позже или напиши без /web.", m.chat.id, tmp.message_id)
        return
    
    try:
        ans = ask_groq(m.chat.id, query, context)
    except Exception as e:
        bot.edit_message_text(f"Ошибка Groq: {e}", m.chat.id, tmp.message_id)
        return

    if send_code_files.get(m.chat.id, False):
        if extract_and_send_files(m, ans):
            bot.delete_message(m.chat.id, tmp.message_id)
            return

    for i in range(0, len(ans), 4000):
        chunk = ans[i:i+4000]
        if i == 0:
            try:
                bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
            except Exception:
                bot.send_message(m.chat.id, chunk, reply_markup=get_main_keyboard(m.chat.id))
        else:
            bot.send_message(m.chat.id, chunk, reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(func=lambda message: message.text.startswith("Роль:"))
def role_menu(m):
    if not is_allowed(m): return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Обычный", callback_data="role_Обычный"))
    markup.add(types.InlineKeyboardButton("Дебаггер", callback_data="role_Дебаггер"))
    bot.reply_to(m, "Выбери роль:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("role_"))
def set_role(call):
    if not is_allowed(call.message): return
    chosen_role = call.data.replace("role_", "")
    user_roles[call.message.chat.id] = chosen_role
    bot.answer_callback_query(call.id, f"Роль: {chosen_role}")
    bot.edit_message_text(f"Теперь моя роль: *{chosen_role}*", call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    bot.send_message(call.message.chat.id, "Меню обновлено.", reply_markup=get_main_keyboard(call.message.chat.id))

@bot.message_handler(func=lambda message: message.text.startswith("Код файлами:"))
def toggle_files(m):
    if not is_allowed(m): return
    current = send_code_files.get(m.chat.id, False)
    send_code_files[m.chat.id] = not current
    status = "ВКЛ" if not current else "ВЫКЛ"
    bot.reply_to(m, f"Отправка кода файлами: {status}", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(func=lambda message: message.text == "Очистить историю")
def clear_history(m):
    if not is_allowed(m): return
    user_histories[m.chat.id] = []
    bot.reply_to(m, "История очищена.", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(func=lambda message: message.text == "Сменить модель")
def model_menu(m):
    if not is_allowed(m): return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Llama 3.3 70B (Умная)", callback_data="model_llama-3.3-70b-versatile"))
    markup.add(types.InlineKeyboardButton("GPT-OSS 20B (Быстрая)", callback_data="model_openai/gpt-oss-20b"))
    markup.add(types.InlineKeyboardButton("Llama 3.1 8B (Слабая)", callback_data="model_llama-3.1-8b-instant"))
    bot.reply_to(m, "Выбери модель:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("model_"))
def set_model(call):
    if not is_allowed(call.message): return
    chosen_model = call.data.replace("model_", "")
    user_models[call.message.chat.id] = chosen_model
    bot.answer_callback_query(call.id, f"Модель: {chosen_model}")
    bot.edit_message_text(f"Теперь я работаю на модели: `{chosen_model}`", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.message_handler(content_types=["text"])
def on_text(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = ask_groq(m.chat.id, m.text)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)
        return
    if send_code_files.get(m.chat.id, False):
        if extract_and_send_files(m, ans):
            bot.delete_message(m.chat.id, tmp.message_id)
            return
    for i in range(0, len(ans), 4000):
        chunk = ans[i:i+4000]
        if i == 0:
            try:
                bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
            except Exception:
                bot.send_message(m.chat.id, chunk, reply_markup=get_main_keyboard(m.chat.id))
        else:
            bot.send_message(m.chat.id, chunk, reply_markup=get_main_keyboard(m.chat.id))

if __name__ == "__main__":
    t = threading.Thread(target=run_health, daemon=True)
    t.start()
    bot.infinity_polling()
