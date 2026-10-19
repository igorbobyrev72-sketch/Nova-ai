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

def ask_groq(user_id, text, is_file=False, system_prompt=None):
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": text})
    
    if len(history) > 10:
        history = history[-10:]
        
    role = user_roles.get(user_id, "Обычный")
    
    if system_prompt:
        sys_prompt = system_prompt
    elif is_file:
        sys_prompt = "Ты — помощник для учебы. Напиши четко, без Markdown."
    elif role == "Дебаггер":
        sys_prompt = "Ты — сеньор-разработчик. Найди ошибку, объясни и выдай исправленный код."
    else:
        sys_prompt = "Ты полезный помощник. Отвечай без Markdown."

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

@bot.message_handler(content_types=['photo'])
def handle_photo(m):
    if not is_allowed(m): return
    if not OPENROUTER_KEY:
        bot.reply_to(m, "Нет ключа OPENROUTER_KEY, не могу распознать фото.")
        return
        
    tmp = bot.reply_to(m, "Смотрю на фото...")
    
    try:
        # Скачиваем фото
        file_info = bot.get_file(m.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        base64_image = base64.b64encode(downloaded_file).decode('utf-8')
        
        # Распознаем текст через бесплатную Vision-модель OpenRouter
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "qwen/qwen-2-vl-72b-instruct:free",
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
        
        bot.edit_message_text(f"Распознал:\n{task_text[:500]}...\n\nРешаю...", m.chat.id, tmp.message_id)
        
        # Теперь просим Groq решить
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

# ... (ОСТАЛЬНОЙ КОД: start, clear_history, role_menu, set_role, model_menu, set_model, txt, handle_doc, conv, on_text) ...
# Он остается точно таким же, как в прошлом сообщении, просто добавь эти функции выше.
