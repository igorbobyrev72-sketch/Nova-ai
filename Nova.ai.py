import os
import threading
import requests
import telebot
import io
import re
import concurrent.futures
import platform
import subprocess

import pandas as pd
import pdf2docx
import img2pdf
from PIL import Image
from fpdf import FPDF

from telebot import types
from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer
)

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
deep_mode_enabled = {}
user_files = {}

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
    deep_status = "ВКЛ" if deep_mode_enabled.get(chat_id, False) else "ВЫКЛ"
    
    btn1 = types.KeyboardButton(f"Deep: {deep_status}")
    btn2 = types.KeyboardButton("Сменить модель")
    btn3 = types.KeyboardButton("Очистить историю")
    
    markup.add(btn1, btn2)
    markup.add(btn3)
    return markup

def is_allowed(m):
    if ADMIN_ID != 0 and m.chat.id != ADMIN_ID:
        bot.reply_to(m, "Иди нахуй, доступ закрыт.")
        return False
    return True

def call_groq_raw(model, text, sys_prompt="Ты полезный помощник."):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    data = {
        "model": model, 
        "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text}],
        "temperature": 0.7
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Ошибка: {e}"

def deep_ask(user_id, text):
    models_to_use = ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
    responses = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(call_groq_raw, model, text) for model in models_to_use]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if not res.startswith("Ошибка"):
                responses.append(res)
                
    if not responses:
        return "Все модели обосрались."
        
    combined = "\n\n---\n\n".join(responses)
    judge_prompt = f"Вопрос: {text}\n\nОтветы ИИ:\n{combined}\n\nВыбери лучший ответ, исправь ошибки и выдай ОДИН идеальный вариант."
    return call_groq_raw("llama-3.3-70b-versatile", judge_prompt, "Ты судья. Выдай лучший ответ.")

@bot.message_handler(commands=["start"])
def start(m):
    if not is_allowed(m): return
    user_histories[m.chat.id] = []
    deep_mode_enabled[m.chat.id] = False
    bot.reply_to(m, "Здарова. Кидай файлы для конвертации или пиши задачу. Deep-режим делает 3 запроса.", reply_markup=get_main_keyboard(m.chat.id))

@bot.message_handler(func=lambda message: message.text.startswith("Deep:"))
def toggle_deep(m):
    if not is_allowed(m): return
    current = deep_mode_enabled.get(m.chat.id, False)
    deep_mode_enabled[m.chat.id] = not current
    status = "ВКЛ" if not current else "ВЫКЛ"
    bot.reply_to(m, f"Deep: {status}", reply_markup=get_main_keyboard(m.chat.id))

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
        
    bot.reply_to(m, f"Файл *{file_name}*. Че делаем?", reply_markup=markup, parse_mode="Markdown")

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
    
    if deep_mode_enabled.get(m.chat.id, False):
        tmp = bot.reply_to(m, "Deep-анализ (3 модели)...")
        try:
            ans = deep_ask(m.chat.id, m.text)
            for i in range(0, len(ans), 4000):
                chunk = ans[i:i+4000]
                if i == 0:
                    bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
                else:
                    bot.send_message(m.chat.id, chunk)
        except Exception as e:
            bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)
        return

    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = call_groq_raw(MODEL, m.text)
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
