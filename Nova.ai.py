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
MODEL = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b"
)

if not BOT_TOKEN or not GROQ_KEY:
    msg = "Ошибка: не заданы переменные"
    raise SystemExit(msg)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="Markdown"
)

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

def ask_groq(text):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Ты помощник."},
            {"role": "user", "content": text}
        ]
    }
    r = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=60
    )
    r.raise_for_status()
    res = r.json()
    return res["choices"][0]["message"]["content"]

@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, "Скинь задачу.")

@bot.message_handler(content_types=["text"])
def on_text(m):
    tmp = bot.reply_to(m, "Думаю...")
    try:
        ans = ask_groq(m.text)
    except Exception as e:
        bot.edit_message_text(
            f"Ошибка: {e}",
            m.chat.id,
            tmp.message_id
        )
        return

    for i in range(0, len(ans), 4000):
        chunk = ans[i:i+4000]
        if i == 0:
            try:
                bot.edit_message_text(
                    chunk,
                    m.chat.id,
                    tmp.message_id
                )
            except Exception:
                bot.send_message(m.chat.id, chunk)
        else:
            bot.send_message(m.chat.id, chunk)

if __name__ == "__main__":
    t = threading.Thread(
        target=run_health,
        daemon=True
    )
    t.start()
    bot.infinity_polling()
