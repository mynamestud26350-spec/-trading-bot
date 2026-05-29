import os
import telebot
import yfinance as yf
import pandas as pd
import ta
import requests
import ccxt
import json
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ========== BYBIT НАСТРОЙКИ ==========
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

if BYBIT_API_KEY and BYBIT_API_SECRET:
    exchange = ccxt.bybit({
        'apiKey': BYBIT_API_KEY,
        'secret': BYBIT_API_SECRET,
        'options': {'defaultType': 'spot'},
        'enableRateLimit': True,
    })
else:
    exchange = None
    print("⚠️ Bybit ключи не заданы, крипто-трейдинг будет недоступен")

# ========== ВАЛЮТНЫЕ ПАРЫ ==========
FOREX_PAIRS = [
    'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDUSD', 'CADCHF', 'CADJPY', 'CHFJPY',
    'EURAUD', 'EURCAD', 'EURCHF', 'EURGBP', 'EURJPY', 'EURUSD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'GBPUSD',
    'USDCAD', 'USDCHF', 'USDJPY', 'NZDJPY'
]

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def fix_ticker(ticker):
    is_otc = False
    if ticker.lower().endswith('_otc'):
        is_otc = True
        ticker = ticker[:-4]
    ticker = ticker.upper().strip()
    if ticker in FOREX_PAIRS:
        return f"{ticker}=X", is_otc
    return ticker, is_otc

def get_forex_price(ticker):
    """Быстрое получение цены для валютной пары"""
    fixed, is_otc = fix_ticker(ticker)
    try:
        data = yf.download(fixed, period="1d", interval="1h", progress=False)
        if data.empty:
            return None, is_otc
        if isinstance(data.columns, pd.MultiIndex):
            close = data['Close'].iloc[:, 0] if 'Close' in data else data.iloc[:, 0]
        else:
            close = data['Close'] if 'Close' in data else data.iloc[:, 0]
        current = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) >= 2 else current
        change = ((current - prev) / prev) * 100
        return current, change, is_otc
    except Exception as e:
        print(f"Ошибка {ticker}: {e}")
        return None, is_otc

def analyze_forex(ticker):
    """Быстрый анализ валютной пары"""
    price, change, is_otc = get_forex_price(ticker)
    if price is None:
        return f"❌ Нет данных по {ticker}"
    
    result = f"""
📊 *{ticker.upper()}*
━━━━━━━━━━━━━━━━━━━━
💰 *Цена:* {price:.5f}
📈 *Изменение:* {change:+.2f}%
"""
    if is_otc:
        result += "\n⚡ *OTC* — волатильность выше!"
    return result

# ========== МЕНЮ (упрощённое для теста) ==========
def create_main_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("💰 Валютные пары", callback_data="menu_forex"),
        telebot.types.InlineKeyboardButton("⚡ OTC пары", callback_data="menu_otc"),
        telebot.types.InlineKeyboardButton("🔧 Статус", callback_data="status")
    )
    return kb

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "📋 *ГЛАВНОЕ МЕНЮ*", parse_mode='Markdown', reply_markup=create_main_menu())

@bot.message_handler(commands=['status'])
def status(message):
    bot.reply_to(message, "✅ Бот работает через Webhook на Render.com!")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if call.data == "menu_forex":
        bot.edit_message_text("💰 *Валютные пары*\nВыберите пару:", call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return
    
    if call.data == "menu_otc":
        bot.edit_message_text("⚡ *OTC пары*\nВыберите пару:", call.message.chat.id, call.message.message_id,
                              parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return
    
    if call.data == "status":
        bot.answer_callback_query(call.id, "Бот работает!")
        return

@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.reply_to(message, analyze_forex(message.text.strip().upper()))

# ========== WEBHOOK ОБРАБОТЧИК ==========
@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'Bad request', 403

@app.route('/health', methods=['GET'])
def health():
    return 'OK', 200

@app.route('/')
def index():
    return 'Bot is running', 200

def set_webhook():
    """Устанавливает вебхук на Render URL"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook установлен: {webhook_url}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL не задан, пропускаем установку вебхука")

if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)