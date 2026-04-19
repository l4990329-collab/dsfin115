import telebot
import requests
import schedule
import time
import threading
from bs4 import BeautifulSoup
import apimoex
from datetime import datetime
import imaplib
import email
from email.header import decode_header
import feedparser
from telebot import types

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = "8726259243:AAF-qnlS1AwyDbzcBqaSVNG2pwFu1aVbvR8"
DEEPSEEK_API_KEY = "sk-84631d70d0f1407b9a2caf2444dc35b0"
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# ========== ТВОИ ДАННЫЕ ПОЧТЫ (ЗАМЕНИ ЗДЕСЬ) ==========
IMAP_SERVER = "imap.mail.ru"
EMAIL = "lex_novikov@inbox.ru"       # ← ЗАМЕНИ
PASSWORD = "QBdfXfS9Nhx0b6bzrVBT"  # ← ЗАМЕНИ

# ========== УРОВНИ СИГНАЛОВ ==========
SBMM_BUY_LEVEL = 17.50
SBMM_SELL_LEVEL = 19.50

YOUR_CHAT_ID = None
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
bot.timeout = 60

# ========== КЛАВИАТУРА ==========
def get_main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn1 = types.KeyboardButton("📊 Отчёт по портфелю")
    btn2 = types.KeyboardButton("📧 Проверить почту")
    btn3 = types.KeyboardButton("🌍 Новости и прогноз")
    btn4 = types.KeyboardButton("🎯 Уровни сигналов")
    btn5 = types.KeyboardButton("❓ Помощь")
    keyboard.add(btn1, btn2, btn3, btn4, btn5)
    return keyboard

# ========== ПОЛУЧЕНИЕ ЦЕН ==========
def get_sbmm_price():
    try:
        url = "https://ru.investing.com/etfs/sbmm"
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        price_tag = soup.find('div', {'data-test': 'instrument-price-last'})
        if not price_tag:
            price_tag = soup.find('span', {'class': 'text-2xl'})
        if price_tag:
            price_text = price_tag.text.replace(' ', '').replace(',', '.').replace('₽', '')
            return float(price_text)
    except:
        pass
    return None

def get_moex_price(ticker):
    try:
        with requests.Session() as sess:
            data = apimoex.get_board_history(sess, ticker, board='TQBR')
            if data:
                price = data[0].get('LEGALCLOSEPRICE') or data[0].get('CLOSE')
                if price:
                    return float(price)
    except:
        pass
    return None

def get_portfolio_value():
    portfolio = {'SBMM': 2772, 'X5': 6, 'ETLN': 18}
    prices, values, total = {}, {}, 0
    
    sbmm_price = get_sbmm_price()
    if sbmm_price:
        prices['SBMM'] = sbmm_price
        values['SBMM'] = sbmm_price * portfolio['SBMM']
        total += values['SBMM']
    else:
        prices['SBMM'] = None
        values['SBMM'] = 0
    
    for ticker in ['X5', 'ETLN']:
        price = get_moex_price(ticker)
        if price:
            prices[ticker] = price
            values[ticker] = price * portfolio[ticker]
            total += values[ticker]
        else:
            prices[ticker] = None
            values[ticker] = 0
            
    return portfolio, prices, values, total

def generate_action_signal(prices):
    if prices.get('SBMM'):
        if prices['SBMM'] < SBMM_BUY_LEVEL:
            return "🔥 ПОКУПАТЬ SBMM! Цена ниже целевого уровня."
        elif prices['SBMM'] > SBMM_SELL_LEVEL:
            return "💰 ПРОДАВАТЬ ЧАСТЬ SBMM! Цена выше целевого уровня."
    return "⏳ ДЕРЖАТЬ. Ждём лучшей точки входа."

# ========== ПОЧТА ==========
def check_emails():
    print("📬 Проверяю почту...")
    found_any = False
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL, PASSWORD)
        mail.select("inbox")
        
        date_filter = datetime.now().strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(FROM "Sberbank" SINCE "{date_filter}")')
        
        if messages[0]:
            ids = messages[0].split()[-3:]
            for id in ids:
                status, msg_data = mail.fetch(id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject = decode_header(msg["Subject"])[0][0]
                        if isinstance(subject, bytes):
                            subject = subject.decode()
                        
                        if "отчет" in subject.lower() or "брокер" in subject.lower():
                            found_any = True
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() == "text/html":
                                        body = part.get_payload(decode=True).decode()
                                        analysis = ask_deepseek(f"Проанализируй отчет брокера и перескажи кратко (3-4 предложения) самое важное:\n{body[:2000]}", "Алексей")
                                        if YOUR_CHAT_ID:
                                            bot.send_message(YOUR_CHAT_ID, f"📧 *Новый отчёт Сбера*\n\n{analysis}", parse_mode="Markdown", reply_markup=get_main_keyboard())
        mail.close()
        mail.logout()
        
        if not found_any and YOUR_CHAT_ID:
            bot.send_message(YOUR_CHAT_ID, "📭 *Почта проверена*\n\nНовых отчётов от Сбера за сегодня не найдено.", parse_mode="Markdown", reply_markup=get_main_keyboard())
            
    except Exception as e:
        print(f"❌ Ошибка почты: {e}")
        if YOUR_CHAT_ID:
            bot.send_message(YOUR_CHAT_ID, f"❌ *Ошибка почты*\n\nПроверь логин/пароль.\n\n`{str(e)[:100]}`", parse_mode="Markdown", reply_markup=get_main_keyboard())

# ========== НОВОСТИ (С РАСШИРЕННЫМ ФИЛЬТРОМ) ==========
def check_news():
    print("🌍 Проверяю новости...")
    try:
        feed = feedparser.parse("https://ru.investing.com/rss/news_301.rss")
        if not feed.entries:
            feed = feedparser.parse("https://static.feed.rbc.ru/rbc/logical/free/news.rss")
        
        important_news = []
        keywords = [
            'нефть', 'Brent', 'рубль', 'доллар', 'ставка', 'ЦБ', 'ключевая ставка',
            'дивиденды', 'Северсталь', 'X5', 'Эталон', 'МосБиржа', 'SBMM',
            'индекс', 'IMOEX', 'инфляция', 'санкции', 'бюджет', 'Минфин',
            'отчетность', 'прибыль', 'акции', 'биржа', 'IPO', 'SPO'
        ]
        
        for entry in feed.entries[:5]:
            title = entry.title
            if any(word.lower() in title.lower() for word in keywords):
                important_news.append(f"• {title}")
        
        if important_news and YOUR_CHAT_ID:
            news_text = "\n".join(important_news)
            analysis = ask_deepseek(f"Проанализируй эти новости и дай прогноз влияния на портфель (SBMM, X5, ETLN). Дай краткий призыв к действию (1 предложение):\n{news_text}", "Алексей")
            bot.send_message(YOUR_CHAT_ID, f"🌍 *Важные новости*\n\n{news_text}\n\n📈 *Прогноз:* {analysis}", parse_mode="Markdown", reply_markup=get_main_keyboard())
        elif YOUR_CHAT_ID:
            bot.send_message(YOUR_CHAT_ID, "📰 *Новости проверены*\n\nВажных новостей для портфеля сейчас нет.", parse_mode="Markdown", reply_markup=get_main_keyboard())
            
    except Exception as e:
        print(f"❌ Ошибка новостей: {e}")
        if YOUR_CHAT_ID:
            bot.send_message(YOUR_CHAT_ID, f"❌ *Ошибка новостей*\n\n`{str(e)[:100]}`", parse_mode="Markdown", reply_markup=get_main_keyboard())

# ========== DEEPSEEK ==========
def ask_deepseek(user_message, user_name):
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    system_prompt = f"""Ты — финансовый аналитик Алексей. Клиент: {user_name}, портфель: SBMM 2772 шт, X5 6 шт, ETLN 18 шт. Отвечай коротко, по делу, максимум 5 предложений. Давай конкретные рекомендации, напоминая, что это не инвестиционная рекомендация."""
    
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
        "temperature": 0.7,
        "max_tokens": 350
    }
    
    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"❌ Ошибка API: {response.status_code}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

# ========== ОБРАБОТЧИКИ ==========
@bot.message_handler(commands=['start'])
def send_welcome(message):
    global YOUR_CHAT_ID
    YOUR_CHAT_ID = message.chat.id
    bot.send_message(message.chat.id, 
        "🤖 *Привет, Алексей!*\n\n"
        "Я твой продвинутый финансовый аналитик. Используй кнопки ниже для управления.\n\n"
        "📊 *Отчёт по портфелю* — точные цены и сигнал\n"
        "📧 *Проверить почту* — анализ отчётов Сбера\n"
        "🌍 *Новости и прогноз* — влияние на твой портфель\n"
        "🎯 *Уровни сигналов* — текущие настройки\n\n"
        "💬 Можешь также задать любой вопрос текстом!",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard())

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    global YOUR_CHAT_ID
    if not YOUR_CHAT_ID:
        YOUR_CHAT_ID = message.chat.id
    
    text = message.text.lower()
    
    if text == "📊 отчёт по портфелю":
        bot.send_chat_action(message.chat.id, 'typing')
        portfolio, prices, values, total = get_portfolio_value()
        action = generate_action_signal(prices)
        
        report = "📊 *ТЕКУЩИЙ ПОРТФЕЛЬ*\n\n"
        for t, q in portfolio.items():
            if prices.get(t):
                report += f"• *{t}*: {q} шт × {prices[t]:.2f}₽ = *{values[t]:,.0f}₽*\n"
            else:
                report += f"• *{t}*: {q} шт — ⚠️ нет данных\n"
        
        report += f"\n💰 *ИТОГО: {total:,.0f}₽*\n\n🎯 *СИГНАЛ:* {action}"
        bot.send_message(message.chat.id, report, parse_mode="Markdown", reply_markup=get_main_keyboard())
    
    elif text == "📧 проверить почту":
        bot.send_chat_action(message.chat.id, 'typing')
        bot.send_message(message.chat.id, "📬 Запускаю проверку почты...", reply_markup=get_main_keyboard())
        check_emails()
    
    elif text == "🌍 новости и прогноз":
        bot.send_chat_action(message.chat.id, 'typing')
        bot.send_message(message.chat.id, "🌍 Анализирую новости и готовлю прогноз...", reply_markup=get_main_keyboard())
        check_news()
    
    elif text == "🎯 уровни сигналов":
        levels_msg = f"🎯 *ТЕКУЩИЕ УРОВНИ ДЛЯ SBMM*\n\n🔴 ПОКУПАТЬ ниже: *{SBMM_BUY_LEVEL}₽*\n🟢 ПРОДАВАТЬ выше: *{SBMM_SELL_LEVEL}₽*"
        bot.send_message(message.chat.id, levels_msg, parse_mode="Markdown", reply_markup=get_main_keyboard())
    
    elif text == "❓ помощь":
        help_msg = "🤖 *ПОМОЩЬ*\n\n📊 Отчёт — сводка портфеля с сигналом\n📧 Почта — анализ отчётов Сбера\n🌍 Новости — прогноз влияния на портфель\n\n💬 Можешь задать любой вопрос текстом!"
        bot.send_message(message.chat.id, help_msg, parse_mode="Markdown", reply_markup=get_main_keyboard())
    
    else:
        bot.send_chat_action(message.chat.id, 'typing')
        answer = ask_deepseek(message.text, message.from_user.first_name)
        bot.send_message(message.chat.id, answer, reply_markup=get_main_keyboard())

# ========== ПЛАНИРОВЩИК ==========
def run_scheduler():
    schedule.every().day.at("10:00").do(lambda: scheduled_report())
    schedule.every(30).minutes.do(check_emails)
    schedule.every(15).minutes.do(check_news)
    while True:
        schedule.run_pending()
        time.sleep(60)

def scheduled_report():
    if YOUR_CHAT_ID:
        portfolio, prices, values, total = get_portfolio_value()
        action = generate_action_signal(prices)
        report = f"🌅 *ДОБРОЕ УТРО, АЛЕКСЕЙ!*\n\n📊 *ПОРТФЕЛЬ НА {datetime.now().strftime('%d.%m.%Y')}*\n\n"
        for t, q in portfolio.items():
            if prices.get(t):
                report += f"• *{t}*: {q} шт × {prices[t]:.2f}₽ = *{values[t]:,.0f}₽*\n"
        report += f"\n💰 *ИТОГО: {total:,.0f}₽*\n\n🎯 *СИГНАЛ:* {action}"
        bot.send_message(YOUR_CHAT_ID, report, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ========== ЗАПУСК ==========
print("🤖 Мега-бот запущен! 📧+🌍+📊")
scheduler_thread = threading.Thread(target=run_scheduler)
scheduler_thread.daemon = True
scheduler_thread.start()

while True:
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"⚠️ Ошибка polling: {e}. Перезапуск через 5 сек...")
        time.sleep(5)