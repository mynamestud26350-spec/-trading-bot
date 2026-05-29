import os
import telebot
import yfinance as yf
import pandas as pd
import ta
import requests
import time
import ccxt
import sqlite3
import threading
import logging
from datetime import datetime
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("TELEGRAM_TOKEN", "7852603191:AAFqFd-ylcjuJ1C_YtL62uIf9c6fOLaFpoQ")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "MsduMr47ykYJVe7ASM")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "OMDaGBGpHWMKHbrHefAOgIvZajJv7X0KIoMP")
ALLOWED_USER_IDS = list(map(int, os.getenv("ALLOWED_USER_IDS", "").split(","))) if os.getenv("ALLOWED_USER_IDS") else []

# ========== ЛОГИРОВАНИЕ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ========== ФЛАГ АВТОТОРГОВЛИ ==========
auto_trade_active = [True]

# ========== ЗАЩИТА ==========
def is_allowed(user_id):
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS

def check_access(call_or_message):
    uid = call_or_message.from_user.id if hasattr(call_or_message, 'from_user') else call_or_message.chat.id
    if not is_allowed(uid):
        logger.warning(f"⛔ Попытка доступа от незнакомого пользователя: {uid}")
        return False
    return True

# ========== БАЗА ДАННЫХ ==========
DB_PATH = 'trades.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ticker TEXT,
            side TEXT,
            amount_usdt REAL,
            price REAL,
            qty REAL,
            pnl REAL,
            pnl_percent REAL,
            status TEXT
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ База данных инициализирована")

def save_trade(ticker, side, amount_usdt, price, qty, status="open"):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trades (timestamp, ticker, side, amount_usdt, price, qty, pnl, pnl_percent, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), ticker, side, amount_usdt, price, qty, 0, 0, status))
        conn.commit()
        logger.info(f"💾 Сохранена сделка: {side} {ticker}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сделки: {e}")
    finally:
        conn.close()

def update_trade_pnl(trade_id, pnl, pnl_percent):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE trades SET pnl = ?, pnl_percent = ?, status = 'closed'
            WHERE id = ?
        ''', (pnl, pnl_percent, trade_id))
        conn.commit()
        logger.info(f"📝 Обновлена сделка #{trade_id}: pnl={pnl:.2f}")
    except Exception as e:
        logger.error(f"❌ Ошибка обновления сделки: {e}")
    finally:
        conn.close()

def get_last_open_trade(ticker):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, qty, price FROM trades 
            WHERE ticker = ? AND status = 'open' 
            ORDER BY id DESC LIMIT 1
        ''', (ticker,))
        result = cursor.fetchone()
        return result
    except Exception as e:
        return None
    finally:
        conn.close()

def get_daily_report():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT COUNT(*), 
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
                   SUM(pnl),
                   AVG(pnl_percent)
            FROM trades 
            WHERE date(timestamp) = date(?) AND status = 'closed'
        ''', (today,))
        result = cursor.fetchone()
        total = result[0] or 0
        win = result[1] or 0
        loss = result[2] or 0
        total_pnl = result[3] or 0
        avg_percent = result[4] or 0
        winrate = (win / total * 100) if total > 0 else 0
        return f"""
📊 *ОТЧЁТ ЗА {today}*
━━━━━━━━━━━━━━━━━━━━━━━
📈 *Всего сделок:* {total}
🟢 *В прибыль:* {win}
🔴 *В убыток:* {loss}
🎯 *Winrate:* {winrate:.1f}%
💰 *Общий P&L:* {total_pnl:.2f} USDT
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Не инвестрекомендация
"""
    except Exception as e:
        return "❌ Ошибка получения отчёта"
    finally:
        conn.close()

def get_all_trades():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT timestamp, ticker, side, amount_usdt, price, pnl, status
            FROM trades ORDER BY id DESC LIMIT 10
        ''')
        results = cursor.fetchall()
        if not results:
            return "📭 Нет сделок в истории"
        response = "📋 *ПОСЛЕДНИЕ СДЕЛКИ*\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        for r in results:
            ts, ticker, side, amount, price, pnl, status = r
            side_icon = "🟢" if side == "buy" else "🔴"
            pnl_icon = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪")
            response += f"\n{side_icon} {ticker} | {amount} USDT\n   Цена: {price:.2f} | {pnl_icon} P&L: {pnl:.2f}\n   {ts[:16]}\n"
        return response
    except Exception as e:
        return "❌ Ошибка получения истории"
    finally:
        conn.close()

init_db()

# ========== BYBIT ==========
exchange = ccxt.bybit({
    'apiKey': BYBIT_API_KEY,
    'secret': BYBIT_API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

# ========== ВАЛЮТНЫЕ ПАРЫ ==========
FOREX_PAIRS = [
    'AUDCAD', 'AUDCHF', 'AUDJPY', 'AUDUSD', 'CADCHF', 'CADJPY', 'CHFJPY',
    'EURAUD', 'EURCAD', 'EURCHF', 'EURGBP', 'EURJPY', 'EURUSD',
    'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPJPY', 'GBPUSD',
    'USDCAD', 'USDCHF', 'USDJPY', 'NZDJPY'
]

OTC_PAIRS = [
    "AEDCNY_otc", "AUDCAD_otc", "AUDCHF_otc", "AUDJPY_otc", "AUDNZD_otc", "AUDUSD_otc",
    "BHDCNY_otc", "CADCHF_otc", "CADJPY_otc", "CHFJPY_otc", "CHFNOK_otc",
    "EURCHF_otc", "EURGBP_otc", "EURHUF_otc", "EURJPY_otc", "EURNZD_otc", "EURUSD_otc", "EURTRY_otc",
    "GBPAUD_otc", "GBPJPY_otc", "GBPUSD_otc", "JODCNY_otc", "KESUSD_otc", "LBPUSD_otc",
    "MADUSD_otc", "NGNUSD_otc", "NZDJPY_otc", "NZDUSD_otc", "OMRCNY_otc", "QARCNY_otc",
    "SARCNY_otc", "TNDUSD_otc", "UAHUSD_otc", "USDARS_otc", "USDBDT_otc", "USDBRL_otc",
    "USDCAD_otc", "USDCHF_otc", "USDCLP_otc", "USDCNH_otc", "USDCOP_otc", "USDDZD_otc",
    "USDEGP_otc", "USDIDR_otc", "USDINR_otc", "USDJPY_otc", "USDMXN_otc", "USDMYR_otc",
    "USDPHP_otc", "USDPKR_otc", "USDRUB_otc", "USDSGD_otc", "USDTHB_otc", "USDVND_otc",
    "YERUSD_otc", "ZARUSD_otc"
]

# ========== КЭШ ==========
_cache = {}
CACHE_TTL = 90

def get_cached(key):
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def set_cached(key, data):
    _cache[key] = (data, time.time())

# ========== ФУНКЦИИ АНАЛИЗА ==========
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
    fixed, is_otc = fix_ticker(ticker)
    try:
        data = yf.download(fixed, period="2d", interval="1h", progress=False)
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
        return None, is_otc

def analyze_forex(ticker):
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

# ========== КРИПТО-ТРЕЙДИНГ ==========
def get_coin_balance(symbol):
    try:
        bal = exchange.fetch_balance()
        return bal[symbol]['free'] if symbol in bal else 0
    except:
        return 0

def get_coin_price(symbol):
    try:
        ticker = exchange.fetch_ticker(f'{symbol}/USDT')
        return ticker['last']
    except:
        return 0

def buy_coin(symbol, amount_usdt):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return f"❌ Не удалось получить цену {symbol}"
        qty = round(amount_usdt / price, 5)
        exchange.create_market_buy_order(f'{symbol}/USDT', qty)
        save_trade(symbol, "buy", amount_usdt, price, qty, "open")
        return f"✅ Куплено {qty} {symbol} по {price} USDT"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def sell_coin(symbol, qty):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return f"❌ Не удалось получить цену {symbol}"
        exchange.create_market_sell_order(f'{symbol}/USDT', qty)
        last_trade = get_last_open_trade(symbol)
        if last_trade:
            trade_id, entry_qty, entry_price = last_trade
            pnl = (price - entry_price) * qty
            pnl_percent = ((price - entry_price) / entry_price) * 100
            update_trade_pnl(trade_id, pnl, pnl_percent)
            return f"✅ Продано {qty} {symbol} по {price} USDT | P&L: {pnl:.2f} USDT"
        return f"✅ Продано {qty} {symbol} по {price} USDT"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ========== МЕНЮ ==========
def create_main_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        telebot.types.InlineKeyboardButton("💰 Валютные пары", callback_data="menu_forex"),
        telebot.types.InlineKeyboardButton("⚡ OTC пары", callback_data="menu_forex_otc"),
        telebot.types.InlineKeyboardButton("📊 Крипто-трейдинг", callback_data="menu_crypto")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_crypto_menu():
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    coins = [
        ("₿ BTC", "crypto_btc"), ("⟠ ETH", "crypto_eth"), ("◎ SOL", "crypto_sol"),
        ("🟡 BNB", "crypto_bnb"), ("💎 XRP", "crypto_xrp"), ("🐕 DOGE", "crypto_doge")
    ]
    for name, data in coins:
        kb.add(telebot.types.InlineKeyboardButton(name, callback_data=data))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_coin_menu(coin, symbol):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("💰 Баланс", callback_data=f"balance_{symbol}"))
    kb.add(telebot.types.InlineKeyboardButton("📊 Отчёт по сделкам", callback_data="crypto_report"))
    kb.add(telebot.types.InlineKeyboardButton("🤖 Автоторговля", callback_data=f"auto_{symbol}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, f"📊 *{coin} ({symbol})*"

def create_category_menu(category):
    categories = {
        "forex": {
            "title": "💰 ВАЛЮТНЫЕ ПАРЫ\nВыберите пару:",
            "pairs": FOREX_PAIRS,
            "back_to": "menu_forex"
        },
        "forex_otc": {
            "title": "⚡ OTC ВАЛЮТНЫЕ ПАРЫ\n⚠️ Повышенная волатильность!\nВыберите пару:",
            "pairs": OTC_PAIRS,
            "back_to": "menu_forex_otc"
        }
    }
    cat = categories.get(category)
    if not cat:
        return None, "Нет данных"
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for pair in cat["pairs"]:
        display = pair.replace("_otc", "⚡") if "_otc" in pair else pair
        buttons.append(telebot.types.InlineKeyboardButton(display, callback_data=f"an_{pair}"))
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            kb.row(buttons[i], buttons[i + 1])
        else:
            kb.row(buttons[i])
    kb.add(
        telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=cat["back_to"]),
        telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, cat["title"]

# ========== ОБРАБОТЧИКИ ==========
@bot.message_handler(commands=['start', 'menu'])
def send_menu(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    bot.send_message(message.chat.id, "📋 *ГЛАВНОЕ МЕНЮ*", parse_mode='Markdown', reply_markup=create_main_menu())

@bot.message_handler(commands=['autostop'])
def autostop_handler(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    auto_trade_active[0] = False
    bot.reply_to(message, "🛑 *Автоторговля остановлена.*", parse_mode='Markdown')

@bot.message_handler(commands=['autostart'])
def autostart_handler(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    auto_trade_active[0] = True
    bot.reply_to(message, "🤖 *Автоторговля запущена.*", parse_mode='Markdown')

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call):
        bot.answer_callback_query(call.id, "⛔ Доступ запрещён.")
        return
    data = call.data
    if data == "close_window":
        bot.edit_message_text("✅ *Окно закрыто*\n\nОтправьте /start для открытия главного меню.", call.message.chat.id, call.message.message_id, parse_mode='Markdown')
        bot.answer_callback_query(call.id)
        return
    if data == "back_to_menu":
        bot.edit_message_text("📋 *ГЛАВНОЕ МЕНЮ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_main_menu())
        bot.answer_callback_query(call.id)
        return
    if data in ("menu_forex", "menu_forex_otc"):
        category = "forex" if data == "menu_forex" else "forex_otc"
        kb, title = create_category_menu(category)
        if kb:
            bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data in ("menu_crypto", "back_to_crypto"):
        bot.edit_message_text("📊 *КРИПТО-ТРЕЙДИНГ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_crypto_menu())
        bot.answer_callback_query(call.id)
        return
    coin_map = {
        "crypto_btc": "BTC", "crypto_eth": "ETH", "crypto_sol": "SOL",
        "crypto_bnb": "BNB", "crypto_xrp": "XRP", "crypto_doge": "DOGE"
    }
    if data in coin_map:
        coin = coin_map[data]
        kb, title = create_coin_menu(coin, coin)
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("balance_"):
        coin = data.replace("balance_", "")
        balance = get_coin_balance(coin)
        price = get_coin_price(coin)
        value = balance * price if price else 0
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=f"crypto_{coin.lower()}"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"💰 *{coin} БАЛАНС*\n━━━━━━━━━━━━━━━\n{coin}: {balance:.8f}\nЦена: {price:.2f} USDT\nСтоимость: {value:.2f} USDT", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data == "crypto_report":
        report = get_daily_report()
        history = get_all_trades()
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"{report}\n\n{history}", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("auto_"):
        coin = data.replace("auto_", "")
        status = "🟢 Активна" if auto_trade_active[0] else "🔴 Остановлена"
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data=f"crypto_{coin.lower()}"))
        kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
        bot.send_message(call.message.chat.id, f"🤖 *Автоторговля для {coin}*\n\n📊 Статус: {status}\n\n⚠️ Торговля ведётся на реальном счёте Bybit!", parse_mode='Markdown', reply_markup=kb)
        bot.answer_callback_query(call.id)
        return
    if data.startswith("an_"):
        ticker = data.replace("an_", "")
        bot.answer_callback_query(call.id, "🔍 Анализирую...")
        wait_msg = bot.send_message(call.message.chat.id, "⏳ *Анализирую...* Подождите.", parse_mode='Markdown')
        def run_analysis():
            try:
                result = portfolio_manager(ticker)
                back_callback = "menu_forex_otc" if "_otc" in ticker else "menu_forex"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(
                    telebot.types.InlineKeyboardButton("◀ НАЗАД К ПАРАМ", callback_data=back_callback),
                    telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
                )
                kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
                bot.delete_message(call.message.chat.id, wait_msg.message_id)
                bot.send_message(call.message.chat.id, result, parse_mode='Markdown', reply_markup=kb)
            except Exception as e:
                logger.error(f"❌ Ошибка анализа: {e}")
                bot.edit_message_text("❌ Ошибка анализа. Попробуйте позже.", call.message.chat.id, wait_msg.message_id)
        threading.Thread(target=run_analysis, daemon=True).start()
        return

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    if not check_access(message):
        bot.reply_to(message, "⛔ Доступ запрещён.")
        return
    ticker = message.text.strip().upper()
    if ticker.startswith('/'):
        return
    wait_msg = bot.reply_to(message, "⏳ *Анализирую...* Подождите.", parse_mode='Markdown')
    def run_analysis():
        try:
            result = portfolio_manager(ticker)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("◀ В ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu"))
            bot.delete_message(message.chat.id, wait_msg.message_id)
            bot.reply_to(message, result, parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            logger.error(f"❌ Ошибка handle_message: {e}")
            bot.edit_message_text("❌ Ошибка анализа.", message.chat.id, wait_msg.message_id)
    threading.Thread(target=run_analysis, daemon=True).start()

# ========== АВТОТОРГОВЛЯ ==========
def auto_trade_loop():
    from ta.momentum import RSIIndicator
    coins = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
    settings = {coin: {"rsi_buy": 30, "rsi_sell": 70, "amount": 10} for coin in coins}
    logger.info("🤖 АВТОТОРГОВЛЯ ЗАПУЩЕНА")
    while True:
        if not auto_trade_active[0]:
            time.sleep(10)
            continue
        for coin in coins:
            try:
                price = get_coin_price(coin)
                if price == 0:
                    continue
                ohlcv = exchange.fetch_ohlcv(f'{coin}/USDT', timeframe='15m', limit=100)
                closes = [c[4] for c in ohlcv]
                rsi = RSIIndicator(pd.Series(closes), window=14).rsi().iloc[-1]
                cfg = settings[coin]
                last_trade = get_last_open_trade(coin)
                if last_trade is None:
                    if rsi < cfg["rsi_buy"]:
                        result = buy_coin(coin, cfg["amount"])
                        logger.info(f"🟢 АВТО-ПОКУПКА {coin} RSI={rsi:.1f} | {result}")
                        time.sleep(2)
                else:
                    trade_id, qty, entry_price = last_trade
                    pnl_percent = ((price - entry_price) / entry_price) * 100
                    if pnl_percent >= 3:
                        result = sell_coin(coin, qty)
                        logger.info(f"✅ ТЕЙК-ПРОФИТ {coin}: +{pnl_percent:.2f}% | {result}")
                        time.sleep(2)
                    elif pnl_percent <= -2:
                        result = sell_coin(coin, qty)
                        logger.info(f"🛑 СТОП-ЛОСС {coin}: {pnl_percent:.2f}% | {result}")
                        time.sleep(2)
                    elif rsi > cfg["rsi_sell"]:
                        result = sell_coin(coin, qty)
                        logger.info(f"🔴 АВТО-ПРОДАЖА {coin} RSI={rsi:.1f} | {result}")
                        time.sleep(2)
            except Exception as e:
                logger.error(f"⚠️ Ошибка автоторговли {coin}: {e}")
            time.sleep(1)
        time.sleep(60)

auto_trade_thread = threading.Thread(target=auto_trade_loop, daemon=True)
auto_trade_thread.start()

# ========== ПОРТФЕЛЬНЫЙ МЕНЕДЖЕР ==========
def portfolio_manager(ticker):
    try:
        fixed, is_otc = fix_ticker(ticker)
        price, change, _ = get_forex_price(ticker)
        if price is None:
            return f"❌ Нет данных по {ticker}"
        result = f"""
📊 *КОМПЛЕКСНЫЙ АНАЛИЗ: {ticker.upper()}*
━━━━━━━━━━━━━━━━━━━━━━━
💰 *Цена:* {price:.5f}
📈 *Изменение:* {change:+.2f}%
"""
        if is_otc:
            result += "\n⚡ *OTC* — волатильность выше!"
        result += "\n━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ Не инвестрекомендация"
        return result
    except Exception as e:
        return f"❌ Ошибка анализа {ticker}"

# ========== WEBHOOK ==========
def set_webhook():
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if render_url:
        webhook_url = f"{render_url}/{TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook установлен: {webhook_url}")

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

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)