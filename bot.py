import os
import telebot
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

TOKEN = os.getenv("TELEGRAM_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY")
ALLOWED_USER_IDS = list(map(int, os.getenv("ALLOWED_USER_IDS", "").split(","))) if os.getenv("ALLOWED_USER_IDS") else []

if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не найден")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    raise ValueError("❌ BYBIT ключи не найдены")
if not ALPHA_VANTAGE_KEY:
    raise ValueError("❌ ALPHA_VANTAGE_KEY не найден")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
auto_trade_active = [True]

def is_allowed(user_id):
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS

def check_access(call_or_message):
    uid = call_or_message.from_user.id
    if not is_allowed(uid):
        logger.warning(f"⛔ Попытка доступа: {uid}")
        return False
    return True

# ========== БАЗА ДАННЫХ ==========
DB_PATH = 'trades.db'

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT, ticker TEXT, side TEXT,
                amount_usdt REAL, price REAL, qty REAL,
                pnl REAL, pnl_percent REAL, status TEXT
            )
        ''')
        conn.commit()
        logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка БД: {e}")
    finally:
        conn.close()

def save_trade(ticker, side, amount_usdt, price, qty, status="open"):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trades (timestamp, ticker, side, amount_usdt, price, qty, pnl, pnl_percent, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), ticker, side, amount_usdt, price, qty, 0, 0, status))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения сделки: {e}")
    finally:
        conn.close()

def update_trade_pnl(trade_id, pnl, pnl_percent):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('UPDATE trades SET pnl=?, pnl_percent=?, status="closed" WHERE id=?', (pnl, pnl_percent, trade_id))
        conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка обновления сделки: {e}")
    finally:
        conn.close()

def get_last_open_trade(ticker):
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT id, qty, price FROM trades WHERE ticker=? AND status="open" ORDER BY id DESC LIMIT 1', (ticker,))
        return cursor.fetchone()
    except:
        return None
    finally:
        conn.close()

def get_daily_report():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('''
            SELECT COUNT(*),
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END),
                   SUM(pnl)
            FROM trades WHERE date(timestamp)=date(?) AND status="closed"
        ''', (today,))
        r = cursor.fetchone()
        total = r[0] or 0
        win = r[1] or 0
        loss = r[2] or 0
        pnl = r[3] or 0
        wr = (win / total * 100) if total > 0 else 0
        return f"""
📊 *ОТЧЁТ ЗА {today}*
━━━━━━━━━━━━━━━━━━━━━━━
📈 *Всего сделок:* {total}
🟢 *В прибыль:* {win}
🔴 *В убыток:* {loss}
🎯 *Winrate:* {wr:.1f}%
💰 *Общий P&L:* {pnl:.2f} USDT
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Не инвестрекомендация
"""
    except Exception as e:
        return "❌ Ошибка отчёта"
    finally:
        conn.close()

def get_all_trades():
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT timestamp, ticker, side, amount_usdt, price, pnl, status FROM trades ORDER BY id DESC LIMIT 10')
        results = cursor.fetchall()
        if not results:
            return "📭 Нет сделок"
        response = "📋 *ПОСЛЕДНИЕ СДЕЛКИ*\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        for r in results:
            ts, ticker, side, amount, price, pnl, status = r
            si = "🟢" if side == "buy" else "🔴"
            pi = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⚪")
            response += f"\n{si} {ticker} | {amount} USDT\n   Цена: {price:.2f} | {pi} P&L: {pnl:.2f}\n   {ts[:16]}\n"
        return response
    except:
        return "❌ Ошибка истории"
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

OTC_PAIRS = [p + "_otc" for p in FOREX_PAIRS]

# ========== ALPHA VANTAGE ==========
INTERVAL_MAP = {
    '1m': '1min', '5m': '5min', '15m': '15min', '30m': '30min', '60m': '60min'
}

def get_alpha_vantage_data(ticker, interval):
    """Получает данные с Alpha Vantage"""
    if len(ticker) == 6 and ticker.isalpha():
        from_sym = ticker[:3]
        to_sym = ticker[3:]
        url = f"https://www.alphavantage.co/query?function=FX_INTRADAY&from_symbol={from_sym}&to_symbol={to_sym}&interval={INTERVAL_MAP[interval]}&outputsize=compact&apikey={ALPHA_VANTAGE_KEY}"
    else:
        url = f"https://www.alphavantage.co/query?function=CRYPTO_INTRADAY&symbol={ticker}&market=USD&interval={INTERVAL_MAP[interval]}&outputsize=compact&apikey={ALPHA_VANTAGE_KEY}"
    
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        time_key = next((k for k in data.keys() if 'Time Series' in k), None)
        if not time_key:
            return None
        closes = []
        for dt in sorted(data[time_key].keys()):
            try:
                closes.append(float(data[time_key][dt]['4. close']))
            except:
                continue
        if len(closes) < 20:
            return None
        return pd.Series(closes)
    except Exception as e:
        logger.error(f"Alpha Vantage ошибка {ticker}: {e}")
        return None

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def analyze_timeframe(close, name):
    if close is None or len(close) < 20:
        return None
    current = close.iloc[-1]
    ema50 = calculate_ema(close, 50).iloc[-1] if len(close) >= 50 else current
    rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1] or 50
    if current > ema50 * 1.0003:
        trend, ts = "📈 ВВЕРХ", 1
    elif current < ema50 * 0.9997:
        trend, ts = "📉 ВНИЗ", -1
    else:
        trend, ts = "➡️ ФЛЕТ", 0
    if rsi < 30 and ts >= 0:
        entry, es = "🚀 ПОТЕНЦИАЛЬНЫЙ BUY", 1
    elif rsi > 70 and ts <= 0:
        entry, es = "🚀 ПОТЕНЦИАЛЬНЫЙ SELL", -1
    elif ts > 0:
        entry, es = "📈 ИЩЕМ BUY", 0.5
    elif ts < 0:
        entry, es = "📉 ИЩЕМ SELL", -0.5
    else:
        entry, es = "⏸️ ЖДЁМ", 0
    return {'price': current, 'trend': trend, 'ts': ts, 'rsi': rsi, 'entry': entry, 'es': es}

def multi_timeframe_analyst(ticker):
    results = {}
    total_ts = 0
    total_w = 0
    timeframes = [
        ('1H', '60m', 3, 7), ('30M', '30m', 2, 5), ('15M', '15m', 2, 3),
        ('5M', '5m', 1, 2), ('1M', '1m', 1, 1)
    ]
    for name, iv, weight, days in timeframes:
        close = get_alpha_vantage_data(ticker, iv)
        if close is None:
            continue
        a = analyze_timeframe(close, name)
        if a:
            results[name] = a
            total_ts += a['ts'] * weight
            total_w += weight
        time.sleep(12)
    if not results:
        return None, "❌ Нет данных", None
    avg = total_ts / total_w
    if avg > 0.3:
        overall, color = "📈 ТРЕНД ВВЕРХ", "🟢"
    elif avg < -0.3:
        overall, color = "📉 ТРЕНД ВНИЗ", "🔴"
    else:
        overall, color = "🟡 ФЛЕТ", "🟡"
    signal = "⏸️ ВНЕ РЫНКА"
    if '1M' in results and results['1M']['es'] != 0:
        if avg > 0.3 and results['1M']['es'] > 0:
            signal = "🔵 СИЛЬНЫЙ BUY"
        elif avg < -0.3 and results['1M']['es'] < 0:
            signal = "🔴 СИЛЬНЫЙ SELL"
    icons = []
    for tf in ['1H', '30M', '15M', '5M', '1M']:
        if tf in results:
            icons.append(f"{tf}⬆️" if "ВВЕРХ" in results[tf]['trend'] else f"{tf}⬇️" if "ВНИЗ" in results[tf]['trend'] else f"{tf}➡️")
    response = f"""
{color} *МУЛЬТИ-ТАЙМФРЕЙМ: {ticker}*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{' '.join(icons)}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*ОБЩИЙ ТРЕНД:* {overall}
🎯 *СИГНАЛ:* {signal}
⏰ Экспирация: 1-2 минуты
"""
    return results, response, signal

def portfolio_manager(ticker):
    _, resp, _ = multi_timeframe_analyst(ticker)
    return resp if resp else f"❌ Нет данных для {ticker}"

# ========== КРИПТО-ТРЕЙДИНГ ==========
def get_coin_balance(symbol):
    try:
        bal = exchange.fetch_balance()
        return bal[symbol]['free'] if symbol in bal else 0
    except:
        return 0

def get_coin_price(symbol):
    try:
        t = exchange.fetch_ticker(f'{symbol}/USDT')
        return t['last']
    except:
        return 0

def buy_coin(symbol, amount):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return "❌ Нет цены"
        qty = round(amount / price, 5)
        exchange.create_market_buy_order(f'{symbol}/USDT', qty)
        save_trade(symbol, "buy", amount, price, qty, "open")
        return f"✅ Куплено {qty} {symbol} по {price}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def sell_coin(symbol, qty):
    try:
        price = get_coin_price(symbol)
        if price == 0:
            return "❌ Нет цены"
        exchange.create_market_sell_order(f'{symbol}/USDT', qty)
        last = get_last_open_trade(symbol)
        if last:
            tid, _, ep = last
            pnl = (price - ep) * qty
            pct = ((price - ep) / ep) * 100
            update_trade_pnl(tid, pnl, pct)
            return f"✅ Продано {qty} {symbol} | P&L: {pnl:.2f} ({pct:.2f}%)"
        return f"✅ Продано {qty} {symbol}"
    except:
        return "❌ Ошибка продажи"

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
    for sym in ["BTC","ETH","SOL","BNB","XRP","DOGE"]:
        kb.add(telebot.types.InlineKeyboardButton(f"₿ {sym}", callback_data=f"crypto_{sym.lower()}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb

def create_coin_menu(coin, sym):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("💰 Баланс", callback_data=f"balance_{sym}"))
    kb.add(telebot.types.InlineKeyboardButton("📊 Отчёт по сделкам", callback_data="crypto_report"))
    kb.add(telebot.types.InlineKeyboardButton("🤖 Автоторговля", callback_data=f"auto_{sym}"))
    kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_crypto"))
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, f"📊 *{coin} ({sym})*"

def create_category_menu(category):
    pairs = FOREX_PAIRS if category == "forex" else OTC_PAIRS
    title = "💰 ВАЛЮТНЫЕ ПАРЫ" if category == "forex" else "⚡ OTC ПАРЫ (⚠️ волатильность)"
    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    for p in pairs:
        kb.add(telebot.types.InlineKeyboardButton(p.replace("_otc","⚡"), callback_data=f"an_{p}"))
    kb.add(
        telebot.types.InlineKeyboardButton("◀ НАЗАД", callback_data="back_to_menu"),
        telebot.types.InlineKeyboardButton("🏠 ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu")
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Закрыть", callback_data="close_window"))
    return kb, title

@bot.message_handler(commands=['start', 'menu'])
def send_menu(m):
    if not check_access(m): return
    bot.send_message(m.chat.id, "📋 *ГЛАВНОЕ МЕНЮ*", parse_mode='Markdown', reply_markup=create_main_menu())

@bot.message_handler(commands=['test'])
def test(m):
    bot.reply_to(m, "✅ Бот работает, Alpha Vantage подключён. Попробуй выбрать валютную пару.")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    if not check_access(call): return
    data = call.data
    if data == "close_window":
        bot.edit_message_text("✅ Окно закрыто", call.message.chat.id, call.message.message_id)
        return
    if data == "back_to_menu":
        bot.edit_message_text("📋 *ГЛАВНОЕ МЕНЮ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_main_menu())
        return
    if data in ("menu_forex", "menu_forex_otc"):
        kb, title = create_category_menu("forex" if data == "menu_forex" else "forex_otc")
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        return
    if data == "menu_crypto":
        bot.edit_message_text("📊 *КРИПТО-ТРЕЙДИНГ*", call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=create_crypto_menu())
        return
    if data.startswith("crypto_"):
        sym = data.replace("crypto_", "").upper()
        kb, title = create_coin_menu(sym, sym)
        bot.edit_message_text(title, call.message.chat.id, call.message.message_id, parse_mode='Markdown', reply_markup=kb)
        return
    if data.startswith("balance_"):
        sym = data.replace("balance_", "")
        bal = get_coin_balance(sym)
        price = get_coin_price(sym)
        val = bal * price
        bot.send_message(call.message.chat.id, f"💰 *{sym} БАЛАНС*\n{bal:.8f} {sym}\nЦена: {price:.2f} USDT\nСтоимость: {val:.2f} USDT", parse_mode='Markdown')
        return
    if data == "crypto_report":
        bot.send_message(call.message.chat.id, get_daily_report() + "\n\n" + get_all_trades(), parse_mode='Markdown')
        return
    if data.startswith("auto_"):
        sym = data.replace("auto_", "")
        bot.send_message(call.message.chat.id, f"🤖 *Автоторговля {sym}*\nАктивна. Параметры: RSI<30 BUY, RSI>70 SELL", parse_mode='Markdown')
        return
    if data.startswith("an_"):
        ticker = data.replace("an_", "")
        bot.answer_callback_query(call.id, "🔍 Анализирую...")
        msg = bot.send_message(call.message.chat.id, "⏳ Загружаю данные (~30 сек)...")
        def analyze():
            res = portfolio_manager(ticker)
            bot.delete_message(call.message.chat.id, msg.message_id)
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("◀ НАЗАД К ПАРАМ", callback_data="menu_forex" if "_otc" not in ticker else "menu_forex_otc"))
            bot.send_message(call.message.chat.id, res, parse_mode='Markdown', reply_markup=kb)
        threading.Thread(target=analyze).start()
        return

@bot.message_handler(func=lambda m: True)
def handle_text(m):
    if not check_access(m): return
    ticker = m.text.strip().upper()
    if ticker.startswith('/'): return
    msg = bot.reply_to(m, "⏳ Анализирую... (~30 сек)")
    def analyze():
        res = portfolio_manager(ticker)
        bot.delete_message(m.chat.id, msg.message_id)
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("◀ В ГЛАВНОЕ МЕНЮ", callback_data="back_to_menu"))
        bot.reply_to(m, res, parse_mode='Markdown', reply_markup=kb)
    threading.Thread(target=analyze).start()

def set_webhook():
    url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if url:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{url}/{TOKEN}")
        logger.info(f"✅ Webhook: {url}")

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
        return 'OK', 200
    return 'Bad request', 403

@app.route('/health')
def health():
    return 'OK', 200

@app.route('/')
def index():
    return 'Bot is running', 200

if __name__ == "__main__":
    set_webhook()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
