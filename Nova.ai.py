import os
import threading
import requests
import telebot

from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_KEY = os.environ.get("GROQ_KEY")
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

if not BOT_TOKEN or not GROQ_KEY:
    msg = "Ошибка: не заданы переменные"
    raise SystemExit(msg)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
user_histories = {}

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

def ask_groq(user_id, text):
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": text})
    
    if len(history) > 10:
        history = history[-10:]
        
    messages = [{"role": "system", "content": "Ты полезный помощник."}] + history
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    data = {"model": MODEL, "messages": messages}
    
    r = requests.post(url, headers=headers, json=data, timeout=60)
    r.raise_for_status()
    ans = r.json()["choices"][0]["message"]["content"]
    
    history.append({"role": "assistant", "content": ans})
    user_histories[user_id] = history
    return ans

@bot.message_handler(commands=["start"])
def start(m):
    user_histories[m.chat.id] = []
    bot.reply_to(m, "Привет. Скинь задачу, текст или .txt файл.")

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not m.document.file_name.endswith('.txt'):
        bot.reply_to(m, "Я работаю только с .txt файлами, хули.")
        return
        
    if m.document.file_size > 5000000:
        bot.reply_to(m, "Файл слишком жирный, сука. Дай поменьше.")
        return

    tmp = bot.reply_to(m, "Читаю файл...")
    try:
        file_info = bot.get_file(m.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        try:
            content = downloaded_file.decode('utf-8')
        except UnicodeDecodeError:
            content = downloaded_file.decode('cp1251')

        prompt = f"Проанализируй этот текст и сделай краткую выжимку:\n\n{content}"
        
        ans = ask_groq(m.chat.id, prompt)
        
        bot.delete_message(m.chat.id, tmp.message_id)
        for i in range(0, len(ans), 4000):
            chunk = ans[i:i+4000]
            bot.send_message(m.chat.id, chunk)
            
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)

@bot.message_handler(content_types=["text"])
def on_text(m):
    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = ask_groq(m.chat.id, m.text)
    except Exception as e:
        bot.edit_message_text(f"Ошибка: {e}", m.chat.id, tmp.message_id)
        return

    for i in range(0, len(ans), 4000):
        chunk = ans[i:i+4000]
        if i == 0:
            try:
                bot.edit_message_text(chunk, m.chat.id, tmp.message_id)
            except Exception:
                bot.send_message(m.chat.id, chunk)
        else:
            bot.send_message(m.chat.id, chunk)

if __name__ == "__main__":
    t = threading.Thread(target=run_health, daemon=True)
    t.start()
    bot.infinity_polling()
