import time
import sqlite3
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import telegram.error
from openai import AsyncOpenAI
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL_ID = "nousresearch/hermes-4-70b"
DB_FILE = "bot_database.sqlite"

# ================= Database Initialization =================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY, 
                            language TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, 
                            user_id INTEGER, 
                            role TEXT, 
                            content TEXT)''')
        conn.commit()

# ================= System Prompts & Messages =================
SYSTEM_PROMPT_AR = (
    "أنت مساعد ذكي ومتخصص وحصري في مجال Microsoft Excel وتحليل البيانات فقط. "
    "وظيفتك هي الإجابة عن الأسئلة المتعلقة بالمعادلات، الجداول المحورية، لغة VBA، أدوات Power BI، وتحليل البيانات الإحصائية. "
    "إذا سألك المستخدم عن أي موضوع خارج هذا النطاق (مثل الطبخ، السفر، الأخبار، البرمجة بلغات أخرى غير الإكسل، إلخ)، "
    "يجب عليك رفض الإجابة بأدب بعبارة ثابتة مثل: 'عذراً، هذا الموضوع خارج نطاق اختصاصي. أنا هنا لمساعدتك في كل ما يخص الإكسل وتحليل البيانات فقط'."
)

SYSTEM_PROMPT_EN = (
    "You are an intelligent assistant specialized exclusively in Microsoft Excel and data analysis. "
    "Your job is to answer questions related to formulas, pivot tables, VBA, Power BI, and statistical data analysis. "
    "If the user asks about any topic outside this scope (e.g., cooking, travel, news, programming in languages other than Excel, etc.), "
    "you must politely decline to answer using a fixed phrase like: 'Sorry, this topic is outside my specialty. I am here to help you with Excel and data analysis only.'"
)

WELCOME_AR = """مرحباً بك! 👋
أنا مساعدك الذكي وخبير Microsoft Excel المحترف. 📊
مهمتي هي مساعدتك في:
🔹 كتابة وشرح المعادلات المعقدة (Formulas)
🔹 تحليل البيانات وتنسيق الجداول
🔹 كتابة أكواد VBA وماكرو (Macros)
🔹 استعلامات Power Query
اكتب سؤالك أو مشكلتك وسأقوم بمساعدتك فوراً."""

WELCOME_EN = """Welcome! 👋
I am your intelligent assistant and professional Microsoft Excel expert. 📊
My mission is to help you with:
🔹 Writing and explaining complex formulas
🔹 Data analysis and table formatting
🔹 Writing VBA code and Macros
🔹 Power Query queries
Type your question or problem, and I will assist you immediately."""

# ================= Database Helpers =================
def get_user_language(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_user_language(user_id, language):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO users (user_id, language) VALUES (?, ?)", (user_id, language))
        conn.commit()

def get_context(user_id, limit=15):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
        rows = cursor.fetchall()
        return [{"role": row[0], "content": row[1]} for row in reversed(rows)]

def add_message(user_id, role, content):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        conn.commit()

# ================= Telegram Handlers =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("العربية 🇸🇦", callback_data='lang_ar'),
            InlineKeyboardButton("English 🇬🇧", callback_data='lang_en'),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        'Please select your preferred language / يرجى تحديد لغتك المفضلة:', 
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() 
    
    user_id = query.from_user.id
    lang_choice = query.data
    
    if lang_choice == 'lang_ar':
        set_user_language(user_id, 'ar')
        await query.edit_message_text(text=WELCOME_AR)
    elif lang_choice == 'lang_en':
        set_user_language(user_id, 'en')
        await query.edit_message_text(text=WELCOME_EN)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_text = update.message.text
    
    language = get_user_language(user_id)
    if not language:
        await update.message.reply_text("Please use /start to select your language first.\nيرجى استخدام /start لاختيار اللغة أولاً.")
        return

    # 1. Save user's message
    add_message(user_id, 'user', user_text)
    
    # 2. Send "Typing..." action
    await update.message.chat.send_action(action="typing")
    
    # 3. Send loading placeholder message
    loading_text = "جاري التفكير وإعداد الإجابة... ⏳" if language == 'ar' else "Thinking and preparing the answer... ⏳"
    placeholder_msg = await update.message.reply_text(loading_text)
    
    system_prompt = SYSTEM_PROMPT_AR if language == 'ar' else SYSTEM_PROMPT_EN
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(get_context(user_id, limit=15)) 
    
    # Initialize OpenAI Async Client for OpenRouter
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )
    
    try:
        # Start Streaming
        stream = await client.chat.completions.create(
            model=MODEL_ID,
            messages=messages,
            stream=True
        )
        
        full_response = ""
        last_edit_time = time.time()
        
        # Collect chunks and edit message periodically
        async for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                full_response += content
                
                # Update message every 1.5 seconds to avoid Telegram rate limits
                current_time = time.time()
                if current_time - last_edit_time > 1.5:
                    try:
                        await placeholder_msg.edit_text(full_response + " ✍️")
                        last_edit_time = current_time
                    except telegram.error.BadRequest:
                        # Ignore BadRequest (like Message is not modified)
                        pass
        
        # 4. Final update with complete message
        if full_response.strip():
            try:
                await placeholder_msg.edit_text(full_response)
            except telegram.error.BadRequest:
                pass
            # Save AI response to DB
            add_message(user_id, 'assistant', full_response)
        else:
            err = "حدث خطأ، استجابة فارغة من الذكاء الاصطناعي." if language == 'ar' else "Error: Empty response from AI."
            await placeholder_msg.edit_text(err)
            
    except Exception as e:
        error_msg = "عذراً، حدث خطأ أثناء الاتصال بالخادم." if language == 'ar' else "Sorry, an error occurred while connecting to the server."
        try:
            await placeholder_msg.edit_text(f"{error_msg}\n{str(e)}")
        except:
            pass

# ================= Main Application Setup =================
def main():
    init_db()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is starting up with streaming enabled...")
    application.run_polling()

if __name__ == '__main__':
    main()
