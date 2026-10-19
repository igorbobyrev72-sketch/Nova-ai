import os
import threading
import requests
import telebot
import io
import re
import base64

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
MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

if not BOT_TOKEN or not GROQ_KEY:
    msg = "Ошибка: не заданы переменные"
    raise SystemExit(msg)

bot = telebot.TeleBot(BOT_TOKEN)
user_histories = {}
user_models = {}
user_roles = {}
user_files = {}
find_error_mode = {}

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
    err_mode = "ВКЛ" if find_error_mode.get(chat_id, False) else "ВЫКЛ"
    
    btn1 = types.KeyboardButton(f"Роль: {role}")
    btn2 = types.KeyboardButton(f"Найди ошибку: {err_mode}")
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

def ask_groq(user_id, text, system_prompt=None):
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": text})
    
    if len(history) > 10:
        history = history[-10:]
        
    role = user_roles.get(user_id, "Обычный")
    
    if system_prompt:
        sys_prompt = system_prompt
    elif role == "Дебаггер":
        sys_prompt = "Ты — сеньор-разработчик. Найди ошибку, объясни и выдай исправленный код."
    else:
        sys_prompt = "Ты полезный помощник. Отвечай без Markdown (без звездочек)."

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

@bot.message_handler(commands=["start"])
def start(m):
    if not is_allowed(m): return
    user_histories[m.chat.id] = []
    user_models[m.chat.id] = MODEL
    user_roles[m.chat.id] = "Обычный"
    bot.reply_to(m, "Здарова. Кидай фото задания или файл. Если надо просто текст - пиши.", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(commands=["txt"])
def gen_txt(m):
    if not is_allowed(m): return
    prompt = m.text.replace("/txt", "").strip()
    if not prompt:
        bot.reply_to(m, "А тему где, хули?")
        return

    tmp = bot.reply_to(m, "Готовлю файл...")
    try:
        sys_p = "Ты — умный помощник для учебы. Напиши структуру презентации, конспект или реферат. Разбивай на слайды или пункты. Не используй Markdown!"
        ans = ask_groq(m.chat.id, f"Сделай учебный материал на тему: {prompt}", system_prompt=sys_p)
        
        file_data = ans.encode('utf-8')
        doc = io.BytesIO(file_data)
        doc.name = "study_material.txt"
        
        bot.delete_message(m.chat.id, tmp.message_id)
        bot.send_document(m.chat.id, doc, caption="Держи файл.")
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    if not is_allowed(m): return
    if not OPENROUTER_KEY:
        bot.reply_to(m, "Нет ключа OPENROUTER_KEY, не могу распознать фото.")
        return
        
    tmp = bot.reply_to(m, "Смотрю на фото...")
    
    try:
        file_info = bot.get_file(m.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        base64_image = base64.b64encode(downloaded_file).decode('utf-8')
        
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "google/gemma-4-26b-a4b-it:free",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Выпиши весь текст с этой картинки. Если это задача — выпиши условие."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ]
        }
        
        r_vis = requests.post(url, headers=headers, json=payload, timeout=60)
        r_vis.raise_for_status()
        task_text = r_vis.json()["choices"][0]["message"]["content"]
        
        bot.edit_message_text(f"Распознал:\n{task_text[:300]}...\n\nРешаю...", m.chat.id, tmp.message_id)
        
        if find_error_mode.get(m.chat.id, False):
            prompt = f"Вот условие задачи и мое решение:\n{task_text}\n\nНайди ошибку в решении, если она есть. Объясни, где я неправ."
        else:
            prompt = f"Реши эту задачу пошагово, объясняя каждый шаг:\n{task_text}"
            
        ans = ask_groq(m.chat.id, prompt)
        
        for i in range(0, len(ans), 4000):
            chunk = ans[i:i+4000]
            if i == 0:
                bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
            else:
                bot.send_message(m.chat.id, chunk)
                
    except Exception as e:
        bot.edit_message_text(f"Ошибка при обработке фото: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(func=lambda message: message.text.startswith("Найди ошибку:"))
def toggle_error_mode(m):
    if not is_allowed(m): return
    current = find_error_mode.get(m.chat.id, False)
    find_error_mode[m.chat.id] = not current
    status = "ВКЛ" if not current else "ВЫКЛ"
    bot.reply_to(m, f"Режим 'Найди ошибку': {status}", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(func=lambda message: message.text == "Очистить историю")
def clear_history(m):
    if not is_allowed(m): return
    user_histories[m.chat.id] = []
    bot.reply_to(m, "История очищена.", reply_markup=get_main_keyboard(m.chat.id))

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
    bot.edit_message_text(f"Теперь моя роль: {chosen_role}", call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Меню обновлено.", reply_markup=get_main_keyboard(call.message.chat.id))

@bot.message_handler(func=lambda message: message.text == "Сменить модель")
def model_menu(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Смотрю модели...")
    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        models = r.json().get("data", [])
        
        valid_models = []
        for m_data in models:
            m_id = m_data["id"]
            if any(x in m_id for x in ["whisper", "guard", "tts"]):
                continue
            valid_models.append(m_id)
            
        if not valid_models:
            bot.edit_message_text("Groq не дал моделей.", m.chat.id, tmp.message_id)
            return
            
        valid_models = valid_models[:10]
        markup = types.InlineKeyboardMarkup()
        for m_id in valid_models:
            markup.add(types.InlineKeyboardButton(m_id, callback_data=f"model_{m_id}"))
            
        bot.edit_message_text("Выбери модель:", m.chat.id, tmp.message_id, reply_markup=markup)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("model_"))
def set_model(call):
    if not is_allowed(call.message): return
    chosen_model = call.data.replace("model_", "")
    user_models[call.message.chat.id] = chosen_model
    bot.answer_callback_query(call.id, f"Модель: {chosen_model}")
    bot.edit_message_text(f"Теперь я работаю на модели: {chosen_model}", call.message.chat.id, call.message.message_id)

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not is_allowed(m): return
    
    file_name = m.document.file_name
    ext = file_name.split('.')[-1].lower()
    file_info = bot.get_file(m.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    
    user_files[m.chat.id] = {"name": file_name, "data": downloaded, "ext": ext}
    
    markup = types.InlineKeyboardMarkup()
    if ext == "pdf":
        markup.add(types.InlineKeyboardButton("В DOCX", callback_data="conv_pdf_docx"))
    elif ext in ["jpg", "jpeg", "png", "webp"]:
        markup.add(types.InlineKeyboardButton("В PDF", callback_data="conv_img_pdf"))
    elif ext == "csv":
        markup.add(types.InlineKeyboardButton("В Excel (XLSX)", callback_data="conv_csv_xlsx"))
    elif ext in ["md", "markdown"]:
        markup.add(types.InlineKeyboardButton("В PDF", callback_data="conv_md_pdf"))
    elif ext in ["mp3", "ogg", "wav", "m4a"]:
        markup.add(types.InlineKeyboardButton("В текст (Groq)", callback_data="conv_audio_txt"))
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
            with open("/tmp/temp.pdf", "wb") as f:
                f.write(fd["data"])
            cv = pdf2docx.Converter("/tmp/temp.pdf")
            cv.convert(docx_path)
            cv.close()
            with open(docx_path, "rb") as f:
                bot.send_document(chat_id, f, caption="Держи DOCX.")
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
            md_text = fd["data"].decode("utf-8")
            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)
            pdf.multi_cell(0, 10, md_text)
            pdf_bytes = io.BytesIO(pdf.output(dest='S').encode('latin-1'))
            pdf_bytes.name = "markdown.pdf"
            bot.send_document(chat_id, pdf_bytes, caption="Держи PDF.")
            
        elif call.data == "conv_audio_txt":
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {GROQ_KEY}"}
            files = {"file": (fd["name"], fd["data"])}
            data = {"model": "whisper-large-v3-turbo"}
            
            r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
            r.raise_for_status()
            text = r.json().get("text", "Пусто.")
            
            txt_bytes = io.BytesIO(text.encode('utf-8'))
            txt_bytes.name = "transcript.txt"
            bot.send_document(chat_id, txt_bytes, caption="Держи текст.")
            
        bot.delete_message(chat_id, tmp.message_id)
        del user_files[chat_id]
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", chat_id, tmp.message_id)

@bot.message_handler(content_types=["text"])
def on_text(m):
    if not is_allowed(m): return
    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = ask_groq(m.chat.id, m.text)
        for i in range(0, len(ans), 4000):
            chunk = ans[i:i+4000]
            if i == 0:
                bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
            else:
                bot.send_message(m.chat.id, chunk)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

if __name__ == "__main__":
    t = threading.Thread(target=run_health, daemon=True)
    t.start()
    bot.infinity_polling()
