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
TOKEN = os.getenv("TELEGRAM_TOKEN", "7852603191:AAE08Eqz8WNc9UD4_ZyM8YS7GmIw1jcgad4")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "UhHGm6bB5zG90miFkG")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "3V7vA1K4hPnVvu8MkOatQGdL6xFneKD5BRHT")
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

def calculate_ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def get_data_for_timeframe(ticker, interval, period_days):
    cache_key = f"{ticker}_{interval}_{period_days}"
    cached = get_cached(cache_key)
    if cached is not None:
        return cached
    try:
        data = yf.download(ticker, period=period_days, interval=interval, progress=False)
        if data.empty:
            logger.warning(f"⚠️ Нет данных для {ticker} {interval}")
            return None
        if isinstance(data.columns, pd.MultiIndex):
            close = data['Close'].iloc[:, 0] if 'Close' in data else data.iloc[:, 0]
        else:
            close = data['Close'] if 'Close' in data else data.iloc[:, 0]
        close = close.dropna()
        if len(close) < 20:
            logger.warning(f"⚠️ Мало данных для {ticker} {interval}: {len(close)} свечей")
            return None
        set_cached(cache_key, close)
        return close
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки данных {ticker} {interval}: {e}")
        return None

def analyze_timeframe(close, timeframe_name):
    if close is None or len(close) < 20:
        return None
    try:
        current_price = float(close.iloc[-1])
        ema50 = calculate_ema(close, 50).iloc[-1] if len(close) >= 50 else current_price
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi = 50 if pd.isna(rsi_val) else rsi_val
        if current_price > ema50 * 1.0003:
            trend, trend_score = "📈 ВВЕРХ", 1
        elif current_price < ema50 * 0.9997:
            trend, trend_score = "📉 ВНИЗ", -1
        else:
            trend, trend_score = "➡️ ФЛЕТ", 0
        if rsi < 30 and trend_score >= 0:
            entry_signal, entry_score = "🚀 ПОТЕНЦИАЛЬНЫЙ BUY", 1
        elif rsi > 70 and trend_score <= 0:
            entry_signal, entry_score = "🚀 ПОТЕНЦИАЛЬНЫЙ SELL", -1
        elif trend_score > 0:
            entry_signal, entry_score = "📈 ИЩЕМ BUY", 0.5
        elif trend_score < 0:
            entry_signal, entry_score = "📉 ИЩЕМ SELL", -0.5
        else:
            entry_signal, entry_score = "⏸️ ЖДЁМ", 0
        return {'price': current_price, 'trend': trend, 'trend_score': trend_score, 'rsi': rsi, 'entry_signal': entry_signal, 'entry_score': entry_score}
    except Exception as e:
        logger.error(f"❌ Ошибка анализа таймфрейма {timeframe_name}: {e}")
        return None

def multi_timeframe_analyst(ticker):
    fixed_ticker, is_otc = fix_ticker(ticker)
    timeframes = [
        {'name': '1H',  'interval': '60m', 'period': '7d', 'weight': 3},
        {'name': '30M', 'interval': '30m', 'period': '5d', 'weight': 2},
        {'name': '15M', 'interval': '15m', 'period': '3d', 'weight': 2},
        {'name': '5M',  'interval': '5m',  'period': '2d', 'weight': 1},
        {'name': '1M',  'interval': '1m',  'period': '1d', 'weight': 1}
    ]
    results = {}
    total_trend_score = 0
    total_weight = 0
    for tf in timeframes:
        close = get_data_for_timeframe(fixed_ticker, tf['interval'], tf['period'])
        analysis = analyze_timeframe(close, tf['name'])
        if analysis:
            results[tf['name']] = analysis
            total_trend_score += analysis['trend_score'] * tf['weight']
            total_weight += tf['weight']
    if not results:
        return None, "Нет данных", None
    avg_trend = total_trend_score / total_weight if total_weight > 0 else 0
    if avg_trend > 0.3:
        overall_trend, overall_color = "📈 ТРЕНД ВВЕРХ", "🟢"
    elif avg_trend < -0.3:
        overall_trend, overall_color = "📉 ТРЕНД ВНИЗ", "🔴"
    else:
        overall_trend, overall_color = "🟡 ФЛЕТ", "🟡"
    signal = "⏸️ ВНЕ РЫНКА"
    if '1M' in results:
        m1 = results['1M']
        if avg_trend > 0.3 and m1['entry_score'] > 0:
            signal = "🔵 СИЛЬНЫЙ BUY"
        elif avg_trend < -0.3 and m1['entry_score'] < 0:
            signal = "🔴 СИЛЬНЫЙ SELL"
        elif avg_trend > 0.3:
            signal = "🟡 BUY (осторожно)"
        elif avg_trend < -0.3:
            signal = "🟡 SELL (осторожно)"
    tf_icons = []
    for tf_name in ['1H', '30M', '15M', '5M', '1M']:
        if tf_name in results:
            r = results[tf_name]
            if "ВВЕРХ" in r['trend']:
                tf_icons.append(f"{tf_name}⬆️")
            elif "ВНИЗ" in r['trend']:
                tf_icons.append(f"{tf_name}⬇️")
            else:
                tf_icons.append(f"{tf_name}➡️")
    tf_line = " ".join(tf_icons)
    response = f"""
{overall_color} *МУЛЬТИ-ТАЙМФРЕЙМ: {ticker.upper()}*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{tf_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*ОБЩИЙ ТРЕНД:* {overall_trend}
🎯 *СИГНАЛ:* {signal}
⏰ Экспирация: 1-2 минуты (вход со следующей свечи)
"""
    if is_otc:
        response += "\n⚠️ OTC актив — волатильность выше!"
    return results, response, signal

def advanced_analysis(ticker, interval='60m'):
    try:
        fixed_ticker, _ = fix_ticker(ticker)
        data = yf.download(fixed_ticker, period='3d', interval=interval, progress=False)
        if data.empty:
            logger.warning(f"⚠️ Нет данных для расширенного анализа {ticker}")
            return None
        if isinstance(data.columns, pd.MultiIndex):
            close = data['Close'].iloc[:, 0]
            high = data['High'].iloc[:, 0]
            low = data['Low'].iloc[:, 0]
            volume = data['Volume'].iloc[:, 0]
        else:
            close = data['Close']
            high = data['High']
            low = data['Low']
            volume = data['Volume']
        close = close.dropna()
        if len(close) < 20:
            return None
        ema50 = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        trend_slope = (float(ema50.iloc[-1]) - float(ema50.iloc[-2])) / float(ema50.iloc[-2]) * 100 if len(ema50) >= 2 else 0
        trend_val = max(0, min(100, 50 + trend_slope * 100))
        trend_signal = "BUY" if trend_val > 60 else ("SELL" if trend_val < 40 else "NEUTRAL")
        avg_volume = volume.tail(10).mean()
        vol_ratio = (float(volume.iloc[-1]) / avg_volume) * 100 if avg_volume > 0 else 50
        vol_val = min(100, vol_ratio)
        vol_signal = "BUY" if vol_val > 70 else ("SELL" if vol_val < 30 else "NEUTRAL")
        macd = ta.trend.MACD(close).macd()
        macd_signal_line = ta.trend.MACD(close).macd_signal()
        if len(macd) >= 2 and float(macd.iloc[-1]) > float(macd.iloc[-2]) and float(macd_signal_line.iloc[-1]) < float(macd_signal_line.iloc[-2]):
            reversal_signal, reversal_val = "BUY", 75
        elif len(macd) >= 2 and float(macd.iloc[-1]) < float(macd.iloc[-2]) and float(macd_signal_line.iloc[-1]) > float(macd_signal_line.iloc[-2]):
            reversal_signal, reversal_val = "SELL", 25
        else:
            reversal_signal, reversal_val = "NEUTRAL", 50
        rsi_val = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi = 50 if pd.isna(rsi_val) else rsi_val
        impulse_val = rsi
        impulse_signal = "BUY" if rsi < 30 else ("SELL" if rsi > 70 else "NEUTRAL")
        williams = ta.momentum.WilliamsRIndicator(high, low, close, lbp=14).williams_r().iloc[-1]
        williams = -50 if pd.isna(williams) else williams
        pressure_val = 50 - williams
        pressure_signal = "BUY" if williams < -80 else ("SELL" if williams > -20 else "NEUTRAL")
        atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1]
        current_price = float(close.iloc[-1])
        atr_percent = (atr / current_price) * 100 if current_price > 0 else 0
        activity_val = min(100, atr_percent * 20)
        activity_signal = "OVERBOUGHT" if activity_val > 80 else ("NEUTRAL" if activity_val > 30 else "OVER SOLD")
        vpt = ta.volume.VolumePriceTrendIndicator(close, volume).volume_price_trend()
        if len(vpt) >= 2 and vpt.iloc[-1] > vpt.iloc[-2]:
            whale_signal, whale_val = "BUY", 70
        elif len(vpt) >= 2:
            whale_signal, whale_val = "SELL", 30
        else:
            whale_signal, whale_val = "NEUTRAL", 50
        ema20 = ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1]
        noise_val = abs((current_price - ema20) / ema20) * 100 if ema20 > 0 else 0
        noise_signal = "LOW VOL" if noise_val < 0.3 else ("HIGH VOL" if noise_val > 1 else "NORMAL")
        liquidity_val = min(100, max(0, 100 - (atr_percent * 10)))
        liquidity_signal = "BUY" if liquidity_val > 60 else ("SELL" if liquidity_val < 40 else "NEUTRAL")
        adx_val = ta.trend.ADXIndicator(high, low, close, window=14).adx().iloc[-1]
        direction_val = 25 if pd.isna(adx_val) else adx_val
        direction_signal = "BUY" if direction_val > 25 else ("SELL" if direction_val < 20 else "NEUTRAL")
        if len(high) >= 10:
            hh = high.iloc[-1] > high.iloc[-5]
            hl = low.iloc[-1] > low.iloc[-5]
            if hh and hl:
                structure_signal, structure_val = "BUY", 75
            elif not hh and not hl:
                structure_signal, structure_val = "SELL", 25
            else:
                structure_signal, structure_val = "NEUTRAL", 50
        else:
            structure_signal, structure_val = "NEUTRAL", 50
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_avg = bb.bollinger_mavg().iloc[-1]
        bb_width = (bb.bollinger_hband().iloc[-1] - bb.bollinger_lband().iloc[-1]) / bb_avg * 100 if bb_avg > 0 else 0
        market_val = min(100, bb_width * 10)
        market_signal = "TRENDING" if market_val > 30 else "RANGING"
        scores = [trend_signal, vol_signal, reversal_signal, impulse_signal, pressure_signal,
                  whale_signal, liquidity_signal, direction_signal, structure_signal]
        buy_count = sum(1 for s in scores if s == "BUY")
        sell_count = sum(1 for s in scores if s == "SELL")
        model_val = 50 + ((buy_count - sell_count) / len(scores)) * 50
        model_signal = "BUY" if model_val > 60 else ("SELL" if model_val < 40 else "NEUTRAL")
        params = [
            ("📈 Тренд", trend_val, trend_signal),
            ("📊 Объёмы", vol_val, vol_signal),
            ("🔄 Разворот", reversal_val, reversal_signal),
            ("⚡ Импульс", impulse_val, impulse_signal),
            ("🏋️ Давление", pressure_val, pressure_signal),
            ("📈 Активность", activity_val, activity_signal),
            ("🐋 Крупная игра", whale_val, whale_signal),
            ("🎛️ Шум/Фильтр", noise_val, noise_signal),
            ("💧 Ликвидность", liquidity_val, liquidity_signal),
            ("🧭 Напр. Тренда", direction_val, direction_signal),
            ("🏗️ Структура", structure_val, structure_signal),
            ("🌡️ Наст. Рынка", market_val, market_signal),
            ("🎯 Модель Напр.", model_val, model_signal),
        ]
        buys = sum(1 for _, _, s in params if s == "BUY")
        sells = sum(1 for _, _, s in params if s == "SELL")
        neutrals = len(params) - buys - sells
        return params, buys, sells, neutrals
    except Exception as e:
        logger.error(f"❌ Ошибка расширенного анализа {ticker}: {e}")
        return None

def news_analyst(ticker):
    try:
        currency_code = ticker[:3].upper()
        rss_url = "https://www.investing.com/rss/news_FOREX.rss"
        response = requests.get(rss_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code != 200:
            logger.warning(f"⚠️ RSS недоступен: статус {response.status_code}")
            return "НЕЙТРАЛЬНЫЕ", "HOLD", "Нет данных", [], 0
        keywords_map = {
            'EUR': ['euro', 'ecb'], 'USD': ['dollar', 'fed'], 'GBP': ['pound'],
            'JPY': ['yen'], 'AUD': ['aussie'], 'CAD': ['loonie'], 'CHF': ['franc']
        }
        keywords = keywords_map.get(currency_code, [currency_code.lower()])
        content = response.text
        news_items = []
        for line in content.split('\n'):
            if '<title>' in line:
                title = line.replace('<title>', '').replace('</title>', '').strip()
                title_lower = title.lower()
                relevance = sum(1 for kw in keywords if kw in title_lower)
                if relevance > 0:
                    sentiment = 0
                    for w in ['surge', 'gain', 'rise', 'up', 'positive']:
                        if w in title_lower: sentiment += 1
                    for w in ['drop', 'fall', 'decline', 'down', 'negative']:
                        if w in title_lower: sentiment -= 1
                    news_items.append({'title': title[:80], 'sentiment': sentiment})
        if not news_items:
            return "НЕЙТРАЛЬНЫЕ", "HOLD", "Нет новостей", [], 0
        news_items.sort(key=lambda x: abs(x['sentiment']), reverse=True)
        top_news = news_items[:3]
        total = sum(n['sentiment'] for n in top_news)
        if total > 0:
            return "ПОЗИТИВНЫЕ", "BUY", "Новости позитивные", top_news, total
        elif total < 0:
            return "НЕГАТИВНЫЕ", "SELL", "Новости негативные", top_news, total
        return "НЕЙТРАЛЬНЫЕ", "HOLD", "Новости нейтральные", top_news, total
    except Exception as e:
        logger.error(f"❌ Ошибка новостного анализа: {e}")
        return "НЕЙТРАЛЬНЫЕ", "HOLD", "Ошибка", [], 0

def risk_manager_binary(is_otc=False):
    if is_otc:
        return "🟡 СРЕДНИЙ", "Всегда только 1% депозита"
    return "🟢 НИЗКИЙ", "Всегда только 1% депозита"

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

# ========== ПОРТФЕЛЬНЫЙ МЕНЕДЖЕР ==========
def portfolio_manager(ticker):
    try:
        mtf_results, mtf_analysis, mtf_signal = multi_timeframe_analyst(ticker)
        if mtf_results is None:
            return f"❌ Нет данных по {ticker}"
        _, is_otc = fix_ticker(ticker)
        advanced = advanced_analysis(ticker, '60m')
        if advanced:
            adv_params, adv_buys, adv_sells, adv_neutrals = advanced
        else:
            adv_params, adv_buys, adv_sells, adv_neutrals = None, 0, 0, 0
        sentiment, news_verdict, news_reason, news_list, news_score = news_analyst(ticker)
        risk_level, risk_advice = risk_manager_binary(is_otc)
        if adv_params:
            if adv_buys > adv_sells + 2:
                final_signal = "ПОКУПАТЬ"
            elif adv_sells > adv_buys + 2:
                final_signal = "ПРОДАВАТЬ"
            elif adv_buys > adv_sells:
                final_signal = "ПОКУПАТЬ (осторожно)"
            elif adv_sells > adv_buys:
                final_signal = "ПРОДАВАТЬ (осторожно)"
            else:
                final_signal = "ДЕРЖАТЬ"
        else:
            if "СИЛЬНЫЙ BUY" in str(mtf_signal):
                final_signal = "ПОКУПАТЬ"
            elif "СИЛЬНЫЙ SELL" in str(mtf_signal):
                final_signal = "ПРОДАВАТЬ"
            elif "BUY" in str(mtf_signal):
                final_signal = "ПОКУПАТЬ (осторожно)"
            elif "SELL" in str(mtf_signal):
                final_signal = "ПРОДАВАТЬ (осторожно)"
            else:
                final_signal = "ДЕРЖАТЬ"
        response = f"""
📊 *КОМПЛЕКСНЫЙ АНАЛИЗ: {ticker.upper()}*
*МУЛЬТИ-ТАЙМФРЕЙМ*
{mtf_analysis}
*РАСШИРЕННЫЙ АНАЛИЗ (13 параметров)*
"""
        if adv_params:
            sig_map = {
                "BUY": "Покупка", "SELL": "Продажа", "NEUTRAL": "Нейтрально",
                "OVERBOUGHT": "Перекуп-ть", "OVER SOLD": "Перепрод-ть",
                "LOW VOL": "Низк. Вол.", "HIGH VOL": "Выс. Вол.",
                "TRENDING": "Тренд", "RANGING": "Флэт"
            }
            for name, val, sig in adv_params:
                sig_text = sig_map.get(sig, str(sig)[:10])
                response += f"\n{name} {val:.1f} {sig_text}"
        response += f"""
*Распределение:* BUY: {adv_buys} | SELL: {adv_sells} | NEUTRAL: {adv_neutrals}
*Новости:* {sentiment}
*Риск:* {risk_level}
*ИТОГ:* {final_signal}
⚠️ Не инвестрекомендация
"""
        if is_otc:
            response += f"\n⚡ OTC — волатильность выше"
        return response
    except Exception as e:
        logger.error(f"❌ Ошибка portfolio_manager: {e}")
        return f"❌ Ошибка анализа {ticker}"

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
