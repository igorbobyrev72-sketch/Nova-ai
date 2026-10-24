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
import csv
import re
from PyPDF2 import PdfReader
import pandas as pd
import pdf2docx
from PIL import Image
from fpdf import FPDF
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from telebot import types
from http.server import BaseHTTPRequestHandler, HTTPServer

# =========================================================
# НАСТРОЙКИ
# =========================================================

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

# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            role TEXT DEFAULT 'Обычный',
            model TEXT,
            requests INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            text TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS core_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            task_text TEXT,
            agent_type TEXT,
            result TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)

    conn.commit()
    conn.close()

init_db()

def get_user(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "SELECT role, model, requests FROM users WHERE chat_id = ?",
        (chat_id,)
    )

    row = c.fetchone()

    if not row:
        c.execute(
            "INSERT INTO users (chat_id, model) VALUES (?, ?)",
            (chat_id, MODEL)
        )
        conn.commit()
        row = ("Обычный", MODEL, 0)

    conn.close()
    return row

def update_user(chat_id, role=None, model=None):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    if role:
        c.execute(
            "UPDATE users SET role = ? WHERE chat_id = ?",
            (role, chat_id)
        )

    if model:
        c.execute(
            "UPDATE users SET model = ? WHERE chat_id = ?",
            (model, chat_id)
        )

    conn.commit()
    conn.close()

def inc_requests(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "UPDATE users SET requests = requests + 1 WHERE chat_id = ?",
        (chat_id,)
    )

    conn.commit()
    conn.close()

def get_history(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute("""
        SELECT role, content
        FROM history
        WHERE chat_id = ?
        ORDER BY id DESC
        LIMIT 10
    """, (chat_id,))

    rows = c.fetchall()
    conn.close()

    return [
        {"role": row[0], "content": row[1]}
        for row in reversed(rows)
    ]

def add_history(chat_id, role, content):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "INSERT INTO history (chat_id, role, content) VALUES (?, ?, ?)",
        (chat_id, role, content)
    )

    conn.commit()
    conn.close()

def clear_history(chat_id):
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))

    conn.commit()
    conn.close()

# =========================================================
# HEALTH CHECK ДЛЯ RENDER
# =========================================================

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

# =========================================================
# ОСНОВНАЯ ЛОГИКА
# =========================================================

def is_allowed(m):
    if ADMIN_ID != 0 and m.chat.id != ADMIN_ID:
        bot.reply_to(m, "Доступ закрыт.")
        return False
    return True

def get_main_keyboard(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    role, _, _ = get_user(chat_id)

    err_mode = (
        "ВКЛ" if find_error_mode.get(chat_id, False)
        else "ВЫКЛ"
    )

    markup.add(
        types.KeyboardButton(f"Роль: {role}"),
        types.KeyboardButton(f"Найди ошибку: {err_mode}")
    )

    markup.add(
        types.KeyboardButton("Сменить модель"),
        types.KeyboardButton("Профиль")
    )

    markup.add(
        types.KeyboardButton("Очистить историю"),
        types.KeyboardButton("Поиск")
    )

    return markup

def get_main_inline_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "💬 ИИ-Чат", callback_data="menu_chat"
        ),
        types.InlineKeyboardButton(
            "🧠 Режимы", callback_data="menu_modes"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "📜 История", callback_data="menu_history"
        ),
        types.InlineKeyboardButton(
            "👤 Профиль", callback_data="menu_profile"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "⚙️ Настройки", callback_data="menu_settings"
        ),
        types.InlineKeyboardButton(
            "❓ Помощь", callback_data="menu_help"
        )
    )

    return markup

def send_smart_response(chat_id, text, reply_to_message_id=None):
    code_blocks = re.findall(
        r"```(?:python)?\s*\n(.*?)```",
        text,
        re.DOTALL
    )

    if code_blocks and len(code_blocks[0]) > 500:
        clean_text = re.sub(
            r"```(?:python)?\s*\n(.*?)```",
            "[Код отправлен файлом ниже]",
            text,
            flags=re.DOTALL
        )

        bot.send_message(
            chat_id,
            clean_text[:4000],
            reply_to_message_id=reply_to_message_id
        )

        doc = io.BytesIO("\n\n".join(code_blocks).encode("utf-8"))
        doc.name = "nova_code.py"

        bot.send_document(
            chat_id,
            doc,
            caption="📁 Код Nova SI"
        )

    elif len(text) > 4000:
        doc = io.BytesIO(text.encode("utf-8"))
        doc.name = "nova_text.txt"

        bot.send_document(
            chat_id,
            doc,
            caption="📁 Текст слишком длинный для одного сообщения."
        )

    else:
        bot.send_message(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id
        )

def ask_groq(user_id, text, system_prompt=None):
    history = get_history(user_id)
    
    # Обрезаем историю, чтобы не было 413
    if len(history) > 6:
        history = history[-6:]
        
    history.append({"role": "user", "content": text[:3000]})

    role, _, _ = get_user(user_id)

    if system_prompt:
        sys_prompt = system_prompt
    elif role == "Дебаггер":
        sys_prompt = (
            "Ты — сеньор-разработчик. Найди ошибку, "
            "объясни её и предложи исправление."
        )
    elif role == "Эксперт":
        sys_prompt = "Ты — строгий эксперт. Отвечай точно и без воды."
    elif role == "Юморист":
        sys_prompt = "Ты — юморист. Отвечай с уместными шутками."
    elif role == "Программист":
        sys_prompt = "Ты — опытный программист. Пиши рабочий код."
    elif role == "Дружелюбный":
        sys_prompt = "Ты — дружелюбный и полезный помощник."
    else:
        sys_prompt = (
            "Ты — Nova SI, полезный ИИ-помощник. "
            "Отвечай преимущественно по-русски. "
            "Для кода используй Markdown-блоки с языком."
        )

    messages = [{"role": "system", "content": sys_prompt}] + history
    _, current_model, _ = get_user(user_id)

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": current_model,
        "messages": messages
    }

    response = requests.post(
        url,
        headers=headers,
        json=data,
        timeout=60
    )

    response.raise_for_status()

    answer = response.json()["choices"][0]["message"]["content"]

    if not answer:
        raise ValueError("Модель вернула пустой ответ.")

    add_history(user_id, "user", text[:3000])
    add_history(user_id, "assistant", answer)
    inc_requests(user_id)

    return answer

# =========================================================
# NOVA FILE STUDIO — ГЕНЕРАТОР ФАЙЛОВ
# =========================================================

@bot.message_handler(commands=["file"])
def file_menu(m):
    if not is_allowed(m):
        return

    markup = types.InlineKeyboardMarkup(row_width=1)

    markup.add(
        types.InlineKeyboardButton(
            "📊 Excel — XLSX",
            callback_data="filetype_xlsx"
        ),
        types.InlineKeyboardButton(
            "📽 Презентация — PPTX",
            callback_data="filetype_pptx"
        ),
        types.InlineKeyboardButton(
            "📝 Текстовый документ — TXT",
            callback_data="filetype_txt"
        )
    )

    bot.reply_to(
        m,
        "📁 Nova File Studio\n\nВыбери формат файла:",
        reply_markup=markup
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("filetype_")
)
def choose_file_type(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    file_type = call.data.replace("filetype_", "", 1)

    if file_type not in ("xlsx", "pptx", "txt"):
        bot.answer_callback_query(call.id, "Неизвестный формат.")
        return

    bot.answer_callback_query(call.id)

    prompt_msg = bot.send_message(
        call.message.chat.id,
        "✍️ Опиши, какой файл создать.\n\n"
        "Примеры:\n"
        "• Таблица расходов за месяц\n"
        "• Презентация об искусственном интеллекте\n"
        "• Документ о космосе"
    )

    bot.register_next_step_handler(
        prompt_msg,
        process_file_request,
        file_type
    )

def clean_model_output(text):
    """Убирает обрамляющие Markdown-блоки."""
    text = text.strip()
    text = re.sub(
        r"^\s*```[a-zA-Z0-9_-]*\s*\n?",
        "",
        text
    )
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()

def process_file_request(m, file_type):
    if not is_allowed(m):
        return

    if not m.text or not m.text.strip():
        bot.reply_to(m, "Отправь текстовое описание задачи.")
        return

    if len(m.text) > 2000:
        bot.reply_to(
            m,
            "Описание слишком длинное. Сократи его до 2000 символов."
        )
        return

    tmp = bot.reply_to(m, "⚙️ Nova создаёт файл...")

    try:
        # -------------------------------------------------
        # TXT — ТЕКСТОВЫЙ ДОКУМЕНТ
        # -------------------------------------------------
        if file_type == "txt":
            content = ask_groq(
                m.chat.id,
                (
                    "Создай полноценный текстовый документ по запросу. "
                    "Используй понятные заголовки, абзацы и списки. "
                    "Не добавляй комментарии о том, что ты сделал. "
                    "Верни только содержимое документа.\n\n"
                    + m.text
                ),
                system_prompt=(
                    "Ты — профессиональный редактор документов. "
                    "Пиши структурированно и грамотно."
                )
            )

            output = io.BytesIO(content.encode("utf-8"))
            output.name = "nova_document.txt"

        # -------------------------------------------------
        # XLSX — EXCEL С ОФОРМЛЕНИЕМ
        # -------------------------------------------------
        elif file_type == "xlsx":
            content = ask_groq(
                m.chat.id,
                (
                    "Создай полезную таблицу по запросу пользователя. "
                    "Верни ТОЛЬКО CSV с разделителем точка с запятой (;). "
                    "Первая строка — названия столбцов. "
                    "Каждая строка должна содержать одинаковое число "
                    "столбцов. Не используй точку с запятой внутри ячеек. "
                    "Не добавляй Markdown, пояснения или блоки кода.\n\n"
                    + m.text
                ),
                system_prompt=(
                    "Ты создаёшь структурированные таблицы для Excel. "
                    "Возвращай только CSV."
                )
            )

            content = clean_model_output(content)

            rows = list(
                csv.reader(
                    io.StringIO(content),
                    delimiter=";"
                )
            )

            rows = [
                [cell.strip() for cell in row]
                for row in rows
                if any(cell.strip() for cell in row)
            ]

            if not rows:
                raise ValueError(
                    "ИИ не создал таблицу. Уточни свой запрос."
                )

            # Выравниваем количество столбцов
            max_columns = max(len(row) for row in rows)

            rows = [
                row + [""] * (max_columns - len(row))
                for row in rows
            ]

            wb = Workbook()
            ws = wb.active
            ws.title = "Nova SI"

            for row in rows:
                ws.append(row)

            # Цвета оформления
            header_fill = PatternFill(
                "solid",
                fgColor="2457A7"
            )

            alternate_fill = PatternFill(
                "solid",
                fgColor="EAF2FF"
            )

            thin_border = Border(
                bottom=Side(
                    style="thin",
                    color="D6E0F0"
                )
            )

            # Заголовки
            for cell in ws[1]:
                cell.font = Font(
                    name="Arial",
                    bold=True,
                    color="FFFFFF",
                    size=11
                )

                cell.fill = header_fill

                cell.alignment = Alignment(
                    horizontal="center",
                    vertical="center",
                    wrap_text=True
                )

            ws.row_dimensions[1].height = 30

            # Основные ячейки
            for row_index in range(2, ws.max_row + 1):
                for cell in ws[row_index]:
                    cell.font = Font(
                        name="Arial",
                        size=10,
                        color="263449"
                    )

                    cell.alignment = Alignment(
                        vertical="center",
                        wrap_text=True
                    )

                    cell.border = thin_border

                    if row_index % 2 == 0:
                        cell.fill = alternate_fill

            # Настройки листа
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            ws.sheet_view.showGridLines = False

            # Подбираем ширину колонок
            for column in ws.columns:
                letter = column[0].column_letter

                max_length = max(
                    len(str(cell.value or ""))
                    for cell in column
                )

                ws.column_dimensions[letter].width = min(
                    max(max_length + 3, 14),
                    35
                )

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            output.name = "nova_table.xlsx"

        # -------------------------------------------------
        # PPTX — ПРЕЗЕНТАЦИЯ С ДИЗАЙНОМ
        # -------------------------------------------------
        elif file_type == "pptx":
            content = ask_groq(
                m.chat.id,
                (
                    "Создай презентацию ровно на 6 слайдов. "
                    "Верни текст в следующем формате:\n\n"
                    "СЛАЙД: Заголовок слайда\n"
                    "- Первый тезис\n"
                    "- Второй тезис\n"
                    "- Третий тезис\n\n"
                    "Каждый слайд обязательно начинай с новой строки "
                    "'СЛАЙД:'. Добавляй 3–5 коротких и содержательных "
                    "тезисов на каждый слайд. Не добавляй вступление "
                    "перед первым слайдом и заключение после последнего. "
                    "Избегай длинных абзацев.\n\n"
                    "Тема презентации:\n" + m.text
                ),
                system_prompt=(
                    "Ты — профессиональный автор презентаций. "
                    "Создавай логичные, понятные и информативные слайды."
                )
            )

            content = clean_model_output(content)

            # Разбиваем ответ модели на слайды
            sections = re.split(
                r"(?im)^\s*СЛАЙД\s*:\s*",
                content
            )

            slides_data = []

            for section in sections:
                lines = [
                    line.strip()
                    for line in section.splitlines()
                    if line.strip()
                ]

                if not lines:
                    continue

                title = lines[0].lstrip("#").strip()
                title = re.sub(r"^\d+[\.\):\-]\s*", "", title)

                bullets = []

                for line in lines[1:]:
                    cleaned = re.sub(
                        r"^\s*(?:[-*•]|\d+[.)])\s*",
                        "",
                        line
                    ).strip()

                    if cleaned:
                        bullets.append(cleaned)

                if not bullets and len(lines) > 1:
                    bullets = lines[1:]

                slides_data.append(
                    (title[:120], bullets[:5])
                )

            # Если модель не соблюла формат, пробуем
            # извлечь содержимое более свободно
            if not slides_data:
                lines = [
                    line.strip()
                    for line in content.splitlines()
                    if line.strip()
                ]

                if lines:
                    slides_data.append(
                        (
                            lines[0][:120],
                            [
                                line.lstrip("-• ").strip()
                                for line in lines[1:20]
                                if line.strip()
                            ][:5]
                        )
                    )

            if not slides_data:
                raise ValueError(
                    "ИИ не вернул содержимое презентации. "
                    "Попробуй другой запрос."
                )

            # Ограничиваем число слайдов для лёгкой работы
            slides_data = slides_data[:8]

            prs = Presentation()
            prs.slide_width = Inches(12)
            prs.slide_height = Inches(6.75)

            # Палитра презентации
            NAVY = RGBColor(18, 29, 54)
            BLUE = RGBColor(48, 110, 230)
            WHITE = RGBColor(255, 255, 255)
            LIGHT = RGBColor(229, 237, 250)
            MUTED = RGBColor(170, 190, 220)

            for index, (title, bullets) in enumerate(
                slides_data,
                start=1
            ):
                # Пустой слайд для полного контроля дизайна
                slide = prs.slides.add_slide(
                    prs.slide_layouts[6]
                )

                # Фон
                background = slide.background.fill
                background.solid()
                background.fore_color.rgb = NAVY

                # Верхняя цветная полоса
                accent = slide.shapes.add_shape(
                    MSO_SHAPE.RECTANGLE,
                    Inches(0),
                    Inches(0),
                    Inches(12),
                    Inches(0.12)
                )

                accent.fill.solid()
                accent.fill.fore_color.rgb = BLUE
                accent.line.fill.background()

                # Номер слайда
                number_box = slide.shapes.add_textbox(
                    Inches(10.75),
                    Inches(0.35),
                    Inches(0.7),
                    Inches(0.35)
                )

                number_tf = number_box.text_frame
                number_tf.clear()

                number_p = number_tf.paragraphs[0]
                number_p.text = (
                    f"{index:02d} / {len(slides_data):02d}"
                )
                number_p.alignment = PP_ALIGN.RIGHT
                number_p.font.size = Pt(10)
                number_p.font.color.rgb = MUTED

                # Заголовок
                title_box = slide.shapes.add_textbox(
                    Inches(0.7),
                    Inches(0.55),
                    Inches(10.1),
                    Inches(1.15)
                )

                title_tf = title_box.text_frame
                title_tf.clear()
                title_tf.word_wrap = True
                title_tf.vertical_anchor = MSO_ANCHOR.MIDDLE

                title_p = title_tf.paragraphs[0]
                title_p.text = title or f"Слайд {index}"
                title_p.font.name = "Arial"
                title_p.font.size = Pt(
                    30 if len(title) < 45 else 24
                )
                title_p.font.bold = True
                title_p.font.color.rgb = WHITE

                # Карточка для тезисов
                card = slide.shapes.add_shape(
                    MSO_SHAPE.ROUNDED_RECTANGLE,
                    Inches(0.7),
                    Inches(1.95),
                    Inches(10.6),
                    Inches(3.95)
                )

                card.fill.solid()
                card.fill.fore_color.rgb = RGBColor(28, 43, 73)
                card.line.color.rgb = RGBColor(48, 67, 103)

                # Внутренний текст карточки
                body_box = slide.shapes.add_textbox(
                    Inches(1.05),
                    Inches(2.18),
                    Inches(9.9),
                    Inches(3.45)
                )

                body_tf = body_box.text_frame
                body_tf.clear()
                body_tf.word_wrap = True
                body_tf.margin_left = Inches(0.08)
                body_tf.margin_right = Inches(0.08)
                body_tf.margin_top = Inches(0.08)
                body_tf.margin_bottom = Inches(0.08)
                body_tf.vertical_anchor = MSO_ANCHOR.MIDDLE

                if not bullets:
                    bullets = ["Основная информация по теме."]

                # Не перегружаем слайды большим количеством текста
                bullets = bullets[:5]

                for bullet_index, bullet in enumerate(bullets):
                    if bullet_index == 0:
                        paragraph = body_tf.paragraphs[0]
                    else:
                        paragraph = body_tf.add_paragraph()

                    paragraph.text = f"•  {bullet}"
                    paragraph.level = 0
                    paragraph.font.name = "Arial"
                    paragraph.font.size = Pt(
                        19 if len(bullets) <= 4 else 16
                    )
                    paragraph.font.color.rgb = LIGHT
                    paragraph.space_after = Pt(15)
                    paragraph.line_spacing = 1.1

                # Нижняя подпись
                footer = slide.shapes.add_textbox(
                    Inches(0.75),
                    Inches(6.15),
                    Inches(10.5),
                    Inches(0.3)
                )

                footer_tf = footer.text_frame
                footer_tf.clear()

                footer_p = footer_tf.paragraphs[0]
                footer_p.text = "NOVA SI  •  PRESENTATION"
                footer_p.font.name = "Arial"
                footer_p.font.size = Pt(9)
                footer_p.font.color.rgb = MUTED

            output = io.BytesIO()
            prs.save(output)
            output.seek(0)
            output.name = "nova_presentation.pptx"

        else:
            raise ValueError("Неизвестный формат файла.")

        # Отправляем документ
        bot.send_document(
            m.chat.id,
            output,
            caption="✅ Файл создан — Nova SI"
        )

        bot.delete_message(m.chat.id, tmp.message_id)

    except Exception as e:
        error_text = f"❌ Не получилось создать файл: {str(e)[:800]}"

        try:
            bot.edit_message_text(
                error_text,
                m.chat.id,
                tmp.message_id
            )
        except Exception:
            bot.send_message(m.chat.id, error_text)

# =========================================================
# NOVA CORE AUTO
# =========================================================

AGENT_PROMPTS = {
    "planner": (
        "Ты — Planner (Планировщик) Nova Core. "
        "Разбей задачу на чёткие пошаговые инструкции. "
        "Используй нумерованный список. Без лишней воды."
    ),
    "builder": (
        "Ты — Builder (Разработчик). "
        "Напиши решение по плану. Для программирования "
        "предпочитай чистый и понятный Python-код."
    ),
    "checker": (
        "Ты — Checker (Проверяющий). "
        "Проверь решение на баги, логические ошибки "
        "и проблемы безопасности. Будь внимателен."
    )
}

@bot.message_handler(commands=["auto"])
def cmd_auto(m):
    if not is_allowed(m):
        return

    msg = bot.reply_to(
        m,
        "🚀 Nova Core Auto активирован.\n\n"
        "Напиши задачу. Агенты выполнят её по цепочке:\n"
        "План → Решение → Проверка."
    )

    bot.register_next_step_handler(msg, process_auto_task)

def process_auto_task(m):
    if not is_allowed(m):
        return

    task_text = m.text

    if not task_text or not task_text.strip():
        bot.reply_to(m, "Задача пустая.")
        return

    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "INSERT INTO core_tasks (chat_id, task_text) VALUES (?, ?)",
        (m.chat.id, task_text)
    )

    task_id = c.lastrowid

    conn.commit()
    conn.close()

    bot.reply_to(
        m,
        f"Задача: {task_text[:100]}\n\n"
        "Запускаю цепочку агентов."
    )

    def safe_groq(prompt, sys_prompt):
        safe_prompt = prompt[:3000]

        for attempt in range(3):
            try:
                return ask_groq(
                    m.chat.id,
                    safe_prompt,
                    system_prompt=sys_prompt
                )

            except Exception as e:
                if "429" in str(e):
                    bot.send_message(
                        m.chat.id,
                        "⏳ Лимит Groq. Жду 30 секунд."
                    )
                    time.sleep(30)

                elif "413" in str(e):
                    safe_prompt = safe_prompt[:1500]

                else:
                    raise

        raise Exception("Не удалось получить ответ от Groq.")

    try:
        msg1 = bot.send_message(
            m.chat.id,
            "⏳ Шаг 1: Planner составляет план..."
        )

        plan = safe_groq(
            task_text,
            AGENT_PROMPTS["planner"]
        )

        bot.edit_message_text(
            "✅ План готов. Следующий шаг...",
            m.chat.id,
            msg1.message_id
        )

        time.sleep(3)

        msg2 = bot.send_message(
            m.chat.id,
            "⏳ Шаг 2: Builder готовит решение..."
        )

        builder_input = (
            f"Задача:\n{task_text}\n\n"
            f"План:\n{plan[:2000]}\n\n"
            "Подготовь решение по этому плану."
        )

        code_result = safe_groq(
            builder_input,
            AGENT_PROMPTS["builder"]
        )

        bot.edit_message_text(
            "✅ Решение готово. Проверяю...",
            m.chat.id,
            msg2.message_id
        )

        time.sleep(3)

        msg3 = bot.send_message(
            m.chat.id,
            "⏳ Шаг 3: Checker проверяет решение..."
        )

        checker_input = (
            f"Задача:\n{task_text}\n\n"
            f"Решение:\n{code_result[:2000]}\n\n"
            "Проверь решение и укажи возможные ошибки."
        )

        review = safe_groq(
            checker_input,
            AGENT_PROMPTS["checker"]
        )

        final_result = (
            f"ПЛАН:\n{plan}\n\n"
            f"РЕШЕНИЕ:\n{code_result}\n\n"
            f"ПРОВЕРКА:\n{review}"
        )

        conn = sqlite3.connect("bot.db")
        c = conn.cursor()

        c.execute("""
            UPDATE core_tasks
            SET agent_type = 'auto',
                result = ?,
                status = 'done'
            WHERE id = ?
        """, (final_result, task_id))

        conn.commit()
        conn.close()

        bot.edit_message_text(
            "✅ Проверка завершена!",
            m.chat.id,
            msg3.message_id
        )

        final_text = (
            f"🔥 РЕЗУЛЬТАТ NOVA CORE AUTO\n\n"
            f"📋 ПЛАН:\n{plan}\n\n"
            f"💻 РЕШЕНИЕ:\n{code_result}\n\n"
            f"🔍 ПРОВЕРКА:\n{review}"
        )

        for i in range(0, len(final_text), 4000):
            bot.send_message(
                m.chat.id,
                final_text[i:i + 4000]
            )

    except Exception as e:
        bot.send_message(
            m.chat.id,
            f"❌ Ошибка в цепочке: {str(e)[:800]}"
        )

# =========================================================
# NOVA CORE — ОТДЕЛЬНЫЕ АГЕНТЫ
# =========================================================

@bot.message_handler(commands=["core"])
def cmd_core(m):
    if not is_allowed(m):
        return

    msg = bot.reply_to(
        m,
        "🧠 Nova Core активирован.\n\nНапиши задачу:"
    )

    bot.register_next_step_handler(msg, process_core_task)

def process_core_task(m):
    if not is_allowed(m):
        return

    task_text = m.text

    if not task_text or not task_text.strip():
        bot.reply_to(m, "Задача пустая.")
        return

    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "INSERT INTO core_tasks (chat_id, task_text) VALUES (?, ?)",
        (m.chat.id, task_text)
    )

    task_id = c.lastrowid

    conn.commit()
    conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)

    markup.add(
        types.InlineKeyboardButton(
            "📋 Planner",
            callback_data=f"core_planner_{task_id}"
        ),
        types.InlineKeyboardButton(
            "💻 Builder",
            callback_data=f"core_builder_{task_id}"
        ),
        types.InlineKeyboardButton(
            "🔍 Checker",
            callback_data=f"core_checker_{task_id}"
        )
    )

    bot.reply_to(
        m,
        f"Задача: {task_text[:100]}\n\nВыбери агента:",
        reply_markup=markup
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("core_")
)
def handle_core_callback(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    parts = call.data.split("_")

    if len(parts) != 3:
        bot.answer_callback_query(call.id, "Некорректная задача.")
        return

    agent_type = parts[1]

    try:
        task_id = int(parts[2])
    except ValueError:
        bot.answer_callback_query(call.id, "Некорректный ID.")
        return

    if agent_type not in AGENT_PROMPTS:
        bot.answer_callback_query(call.id, "Неизвестный агент.")
        return

    bot.answer_callback_query(
        call.id,
        f"Агент {agent_type} работает..."
    )

    conn = sqlite3.connect("bot.db")
    c = conn.cursor()

    c.execute(
        "SELECT task_text FROM core_tasks WHERE id = ? AND chat_id = ?",
        (task_id, call.message.chat.id)
    )

    row = c.fetchone()
    conn.close()

    if not row:
        bot.send_message(call.message.chat.id, "Задача не найдена.")
        return

    task_text = row[0]

    try:
        bot.edit_message_text(
            f"⏳ Агент {agent_type.capitalize()} думает...",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception:
        pass

    try:
        answer = ask_groq(
            call.message.chat.id,
            task_text[:3000],
            system_prompt=AGENT_PROMPTS[agent_type]
        )

        conn = sqlite3.connect("bot.db")
        c = conn.cursor()

        c.execute("""
            UPDATE core_tasks
            SET agent_type = ?,
                result = ?,
                status = 'done'
            WHERE id = ? AND chat_id = ?
        """, (
            agent_type,
            answer,
            task_id,
            call.message.chat.id
        ))

        conn.commit()
        conn.close()

        full_text = (
            f"🤖 Ответ от {agent_type.capitalize()}:\n\n"
            f"{answer}"
        )

        chunks = [
            full_text[i:i + 4000]
            for i in range(0, len(full_text), 4000)
        ]

        for index, chunk in enumerate(chunks):
            if index == 0:
                try:
                    bot.edit_message_text(
                        chunk,
                        call.message.chat.id,
                        call.message.message_id
                    )
                except Exception:
                    bot.send_message(call.message.chat.id, chunk)
            else:
                bot.send_message(call.message.chat.id, chunk)

    except Exception as e:
        bot.send_message(
            call.message.chat.id,
            f"❌ Ошибка агента: {str(e)[:800]}"
        )

# =========================================================
# КОМАНДЫ
# =========================================================

@bot.message_handler(commands=["start"])
def start(m):
    if not is_allowed(m):
        return

    get_user(m.chat.id)

    welcome_text = (
        f"👋 Привет, {m.from_user.first_name}!\n\n"
        "Я — Nova SI. Выбери раздел ниже:"
    )

    bot.send_message(
        m.chat.id,
        welcome_text,
        reply_markup=get_main_inline_menu()
    )

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add(
        types.KeyboardButton("Очистить чат"),
        types.KeyboardButton("Сменить модель")
    )

    bot.send_message(
        m.chat.id,
        "Кнопки быстрого доступа:",
        reply_markup=markup
    )

@bot.message_handler(commands=["help"])
def cmd_help(m):
    if not is_allowed(m):
        return

    help_text = (
        "🤖 Возможности Nova SI:\n\n"
        "💬 ИИ-чат — отправь сообщение.\n"
        "📎 Файлы — PDF, TXT и изображения.\n"
        "🖼 Картинки — /image твой_запрос.\n"
        "🎙 Голос — отправь голосовое сообщение.\n"
        "🌐 Поиск — кнопка «Поиск».\n"
        "🚀 Nova Core Auto — /auto.\n"
        "🧠 Отдельные агенты — /core.\n"
        "📁 Генератор файлов — /file.\n\n"
        "Команды:\n"
        "/start — меню\n"
        "/clear — очистить историю\n"
        "/profile — профиль\n"
        "/help — помощь"
    )

    bot.send_message(m.chat.id, help_text)

@bot.message_handler(commands=["clear"])
def cmd_clear(m):
    if not is_allowed(m):
        return

    clear_history(m.chat.id)

    bot.reply_to(
        m,
        "🧹 История очищена.",
        reply_markup=get_main_keyboard(m.chat.id)
    )

@bot.message_handler(commands=["profile"])
def cmd_profile(m):
    if not is_allowed(m):
        return

    role, model, requests_count = get_user(m.chat.id)

    text = (
        "👤 Профиль:\n\n"
        f"Роль: {role}\n"
        f"Модель: {model}\n"
        f"Запросов: {requests_count}"
    )

    bot.reply_to(
        m,
        text,
        reply_markup=get_main_keyboard(m.chat.id)
    )

# =========================================================
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# =========================================================

@bot.message_handler(commands=["image"])
def gen_image(m):
    if not is_allowed(m):
        return

    prompt = m.text.replace("/image", "", 1).strip()

    if not prompt:
        bot.reply_to(m, "Напиши описание изображения после /image.")
        return

    tmp = bot.reply_to(m, "🎨 Создаю изображение...")

    try:
        url = (
            "https://image.pollinations.ai/prompt/"
            f"{requests.utils.quote(prompt)}"
            "?width=1024&height=1024&nologo=true"
        )

        response = requests.get(url, timeout=60)
        response.raise_for_status()

        if not response.content:
            raise ValueError("Сервис вернул пустое изображение.")

        bot.delete_message(m.chat.id, tmp.message_id)

        photo = io.BytesIO(response.content)
        photo.name = "nova_image.jpg"

        bot.send_photo(
            m.chat.id,
            photo,
            caption=f"🎨 Запрос: {prompt[:800]}"
        )

    except Exception as e:
        bot.edit_message_text(
            f"Ошибка создания изображения: {str(e)[:700]}",
            m.chat.id,
            tmp.message_id
        )

# =========================================================
# ГОЛОСОВЫЕ СООБЩЕНИЯ
# =========================================================

@bot.message_handler(content_types=["voice"])
def handle_voice(m):
    if not is_allowed(m):
        return

    tmp = bot.reply_to(m, "🎙 Слушаю...")

    audio_file = f"/tmp/ans_{m.chat.id}.mp3"

    try:
        file_info = bot.get_file(m.voice.file_id)
        downloaded = bot.download_file(file_info.file_path)

        url = "https://api.groq.com/openai/v1/audio/transcriptions"

        headers = {
            "Authorization": f"Bearer {GROQ_KEY}"
        }

        files = {
            "file": ("voice.ogg", downloaded)
        }

        data = {
            "model": "whisper-large-v3-turbo"
        }

        response = requests.post(
            url,
            headers=headers,
            files=files,
            data=data,
            timeout=60
        )

        response.raise_for_status()

        text = response.json().get("text", "").strip()

        if not text:
            raise ValueError("Не удалось распознать речь.")

        bot.edit_message_text(
            f"Ты сказал: {text[:1000]}\n\nДумаю...",
            m.chat.id,
            tmp.message_id
        )

        answer = ask_groq(m.chat.id, text)

        async def tts():
            communicate = edge_tts.Communicate(
                answer[:4000],
                "ru-RU-DmitryNeural"
            )
            await communicate.save(audio_file)

        asyncio.run(tts())

        with open(audio_file, "rb") as audio:
            bot.send_voice(
                m.chat.id,
                audio,
                caption=answer[:900]
            )

        os.remove(audio_file)

    except Exception as e:
        bot.edit_message_text(
            f"Ошибка обработки голоса: {str(e)[:700]}",
            m.chat.id,
            tmp.message_id
        )

    finally:
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except OSError:
                pass

# =========================================================
# ПОИСК В ИНТЕРНЕТЕ
# =========================================================

@bot.message_handler(
    func=lambda message: (
        bool(message.text) and message.text == "Поиск"
    )
)
def search_web(m):
    if not is_allowed(m):
        return

    msg = bot.reply_to(m, "Введи поисковый запрос:")
    bot.register_next_step_handler(msg, process_search)

def process_search(m):
    if not is_allowed(m):
        return

    if not m.text or not m.text.strip():
        bot.reply_to(m, "Поисковый запрос пустой.")
        return

    tmp = bot.reply_to(m, "🌐 Ищу в интернете...")

    instances = [
        "https://searx.tiekoetter.com",
        "https://searx.be",
        "https://searxng.site",
        "https://priv.au"
    ]

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    results = None

    for base_url in instances:
        try:
            response = requests.get(
                f"{base_url}/search",
                params={
                    "q": m.text,
                    "format": "json"
                },
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()

                if data.get("results"):
                    results = data["results"][:3]
                    break

        except Exception:
            continue

    if not results:
        bot.edit_message_text(
            "Поисковые сервисы сейчас недоступны. Попробуй позже.",
            m.chat.id,
            tmp.message_id
        )
        return

    context = ""

    for result in results:
        context += (
            f"Источник: {result.get('title', '')}\n"
            f"Ссылка: {result.get('url', '')}\n"
            f"{result.get('content', '')}\n\n"
        )

    try:
        answer = ask_groq(
            m.chat.id,
            (
                f"Ответь на запрос: {m.text}\n\n"
                "Используй информацию из результатов поиска. "
                "Если данных недостаточно, прямо скажи об этом.\n\n"
                f"{context[:3000]}"
            )
        )

        bot.edit_message_text(
            answer[:4000],
            m.chat.id,
            tmp.message_id
        )

    except Exception as e:
        bot.edit_message_text(
            f"Ошибка ИИ: {str(e)[:700]}",
            m.chat.id,
            tmp.message_id
        )

# =========================================================
# ИНЛАЙН-МЕНЮ
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("menu_")
)
def handle_menu(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    bot.answer_callback_query(call.id)
    data = call.data

    if data == "menu_chat":
        bot.edit_message_text(
            "💬 ИИ-Чат активирован. Просто отправь сообщение боту.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

    elif data == "menu_modes":
        markup = types.InlineKeyboardMarkup(row_width=2)

        for role in [
            "Обычный",
            "Дебаггер",
            "Эксперт",
            "Юморист",
            "Программист",
            "Дружелюбный"
        ]:
            markup.add(
                types.InlineKeyboardButton(
                    role,
                    callback_data=f"role_{role}"
                )
            )

        markup.add(
            types.InlineKeyboardButton(
                "🔙 Назад",
                callback_data="menu_back"
            )
        )

        bot.edit_message_text(
            "🧠 Выбери режим:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )

    elif data == "menu_history":
        history = get_history(call.message.chat.id)

        if history:
            text = "📜 Последние сообщения сохранены в истории."
        else:
            text = "📜 История пока пустая."

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

    elif data == "menu_profile":
        role, model, reqs = get_user(call.message.chat.id)

        text = (
            "👤 Профиль\n\n"
            f"Роль: {role}\n"
            f"Модель: {model}\n"
            f"Запросов: {reqs}"
        )

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

    elif data == "menu_settings":
        bot.edit_message_text(
            "⚙️ Настройки\n\nДля выбора модели используй кнопку "
            "«Сменить модель».",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

    elif data == "menu_help":
        help_text = (
            "🤖 Nova SI\n\n"
            "/start — главное меню\n"
            "/help — помощь\n"
            "/clear — очистить историю\n"
            "/profile — профиль\n"
            "/file — генератор файлов\n"
            "/image — создание изображения\n"
            "/core — отдельные агенты\n"
            "/auto — цепочка агентов"
        )

        bot.edit_message_text(
            help_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

    elif data == "menu_back":
        bot.edit_message_text(
            "Выбери раздел:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_main_inline_menu()
        )

# =========================================================
# ВЫБОР РОЛИ
# =========================================================

@bot.message_handler(
    func=lambda message: (
        bool(message.text) and message.text.startswith("Роль:")
    )
)
def role_menu(m):
    if not is_allowed(m):
        return

    markup = types.InlineKeyboardMarkup()

    for role in [
        "Обычный",
        "Дебаггер",
        "Эксперт",
        "Юморист",
        "Программист",
        "Дружелюбный"
    ]:
        markup.add(
            types.InlineKeyboardButton(
                role,
                callback_data=f"role_{role}"
            )
        )

    bot.reply_to(m, "Выбери роль:", reply_markup=markup)

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("role_")
)
def set_role(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    role = call.data.replace("role_", "", 1)

    allowed_roles = {
        "Обычный",
        "Дебаггер",
        "Эксперт",
        "Юморист",
        "Программист",
        "Дружелюбный"
    }

    if role not in allowed_roles:
        bot.answer_callback_query(call.id, "Неизвестная роль.")
        return

    update_user(call.message.chat.id, role=role)

    bot.answer_callback_query(call.id, f"Роль: {role}")

    try:
        bot.edit_message_text(
            f"Теперь моя роль: {role}",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception:
        pass

    bot.send_message(
        call.message.chat.id,
        "Меню обновлено.",
        reply_markup=get_main_keyboard(call.message.chat.id)
    )

# =========================================================
# СМЕНА МОДЕЛИ
# =========================================================

@bot.message_handler(
    func=lambda message: (
        bool(message.text) and message.text == "Сменить модель"
    )
)
def model_menu(m):
    if not is_allowed(m):
        return

    tmp = bot.reply_to(m, "Смотрю доступные модели...")

    try:
        response = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}"
            },
            timeout=20
        )

        response.raise_for_status()

        valid_models = [
            item["id"]
            for item in response.json().get("data", [])
            if not any(
                word in item["id"].lower()
                for word in ["whisper", "guard", "tts"]
            )
        ]

        if not valid_models:
            bot.edit_message_text(
                "Доступных моделей нет.",
                m.chat.id,
                tmp.message_id
            )
            return

        markup = types.InlineKeyboardMarkup()

        for model_id in valid_models[:10]:
            markup.add(
                types.InlineKeyboardButton(
                    model_id,
                    callback_data=f"model_{model_id}"
                )
            )

        bot.edit_message_text(
            "Выбери модель:",
            m.chat.id,
            tmp.message_id,
            reply_markup=markup
        )

    except Exception as e:
        bot.edit_message_text(
            f"Ошибка получения моделей: {str(e)[:700]}",
            m.chat.id,
            tmp.message_id
        )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("model_")
)
def set_model(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    model_id = call.data.replace("model_", "", 1)

    update_user(call.message.chat.id, model=model_id)

    bot.answer_callback_query(call.id, "Модель обновлена.")

    bot.edit_message_text(
        f"Теперь я работаю на модели:\n{model_id}",
        call.message.chat.id,
        call.message.message_id
    )

@bot.message_handler(
    func=lambda message: (
        bool(message.text) and message.text == "Профиль"
    )
)
def profile_btn(m):
    cmd_profile(m)

@bot.message_handler(
    func=lambda message: (
        bool(message.text)
        and message.text in ["Очистить историю", "Очистить чат"]
    )
)
def clear_btn(m):
    cmd_clear(m)

# =========================================================
# ОБРАБОТКА ФОТОГРАФИЙ
# =========================================================

@bot.message_handler(content_types=["photo"])
def handle_photo(m):
    if not is_allowed(m):
        return

    if not OPENROUTER_KEY:
        bot.reply_to(m, "Не задан ключ OPENROUTER_KEY.")
        return

    tmp = bot.reply_to(m, "🖼 Анализирую изображение...")

    try:
        file_info = bot.get_file(m.photo[-1].file_id)

        image_bytes = bot.download_file(file_info.file_path)

        base64_image = base64.b64encode(
            image_bytes
        ).decode("utf-8")

        url = "https://openrouter.ai/api/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json"
        }

        recognized_text = None

        models = [
            "google/gemma-4-26b-a4b-it:free",
            "nvidia/nemotron-nano-12b-v2-vl:free"
        ]

        for model_id in models:
            payload = {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Распознай текст на изображении. "
                                    "Если текста нет, опиши изображение."
                                )
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": (
                                        "data:image/jpeg;base64,"
                                        + base64_image
                                    )
                                }
                            }
                        ]
                    }
                ]
            }

            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=40
                )

                if response.status_code == 200:
                    recognized_text = (
                        response.json()["choices"][0]
                        ["message"]["content"]
                    )

                    if recognized_text:
                        break

            except Exception:
                continue

        if not recognized_text:
            bot.edit_message_text(
                "Модели распознавания сейчас недоступны.",
                m.chat.id,
                tmp.message_id
            )
            return

        bot.edit_message_text(
            "Распознал изображение. Готовлю ответ...",
            m.chat.id,
            tmp.message_id
        )

        if find_error_mode.get(m.chat.id, False):
            prompt = (
                "Найди ошибки в решении, если они есть. "
                "Объясни исправления:\n\n"
                + recognized_text[:2500]
            )
        else:
            prompt = (
                "Реши или объясни содержимое изображения пошагово:\n\n"
                + recognized_text[:2500]
            )

        answer = ask_groq(m.chat.id, prompt)

        bot.delete_message(m.chat.id, tmp.message_id)

        send_smart_response(m.chat.id, answer)

    except Exception as e:
        try:
            bot.edit_message_text(
                f"Ошибка анализа изображения: {str(e)[:700]}",
                m.chat.id,
                tmp.message_id
            )
        except Exception:
            pass

# =========================================================
# РЕЖИМ «НАЙДИ ОШИБКУ»
# =========================================================

@bot.message_handler(
    func=lambda message: (
        bool(message.text)
        and message.text.startswith("Найди ошибку:")
    )
)
def toggle_error_mode(m):
    if not is_allowed(m):
        return

    current = find_error_mode.get(m.chat.id, False)
    find_error_mode[m.chat.id] = not current

    status = "ВКЛ" if not current else "ВЫКЛ"

    bot.reply_to(
        m,
        f"Режим поиска ошибок: {status}",
        reply_markup=get_main_keyboard(m.chat.id)
    )

# =========================================================
# ПРИЁМ И ОБРАБОТКА ДОКУМЕНТОВ
# =========================================================

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not is_allowed(m):
        return

    file_name = m.document.file_name or "uploaded_file"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

    try:
        file_info = bot.get_file(m.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(m, f"Не удалось скачать файл: {str(e)[:500]}")
        return

    user_files[m.chat.id] = {
        "name": file_name,
        "data": downloaded,
        "ext": ext
    }

    if ext in ["pdf", "txt"]:
        tmp = bot.reply_to(m, "📄 Читаю документ...")

        try:
            if ext == "pdf":
                pages = PdfReader(io.BytesIO(downloaded)).pages
                text = "\n".join(
                    page.extract_text() or ""
                    for page in pages
                )
            else:
                text = downloaded.decode(
                    "utf-8",
                    errors="ignore"
                )

            if not text.strip():
                bot.edit_message_text(
                    "В документе не удалось найти текст.",
                    m.chat.id,
                    tmp.message_id
                )
                return

            answer = ask_groq(
                m.chat.id,
                (
                    "Проанализируй документ. Дай краткую выжимку "
                    "и ответь по его содержанию:\n\n"
                    + text[:3000]
                )
            )

            bot.edit_message_text(
                answer[:4000],
                m.chat.id,
                tmp.message_id
            )

            user_files.pop(m.chat.id, None)

        except Exception as e:
            bot.edit_message_text(
                f"Ошибка чтения документа: {str(e)[:700]}",
                m.chat.id,
                tmp.message_id
            )

        return

    markup = types.InlineKeyboardMarkup()

    if ext == "pdf":
        markup.add(
            types.InlineKeyboardButton(
                "В DOCX",
                callback_data="conv_pdf_docx"
            )
        )

    elif ext in ["jpg", "jpeg", "png", "webp"]:
        markup.add(
            types.InlineKeyboardButton(
                "В PDF",
                callback_data="conv_img_pdf"
            )
        )

    elif ext == "csv":
        markup.add(
            types.InlineKeyboardButton(
                "В Excel",
                callback_data="conv_csv_xlsx"
            )
        )

    elif ext in ["md", "markdown"]:
        markup.add(
            types.InlineKeyboardButton(
                "В PDF",
                callback_data="conv_md_pdf"
            )
        )

    elif ext in ["mp3", "ogg", "wav", "m4a"]:
        markup.add(
            types.InlineKeyboardButton(
                "В текст",
                callback_data="conv_audio_txt"
            )
        )

    else:
        user_files.pop(m.chat.id, None)

        bot.reply_to(
            m,
            f"Формат .{ext or '?'} пока не поддерживается."
        )
        return

    bot.reply_to(
        m,
        f"Файл «{file_name}» получен. Что делаем?",
        reply_markup=markup
    )

# =========================================================
# КОНВЕРТАЦИЯ ФАЙЛОВ
# =========================================================

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("conv_")
)
def process_conversion(call):
    if not is_allowed(call.message):
        bot.answer_callback_query(call.id, "Нет доступа.")
        return

    chat_id = call.message.chat.id

    if chat_id not in user_files:
        bot.answer_callback_query(
            call.id,
            "Файл больше недоступен. Отправь его заново."
        )
        return

    bot.answer_callback_query(call.id)

    file_data = user_files[chat_id]
    tmp = bot.send_message(chat_id, "⚙️ Конвертирую файл...")

    temp_paths = []

    try:
        # PDF -> DOCX
        if call.data == "conv_pdf_docx":
            source_path = "/tmp/nova_source.pdf"
            output_path = "/tmp/nova_converted.docx"

            temp_paths.extend([source_path, output_path])

            with open(source_path, "wb") as file:
                file.write(file_data["data"])

            converter = pdf2docx.Converter(source_path)

            try:
                converter.convert(output_path)
            finally:
                converter.close()

            with open(output_path, "rb") as file:
                document = io.BytesIO(file.read())

            document.name = "nova_converted.docx"

            bot.send_document(
                chat_id,
                document,
                caption="✅ Готово: DOCX"
            )

        # IMAGE -> PDF
        elif call.data == "conv_img_pdf":
            img = Image.open(
                io.BytesIO(file_data["data"])
            )

            pdf_bytes = io.BytesIO()

            img.convert("RGB").save(
                pdf_bytes,
                format="PDF"
            )

            pdf_bytes.seek(0)
            pdf_bytes.name = "nova_image.pdf"

            bot.send_document(
                chat_id,
                pdf_bytes,
                caption="✅ Готово: PDF"
            )

        # CSV -> XLSX
        elif call.data == "conv_csv_xlsx":
            df = pd.read_csv(
                io.BytesIO(file_data["data"])
            )

            xlsx_bytes = io.BytesIO()

            df.to_excel(
                xlsx_bytes,
                index=False
            )

            xlsx_bytes.seek(0)
            xlsx_bytes.name = "nova_data.xlsx"

            bot.send_document(
                chat_id,
                xlsx_bytes,
                caption="✅ Готово: Excel"
            )

        # MARKDOWN -> PDF
        elif call.data == "conv_md_pdf":
            bot.send_message(
                chat_id,
                "⚠️ Простой PDF-конвертер может не поддерживать "
                "кириллицу. Если появятся ошибки шрифтов, "
                "нужно будет добавить Unicode-шрифт."
            )

            pdf = FPDF()
            pdf.add_page()
            pdf.set_font("Arial", size=12)

            text = file_data["data"].decode(
                "utf-8",
                errors="replace"
            )

            pdf.multi_cell(0, 8, text)

            pdf_bytes = io.BytesIO(
                pdf.output(dest="S").encode("latin-1")
            )

            pdf_bytes.name = "nova_markdown.pdf"

            bot.send_document(
                chat_id,
                pdf_bytes,
                caption="PDF создан."
            )

        # AUDIO -> TXT
        elif call.data == "conv_audio_txt":
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={
                    "Authorization": f"Bearer {GROQ_KEY}"
                },
                files={
                    "file": (
                        file_data["name"],
                        file_data["data"]
                    )
                },
                data={
                    "model": "whisper-large-v3-turbo"
                },
                timeout=60
            )

            response.raise_for_status()

            transcript = response.json().get("text", "Пусто.")

            txt_bytes = io.BytesIO(
                transcript.encode("utf-8")
            )

            txt_bytes.name = "nova_transcript.txt"

            bot.send_document(
                chat_id,
                txt_bytes,
                caption="✅ Готово: текстовая расшифровка"
            )

        bot.delete_message(chat_id, tmp.message_id)
        user_files.pop(chat_id, None)

    except Exception as e:
        try:
            bot.edit_message_text(
                f"Ошибка конвертации: {str(e)[:700]}",
                chat_id,
                tmp.message_id
            )
        except Exception:
            pass

    finally:
        for path in temp_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

# =========================================================
# ОБРАБОТКА ОБЫЧНОГО ТЕКСТА
# =========================================================

@bot.message_handler(content_types=["text"])
def on_text(m):
    if not is_allowed(m):
        return

    if not m.text:
        return

    if m.text.startswith("Роль:"):
        return role_menu(m)

    if m.text.startswith("Найди ошибку:"):
        return toggle_error_mode(m)

    if m.text == "Сменить модель":
        return model_menu(m)

    if m.text == "Профиль":
        return profile_btn(m)

    if m.text in ["Очистить историю", "Очистить чат"]:
        return clear_btn(m)

    if m.text == "Поиск":
        return search_web(m)

    if len(m.text) > 3000:
        bot.reply_to(
            m,
            "❌ Слишком длинный запрос. Сократи его до 3000 символов."
        )
        return

    tmp = bot.reply_to(m, "💭 Думаю...")

    try:
        answer = ask_groq(m.chat.id, m.text)

        try:
            bot.delete_message(m.chat.id, tmp.message_id)
        except Exception:
            pass

        send_smart_response(m.chat.id, answer)

    except Exception as e:
        error = str(e)

        if "413" in error:
            error_text = (
                "❌ Запрос или ответ слишком большой. "
                "Попробуй более короткий запрос."
            )
        elif "429" in error:
            error_text = (
                "⏳ Достигнут лимит запросов к модели. "
                "Попробуй позже."
            )
        else:
            error_text = f"Ошибка ИИ: {error[:700]}"

        try:
            bot.edit_message_text(
                error_text,
                m.chat.id,
                tmp.message_id
            )
        except Exception:
            bot.send_message(m.chat.id, error_text)

# =========================================================
# ЗАПУСК БОТА
# =========================================================

if __name__ == "__main__":
    health_thread = threading.Thread(
        target=run_health,
        daemon=True
    )

    health_thread.start()

    print("Nova SI запущен.")

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
        )
