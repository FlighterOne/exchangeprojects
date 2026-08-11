import telebot
import time
import sqlite3
from datetime import datetime
import logging
import sys
import pytz
from telebot import types
import requests
import re
import os
from functools import wraps
from typing import Dict
import threading
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ==================== НАСТРОЙКИ ====================

TOKEN = os.getenv('BOT_TOKEN', "8893790246:AAEsn-QtYrJoqHRkC9s_De5T2O6F18P81Jg")
CHANNEL_LINK = os.getenv('CHANNEL_LINK', "https://t.me/ваш_канал")
MIN_WITHDRAW = int(os.getenv('MIN_WITHDRAW', 5))

# ==================== ОБЯЗАТЕЛЬНЫЕ ПОЛЬЗОВАТЕЛИ ====================

MANDATORY_USERS = {
    1869905379: 6,
    7627217501: 6,
    8478129002: 6,
}

ADMIN_IDS = [user_id for user_id, role in MANDATORY_USERS.items() if role >= 5]

MSK_TZ = pytz.timezone('Europe/Moscow')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)

user_messages: Dict[int, list] = {}
user_data: Dict[int, dict] = {}
DB_PATH = os.getenv('DB_PATH', '/data/exchange.db')

ROLES = {
    0: "Пользователь",
    1: "Партнер",
    2: "Корпоративный",
    3: "Стажер",
    4: "Модератор",
    5: "Админ",
    6: "Создатель"
}

ROLE_LEVELS = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6
}

# ==================== ПРАВА ДОСТУПА ====================

PANEL_ACCESS_ROLE = 2
ROLE_CHANGE_ROLE = 5
CREATOR_ASSIGN_ROLE = 6
STAFF_MIN_ROLE = 1
MODERATOR_ROLE = 4

# --- NEWS / CHANNEL SYNC (t.me/xcodru) ---
NEWS_CHANNEL_URL = "https://t.me/s/xcodru"
NEWS_CHANNEL_USERNAME = "xcodru"
SERVICE_NEWS_TITLE = "Добро пожаловать в X-Cod Exchange"
SERVICE_NEWS_BODY = (
    "Платформа для безопасной покупки и продажи Telegram-ботов запущена. "
    "Следите за обновлениями!"
)

_MONTHS_RU = {
    1: "янв.", 2: "фев.", 3: "мар.", 4: "апр.", 5: "мая", 6: "июн.",
    7: "июл.", 8: "авг.", 9: "сент.", 10: "окт.", 11: "нояб.", 12: "дек.",
}

def format_news_datetime(iso_or_dt):
    """09 авг. 2026 20:00 (МСК)."""
    try:
        if isinstance(iso_or_dt, datetime):
            dt = iso_or_dt
        else:
            s = str(iso_or_dt or "").replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MSK_TZ)
        else:
            dt = dt.astimezone(MSK_TZ)
        return f"{dt.day:02d} {_MONTHS_RU[dt.month]} {dt.year} {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return "Недавно"


def _ensure_news_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT,
            body_html TEXT,
            views_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            is_pinned INTEGER DEFAULT 0,
            tg_msg_id INTEGER,
            published_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("PRAGMA table_info(news)")
    cols = {c[1] for c in cursor.fetchall()}
    alters = {
        "body_html": "ALTER TABLE news ADD COLUMN body_html TEXT",
        "is_pinned": "ALTER TABLE news ADD COLUMN is_pinned INTEGER DEFAULT 0",
        "tg_msg_id": "ALTER TABLE news ADD COLUMN tg_msg_id INTEGER",
        "published_at": "ALTER TABLE news ADD COLUMN published_at TEXT",
    }
    for col, sql in alters.items():
        if col not in cols:
            try:
                cursor.execute(sql)
            except Exception:
                pass


def _html_to_plain(html):
    import re as _re
    if not html:
        return ""
    t = html
    t = _re.sub(r"<br\s*/?>", "\n", t, flags=_re.I)
    t = _re.sub(r"</p\s*>", "\n", t, flags=_re.I)
    t = _re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    t = _re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _sanitize_tg_html(html):
    """Оставляем безопасные теги форматирования Telegram."""
    import re as _re
    if not html:
        return ""
    # br/p -> newlines preserved via <br>
    t = _re.sub(r"</?p[^>]*>", "<br>", html, flags=_re.I)
    allowed = []
    # keep b/strong, i/em, u, s, a, code, pre, blockquote, br
    # strip scripts etc already by only allowing known tags via simple pass
    # Convert <a href="..."> to keep
    return t


def fetch_channel_posts(limit=30):
    """Публичная лента t.me/s/xcodru без API."""
    import re as _re
    import urllib.request
    try:
        req = urllib.request.Request(
            NEWS_CHANNEL_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; XCodBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.error(f"news channel fetch: {e}")
        return []

    posts = []
    # каждый виджет сообщения
    blocks = _re.split(r'<div class="tgme_widget_message\b', raw)
    for block in blocks[1:]:
        # message id from data-post="xcodru/123"
        m_id = _re.search(r'data-post="[^"/]+/(\d+)"', block)
        if not m_id:
            m_id = _re.search(r'href="https://t\.me/xcodru/(\d+)"', block)
        if not m_id:
            continue
        tg_id = int(m_id.group(1))

        # datetime
        m_dt = _re.search(r'<time[^>]*datetime="([^"]+)"', block)
        published = m_dt.group(1) if m_dt else None

        # text html
        m_text = _re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            _re.S,
        )
        body_html = m_text.group(1).strip() if m_text else ""
        plain = _html_to_plain(body_html)

        # skip pure media posts (no text)
        if not plain or len(plain.strip()) < 2:
            continue

        # title: first line, truncated
        first_line = plain.split("\n", 1)[0].strip()
        title = first_line[:120] if first_line else f"Пост #{tg_id}"

        posts.append({
            "tg_msg_id": tg_id,
            "title": title,
            "body": plain,
            "body_html": body_html,
            "published_at": published,
        })

    # newest first
    posts.sort(key=lambda p: p["tg_msg_id"], reverse=True)
    return posts[:limit]


def sync_news_from_channel():
    """Импорт новых постов канала в таблицу news."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        _ensure_news_schema(cur)
        # pinned service
        cur.execute("SELECT id FROM news WHERE is_pinned = 1 LIMIT 1")
        if not cur.fetchone():
            cur.execute(
                "SELECT id FROM news WHERE title = ?",
                (SERVICE_NEWS_TITLE,),
            )
            row = cur.fetchone()
            now = datetime.now(MSK_TZ).isoformat()
            if row:
                cur.execute(
                    "UPDATE news SET is_pinned = 1, status = 'active', published_at = COALESCE(published_at, ?) WHERE id = ?",
                    (now, row[0]),
                )
            else:
                cur.execute(
                    "INSERT INTO news (title, body, body_html, views_count, status, is_pinned, published_at, created_at) "
                    "VALUES (?, ?, ?, 0, 'active', 1, ?, ?)",
                    (SERVICE_NEWS_TITLE, SERVICE_NEWS_BODY, SERVICE_NEWS_BODY, now, now),
                )

        posts = fetch_channel_posts(40)
        inserted = 0
        for p in posts:
            cur.execute("SELECT id FROM news WHERE tg_msg_id = ?", (p["tg_msg_id"],))
            if cur.fetchone():
                continue
            # published_at normalize
            pub = p.get("published_at") or datetime.now(MSK_TZ).isoformat()
            cur.execute(
                "INSERT INTO news (title, body, body_html, views_count, status, is_pinned, tg_msg_id, published_at, created_at) "
                "VALUES (?, ?, ?, 0, 'active', 0, ?, ?, ?)",
                (
                    p["title"],
                    p["body"],
                    p.get("body_html") or p["body"],
                    p["tg_msg_id"],
                    pub,
                    datetime.now(MSK_TZ).isoformat(),
                ),
            )
            inserted += 1
        conn.commit()
        conn.close()
        if inserted:
            logger.info(f"news sync: +{inserted} posts from @{NEWS_CHANNEL_USERNAME}")
        return inserted
    except Exception as e:
        logger.error(f"sync_news_from_channel: {e}", exc_info=True)
        return 0


def news_sync_loop():
    import time as _time
    _time.sleep(8)
    while True:
        try:
            sync_news_from_channel()
        except Exception as e:
            logger.error(f"news_sync_loop: {e}")
        _time.sleep(300)  # каждые 5 минут



# ==================== FLASK API ДЛЯ MINI APP ====================

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

@app.after_request
def _cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp




def sanitize_files_meta(meta, allow_content=False):
    """Убирает содержимое файлов, если нет права скачивать."""
    if not meta:
        return meta
    try:
        import json as _json
        data = _json.loads(meta) if isinstance(meta, str) else meta
        if not isinstance(data, list):
            return meta if allow_content else None
        if allow_content:
            return meta if isinstance(meta, str) else _json.dumps(data, ensure_ascii=False)
        names = []
        for item in data:
            if isinstance(item, dict):
                names.append({"name": item.get("name") or "file", "size": item.get("size") or 0})
            else:
                names.append({"name": str(item)})
        return _json.dumps(names, ensure_ascii=False)
    except Exception:
        return None if not allow_content else meta

def extract_init_data():
    """Telegram WebView иногда режет кастомные заголовки — берём initData откуда можно."""
    h = request.headers.get("X-Telegram-Init-Data") or request.headers.get("x-telegram-init-data")
    if h:
        return h
    q = request.args.get("initData") or request.args.get("init_data")
    if q:
        return q
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            data = request.get_json(silent=True) or {}
            if data.get("initData"):
                return data.get("initData")
        except Exception:
            pass
    return None

def validate_init_data(init_data: str):
    """Проверяем, что данные реально пришли из Telegram"""
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData", TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if calculated_hash != received_hash:
            return None

        user = json.loads(parsed.get("user", "{}"))
        return user
    except Exception as e:
        logger.error(f"Ошибка валидации initData: {e}")
        return None


@app.route("/api/user", methods=["GET"])
def api_get_user():
    try:
        init_data = request.headers.get("X-Telegram-Init-Data") or request.headers.get("x-telegram-init-data")
        if not init_data:
            return jsonify({"error": "No initData"}), 401

        user = validate_init_data(init_data)
        if not user or not user.get("id"):
            return jsonify({"error": "Invalid initData"}), 401

        user_id = user["id"]
        db_user = get_user(user_id)

        if not db_user:
            add_user(
                user_id,
                user.get("username"),
                user.get("first_name"),
                user.get("last_name")
            )
            db_user = get_user(user_id)

        # всегда обновляем ник/имя/фото из Telegram
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET username = COALESCE(?, username), first_name = COALESCE(?, first_name), "
                "last_name = COALESCE(?, last_name), photo_url = COALESCE(?, photo_url), last_active = ? WHERE user_id = ?",
                (
                    user.get("username") or None,
                    user.get("first_name") or None,
                    user.get("last_name") or None,
                    user.get("photo_url") or None,
                    datetime.now(MSK_TZ).isoformat(),
                    safe_int(user_id),
                ),
            )
            conn.commit()
            conn.close()
            db_user = get_user(user_id) or db_user
        except Exception as _ue:
            logger.error(f"user sync: {_ue}")

        if not db_user:
            return jsonify({
                "user_id": user_id,
                "username": user.get("username"),
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "balance": 0,
                "role": 0,
                "role_name": ROLES.get(0, "Пользователь"),
                "rating_seller": 5.0,
                "rating_buyer": 5.0,
                "rating_staff": None,
                "created_at": None
            })

        role = safe_int(db_user.get("role", 0))
        ban_info = get_ban_info(user_id)
        return jsonify({
            "user_id": db_user["user_id"],
            "username": db_user.get("username"),
            "first_name": db_user.get("first_name"),
            "last_name": db_user.get("last_name"),
            "balance": safe_int(db_user.get("balance", 0)),
            "role": role,
            "role_name": ROLES.get(role, "Неизвестно"),
            "rating_seller": float(db_user.get("rating_seller") or 5.0),
            "rating_buyer": float(db_user.get("rating_buyer") or 5.0),
            "rating_staff": float(db_user.get("rating_staff") or 5.0) if role >= STAFF_MIN_ROLE else None,
            "created_at": db_user.get("created_at"),
            "photo_url": db_user.get("photo_url"),
            "banned": bool(ban_info),
            "ban_info": ban_info or {},
            "username": db_user.get("username") or user.get("username"),
            "first_name": db_user.get("first_name") or user.get("first_name"),
        })
    except Exception as e:
        logger.error(f"api_get_user error: {e}", exc_info=True)
        return jsonify({"error": f"Server error: {e}"}), 500


@app.route("/api/balance", methods=["GET"])
def api_get_balance():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    balance = get_balance(user["id"])
    return jsonify({
        "balance": balance,
        "user_id": user["id"]
    })


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"status": "ok"})



@app.route("/api/requests/<int:request_id>", methods=["GET"])
def api_get_request(request_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    req = get_request_by_id(request_id)
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404
    role = get_user_role(user["id"])
    if safe_int(req["user_id"]) != safe_int(user["id"]) and role < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Нет доступа"}), 403
    return jsonify({"request": req})


@app.route("/api/requests", methods=["GET"])
def api_get_requests():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    reqs = get_user_requests(user["id"], limit=50)
    return jsonify({"requests": reqs})



def send_stars_invoice(user_id, request_id, amount):
    """Отправляет invoice Telegram Stars пользователю в бот."""
    try:
        stars_amount = int(amount) * 100
        prices = [types.LabeledPrice(label=f"Пополнение {int(amount)}$", amount=stars_amount)]
        payload = f"deposit_stars_{request_id}_{user_id}_{int(amount)}"
        bot.send_invoice(
            chat_id=user_id,
            title=f"Пополнение баланса на {int(amount)}$",
            description=(
                f"Оплата {stars_amount} ⭐ Telegram Stars за пополнение баланса на {int(amount)}$ "
                f"(заявка #{request_id})"
            ),
            invoice_payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        bot.send_message(
            user_id,
            (
                f"⭐ <b>Оплата через Telegram Stars</b>\n\n"
                f"Сумма пополнения: <b>{int(amount)}$</b>\n"
                f"К оплате: <b>{stars_amount} Stars</b>\n"
                f"Заявка: <b>#{request_id}</b>\n\n"
                f"Нажмите кнопку оплаты в сообщении выше (invoice).\n"
                f"После успешной оплаты баланс зачислится автоматически."
            ),
            parse_mode="html",
        )
        return True
    except Exception as e:
        logger.error(f"send_stars_invoice error: {e}", exc_info=True)
        return False


@app.route("/api/deposit", methods=["POST"])
def api_create_deposit():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    data = request.json or {}
    amount = safe_int(data.get("amount", 0))
    method = data.get("method", "card")
    comment = data.get("comment", "")

    if amount < 1:
        return jsonify({"error": "Минимальная сумма пополнения — 1$"}), 400

    if method not in ("card", "crypto", "stars"):
        return jsonify({"error": "Неверный способ пополнения"}), 400

    # Stars — заявка + invoice в бот, без ручного «я оплатил»
    if method == "stars":
        request_id = add_request(user["id"], "deposit", amount, "stars", comment)
        sent = send_stars_invoice(user["id"], request_id, amount)
        return jsonify({
            "status": "ok",
            "request_id": request_id,
            "message": "Перейдите в бота для оплаты Telegram Stars",
            "method": "stars",
            "amount": amount,
            "invoice_sent": bool(sent),
            "stars_amount": int(amount) * 100,
        })

    request_id = add_request(user["id"], "deposit", amount, method, comment)

    usd_to_rub = get_usd_to_rub()
    rub_amount = int(amount * usd_to_rub)

    payment_info = {}
    if method == "card":
        payment_info = {
            "phone": "+79991112233",
            "bank": "Т-Банк",
            "recipient_name": "Иванов И.И.",
            "rub_amount": rub_amount
        }
    else:
        payment_info = {
            "wallet": "TSM4p8JjU2AqC7Xqy8Zf9gH3kL5nP2rV6w",
            "network": "USDT TRC20"
        }

    return jsonify({
        "status": "ok",
        "request_id": request_id,
        "amount": amount,
        "method": method,
        "rub_amount": rub_amount,
        "payment_info": payment_info
    })


@app.route("/api/withdraw", methods=["POST"])
def api_create_withdraw():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    data = request.json or {}
    amount = safe_int(data.get("amount", 0))
    method = data.get("method", "card")
    recipient = (data.get("recipient") or "").strip()
    recipient_name = (data.get("recipient_name") or "").strip()
    comment = data.get("comment", "")

    if amount < MIN_WITHDRAW:
        return jsonify({"error": f"Минимальная сумма вывода — {MIN_WITHDRAW}$"}), 400

    balance = get_balance(user["id"])
    if amount > balance:
        return jsonify({"error": f"Недостаточно средств. Баланс: {balance}$"}), 400

    if not recipient:
        return jsonify({"error": "Укажите реквизиты для вывода"}), 400

    if method == "crypto":
        if not re.match(r'^T[A-Za-z0-9]{33}$', recipient):
            return jsonify({"error": "Неверный формат адреса USDT TRC20"}), 400
        comment = comment or "Вывод USDT TRC20"

    # Списываем баланс сразу
    update_balance(user["id"], -amount)

    request_id = add_request(
        user["id"], "withdraw", amount, method, comment, recipient, recipient_name
    )

    if not request_id:
        update_balance(user["id"], amount)  # откат
        return jsonify({"error": "Ошибка создания заявки"}), 500

    add_transaction(user["id"], "withdraw", amount, f"Вывод {amount}$ ({method}) - заявка #{request_id}")
    update_request_status(request_id, "processing")

    # Уведомляем админов
    for admin_id in ADMIN_IDS:
        try:
            admin_msg = (
                f"🆕 <b>Новая заявка на вывод (Mini App)</b>\n\n"
                f"<b>Заявка:</b> #{request_id}\n"
                f"<b>Пользователь:</b> {user['id']}\n"
                f"<b>Сумма:</b> {amount}$\n"
                f"<b>Метод:</b> {'Карта' if method == 'card' else 'Криптовалюта'}\n"
                f"<b>Получатель:</b> {recipient}\n"
            )
            if recipient_name:
                admin_msg += f"<b>Имя:</b> {recipient_name}\n"
            admin_msg += f"\n<i>Используйте /panel для управления</i>"
            bot.send_message(admin_id, admin_msg, parse_mode="html")
        except Exception:
            pass

    return jsonify({
        "status": "ok",
        "request_id": request_id,
        "amount": amount,
        "method": method,
        "new_balance": get_balance(user["id"])
    })


@app.route("/api/request/confirm", methods=["POST"])
def api_confirm_request():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    data = request.json or {}
    request_id = safe_int(data.get("request_id", 0))

    req = get_request_by_id(request_id, user["id"])
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404

    if req["status"] != "pending":
        return jsonify({"error": f"Заявка уже {req['status']}"}), 400

    update_request_status(request_id, "processing")

    for admin_id in ADMIN_IDS:
        try:
            type_emoji = "💰" if req["type"] == "deposit" else "💳"
            type_name = "Пополнение" if req["type"] == "deposit" else "Вывод"
            admin_msg = (
                f"🆕 <b>Заявка #{request_id} (Mini App) требует рассмотрения</b>\n\n"
                f"{type_emoji} <b>Тип:</b> {type_name}\n"
                f"<b>Пользователь:</b> {user['id']}\n"
                f"<b>Сумма:</b> {req['amount']}$\n"
                f"<b>Метод:</b> {req['method']}\n"
            )
            if req.get("comment"):
                admin_msg += f"<b>Комментарий:</b> {req['comment']}\n"
            admin_msg += f"\n<i>Используйте /panel</i>"
            bot.send_message(admin_id, admin_msg, parse_mode="html")
        except Exception:
            pass

    return jsonify({"status": "ok", "request_id": request_id})


@app.route("/api/request/cancel", methods=["POST"])
def api_cancel_request():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    data = request.json or {}
    request_id = safe_int(data.get("request_id", 0))

    req = get_request_by_id(request_id, user["id"])
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404

    if req["status"] != "pending":
        return jsonify({"error": f"Заявка уже {req['status']}"}), 400

    update_request_status(request_id, "cancelled")
    return jsonify({"status": "ok", "request_id": request_id})


@app.route("/api/bots", methods=["GET"])
def api_get_bots():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    bots = get_user_bots(user["id"])
    return jsonify({"bots": bots})


@app.route("/api/transactions", methods=["GET"])
def api_get_transactions():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, type, amount, description, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 50
    ''', (safe_int(user["id"]),))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"transactions": rows})



@app.route("/api/admin/refund-detail/<int:request_id>", methods=["GET"])
def api_admin_refund_detail(request_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    req = get_request_by_id(request_id)
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404
    order_id = safe_int(req.get("recipient") or 0)
    listing_id = safe_int(req.get("recipient_name") or 0)
    order = None
    listing = None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if order_id:
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        o = cur.fetchone()
        order = dict(o) if o else None
    if listing_id:
        cur.execute(
            "SELECT l.*, u.username as seller_username, u.first_name as seller_name "
            "FROM listings l LEFT JOIN users u ON u.user_id = l.seller_id WHERE l.id = ?",
            (listing_id,),
        )
        l = cur.fetchone()
        listing = dict(l) if l else None
    conn.close()
    return jsonify({"request": req, "order": order, "listing": listing})

@app.route("/api/admin/requests", methods=["GET"])
def api_admin_requests():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    role = get_user_role(user["id"])
    if role < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    pending = get_pending_requests(limit=100)
    return jsonify({"requests": pending})


@app.route("/api/admin/approve", methods=["POST"])
def api_admin_approve():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    role = get_user_role(user["id"])
    if role < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    request_id = safe_int(data.get("request_id", 0))
    req = get_request_by_id(request_id)
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404
    if req["status"] != "processing":
        return jsonify({"error": f"Заявка уже {req['status']}"}), 400

    if req["type"] == "deposit":
        update_balance(req["user_id"], req["amount"])
        add_transaction(req["user_id"], "income", req["amount"], f"Пополнение (заявка #{request_id})")
        try:
            bot.send_message(
                req["user_id"],
                f"✅ <b>Заявка #{request_id} одобрена!</b>\n\n"
                f"<b>Баланс пополнен на</b> {req['amount']}$",
                parse_mode="html"
            )
        except Exception:
            pass
    elif req["type"] == "withdraw":
        try:
            msg = (
                f"✅ <b>Заявка #{request_id} одобрена!</b>\n\n"
                f"<b>Сумма вывода:</b> {req['amount']}$\n"
            )
            if req.get("recipient"):
                msg += f"<b>Получатель:</b> {req['recipient']}\n"
            bot.send_message(req["user_id"], msg, parse_mode="html")
        except Exception:
            pass

    
    elif req["type"] == "refund":
        amount = safe_int(req["amount"])
        buyer_id = safe_int(req["user_id"])
        order_id = safe_int(req.get("recipient") or 0)
        listing_id = safe_int(req.get("recipient_name") or 0)
        update_balance(buyer_id, amount)
        add_transaction(buyer_id, "purchase_refund", amount, f"Возврат по сделке #{order_id}")
        now = datetime.now(MSK_TZ).isoformat()
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            if order_id:
                cur.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now, order_id))
            if listing_id:
                cur.execute("UPDATE listings SET status='active', updated_at=? WHERE id=?", (now, listing_id))
            conn.commit()
            conn.close()
        except Exception as _e:
            logger.error(f"refund approve: {_e}")
        try:
            bot.send_message(buyer_id, f"✅ Возврат {amount}$ по сделке #{order_id} выполнен.")
        except Exception:
            pass

    update_request_status(request_id, "completed")
    return jsonify({"status": "ok", "request_id": request_id})


@app.route("/api/admin/reject", methods=["POST"])
def api_admin_reject():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    role = get_user_role(user["id"])
    if role < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    request_id = safe_int(data.get("request_id", 0))
    req = get_request_by_id(request_id)
    if not req:
        return jsonify({"error": "Заявка не найдена"}), 404
    if req["status"] != "processing":
        return jsonify({"error": f"Заявка уже {req['status']}"}), 400

    if req["type"] == "withdraw":
        update_balance(req["user_id"], req["amount"])
        add_transaction(req["user_id"], "refund", req["amount"], f"Возврат (отклонена заявка #{request_id})")

    update_request_status(request_id, "cancelled")
    try:
        bot.send_message(
            req["user_id"],
            f"❌ <b>Заявка #{request_id} отклонена</b>\n\n"
            f"<b>Сумма:</b> {req['amount']}$",
            parse_mode="html"
        )
    except Exception:
        pass

    return jsonify({"status": "ok", "request_id": request_id})


@app.route("/api/job-applications", methods=["POST"])
def api_create_job_application():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    data = request.json or {}
    full_name = (data.get("full_name") or "").strip()
    age = safe_int(data.get("age", 0))
    employment = (data.get("employment") or "").strip()
    username = (data.get("username") or "").strip().lstrip("@")
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()

    allowed_emp = {
        "school": "Школьник",
        "student": "Студент",
        "working": "Работаю",
        "retired": "На пенсии",
        "searching": "Окончил обучение, в активных поисках работы",
    }
    if not full_name:
        return jsonify({"error": "Укажите ФИО"}), 400
    if age < 16:
        return jsonify({"error": "Минимальный возраст — 16 лет"}), 400
    if employment not in allowed_emp:
        return jsonify({"error": "Выберите занятость"}), 400
    if not phone:
        return jsonify({"error": "Укажите номер телефона"}), 400
    if not email or "@" not in email:
        return jsonify({"error": "Укажите корректную почту"}), 400

    add_user(user["id"], user.get("username"), user.get("first_name"), user.get("last_name"))
    now = datetime.now(MSK_TZ).isoformat()
    if not username:
        username = user.get("username") or ""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO job_applications
        (user_id, full_name, age, employment, username, phone, email, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    ''', (user["id"], full_name, age, employment, username, phone, email, now, now))
    app_id = cursor.lastrowid
    conn.commit()
    conn.close()

    emp_label = allowed_emp.get(employment, employment)
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"🆕 <b>Заявка на работу #{app_id}</b>\n\n"
                f"<b>ФИО:</b> {full_name}\n"
                f"<b>Возраст:</b> {age}\n"
                f"<b>Занятость:</b> {emp_label}\n"
                f"<b>Username:</b> @{username or '—'}\n"
                f"<b>Телефон:</b> {phone}\n"
                f"<b>Почта:</b> {email}\n"
                f"<b>TG ID:</b> {user['id']}",
                parse_mode="html"
            )
        except Exception:
            pass

    return jsonify({"status": "ok", "id": app_id})


@app.route("/api/admin/job-applications", methods=["GET"])
def api_admin_job_applications():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    status_filter = (request.args.get("status") or "pending").strip()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if status_filter == "all":
        cursor.execute('''
            SELECT * FROM job_applications
            ORDER BY created_at DESC LIMIT 100
        ''')
    else:
        cursor.execute('''
            SELECT * FROM job_applications
            WHERE status = ?
            ORDER BY created_at ASC LIMIT 100
        ''', (status_filter,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    labels = {
        "school": "Школьник",
        "student": "Студент",
        "working": "Работаю",
        "retired": "На пенсии",
        "searching": "Окончил обучение, в активных поисках работы",
    }
    for r in rows:
        r["employment_label"] = labels.get(r.get("employment"), r.get("employment"))
    return jsonify({"applications": rows})


@app.route("/api/admin/job-applications/<int:app_id>/status", methods=["POST"])
def api_admin_job_application_status(app_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    new_status = (data.get("status") or "").strip()
    if new_status not in ("pending", "accepted", "rejected"):
        return jsonify({"error": "Неверный статус"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM job_applications WHERE id = ?', (app_id,))
    app = cursor.fetchone()
    if not app:
        conn.close()
        return jsonify({"error": "Заявка не найдена"}), 404
    app = dict(app)

    if app.get("status") == "accepted" and new_status == "accepted":
        conn.close()
        return jsonify({"error": "Заявка уже принята"}), 400

    now = datetime.now(MSK_TZ).isoformat()
    hired_role = 3  # Стажер — входная должность (вакансия модератора)
    hired_role_name = ROLES.get(hired_role, "Стажер")

    cursor.execute(
        'UPDATE job_applications SET status = ?, updated_at = ? WHERE id = ?',
        (new_status, now, app_id)
    )

    if new_status == "accepted":
        uid = safe_int(app["user_id"])
        cursor.execute("SELECT user_id, role FROM users WHERE user_id = ?", (uid,))
        urow = cursor.fetchone()
        if not urow:
            uname = (app.get("username") or "").lstrip("@")
            fname = (app.get("full_name") or "").split()[0] if app.get("full_name") else "Сотрудник"
            cursor.execute(
                "INSERT INTO users (user_id, username, first_name, balance, role, created_at, last_active) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (uid, uname, fname, hired_role, now, now),
            )
        else:
            current_role = safe_int(urow["role"])
            if uid not in MANDATORY_USERS:
                if current_role < hired_role or current_role < STAFF_MIN_ROLE:
                    cursor.execute("UPDATE users SET role = ? WHERE user_id = ?", (hired_role, uid))
                else:
                    hired_role = current_role
                    hired_role_name = ROLES.get(hired_role, hired_role_name)

    conn.commit()
    conn.close()

    status_text = {"accepted": "принята", "rejected": "отклонена", "pending": "ожидает"}.get(new_status, new_status)
    try:
        if new_status == "accepted":
            bot.send_message(
                app["user_id"],
                "✅ <b>Заявка на работу #{} принята!</b>\n\nДолжность: <b>{}</b>\nВы отображаетесь в списке сотрудников.\nДобро пожаловать в команду X-Cod!".format(app_id, hired_role_name),
                parse_mode="html",
            )
        else:
            bot.send_message(
                app["user_id"],
                "📋 <b>Заявка на работу #{}</b>\n\nСтатус: <b>{}</b>".format(app_id, status_text),
                parse_mode="html",
            )
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "id": app_id,
        "new_status": new_status,
        "hired_role": hired_role if new_status == "accepted" else None,
        "hired_role_name": hired_role_name if new_status == "accepted" else None,
    })


@app.route("/api/admin/user/balance", methods=["POST"])
def api_admin_set_balance():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    target_id = safe_int(data.get("user_id", 0))
    mode = (data.get("mode") or "set").strip()
    if not target_id:
        return jsonify({"error": "Укажите user_id"}), 400

    target = get_user(target_id)
    if not target:
        return jsonify({"error": "Пользователь не найден"}), 404

    if mode == "set":
        new_balance = safe_int(data.get("balance", 0))
        if new_balance < 0:
            return jsonify({"error": "Баланс не может быть отрицательным"}), 400
        delta = new_balance - safe_int(target["balance"])
        if delta != 0:
            update_balance(target_id, delta)
            add_transaction(target_id, "admin_adjust", abs(delta), f"Изменение баланса админом {user['id']} (set → {new_balance})")
    elif mode == "add":
        amount = safe_int(data.get("amount", 0))
        if amount == 0:
            return jsonify({"error": "Укажите amount"}), 400
        update_balance(target_id, amount)
        add_transaction(target_id, "admin_adjust", abs(amount), f"Изменение баланса админом {user['id']} (add {amount})")
    else:
        return jsonify({"error": "mode: set или add"}), 400

    return jsonify({"status": "ok", "user_id": target_id, "balance": get_balance(target_id)})



def _normalize_listing_category(product_type, listing_type):
    if listing_type == "partner":
        return "Партнер X-Cod"
    pt = (product_type or "").lower()
    if "mini" in pt or "мини" in pt:
        if "сайт" in pt or "site" in pt or "web" in pt:
            return "Мини-апп + сайт"
        return "Мини-аппы"
    if "сайт" in pt or "site" in pt or "web" in pt:
        if "бот" in pt or "bot" in pt:
            return "ТГ боты и сайты"
        return "Сайты"
    if "бот" in pt or "bot" in pt:
        return "ТГ боты"
    if product_type:
        return product_type.strip()[:40]
    return "Другое"


def _pdf_font():
    """Пробуем Unicode-шрифт, иначе Helvetica."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("XCodFont", path))
                return "XCodFont"
        except Exception:
            continue
    return "Helvetica"


def _build_pdf_report(title, period_from, period_to, total, rows, amount_label="Sum", kind="ops"):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    w, h = A4
    font = _pdf_font()
    y = h - 18 * mm

    c.setFont(font, 14)
    c.drawString(15 * mm, y, str(title)[:90])
    y -= 7 * mm
    c.setFont(font, 10)
    c.drawString(15 * mm, y, f"All operations: {period_from} — {period_to}")
    y -= 5 * mm
    c.drawString(15 * mm, y, f"Total: {total} USD  |  Rows: {len(rows)}")
    y -= 8 * mm

    # header
    c.setFont(font, 8)
    headers = ["User ID", "Date / time", "Type", "Amount", "Method", "Requisites"]
    xs = [15 * mm, 35 * mm, 70 * mm, 95 * mm, 115 * mm, 140 * mm]
    for x, head in zip(xs, headers):
        c.drawString(x, y, head)
    y -= 3 * mm
    c.line(15 * mm, y, 195 * mm, y)
    y -= 5 * mm

    c.setFont(font, 7)
    for r in rows[:200]:
        if y < 18 * mm:
            c.showPage()
            y = h - 18 * mm
            c.setFont(font, 8)
            for x, head in zip(xs, headers):
                c.drawString(x, y, head)
            y -= 3 * mm
            c.line(15 * mm, y, 195 * mm, y)
            y -= 5 * mm
            c.setFont(font, 7)

        dt = str(r.get("created_at") or "")[:19].replace("T", " ")
        typ = str(r.get("type") or "")[:16]
        method = str(r.get("method") or "—")[:14]
        req = str(r.get("recipient") or r.get("comment") or r.get("requisites") or "—")[:28]
        vals = [
            str(r.get("user_id", ""))[:12],
            dt,
            typ,
            str(r.get("amount", "")),
            method,
            req,
        ]
        for x, val in zip(xs, vals):
            c.drawString(x, y, val)
        y -= 4.2 * mm

    c.save()
    buffer.seek(0)
    return buffer


@app.route("/api/users/<int:target_id>/profile", methods=["GET"])
def api_user_profile(target_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    db_user = get_user(target_id)
    if not db_user:
        return jsonify({"error": "Пользователь не найден"}), 404

    ban = get_ban_info(target_id)
    viewer_id = safe_int(user["id"])
    # чужой профиль забаненного — без данных, только пометка
    if ban and viewer_id != safe_int(target_id):
        role = safe_int(db_user.get("role") or 0)
        return jsonify({
            "user_id": target_id,
            "banned_profile": True,
            "banned": True,
            "ban_info": ban,
            "message": "Пользователь заблокирован за нарушение правил сообщества",
            "username": None,
            "first_name": None,
            "last_name": None,
            "role": role,
            "role_name": ROLES.get(role, ""),
            "listings": [],
            "count_active": 0,
            "count_completed": 0,
            "count_moderation": 0,
        })

    status = (request.args.get("status") or "active").strip()
    if status not in ("active", "completed", "all"):
        status = "active"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if status == "active":
        cursor.execute(
            "SELECT id, title, description, price, status, product_type, listing_type, bot_username, created_at "
            "FROM listings WHERE seller_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 100",
            (target_id,)
        )
    elif status == "completed":
        cursor.execute(
            "SELECT id, title, description, price, status, product_type, listing_type, bot_username, created_at "
            "FROM listings WHERE seller_id = ? AND status IN ('sold', 'completed', 'closed', 'unpublished') "
            "ORDER BY created_at DESC LIMIT 100",
            (target_id,)
        )
    else:
        cursor.execute(
            "SELECT id, title, description, price, status, product_type, listing_type, bot_username, created_at "
            "FROM listings WHERE seller_id = ? AND status != 'deleted' ORDER BY created_at DESC LIMIT 100",
            (target_id,)
        )
    listings = [dict(r) for r in cursor.fetchall()]
    cursor.execute(
        "SELECT "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as cnt_active, "
        "SUM(CASE WHEN status IN ('sold','completed','closed','unpublished','blocked') THEN 1 ELSE 0 END) as cnt_completed, "
        "SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as cnt_moderation "
        "FROM listings WHERE seller_id = ?",
        (target_id,)
    )
    counts = cursor.fetchone()
    conn.close()

    role = db_user["role"]
    payload = {
        "user_id": db_user["user_id"],
        "username": db_user.get("username"),
        "first_name": db_user.get("first_name"),
        "last_name": db_user.get("last_name"),
        "role": role,
        "role_name": ROLES.get(role, "Неизвестно"),
        "photo_url": db_user.get("photo_url"),
        "rating_seller": float(db_user.get("rating_seller") or 5.0),
        "rating_buyer": float(db_user.get("rating_buyer") or 5.0),
        "count_active": int(counts["cnt_active"] or 0) if counts else 0,
        "count_completed": int(counts["cnt_completed"] or 0) if counts else 0,
        "count_moderation": int(counts["cnt_moderation"] or 0) if counts else 0,
        "listings": listings,
    }
    if role >= STAFF_MIN_ROLE:
        payload["rating_staff"] = float(db_user.get("rating_staff") or 5.0)
    return jsonify(payload)


@app.route("/api/admin/analytics/summary", methods=["GET"])
def api_admin_analytics_summary():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    kind = (request.args.get("kind") or "expenses").strip()
    if kind == "listings" and get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Аналитика объявлений доступна админам и выше"}), 403

    kind = (request.args.get("kind") or "expenses").strip()  # expenses|income|listings
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    if not date_from:
        date_from = "2000-01-01"
    if not date_to:
        date_to = "2099-12-31"
    # inclusive end of day
    date_to_q = date_to + "T23:59:59"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if kind == "expenses":
        cursor.execute(
            "SELECT id, user_id, type, amount, method, status, created_at, recipient, comment "
            "FROM requests WHERE type = 'withdraw' AND status = 'completed' "
            "AND created_at >= ? AND created_at <= ? ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        total = sum(safe_int(r["amount"]) for r in rows)
        conn.close()
        return jsonify({"kind": kind, "from": date_from, "to": date_to, "total": total, "count": len(rows), "items": rows})

    if kind == "income":
        cursor.execute(
            "SELECT id, user_id, type, amount, method, status, created_at, comment "
            "FROM requests WHERE type = 'deposit' AND status = 'completed' "
            "AND created_at >= ? AND created_at <= ? ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        total = sum(safe_int(r["amount"]) for r in rows)
        conn.close()
        return jsonify({"kind": kind, "from": date_from, "to": date_to, "total": total, "count": len(rows), "items": rows})

    # listings categories
    cursor.execute(
        "SELECT product_type, listing_type, status FROM listings "
        "WHERE created_at >= ? AND created_at <= ?",
        (date_from, date_to_q)
    )
    cats = {}
    for r in cursor.fetchall():
        cat = _normalize_listing_category(r["product_type"], r["listing_type"])
        cats[cat] = cats.get(cat, 0) + 1
    conn.close()
    chart = [{"name": k, "value": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])]
    return jsonify({"kind": "listings", "from": date_from, "to": date_to, "total": sum(cats.values()), "categories": chart})


@app.route("/api/admin/analytics/report.pdf", methods=["GET"])
def api_admin_analytics_pdf():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    kind = (request.args.get("kind") or request.json.get("kind") if request.json else None) or "expenses"
    kind = str(kind).strip()
    if kind == "listings" and get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Аналитика объявлений доступна админам и выше"}), 403

    kind = (request.args.get("kind") or "expenses").strip()
    date_from = (request.args.get("from") or "2000-01-01").strip()
    date_to = (request.args.get("to") or "2099-12-31").strip()
    date_to_q = date_to + "T23:59:59"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if kind == "listings":
        cursor.execute(
            "SELECT id, seller_id as user_id, product_type, listing_type, price as amount, status, created_at "
            "FROM listings WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            d["type"] = _normalize_listing_category(d.get("product_type"), d.get("listing_type"))
            d["method"] = d.get("status") or "—"
            d["recipient"] = d.get("product_type") or "—"
            rows.append(d)
        total = len(rows)
        title = f"X-Cod: Listings analytics ({date_from} - {date_to})"
        amount_label = "Price"
    elif kind == "income":
        cursor.execute(
            "SELECT id, user_id, type, amount, method, recipient, comment, created_at FROM requests "
            "WHERE type = 'deposit' AND status = 'completed' AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            r["type"] = "deposit"
        total = sum(safe_int(r["amount"]) for r in rows)
        title = f"X-Cod: Income / deposits ({date_from} - {date_to})"
        amount_label = "Amount USD"
    else:
        cursor.execute(
            "SELECT id, user_id, type, amount, method, recipient, comment, created_at FROM requests "
            "WHERE type = 'withdraw' AND status = 'completed' AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            r["type"] = "withdraw"
        total = sum(safe_int(r["amount"]) for r in rows)
        title = f"X-Cod: Expenses / withdrawals ({date_from} - {date_to})"
        amount_label = "Amount USD"
    conn.close()

    pdf = _build_pdf_report(title, date_from, date_to, total, rows, amount_label, kind=kind)
    return send_file(
        pdf,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"xcod_{kind}_{date_from}_{date_to}.pdf"
    )



@app.route("/api/admin/analytics/report.send", methods=["POST"])
def api_admin_analytics_pdf_send():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.json or {}
    kind = (data.get("kind") or request.args.get("kind") or "expenses")
    kind = str(kind).strip()
    if kind == "listings" and get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Аналитика объявлений доступна админам и выше"}), 403

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or request.args.get("kind") or "expenses").strip()
    date_from = (data.get("from") or request.args.get("from") or "2000-01-01").strip()
    date_to = (data.get("to") or request.args.get("to") or "2099-12-31").strip()
    date_to_q = date_to + "T23:59:59"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    if kind == "listings":
        cursor.execute(
            "SELECT id, seller_id as user_id, product_type, listing_type, price as amount, status, created_at "
            "FROM listings WHERE created_at >= ? AND created_at <= ? ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = []
        for r in cursor.fetchall():
            d = dict(r)
            d["type"] = _normalize_listing_category(d.get("product_type"), d.get("listing_type"))
            d["method"] = d.get("status") or "—"
            d["recipient"] = d.get("product_type") or "—"
            rows.append(d)
        total = len(rows)
        title = f"X-Cod: Listings analytics ({date_from} - {date_to})"
    elif kind == "income":
        cursor.execute(
            "SELECT id, user_id, type, amount, method, recipient, comment, created_at FROM requests "
            "WHERE type = 'deposit' AND status = 'completed' AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            r["type"] = "deposit"
        total = sum(safe_int(r["amount"]) for r in rows)
        title = f"X-Cod: Income / deposits ({date_from} - {date_to})"
    else:
        kind = "expenses"
        cursor.execute(
            "SELECT id, user_id, type, amount, method, recipient, comment, created_at FROM requests "
            "WHERE type = 'withdraw' AND status = 'completed' AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC",
            (date_from, date_to_q)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            r["type"] = "withdraw"
        total = sum(safe_int(r["amount"]) for r in rows)
        title = f"X-Cod: Expenses / withdrawals ({date_from} - {date_to})"
    conn.close()

    pdf = _build_pdf_report(title, date_from, date_to, total, rows, kind=kind)
    filename = f"xcod_{kind}_{date_from}_{date_to}.pdf"

    try:
        pdf.seek(0)
        try:
            from telebot.types import InputFile
            doc = InputFile(pdf, file_name=filename)
        except Exception:
            doc = pdf
        bot.send_document(
            user["id"],
            doc,
            caption="📄 Файл готов к скачиванию"
        )
    except Exception as e:
        logger.error(f"send pdf error: {e}", exc_info=True)
        return jsonify({"error": f"Не удалось отправить файл: {e}"}), 500

    return jsonify({"ok": True, "filename": filename})





@app.route("/api/admin/active-restrictions", methods=["GET"])
def api_active_restrictions():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    kind = (request.args.get("type") or "ban").strip()
    if kind not in ("ban", "publish"):
        return jsonify({"error": "type=ban|publish"}), 400
    now = datetime.now(MSK_TZ)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    ensure_user_moderation_schema(cur)
    conn.commit()
    if kind == "ban":
        cur.execute(
            """
            SELECT m.user_id, m.ban_reason as reason, m.ban_until as until_at, m.moderated_by, m.updated_at,
                   u.username, u.first_name, u.last_name
            FROM user_moderation m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE COALESCE(m.is_banned, 0) = 1
            ORDER BY m.updated_at DESC
            LIMIT 200
            """
        )
    else:
        cur.execute(
            """
            SELECT m.user_id, m.publish_reason as reason, m.publish_blocked_until as until_at, m.moderated_by, m.updated_at,
                   u.username, u.first_name, u.last_name
            FROM user_moderation m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.publish_blocked_until IS NOT NULL AND TRIM(m.publish_blocked_until) != ''
            ORDER BY m.updated_at DESC
            LIMIT 200
            """
        )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        until = d.get("until_at")
        # filter expired
        if until:
            try:
                ts = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=MSK_TZ)
                if ts < now:
                    continue
            except Exception:
                pass
        uname = d.get("username")
        fname = ((d.get("first_name") or "") + " " + (d.get("last_name") or "")).strip()
        d["display_name"] = ("@" + uname) if uname else (fname or str(d["user_id"]))
        d["kind"] = kind
        d["kind_label"] = "Блокировка аккаунта" if kind == "ban" else "Запрет публикации"
        rows.append(d)
    conn.close()
    return jsonify({"items": rows, "type": kind})


@app.route("/api/admin/moderation-actions", methods=["GET"])
def api_moderation_actions():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    ensure_mod_actions_schema(cur)
    conn.commit()
    cur.execute(
        """
        SELECT a.id, a.actor_id, a.target_id, a.action_type, a.days, a.reason, a.created_at,
               ua.username as actor_username, ua.first_name as actor_first, ua.last_name as actor_last, ua.role as actor_role,
               ut.username as target_username, ut.first_name as target_first, ut.last_name as target_last
        FROM moderation_actions a
        LEFT JOIN users ua ON ua.user_id = a.actor_id
        LEFT JOIN users ut ON ut.user_id = a.target_id
        ORDER BY a.id DESC LIMIT 200
        """
    )
    rows = []
    labels = {
        "restrict_publish": "Запрет публикации объявлений",
        "ban": "Блокировка аккаунта",
        "lift_publish": "Досрочное снятие запрета публикации",
        "lift_ban": "Досрочное снятие блокировки",
    }
    for r in cur.fetchall():
        d = dict(r)
        d["action_label"] = labels.get(d.get("action_type"), d.get("action_type"))
        d["actor_role_name"] = ROLES.get(safe_int(d.get("actor_role")), "—")
        d["actor_name"] = ((d.get("actor_first") or "") + " " + (d.get("actor_last") or "")).strip() or (d.get("actor_username") and ("@" + d["actor_username"])) or str(d["actor_id"])
        d["target_name"] = ((d.get("target_first") or "") + " " + (d.get("target_last") or "")).strip() or (d.get("target_username") and ("@" + d["target_username"])) or str(d["target_id"])
        rows.append(d)
    conn.close()
    return jsonify({"actions": rows})


@app.route("/api/admin/users/<int:target_id>/history/<kind>", methods=["GET"])
def api_admin_user_history(target_id, kind):
    """История пользователя для модераторов: transactions|listings|requests|orders."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    items = []
    if kind == "transactions":
        try:
            cur.execute(
                "SELECT id, type, amount, description, created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 100",
                (target_id,),
            )
            items = [dict(r) for r in cur.fetchall()]
        except Exception:
            items = []
    elif kind == "listings":
        cur.execute(
            "SELECT id, title, price, status, created_at FROM listings WHERE seller_id=? AND status!='deleted' ORDER BY id DESC LIMIT 100",
            (target_id,),
        )
        items = [dict(r) for r in cur.fetchall()]
    elif kind == "requests":
        try:
            cur.execute(
                "SELECT id, type, amount, status, method, created_at FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 100",
                (target_id,),
            )
            items = [dict(r) for r in cur.fetchall()]
        except Exception:
            items = []
    elif kind == "orders":
        try:
            cur.execute(
                "SELECT id, listing_id, buyer_id, seller_id, amount, status, created_at FROM orders WHERE buyer_id=? OR seller_id=? ORDER BY id DESC LIMIT 100",
                (target_id, target_id),
            )
            items = [dict(r) for r in cur.fetchall()]
        except Exception:
            items = []
    else:
        conn.close()
        return jsonify({"error": "Неизвестный тип истории"}), 400
    conn.close()
    return jsonify({"items": items, "kind": kind, "user_id": target_id})



@app.route("/api/admin/duty-status", methods=["GET"])
def api_duty_status():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    role = get_user_role(uid)
    on_duty = True if role >= 6 else user_on_duty_shift(uid)
    return jsonify({"on_duty": on_duty, "role": role})



@app.route("/api/admin/user/lift-punishment", methods=["POST"])
def api_lift_punishment():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    actor_role = get_user_role(user["id"])
    if actor_role < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    if actor_role < 6 and not user_on_duty_shift(user["id"]):
        return jsonify({"error": "Вы не на смене, нет доступа к разделу", "code": "not_on_shift"}), 403
    data = request.get_json(silent=True) or {}
    target_id = safe_int(data.get("user_id"))
    action_type = (data.get("type") or data.get("action_type") or "").strip()
    action_id = safe_int(data.get("action_id") or 0)
    if not target_id:
        return jsonify({"error": "Укажите user_id"}), 400
    target_role = get_user_role(target_id)
    if target_role >= actor_role:
        return jsonify({"error": "Нельзя снять наказание с пользователя равной или высшей роли"}), 403
    if action_id and not action_type:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            ensure_mod_actions_schema(cur)
            cur.execute("SELECT action_type FROM moderation_actions WHERE id=?", (action_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                action_type = row["action_type"]
        except Exception:
            pass
    if action_type not in ("restrict_publish", "ban", "publish", "block"):
        return jsonify({"error": "Неизвестный тип наказания"}), 400
    now = datetime.now(MSK_TZ).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE user_moderation ADD COLUMN ban_until TEXT")
    except Exception:
        pass
    if action_type in ("restrict_publish", "publish"):
        cur.execute(
            "INSERT INTO user_moderation (user_id, publish_blocked_until, publish_reason, is_banned, moderated_by, updated_at) "
            "VALUES (?, NULL, NULL, 0, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET publish_blocked_until=NULL, publish_reason=NULL, "
            "moderated_by=excluded.moderated_by, updated_at=excluded.updated_at",
            (target_id, user["id"], now),
        )
        label = "Запрет публикации снят досрочно"
    else:
        cur.execute(
            "INSERT INTO user_moderation (user_id, is_banned, ban_reason, ban_until, moderated_by, updated_at) "
            "VALUES (?, 0, NULL, NULL, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_banned=0, ban_reason=NULL, ban_until=NULL, "
            "moderated_by=excluded.moderated_by, updated_at=excluded.updated_at",
            (target_id, user["id"], now),
        )
        label = "Блокировка снята досрочно"
    conn.commit()
    conn.close()
    log_moderation_action(user["id"], target_id, "lift_" + ("publish" if action_type in ("restrict_publish", "publish") else "ban"), 0, label)
    try:
        bot.send_message(target_id, f"✅ {label}.")
    except Exception:
        pass
    return jsonify({"ok": True, "message": label})



@app.route("/api/admin/users/<int:target_id>/ban-history", methods=["GET"])
def api_user_ban_history(target_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    ensure_mod_actions_schema(cur)
    conn.commit()
    cur.execute(
        """
        SELECT a.id, a.actor_id, a.target_id, a.action_type, a.days, a.reason, a.created_at,
               ua.username as actor_username, ua.first_name as actor_first
        FROM moderation_actions a
        LEFT JOIN users ua ON ua.user_id = a.actor_id
        WHERE a.target_id = ?
          AND a.action_type IN ('ban', 'restrict_publish', 'lift_ban', 'lift_publish')
        ORDER BY a.id DESC LIMIT 100
        """,
        (safe_int(target_id),),
    )
    labels = {
        "restrict_publish": "Запрет публикации объявлений",
        "ban": "Блокировка аккаунта",
        "lift_publish": "Снятие запрета публикации",
        "lift_ban": "Снятие блокировки",
    }
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        d["action_label"] = labels.get(d.get("action_type"), d.get("action_type"))
        d["actor_name"] = (d.get("actor_username") and ("@" + d["actor_username"])) or (d.get("actor_first") or str(d["actor_id"]))
        rows.append(d)
    conn.close()
    return jsonify({"actions": rows})


@app.route("/api/admin/user/restrict-publish", methods=["POST"])
def api_restrict_publish():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    actor_role = get_user_role(user["id"])
    if actor_role < 4:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.get_json(silent=True) or {}
    target_id = safe_int(data.get("user_id"))
    days = safe_int(data.get("days", 0))
    reason = (data.get("reason") or "").strip()
    if not target_id or days < 1:
        return jsonify({"error": "Укажите user_id и срок в днях"}), 400
    target_role = get_user_role(target_id)
    if target_role >= actor_role:
        return jsonify({"error": "Нельзя ограничить пользователя с равной или высшей ролью"}), 403
    if actor_role < 6 and not user_on_duty_shift(user["id"]):
        return jsonify({"error": "Вы не на смене, нет доступа к разделу", "code": "not_on_shift"}), 403
    from datetime import timedelta
    now_dt = datetime.now(MSK_TZ)
    until = (now_dt + timedelta(days=days)).isoformat()
    now = now_dt.isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    ensure_user_moderation_schema(cur)
    cur.execute("SELECT user_id FROM user_moderation WHERE user_id = ?", (target_id,))
    if cur.fetchone():
        cur.execute(
            "UPDATE user_moderation SET publish_blocked_until=?, publish_reason=?, moderated_by=?, updated_at=? WHERE user_id=?",
            (until, reason, user["id"], now, target_id),
        )
    else:
        cur.execute(
            "INSERT INTO user_moderation (user_id, publish_blocked_until, publish_reason, is_banned, moderated_by, updated_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (target_id, until, reason, user["id"], now),
        )
    # активные объявления -> blocked (в завершённые; после снятия ограничения нельзя опубликовать снова)
    cur.execute(
        "UPDATE listings SET status = 'blocked', updated_at = ? WHERE seller_id = ? AND status = 'active'",
        (now, target_id),
    )
    blocked_n = cur.rowcount
    # активные объявления → blocked (в завершённые, после снятия ограничения только удаление)
    cur.execute(
        "UPDATE listings SET status='blocked', updated_at=? WHERE seller_id=? AND status='active'",
        (now, target_id),
    )
    blocked_n = cur.rowcount
    conn.commit()
    conn.close()
    try:
        bot.send_message(
            target_id,
            f"Публикация ограничена на {days} дн. Причина: {reason or '-'}"
            + (f"\nАктивных объявлений переведено в «заблокировано»: {blocked_n}" if blocked_n else ""),
        )
    except Exception:
        pass
    log_moderation_action(user["id"], target_id, "restrict_publish", days, reason)
    return jsonify({"ok": True, "until": until, "listings_blocked": blocked_n})


@app.route("/api/admin/user/ban", methods=["POST"])
def api_ban_user():
    try:
        init_data = extract_init_data()
        if not init_data:
            return jsonify({"error": "No initData"}), 401
        user = validate_init_data(init_data)
        if not user:
            return jsonify({"error": "Invalid initData"}), 401
        actor_role = get_user_role(user["id"])
        if actor_role < ROLE_CHANGE_ROLE:
            return jsonify({"error": "Недостаточно прав"}), 403
        data = request.get_json(silent=True) or request.json or {}
        target_id = safe_int(data.get("user_id"))
        reason = (data.get("reason") or "").strip()
        days = safe_int(data.get("days", 0))
        if not target_id:
            return jsonify({"error": "Укажите user_id"}), 400
        if days < 1:
            return jsonify({"error": "Укажите срок в днях"}), 400
        target_role = get_user_role(target_id)
        if target_role >= actor_role:
            return jsonify({"error": "Нельзя заблокировать пользователя с равной или высшей ролью"}), 403
        if target_id in MANDATORY_USERS:
            return jsonify({"error": "Нельзя блокировать обязательного пользователя"}), 403
        if actor_role < 6 and not user_on_duty_shift(user["id"]):
            return jsonify({"error": "Вы не на смене, нет доступа к разделу", "code": "not_on_shift"}), 403

        from datetime import timedelta
        now = datetime.now(MSK_TZ)
        until = (now + timedelta(days=days)).isoformat()
        now_s = now.isoformat()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        ensure_user_moderation_schema(cur)
        # upsert без ON CONFLICT на случай старой схемы
        cur.execute("SELECT user_id FROM user_moderation WHERE user_id = ?", (target_id,))
        if cur.fetchone():
            cur.execute(
                "UPDATE user_moderation SET is_banned=1, ban_reason=?, ban_until=?, moderated_by=?, updated_at=? WHERE user_id=?",
                (reason, until, user["id"], now_s, target_id),
            )
        else:
            cur.execute(
                "INSERT INTO user_moderation (user_id, is_banned, ban_reason, ban_until, moderated_by, updated_at) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (target_id, reason, until, user["id"], now_s),
            )
        # активные объявления → завершённые (после разбана можно выставить снова)
        cur.execute(
            "UPDATE listings SET status='completed', updated_at=? WHERE seller_id=? AND status='active'",
            (now_s, target_id),
        )
        completed_n = cur.rowcount
        conn.commit()
        conn.close()

        try:
            bot.send_message(
                target_id,
                "Аккаунт заблокирован на {} дн. Причина: {}".format(days, reason or "-"),
            )
        except Exception:
            pass
        try:
            log_moderation_action(user["id"], target_id, "ban", days, reason)
        except Exception as e:
            logger.error("log ban action: %s", e)
        return jsonify({"ok": True, "until": until})
    except Exception as e:
        logger.error("api_ban_user: %s", e, exc_info=True)
        return jsonify({"error": "Ошибка блокировки: " + str(e)}), 500


@app.route("/api/orders", methods=["GET"])
def api_get_orders():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT o.*, l.title as listing_title, l.description as listing_description, "
        "l.has_private_files, l.files_meta, l.listing_type, l.contract_number, "
        "l.product_type, l.bot_username, l.tgstat_url, "
        "l.seller_id as listing_seller_id, "
        "bu.username as buyer_username, su.username as seller_username, "
        "bu.user_id as buyer_uid "
        "FROM orders o "
        "LEFT JOIN listings l ON l.id = o.listing_id "
        "LEFT JOIN users bu ON bu.user_id = o.buyer_id "
        "LEFT JOIN users su ON su.user_id = o.seller_id "
        "WHERE o.buyer_id = ? OR o.seller_id = ? "
        "ORDER BY o.created_at DESC LIMIT 100",
        (uid, uid),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"orders": rows})


@app.route("/api/orders", methods=["POST"])
def api_create_order():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if is_user_banned(user["id"]):
        return jsonify({"error": "Аккаунт заблокирован"}), 403
    data = request.get_json(silent=True) or {}
    listing_id = safe_int(data.get("listing_id"))
    if not listing_id:
        return jsonify({"error": "Укажите listing_id"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
    listing = cur.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404
    listing = dict(listing)
    if listing.get("status") != "active":
        conn.close()
        return jsonify({"error": "Объявление недоступно"}), 400
    seller_id = safe_int(listing["seller_id"])
    buyer_id = safe_int(user["id"])
    if seller_id == buyer_id:
        conn.close()
        return jsonify({"error": "Нельзя купить своё объявление"}), 400
    amount = safe_int(listing["price"])
    if amount <= 0:
        conn.close()
        return jsonify({"error": "Некорректная цена"}), 400
    bal = get_balance(buyer_id)
    if bal < amount:
        conn.close()
        return jsonify({"error": f"Недостаточно средств. Баланс: {bal}$"}), 400
    update_balance(buyer_id, -amount)
    add_transaction(buyer_id, "purchase_hold", amount, f"Покупка объявления #{listing_id} (холд)")
    now_dt = datetime.now(MSK_TZ)
    now = now_dt.isoformat()
    deadline = (now_dt + __import__("datetime").timedelta(minutes=120)).isoformat()
    cur.execute(
        "INSERT INTO orders (listing_id, buyer_id, seller_id, amount, status, seller_sent_at, review_deadline, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'buyer_review', ?, ?, ?, ?)",
        (listing_id, buyer_id, seller_id, amount, now, deadline, now, now),
    )
    order_id = cur.lastrowid
    cur.execute("UPDATE listings SET status = 'sold', updated_at = ? WHERE id = ?", (now, listing_id))
    conn.commit()
    conn.close()
    try:
        bot.send_message(
            seller_id,
            f"Новый заказ #{order_id}. Объявление: {listing.get('title')}. Сумма: {amount}$. "
            f"Покупатель получил файлы с площадки. Ожидайте подтверждения (120 мин).",
        )
        bot.send_message(
            buyer_id,
            f"Заказ #{order_id} создан. Файлы доступны в мини-аппе. На проверку 120 минут. "
            f"{amount}$ удерживаются до завершения.",
        )
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "status": "buyer_review",
        "review_deadline": deadline,
        "balance": get_balance(buyer_id),
        "has_private_files": bool(listing.get("has_private_files")),
        "files_meta": listing.get("files_meta"),
    })



@app.route("/api/orders/<int:order_id>/cancel", methods=["POST"])
def api_order_cancel(order_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Заказ не найден"}), 404
    order = dict(order)
    uid = safe_int(user["id"])
    if uid not in (safe_int(order["buyer_id"]), safe_int(order["seller_id"])):
        conn.close()
        return jsonify({"error": "Нет доступа"}), 403
    if order["status"] not in ("awaiting_seller", "buyer_review"):
        conn.close()
        return jsonify({"error": "Отмена недоступна"}), 400
    amount = safe_int(order["amount"])
    # возврат покупателю
    update_balance(safe_int(order["buyer_id"]), amount)
    add_transaction(safe_int(order["buyer_id"]), "purchase_refund", amount, f"Отмена заказа #{order_id}")
    now = datetime.now(MSK_TZ).isoformat()
    cur.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now, order_id))
    # объявление снова активно
    cur.execute("UPDATE listings SET status='active', updated_at=? WHERE id=?", (now, order["listing_id"]))
    conn.commit()
    conn.close()
    other = safe_int(order["seller_id"]) if uid == safe_int(order["buyer_id"]) else safe_int(order["buyer_id"])
    try:
        bot.send_message(other, f"Заказ #{order_id} отменён. Объявление снова опубликовано.")
        bot.send_message(uid, f"Заказ #{order_id} отменён. Средства возвращены покупателю.")
    except Exception:
        pass
    return jsonify({"ok": True, "status": "cancelled"})


@app.route("/api/orders/<int:order_id>/seller-sent", methods=["POST"])
def api_order_seller_sent(order_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Заказ не найден"}), 404
    order = dict(order)
    if safe_int(order["seller_id"]) != safe_int(user["id"]):
        conn.close()
        return jsonify({"error": "Только продавец"}), 403
    if order["status"] != "awaiting_seller":
        conn.close()
        return jsonify({"error": "Неверный статус заказа"}), 400
    now = datetime.now(MSK_TZ)
    deadline = (now + __import__("datetime").timedelta(minutes=120)).isoformat()
    cur.execute(
        "UPDATE orders SET status='buyer_review', seller_sent_at=?, review_deadline=?, updated_at=? WHERE id=?",
        (now.isoformat(), deadline, now.isoformat(), order_id),
    )
    conn.commit()
    conn.close()
    try:
        bot.send_message(order["buyer_id"], f"Продавец отправил файлы по заказу #{order_id}. У вас 120 минут на проверку.")
    except Exception:
        pass
    return jsonify({"ok": True, "status": "buyer_review", "review_deadline": deadline})


@app.route("/api/orders/<int:order_id>/buyer-confirm", methods=["POST"])
def api_order_buyer_confirm(order_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Заказ не найден"}), 404
    order = dict(order)
    if safe_int(order["buyer_id"]) != safe_int(user["id"]):
        conn.close()
        return jsonify({"error": "Только покупатель"}), 403
    if order["status"] != "buyer_review":
        conn.close()
        return jsonify({"error": "Неверный статус"}), 400
    amount = safe_int(order["amount"])
    seller_id = safe_int(order["seller_id"])
    update_balance(seller_id, amount)
    add_transaction(seller_id, "purchase_release", amount, f"Продажа, заказ #{order_id}")
    now = datetime.now(MSK_TZ).isoformat()
    cur.execute("UPDATE orders SET status='completed', updated_at=? WHERE id=?", (now, order_id))
    conn.commit()
    conn.close()
    try:
        bot.send_message(seller_id, f"Заказ #{order_id} завершён. +{amount}$. Оцените покупателя в мини-аппе.")
        bot.send_message(user["id"], f"Заказ #{order_id} завершён. Оцените продавца в мини-аппе.")
    except Exception:
        pass
    return jsonify({"ok": True, "status": "completed"})


@app.route("/api/orders/<int:order_id>/buyer-reject", methods=["POST"])
def api_order_buyer_reject(order_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Заказ не найден"}), 404
    order = dict(order)
    if safe_int(order["buyer_id"]) != safe_int(user["id"]):
        conn.close()
        return jsonify({"error": "Только покупатель"}), 403
    if order["status"] not in ("buyer_review", "awaiting_seller"):
        conn.close()
        return jsonify({"error": "Неверный статус"}), 400
    data = request.json or {}
    reject_comment = (data.get("comment") or data.get("reject_comment") or "").strip()
    if not reject_comment or len(reject_comment) < 3:
        conn.close()
        return jsonify({"error": "Укажите комментарий: что не подошло"}), 400
    now = datetime.now(MSK_TZ).isoformat()
    try:
        cur.execute(
            "UPDATE orders SET status='refund_moderation', reject_comment=?, updated_at=? WHERE id=?",
            (reject_comment, now, order_id),
        )
    except Exception:
        cur.execute("UPDATE orders SET status='refund_moderation', updated_at=? WHERE id=?", (now, order_id))
    cur.execute("UPDATE listings SET status='refund_moderation', updated_at=? WHERE id=?", (now, order["listing_id"]))
    conn.commit()
    conn.close()
    try:
        rid = add_request(
            safe_int(order["buyer_id"]),
            "refund",
            safe_int(order["amount"]),
            method="order",
            comment=reject_comment,
            recipient=str(order_id),
            recipient_name=str(order["listing_id"]),
        )
        # сразу в processing чтобы попала в панель
        conn2 = sqlite3.connect(DB_PATH)
        cur2 = conn2.cursor()
        cur2.execute("UPDATE requests SET status='processing', updated_at=? WHERE id=?", (now, rid))
        conn2.commit()
        conn2.close()
    except Exception as _re:
        logger.error(f"refund request: {_re}")
        rid = None
    try:
        bot.send_message(
            order["seller_id"],
            f"Покупатель отклонил файлы по сделке #{order_id}. Заявка на возврат отправлена в модерацию транзакций.",
        )
        bot.send_message(
            order["buyer_id"],
            f"Заявка на возврат по #{order_id} отправлена модераторам.",
        )
    except Exception:
        pass
    return jsonify({"ok": True, "status": "refund_moderation", "request_id": rid})


@app.route("/api/orders/<int:order_id>/rate", methods=["POST"])
def api_order_rate(order_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    data = request.get_json(silent=True) or {}
    stars = safe_int(data.get("stars", 0))
    if stars < 1 or stars > 5:
        return jsonify({"error": "Оценка от 1 до 5"}), 400
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"error": "Заказ не найден"}), 404
    order = dict(order)
    if order["status"] != "completed":
        conn.close()
        return jsonify({"error": "Оценка после завершения"}), 400
    uid = safe_int(user["id"])
    now = datetime.now(MSK_TZ).isoformat()
    if uid == safe_int(order["buyer_id"]):
        cur.execute("UPDATE orders SET buyer_rating=?, updated_at=? WHERE id=?", (stars, now, order_id))
        target = safe_int(order["seller_id"])
        field = "rating_seller"
    elif uid == safe_int(order["seller_id"]):
        cur.execute("UPDATE orders SET seller_rating=?, updated_at=? WHERE id=?", (stars, now, order_id))
        target = safe_int(order["buyer_id"])
        field = "rating_buyer"
    else:
        conn.close()
        return jsonify({"error": "Нет доступа"}), 403
    try:
        cur.execute(f"SELECT COALESCE({field}, 5.0) as r FROM users WHERE user_id = ?", (target,))
        row = cur.fetchone()
        old = float(row["r"]) if row else 5.0
        new_r = round((old * 4 + stars) / 5.0, 2)
        cur.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (new_r, target))
    except Exception:
        new_r = stars
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "new_rating": new_r})


@app.route("/api/admin/staff", methods=["GET"])
def api_admin_staff():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    role = get_user_role(user["id"])
    if role < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    staff = get_staff_users(limit=100)
    for s in staff:
        s["role_name"] = ROLES.get(s["role"], "Неизвестно")
    return jsonify({"staff": staff})


@app.route("/api/admin/staff", methods=["POST"])
def api_admin_add_staff():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    admin_role = get_user_role(user["id"])
    if admin_role < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    target_id = safe_int(data.get("user_id", 0))
    new_role = safe_int(data.get("role", 1))

    if not target_id:
        return jsonify({"error": "Укажите ID пользователя"}), 400

    if new_role < STAFF_MIN_ROLE:
        return jsonify({"error": "Минимальная роль сотрудника — Партнер (1)"}), 400

    if not can_change_role(user["id"], new_role):
        return jsonify({"error": "Нельзя назначить эту роль"}), 403

    if target_id in MANDATORY_USERS:
        return jsonify({"error": "Нельзя изменить роль обязательного пользователя"}), 403

    existing = get_user(target_id)
    if not existing:
        add_user(target_id, "", "Сотрудник", "")

    ok = set_user_role(target_id, new_role, user["id"])
    if not ok:
        return jsonify({"error": "Не удалось назначить роль"}), 400

    try:
        bot.send_message(
            target_id,
            f"👑 <b>Ваша роль изменена!</b>\n\n"
            f"Вам назначена роль: {ROLES.get(new_role, 'Неизвестно')}",
            parse_mode="html"
        )
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "user_id": target_id,
        "role": new_role,
        "role_name": ROLES.get(new_role, "Неизвестно")
    })




@app.route("/api/news", methods=["GET"])
def api_get_news():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    if not validate_init_data(init_data):
        return jsonify({"error": "Invalid initData"}), 401
    # soft sync (не блокируем надолго)
    try:
        sync_news_from_channel()
    except Exception:
        pass
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        _ensure_news_schema(cur)
        conn.commit()
        cur.execute(
            """
            SELECT id, title, body, body_html, views_count, is_pinned, tg_msg_id, published_at, created_at
            FROM news
            WHERE status = 'active' AND COALESCE(is_pinned, 0) = 0
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT 50
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["date_label"] = format_news_datetime(r.get("published_at") or r.get("created_at"))
    except Exception as e:
        logger.error(f"news list: {e}")
        rows = []
    conn.close()
    return jsonify({"news": rows})


@app.route("/api/news/<int:news_id>", methods=["GET"])
def api_get_news_item(news_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    if not validate_init_data(init_data):
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        _ensure_news_schema(cur)
        cur.execute(
            "UPDATE news SET views_count = COALESCE(views_count, 0) + 1 WHERE id = ? AND status = 'active'",
            (news_id,),
        )
        conn.commit()
        cur.execute(
            """
            SELECT id, title, body, body_html, views_count, is_pinned, tg_msg_id, published_at, created_at
            FROM news WHERE id = ? AND status = 'active'
            """,
            (news_id,),
        )
        row = cur.fetchone()
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 500
    conn.close()
    if not row:
        return jsonify({"error": "Новость не найдена"}), 404
    data = dict(row)
    data["date_label"] = format_news_datetime(data.get("published_at") or data.get("created_at"))
    return jsonify(data)


@app.route("/api/news/<int:news_id>", methods=["DELETE"])
def api_delete_news(news_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < MODERATOR_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE news SET status = 'deleted' WHERE id = ?", (news_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})





# ==================== SHIFTS / WORK CENTER ====================
MAX_ACTIVE_SHIFTS = 5

SHIFT_MAX_TOTAL = 5
SHIFT_MAX_ADMINS = 1
SHIFT_MAX_MODS = 4


def user_on_duty_shift(user_id):
    """Записан ли пользователь на текущую (идущую сейчас) смену."""
    now = _msk_now()
    cur_sh = _find_current_shift(now)
    if not cur_sh:
        return False
    sd, st = cur_sh[0], cur_sh[1]
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        _ensure_shifts_schema(cur)
        cur.execute(
            "SELECT 1 FROM shift_signups WHERE user_id=? AND shift_date=? AND shift_type=? AND status='signed'",
            (safe_int(user_id), sd, st),
        )
        ok = cur.fetchone() is not None
        conn.close()
        return ok
    except Exception:
        return False


def can_moderate_content(user_id):
    """Модерация объявлений/транзакций/жалоб: создатель всегда; иначе только на текущей смене и роль >= модер."""
    role = get_user_role(user_id)
    if role >= 6:
        return True
    if role < MODERATOR_ROLE:
        return False
    return user_on_duty_shift(user_id)


def can_control_shift(user_id):
    role = get_user_role(user_id)
    if role >= 6:
        return True
    if role < ROLE_CHANGE_ROLE:
        return False
    return user_on_duty_shift(user_id)


def _ensure_shifts_schema(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shift_signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            shift_type TEXT NOT NULL,
            status TEXT DEFAULT 'signed',
            created_at TEXT NOT NULL,
            UNIQUE(user_id, shift_date, shift_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shift_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_date TEXT NOT NULL,
            shift_type TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            lead_admin_id INTEGER,
            opened_at TEXT,
            attendance_done_at TEXT,
            closed_at TEXT,
            UNIQUE(shift_date, shift_type)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shift_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_date TEXT NOT NULL,
            shift_type TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            present INTEGER DEFAULT 0,
            credit TEXT DEFAULT 'pending',
            credit_amount REAL DEFAULT 0,
            marked_at TEXT,
            credited_at TEXT,
            UNIQUE(shift_date, shift_type, user_id)
        )
    """)


def _msk_now():
    return datetime.now(MSK_TZ)


def _auto_close_deadline(shift_date, shift_type):
    """Автозакрытие: день до 22:00, ночь до 10:00 (конец смены + 1ч)."""
    from datetime import timedelta
    _, end = _shift_bounds(shift_date, shift_type)
    return end + timedelta(hours=1)


def _finalize_shift_session(cur, sd, st, closer_id=None, now=None):
    """Закрыть смену и начислить баланс по отметкам credit. Возвращает paid list."""
    now = now or _msk_now()
    now_iso = now.isoformat()
    cur.execute(
        "INSERT INTO shift_sessions (shift_date, shift_type, status, lead_admin_id, closed_at) VALUES (?, ?, 'closed', ?, ?) "
        "ON CONFLICT(shift_date, shift_type) DO UPDATE SET status='closed', closed_at=excluded.closed_at",
        (sd, st, closer_id, now_iso),
    )
    cur.execute(
        "SELECT user_id, credit, COALESCE(credit_amount, 0) FROM shift_attendance "
        "WHERE shift_date=? AND shift_type=? AND COALESCE(credit,'pending') != 'pending'",
        (sd, st),
    )
    paid = []
    for suid, credit, amt in cur.fetchall():
        amt = float(amt or 0)
        if amt > 0:
            try:
                update_balance(suid, amt)
                add_transaction(suid, "income", amt, f"Смена {sd} {st}: {credit}")
                paid.append({"user_id": suid, "amount": amt, "credit": credit})
                try:
                    cur.execute(
                        "UPDATE shift_attendance SET credited_at=? WHERE shift_date=? AND shift_type=? AND user_id=?",
                        (now_iso, sd, st, suid),
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"shift close pay {suid}: {e}")
    return paid


def _auto_close_overdue_shifts(cur, now=None):
    """Автозакрытие открытых смен после дедлайна 22:00 / 10:00."""
    now = now or _msk_now()
    cur.execute("SELECT shift_date, shift_type, lead_admin_id FROM shift_sessions WHERE status='open'")
    rows = list(cur.fetchall())
    closed = []
    for row in rows:
        sd, st = row[0], row[1]
        lead = row[2] if len(row) > 2 else None
        try:
            deadline = _auto_close_deadline(sd, st)
            if now >= deadline:
                paid = _finalize_shift_session(cur, sd, st, closer_id=lead, now=now)
                closed.append({"shift_date": sd, "shift_type": st, "paid": paid, "auto": True})
                logger.info("Auto-closed shift %s %s, paid=%s", sd, st, paid)
        except Exception as e:
            logger.error("auto_close %s %s: %s", sd, st, e)
    return closed




def _shift_bounds(shift_date_str, shift_type):
    """Возвращает (start_dt, end_dt) в MSK."""
    from datetime import timedelta
    y, m, d = [int(x) for x in shift_date_str.split("-")]
    base = datetime(y, m, d, tzinfo=MSK_TZ)
    if shift_type == "day":
        start = base.replace(hour=9, minute=0, second=0, microsecond=0)
        end = base.replace(hour=21, minute=0, second=0, microsecond=0)
    else:
        start = base.replace(hour=21, minute=0, second=0, microsecond=0)
        end = (base + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return start, end


def _shift_schedule(shift_type):
    if shift_type == "day":
        return [
            {"time": "08:45", "title": "На рабочем месте", "text": "Сотрудник уже на рабочем месте"},
            {"time": "08:50", "title": "Созвон в общей группе", "text": "Передача данных модераторов и админов с прошлой смены"},
            {"time": "09:00–09:15", "title": "Планерка новой смены", "text": "Со своим админом: распределение обязанностей (модерация объявлений, модерация транзакций, модерация поддержки, поиск нарушителей на площадке)"},
            {"time": "09:15–12:00", "title": "Работа по направлению", "text": ""},
            {"time": "12:00–12:15", "title": "Планерка", "text": "Отчёт о проделанной работе, вопросы к админу, смена направления при необходимости"},
            {"time": "12:15–15:00", "title": "Работа по направлениям", "text": ""},
            {"time": "15:00–15:30", "title": "Перекус / обед / отдых", "text": ""},
            {"time": "15:30–18:00", "title": "Работа по направлениям", "text": ""},
            {"time": "18:00–18:15", "title": "Промежуточный отчёт", "text": "К окончанию смены"},
            {"time": "20:30–20:45", "title": "Итоги дня", "text": ""},
            {"time": "20:50–21:00", "title": "Передача смены", "text": ""},
        ]
    # night 21:00–09:00
    return [
        {"time": "20:45", "title": "На рабочем месте", "text": "Сотрудник уже на рабочем месте"},
        {"time": "20:50", "title": "Созвон в общей группе", "text": "Передача данных модераторов и админов с прошлой смены"},
        {"time": "21:00–21:15", "title": "Планерка новой смены", "text": "Со своим админом: распределение обязанностей (модерация объявлений, модерация транзакций, модерация поддержки, поиск нарушителей на площадке)"},
        {"time": "21:15–00:00", "title": "Работа по направлению", "text": ""},
        {"time": "00:00–00:15", "title": "Планерка", "text": "Отчёт о проделанной работе, вопросы к админу, смена направления при необходимости"},
        {"time": "00:15–03:00", "title": "Работа по направлениям", "text": ""},
        {"time": "03:00–03:30", "title": "Перекус / обед / отдых", "text": ""},
        {"time": "03:30–06:00", "title": "Работа по направлениям", "text": ""},
        {"time": "06:00–06:15", "title": "Промежуточный отчёт", "text": "К окончанию смены"},
        {"time": "08:30–08:45", "title": "Итоги смены", "text": ""},
        {"time": "08:50–09:00", "title": "Передача смены", "text": ""},
    ]


@app.route("/api/shifts/calendar", methods=["GET"])
def api_shifts_calendar():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    from datetime import timedelta
    now = _msk_now()
    today = now.date()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    conn.commit()
    cur.execute(
        "SELECT shift_date, shift_type FROM shift_signups WHERE user_id = ? AND status = 'signed'",
        (uid,),
    )
    signed = {(r[0], r[1]) for r in cur.fetchall()}
    conn.close()

    days = []
    for i in range(1, 8):  # без сегодня: +1..+7
        d = today + timedelta(days=i)
        dstr = d.isoformat()
        slots = []
        for stype in ("day", "night"):
            start, end = _shift_bounds(dstr, stype)
            in_progress = now >= start and now < end
            finished = now >= end
            can_signup = (not in_progress) and (not finished) and ((dstr, stype) not in signed)
            slots.append({
                "shift_date": dstr,
                "shift_type": stype,
                "label": "Дневная 09:00–21:00" if stype == "day" else "Ночная 21:00–09:00",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "signed": (dstr, stype) in signed,
                "in_progress": in_progress,
                "finished": finished,
                "can_signup": can_signup,
            })
        days.append({
            "date": dstr,
            "label": f"{d.day:02d}.{d.month:02d}.{d.year}",
            "weekday": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][d.weekday()],
            "slots": slots,
        })

    # active signed count (future or in progress)
    active_count = 0
    for sd, st in signed:
        try:
            _, end = _shift_bounds(sd, st)
            if now < end:
                active_count += 1
        except Exception:
            active_count += 1

    return jsonify({
        "days": days,
        "active_signups": active_count,
        "max_signups": MAX_ACTIVE_SHIFTS,
        "note": "К 08:45 (день) / 20:45 (ночь) сотрудник уже на рабочем месте. Время — МСК.",
    })


@app.route("/api/shifts/signup", methods=["POST"])
def api_shifts_signup():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    data = request.json or {}
    shift_date = (data.get("shift_date") or "").strip()
    shift_type = (data.get("shift_type") or "").strip()
    if shift_type not in ("day", "night") or len(shift_date) != 10:
        return jsonify({"error": "Некорректная смена"}), 400

    from datetime import timedelta
    now = _msk_now()
    today = now.date()
    try:
        y, m, d = [int(x) for x in shift_date.split("-")]
        sdate = datetime(y, m, d).date()
    except Exception:
        return jsonify({"error": "Некорректная дата"}), 400

    if sdate <= today:
        return jsonify({"error": "Нельзя записаться на сегодня или прошедшие дни"}), 400
    if sdate > today + timedelta(days=7):
        return jsonify({"error": "Доступны только ближайшие 7 дней"}), 400

    start, end = _shift_bounds(shift_date, shift_type)
    if now >= start:
        return jsonify({"error": "Нельзя записаться на смену, которая уже идёт или завершена"}), 400

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    # count active
    cur.execute(
        "SELECT shift_date, shift_type FROM shift_signups WHERE user_id = ? AND status = 'signed'",
        (uid,),
    )
    active = 0
    for sd, st in cur.fetchall():
        try:
            _, e = _shift_bounds(sd, st)
            if now < e:
                active += 1
        except Exception:
            active += 1
    if active >= MAX_ACTIVE_SHIFTS:
        conn.close()
        return jsonify({"error": f"Лимит: одновременно не более {MAX_ACTIVE_SHIFTS} смен"}), 400

    # лимит на смену: 5 человек (4 модера + 1 админ)
    role = get_user_role(uid)
    cur.execute(
        """
        SELECT s.user_id, COALESCE(u.role, 0) as role
        FROM shift_signups s
        LEFT JOIN users u ON u.user_id = s.user_id
        WHERE s.shift_date = ? AND s.shift_type = ? AND s.status = 'signed'
        """,
        (shift_date, shift_type),
    )
    signed_rows = cur.fetchall()
    if len(signed_rows) >= SHIFT_MAX_TOTAL:
        conn.close()
        return jsonify({"error": f"Смена заполнена (макс. {SHIFT_MAX_TOTAL} сотрудников)"}), 400
    n_admins = sum(1 for r in signed_rows if safe_int(r[1]) >= ROLE_CHANGE_ROLE)
    # стажёры (3) и модеры (4) в слот модеров
    n_mods = sum(1 for r in signed_rows if 3 <= safe_int(r[1]) <= 4)
    if role >= ROLE_CHANGE_ROLE:
        if n_admins >= SHIFT_MAX_ADMINS:
            conn.close()
            return jsonify({"error": "На смене уже есть админ (макс. 1)"}), 400
    elif 3 <= role <= 4:
        if n_mods >= SHIFT_MAX_MODS:
            conn.close()
            return jsonify({"error": f"Слот модераторов заполнен (макс. {SHIFT_MAX_MODS})"}), 400
    else:
        conn.close()
        return jsonify({"error": "На смену записываются только сотрудники (стажёр/модер/админ)"}), 403

    now_iso = now.isoformat()
    try:
        cur.execute(
            "INSERT INTO shift_signups (user_id, shift_date, shift_type, status, created_at) VALUES (?, ?, ?, 'signed', ?)",
            (uid, shift_date, shift_type, now_iso),
        )
    except sqlite3.IntegrityError:
        cur.execute(
            "UPDATE shift_signups SET status = 'signed', created_at = ? WHERE user_id = ? AND shift_date = ? AND shift_type = ?",
            (now_iso, uid, shift_date, shift_type),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/shifts/cancel", methods=["POST"])
def api_shifts_cancel():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    data = request.json or {}
    shift_date = (data.get("shift_date") or "").strip()
    shift_type = (data.get("shift_type") or "").strip()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    cur.execute(
        "UPDATE shift_signups SET status = 'cancelled' WHERE user_id = ? AND shift_date = ? AND shift_type = ? AND status = 'signed'",
        (uid, shift_date, shift_type),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})



@app.route("/api/shifts/history", methods=["GET"])
def api_shifts_history():
    """Отработанные смены с часами и суммой начисления."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    hours_map = {"full": 12, "partial_8": 8, "partial_6": 6, "none": 0}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    conn.commit()
    # отработанные: attendance с credit != pending ИЛИ закрытая сессия + запись
    cur.execute(
        """
        SELECT a.shift_date, a.shift_type, a.credit, COALESCE(a.credit_amount, 0) as amount,
               a.credited_at, a.present, sess.closed_at, sess.status as session_status
        FROM shift_attendance a
        LEFT JOIN shift_sessions sess ON sess.shift_date = a.shift_date AND sess.shift_type = a.shift_type
        WHERE a.user_id = ?
          AND (
            (a.credit IS NOT NULL AND a.credit != 'pending')
            OR sess.status = 'closed'
          )
        ORDER BY a.shift_date DESC, a.shift_type DESC
        LIMIT 100
        """,
        (uid,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        credit = d.get("credit") or "none"
        if credit == "pending":
            credit = "none"
        hours = hours_map.get(credit, 0)
        d["hours"] = hours
        d["amount"] = float(d.get("amount") or 0)
        d["label"] = "Дневная 09:00–21:00" if d.get("shift_type") == "day" else "Ночная 21:00–09:00"
        d["credit"] = credit
        rows.append(d)
    conn.close()
    return jsonify({"shifts": rows})


@app.route("/api/shifts/my", methods=["GET"])
def api_shifts_my():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    now = _msk_now()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    conn.commit()
    cur.execute(
        "SELECT id, shift_date, shift_type, status, created_at FROM shift_signups WHERE user_id = ? AND status = 'signed' ORDER BY shift_date ASC, shift_type ASC",
        (uid,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            start, end = _shift_bounds(d["shift_date"], d["shift_type"])
            d["start"] = start.isoformat()
            d["end"] = end.isoformat()
            d["label"] = "Дневная 09:00–21:00" if d["shift_type"] == "day" else "Ночная 21:00–09:00"
            d["in_progress"] = now >= start and now < end
            d["finished"] = now >= end
        except Exception:
            pass
        # статус сессии (открыл ли админ смену)
        try:
            cur.execute(
                "SELECT status, opened_at FROM shift_sessions WHERE shift_date=? AND shift_type=?",
                (d["shift_date"], d["shift_type"]),
            )
            sess = cur.fetchone()
            if sess:
                d["session_status"] = sess[0]
                d["session_opened"] = sess[0] in ("open", "closed") or bool(sess[1])
                d["session_opened_at"] = sess[1]
            else:
                d["session_status"] = None
                d["session_opened"] = False
                d["session_opened_at"] = None
        except Exception:
            d["session_status"] = None
            d["session_opened"] = False
        rows.append(d)
    conn.close()
    return jsonify({"shifts": rows})


@app.route("/api/shifts/schedule", methods=["GET"])
def api_shifts_schedule():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    if not validate_init_data(init_data):
        return jsonify({"error": "Invalid initData"}), 401
    shift_type = (request.args.get("shift_type") or "day").strip()
    if shift_type not in ("day", "night"):
        shift_type = "day"
    return jsonify({
        "shift_type": shift_type,
        "schedule": _shift_schedule(shift_type),
        "arrive_note": "К 08:45 сотрудник уже на рабочем месте" if shift_type == "day" else "К 20:45 сотрудник уже на рабочем месте",
    })



def _find_current_shift(now=None):
    """Текущая смена по времени МСК или None."""
    from datetime import timedelta
    now = now or _msk_now()
    # day today 09-21, night yesterday 21 - today 09, night today 21 - tomorrow 09
    today = now.date()
    candidates = []
    for delta in (-1, 0):
        d = (today + timedelta(days=delta)).isoformat()
        for st in ("day", "night"):
            start, end = _shift_bounds(d, st)
            if start <= now < end:
                candidates.append((d, st, start, end))
    return candidates[0] if candidates else None


def _attendance_deadline(shift_date, shift_type):
    """До второй планерки: день 12:00, ночь 00:15 следующего дня."""
    from datetime import timedelta
    y, m, d = [int(x) for x in shift_date.split("-")]
    base = datetime(y, m, d, tzinfo=MSK_TZ)
    if shift_type == "day":
        return base.replace(hour=12, minute=0, second=0, microsecond=0)
    return (base + timedelta(days=1)).replace(hour=0, minute=15, second=0, microsecond=0)


def _credit_opens_at(shift_date, shift_type):
    """После 20:45 день / 08:45 ночь."""
    from datetime import timedelta
    y, m, d = [int(x) for x in shift_date.split("-")]
    base = datetime(y, m, d, tzinfo=MSK_TZ)
    if shift_type == "day":
        return base.replace(hour=20, minute=45, second=0, microsecond=0)
    return (base + timedelta(days=1)).replace(hour=8, minute=45, second=0, microsecond=0)


def _previous_shift(shift_date, shift_type):
    from datetime import timedelta
    y, m, d = [int(x) for x in shift_date.split("-")]
    base = datetime(y, m, d).date()
    if shift_type == "day":
        # previous is night of previous calendar day
        prev_date = (base - timedelta(days=1)).isoformat()
        return prev_date, "night"
    # night previous is day same date
    return shift_date, "day"


@app.route("/api/shifts/control", methods=["GET"])
def api_shifts_control():
    """Контроль сотрудников — только админ 5+ записанный на текущую/свою смену."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403

    now = _msk_now()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    _auto_close_overdue_shifts(cur, now)
    conn.commit()

    # активные записи админа на незавершённые смены
    cur.execute(
        "SELECT shift_date, shift_type FROM shift_signups WHERE user_id = ? AND status = 'signed'",
        (uid,),
    )
    my_shifts = []
    for r in cur.fetchall():
        sd, st = r[0], r[1]
        start, end = _shift_bounds(sd, st)
        if now < end:
            my_shifts.append({"shift_date": sd, "shift_type": st, "start": start.isoformat(), "end": end.isoformat(), "in_progress": start <= now < end})

    current = _find_current_shift(now)
    role = get_user_role(uid)

    # только идущая сейчас смена
    if not current:
        conn.close()
        return jsonify({
            "ok": False,
            "message": "Сейчас нет активной смены",
            "my_shifts": my_shifts,
            "current": None,
        })

    sd, st = current[0], current[1]
    on_this = any(s["shift_date"] == sd and s["shift_type"] == st for s in my_shifts)
    if role < 6 and not on_this:
        conn.close()
        return jsonify({
            "ok": False,
            "message": "Вы не являетесь главным на текущей смене",
            "my_shifts": my_shifts,
            "current": {"shift_date": sd, "shift_type": st},
        })
    start, end = _shift_bounds(sd, st)
    prev_d, prev_t = _previous_shift(sd, st)
    cur.execute("SELECT * FROM shift_sessions WHERE shift_date = ? AND shift_type = ?", (prev_d, prev_t))
    prev_sess = cur.fetchone()
    prev_closed = True
    # если предыдущая смена уже должна была идти по календарю — требуем закрытия
    try:
        _, prev_end = _shift_bounds(prev_d, prev_t)
        if prev_end <= now:
            prev_closed = bool(prev_sess and prev_sess["status"] == "closed")
            # если никто не открывал предыдущую — считаем закрытой (bootstrap)
            if not prev_sess:
                prev_closed = True
    except Exception:
        prev_closed = True

    cur.execute("SELECT * FROM shift_sessions WHERE shift_date = ? AND shift_type = ?", (sd, st))
    sess = cur.fetchone()
    sess_d = dict(sess) if sess else None

    # список записанных
    cur.execute(
        """
        SELECT s.user_id, s.shift_date, s.shift_type, u.username, u.first_name, u.last_name, u.role,
               COALESCE(a.present, 0) as present, COALESCE(a.credit, 'pending') as credit,
               COALESCE(a.credit_amount, 0) as credit_amount, a.marked_at
        FROM shift_signups s
        LEFT JOIN users u ON u.user_id = s.user_id
        LEFT JOIN shift_attendance a ON a.user_id = s.user_id AND a.shift_date = s.shift_date AND a.shift_type = s.shift_type
        WHERE s.shift_date = ? AND s.shift_type = ? AND s.status = 'signed'
        ORDER BY u.role DESC, s.created_at ASC
        """,
        (sd, st),
    )
    staff = [dict(r) for r in cur.fetchall()]
    for x in staff:
        x["role_name"] = ROLES.get(safe_int(x.get("role")), "—")
        x["name"] = ((x.get("first_name") or "") + " " + (x.get("last_name") or "")).strip() or (x.get("username") and ("@" + x["username"])) or str(x["user_id"])

    att_deadline = _attendance_deadline(sd, st)
    credit_open = _credit_opens_at(sd, st)
    can_attendance = prev_closed and now < att_deadline and now >= start
    # если сессия ещё не открыта, но prev closed и shift started - can open
    can_open = prev_closed and now >= start and (not sess_d or sess_d.get("status") in (None, "pending"))
    can_credit = now >= credit_open and now < end + __import__("datetime").timedelta(hours=12)
    if sess_d and sess_d.get("status") == "closed":
        can_attendance = False
        can_credit = False

    all_marked = True
    for x in staff:
        if not x.get("marked_at"):
            all_marked = False
            break
    can_close = (not sess_d or sess_d.get("status") != "closed") and all_marked and now >= end
    can_open = can_open and now >= start

    conn.close()
    return jsonify({
        "ok": True,
        "shift_date": sd,
        "shift_type": st,
        "label": "Дневная 09:00–21:00" if st == "day" else "Ночная 21:00–09:00",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev_shift": {"shift_date": prev_d, "shift_type": prev_t, "closed": prev_closed},
        "session": sess_d,
        "staff": staff,
        "can_open": can_open,
        "can_attendance": can_attendance or (prev_closed and now >= start and now < att_deadline and (not sess_d or sess_d.get("status") != "closed")),
        "attendance_deadline": att_deadline.isoformat(),
        "can_credit": bool(can_credit and (sess_d is None or sess_d.get("status") != "closed")),
        "can_close": can_close,
        "all_marked": all_marked,
        "credit_opens_at": credit_open.isoformat(),
        "my_shifts": my_shifts,
        "is_lead": True,
    })


@app.route("/api/shifts/control/open", methods=["POST"])
def api_shifts_control_open():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.json or {}
    sd = (data.get("shift_date") or "").strip()
    st = (data.get("shift_type") or "").strip()
    if st not in ("day", "night"):
        return jsonify({"error": "Некорректная смена"}), 400
    now = _msk_now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    cur.execute("SELECT 1 FROM shift_signups WHERE user_id=? AND shift_date=? AND shift_type=? AND status='signed'", (uid, sd, st))
    if not cur.fetchone() and get_user_role(uid) < 6:
        conn.close()
        return jsonify({"error": "Вы не записаны на эту смену"}), 403
    _auto_close_overdue_shifts(cur, now)
    start, end = _shift_bounds(sd, st)
    cur.execute("SELECT status FROM shift_sessions WHERE shift_date=? AND shift_type=?", (sd, st))
    cur_sess = cur.fetchone()
    if cur_sess and cur_sess[0] in ("open", "closed"):
        conn.close()
        if cur_sess[0] == "closed":
            return jsonify({"error": "Смена уже закрыта"}), 400
        return jsonify({"error": "Смена уже открыта"}), 400
    prev_d, prev_t = _previous_shift(sd, st)
    cur.execute("SELECT status FROM shift_sessions WHERE shift_date=? AND shift_type=?", (prev_d, prev_t))
    prev_row = cur.fetchone()
    prev_status = prev_row[0] if prev_row else None
    prev_closed = prev_status == "closed"
    # автозакрыть просроченную предыдущую
    if prev_status == "open":
        try:
            if now >= _auto_close_deadline(prev_d, prev_t):
                _finalize_shift_session(cur, prev_d, prev_t, closer_id=None, now=now)
                prev_closed = True
                prev_status = "closed"
        except Exception as e:
            logger.error("auto prev close: %s", e)
    # открыть: после 9/21 ИЛИ после закрытия предыдущей (передача смены)
    if now < start and not prev_closed:
        conn.close()
        return jsonify({"error": "Ещё рано открывать смену (после 9:00 / 21:00 или после закрытия предыдущей)"}), 400
    if prev_status == "open" and not prev_closed:
        conn.close()
        return jsonify({"error": "Дождитесь закрытия предыдущей смены"}), 400
    now_iso = now.isoformat()
    try:
        cur.execute(
            "INSERT INTO shift_sessions (shift_date, shift_type, status, lead_admin_id, opened_at) VALUES (?, ?, 'open', ?, ?)",
            (sd, st, uid, now_iso),
        )
    except sqlite3.IntegrityError:
        cur.execute(
            "UPDATE shift_sessions SET status='open', lead_admin_id=COALESCE(lead_admin_id, ?), opened_at=COALESCE(opened_at, ?) WHERE shift_date=? AND shift_type=? AND status!='closed'",
            (uid, now_iso, sd, st),
        )
    # seed attendance rows
    cur.execute("SELECT user_id FROM shift_signups WHERE shift_date=? AND shift_type=? AND status='signed'", (sd, st))
    for (suid,) in cur.fetchall():
        try:
            cur.execute(
                "INSERT OR IGNORE INTO shift_attendance (shift_date, shift_type, user_id, present, credit) VALUES (?, ?, ?, 0, 'pending')",
                (sd, st, suid),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/shifts/control/attendance", methods=["POST"])
def api_shifts_control_attendance():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.json or {}
    sd = (data.get("shift_date") or "").strip()
    st = (data.get("shift_type") or "").strip()
    marks = data.get("marks") or []  # [{user_id, present: bool}]
    now = _msk_now()
    deadline = _attendance_deadline(sd, st)
    if now > deadline:
        return jsonify({"error": "Время отметки присутствия истекло (до второй планерки)"}), 400
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    cur.execute("SELECT 1 FROM shift_signups WHERE user_id=? AND shift_date=? AND shift_type=? AND status='signed'", (uid, sd, st))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "Вы не главный на этой смене"}), 403
    now_iso = now.isoformat()
    for m in marks:
        suid = safe_int(m.get("user_id"))
        present = 1 if m.get("present") else 0
        cur.execute(
            "INSERT INTO shift_attendance (shift_date, shift_type, user_id, present, credit, marked_at) VALUES (?, ?, ?, ?, 'pending', ?) "
            "ON CONFLICT(shift_date, shift_type, user_id) DO UPDATE SET present=excluded.present, marked_at=excluded.marked_at",
            (sd, st, suid, present, now_iso),
        )
    cur.execute(
        "UPDATE shift_sessions SET attendance_done_at=? WHERE shift_date=? AND shift_type=?",
        (now_iso, sd, st),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/shifts/control/credit", methods=["POST"])
def api_shifts_control_credit():
    """Засчитать / не засчитать / неполный день."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.json or {}
    sd = (data.get("shift_date") or "").strip()
    st = (data.get("shift_type") or "").strip()
    target = safe_int(data.get("user_id"))
    credit = (data.get("credit") or "").strip()  # full | none | partial_8 | partial_6
    amounts = {"full": 25.0, "none": 0.0, "partial_8": 15.0, "partial_6": 10.0}
    if credit not in amounts:
        return jsonify({"error": "Некорректный тип зачёта"}), 400
    now = _msk_now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    cur.execute("SELECT 1 FROM shift_signups WHERE user_id=? AND shift_date=? AND shift_type=? AND status='signed'", (uid, sd, st))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "Вы не главный на этой смене"}), 403
    amt = amounts[credit]
    now_iso = now.isoformat()
    cur.execute(
        "INSERT INTO shift_attendance (shift_date, shift_type, user_id, present, credit, credit_amount, credited_at) VALUES (?, ?, ?, 1, ?, ?, ?) "
        "ON CONFLICT(shift_date, shift_type, user_id) DO UPDATE SET credit=excluded.credit, credit_amount=excluded.credit_amount, credited_at=excluded.credited_at",
        (sd, st, target, credit, amt, now_iso),
    )
    # выплата только при закрытии смены
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "credit": credit, "amount": amt, "paid": False, "note": "Начисление после закрытия смены"})


@app.route("/api/shifts/control/close", methods=["POST"])
def api_shifts_control_close():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    if get_user_role(uid) < ROLE_CHANGE_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    data = request.json or {}
    sd = (data.get("shift_date") or "").strip()
    st = (data.get("shift_type") or "").strip()
    now = _msk_now()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    _ensure_shifts_schema(cur)
    if get_user_role(uid) < 6:
        cur.execute("SELECT 1 FROM shift_signups WHERE user_id=? AND shift_date=? AND shift_type=? AND status='signed'", (uid, sd, st))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": "Вы не главный на этой смене"}), 403
    _auto_close_overdue_shifts(cur, now)
    cur.execute("SELECT status FROM shift_sessions WHERE shift_date=? AND shift_type=?", (sd, st))
    sess = cur.fetchone()
    if sess and sess[0] == "closed":
        conn.close()
        return jsonify({"error": "Смена уже закрыта"}), 400
    # все сотрудники отмечены (если есть)
    cur.execute(
        "SELECT s.user_id, a.marked_at FROM shift_signups s "
        "LEFT JOIN shift_attendance a ON a.user_id=s.user_id AND a.shift_date=s.shift_date AND a.shift_type=s.shift_type "
        "WHERE s.shift_date=? AND s.shift_type=? AND s.status='signed'",
        (sd, st),
    )
    rows = cur.fetchall()
    if rows and any(not r[1] for r in rows):
        conn.close()
        return jsonify({"error": "Не все сотрудники отмечены"}), 400
    try:
        start, end = _shift_bounds(sd, st)
        # закрытие с окончания смены (21:00 / 09:00), не раньше
        if now < end:
            conn.close()
            return jsonify({
                "error": "Ещё слишком рано закрывать смену",
                "can_close_at": end.isoformat(),
                "now": now.isoformat(),
            }), 400
    except Exception as e:
        logger.error("close bounds: %s", e)
    paid = _finalize_shift_session(cur, sd, st, closer_id=uid, now=now)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "paid": paid})


@app.route("/api/exchange/product-types", methods=["GET"])
def api_exchange_product_types():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    if not validate_init_data(init_data):
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DISTINCT TRIM(product_type) as pt
            FROM listings
            WHERE status = 'active'
              AND product_type IS NOT NULL
              AND TRIM(product_type) != ''
            ORDER BY pt COLLATE NOCASE
            LIMIT 200
            """
        )
        types = [r[0] for r in cur.fetchall() if r and r[0]]
    except Exception as e:
        logger.error(f"product-types: {e}")
        types = []
    conn.close()
    return jsonify({"types": types})


@app.route("/api/exchange/search", methods=["GET"])
def api_exchange_search():
    """Поиск по бирже с фильтрами."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])

    q = (request.args.get("q") or "").strip().lower()
    listing_type = (request.args.get("listing_type") or "").strip()  # partner|own|empty
    product_type = (request.args.get("product_type") or "").strip().lower()
    rating_min = request.args.get("rating_min")
    price_from = request.args.get("price_from")
    price_to = request.args.get("price_to")
    users_from = request.args.get("users_from")
    users_to = request.args.get("users_to")
    earnings_from = request.args.get("earnings_from")
    earnings_to = request.args.get("earnings_to")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(listings)").fetchall()}
    um = "COALESCE(l.users_month, 0)" if "users_month" in cols else "0"
    em = "COALESCE(l.earnings_month, 0)" if "earnings_month" in cols else "0"
    rs = "COALESCE(u.rating_seller, 5.0)" if True else "5.0"

    sql = f"""
        SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
               l.status, l.listing_type, l.product_type, l.tgstat_url, l.created_at,
               COALESCE(l.views_count, 0) as views_count,
               {um} as users_month, {em} as earnings_month,
               u.username as seller_username, u.first_name as seller_name,
               {rs} as rating_seller,
               CASE WHEN f.listing_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite
        FROM listings l
        LEFT JOIN users u ON u.user_id = l.seller_id
        LEFT JOIN favorites f ON f.listing_id = l.id AND f.user_id = ?
        WHERE l.status = 'active'
          AND COALESCE(l.title, '') NOT IN ('X-Cod', 'X-Cod Exchange — платформа для продажи Telegram-ботов')
    """
    params = [uid]
    if listing_type in ("partner", "own"):
        sql += " AND l.listing_type = ?"
        params.append(listing_type)
    if product_type:
        sql += " AND LOWER(COALESCE(l.product_type, '')) LIKE ?"
        params.append("%" + product_type + "%")
    if q:
        sql += " AND (LOWER(l.title) LIKE ? OR LOWER(COALESCE(l.description,'')) LIKE ? OR LOWER(COALESCE(l.product_type,'')) LIKE ? OR LOWER(COALESCE(l.bot_username,'')) LIKE ?)"
        qq = "%" + q + "%"
        params.extend([qq, qq, qq, qq])
    try:
        if rating_min not in (None, ""):
            sql += f" AND {rs} >= ?"
            params.append(float(rating_min))
    except Exception:
        pass
    try:
        if price_from not in (None, ""):
            sql += " AND l.price >= ?"
            params.append(float(price_from))
        if price_to not in (None, ""):
            sql += " AND l.price <= ?"
            params.append(float(price_to))
    except Exception:
        pass
    try:
        if users_from not in (None, ""):
            sql += f" AND {um} >= ?"
            params.append(int(float(users_from)))
        if users_to not in (None, ""):
            sql += f" AND {um} <= ?"
            params.append(int(float(users_to)))
    except Exception:
        pass
    try:
        if earnings_from not in (None, ""):
            sql += f" AND {em} >= ?"
            params.append(float(earnings_from))
        if earnings_to not in (None, ""):
            sql += f" AND {em} <= ?"
            params.append(float(earnings_to))
    except Exception:
        pass

    sql += " ORDER BY l.created_at DESC LIMIT 100"
    try:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"exchange search: {e}")
        rows = []
    conn.close()
    for r in rows:
        r["files_meta"] = None
        r["is_favorite"] = 1 if safe_int(r.get("is_favorite")) else 0
    return jsonify({"listings": rows, "count": len(rows)})



def ensure_pinned_listings():
    """Всегда гарантирует два объявления для новостей: X-Cod 250k и X-Cod Exchange 100k."""
    now = datetime.now(MSK_TZ).isoformat()
    seller_id = list(MANDATORY_USERS.keys())[0]
    pinned_defs = [
        {
            "title": "X-Cod",
            "description": (
                "X-Cod — медиа IT-компания.\n\n"
                "Владеет и развивает:\n"
                "• сайты и веб-сервисы\n"
                "• Telegram-боты и Mini Apps\n"
                "• мобильные и веб-приложения\n"
                "• сети Telegram-каналов\n"
                "• IT-продукты и инфраструктуру\n\n"
                "Компания объединяет медиа, разработку и digital-активы. "
                "Продаётся доля / права на бренд и экосистему по договорённости."
            ),
            "price": 250000,
            "product_type": "Медиа IT-компания",
        },
        {
            "title": "X-Cod Exchange — платформа для продажи Telegram-ботов",
            "description": (
                "Готовый Telegram-бот + Mini App для биржи ботов.\n"
                "• Баланс, пополнение, вывод\n"
                "• Заявки и админ-панель\n"
                "• Mini App с лентой объявлений\n"
                "• SQLite, роли сотрудников\n\n"
                "Продаётся исходный код и права на проект."
            ),
            "price": 100000,
            "product_type": "Telegram-бот + Mini App",
        },
    ]
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        # гарантируем продавца
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (seller_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, balance, role, created_at, last_active) "
                "VALUES (?, ?, ?, 0, ?, ?, ?)",
                (seller_id, "xcod", "X-Cod", 6, now, now),
            )
        for pd in pinned_defs:
            cur.execute("SELECT id FROM listings WHERE title = ? ORDER BY id ASC LIMIT 1", (pd["title"],))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO listings (seller_id, title, description, price, bot_username, status, "
                    "listing_type, product_type, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', 'own', ?, ?, ?)",
                    (seller_id, pd["title"], pd["description"], pd["price"], "", pd["product_type"], now, now),
                )
                logger.info("Created pinned listing: %s", pd["title"])
            else:
                cur.execute(
                    "UPDATE listings SET seller_id = ?, description = ?, price = ?, product_type = ?, "
                    "status = 'active', updated_at = ? WHERE id = ?",
                    (seller_id, pd["description"], pd["price"], pd["product_type"], now, row[0]),
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("ensure_pinned_listings: %s", e)


@app.route("/api/listings/pinned", methods=["GET"])
def api_get_pinned_listings():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])
    ensure_pinned_listings()
    titles = ["X-Cod", "X-Cod Exchange — платформа для продажи Telegram-ботов"]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    out = []
    for title in titles:
        cur.execute(
            "SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username, "
            "l.status, l.listing_type, l.product_type, l.created_at, "
            "u.username as seller_username, u.first_name as seller_name, "
            "CASE WHEN f.listing_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite "
            "FROM listings l "
            "LEFT JOIN users u ON u.user_id = l.seller_id "
            "LEFT JOIN favorites f ON f.listing_id = l.id AND f.user_id = ? "
            "WHERE l.title = ? AND l.status = 'active' "
            "ORDER BY l.id ASC LIMIT 1",
            (uid, title),
        )
        row = cur.fetchone()
        if row:
            d = dict(row)
            d["files_meta"] = None
            out.append(d)
    conn.close()
    return jsonify({"listings": out})


@app.route("/api/listings", methods=["GET"])
def api_get_listings():
    """Лента: если >12 active (без pinned) — алгоритм 4, иначе все по новизне."""
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    uid = safe_int(user["id"])

    pinned_exact = {
        "X-Cod",
        "X-Cod Exchange — платформа для продажи Telegram-ботов",
        "X-Cod Exchange - платформа для продажи Telegram-ботов",
    }

    def is_pinned_title(title):
        t = (title or "").strip()
        if t in pinned_exact:
            return True
        # запасной матч по началу
        if t == "X-Cod" or t.startswith("X-Cod Exchange"):
            return True
        return False

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cols = {r[1] for r in cur.execute("PRAGMA table_info(listings)").fetchall()}
    views_sql = "COALESCE(l.views_count, 0)" if "views_count" in cols else "0"
    has_priv = "l.has_private_files" if "has_private_files" in cols else "0 as has_private_files"

    try:
        cur.execute(
            f"""
            SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
                   l.status, l.listing_type, l.product_type, l.tgstat_url,
                   l.created_at, l.updated_at, {views_sql} as views_count,
                   {has_priv},
                   u.username as seller_username, u.first_name as seller_name, u.last_name as seller_last_name,
                   CASE WHEN f.listing_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite
            FROM listings l
            LEFT JOIN users u ON u.user_id = l.seller_id
            LEFT JOIN favorites f ON f.listing_id = l.id AND f.user_id = ?
            WHERE LOWER(COALESCE(l.status, '')) = 'active'
            ORDER BY l.created_at DESC
            LIMIT 300
            """,
            (uid,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"listings feed query failed: {e}", exc_info=True)
        # fallback без favorites
        try:
            cur.execute(
                f"""
                SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
                       l.status, l.listing_type, l.product_type, l.tgstat_url,
                       l.created_at, l.updated_at, {views_sql} as views_count,
                       u.username as seller_username, u.first_name as seller_name, u.last_name as seller_last_name,
                       0 as is_favorite
                FROM listings l
                LEFT JOIN users u ON u.user_id = l.seller_id
                WHERE LOWER(COALESCE(l.status, '')) = 'active'
                ORDER BY l.created_at DESC
                LIMIT 300
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        except Exception as e2:
            logger.error(f"listings feed fallback failed: {e2}", exc_info=True)
            conn.close()
            return jsonify({"listings": [], "error": str(e2)}), 500

    candidates = [
        r for r in rows
        if not is_pinned_title(r.get("title"))
        and not safe_int(r.get("is_favorite"))
        and safe_int(r.get("seller_id")) != uid
    ]
    # также исключаем по таблице favorites (на случай сбоя JOIN)
    try:
        cur2_ids = set()
        cur.execute("SELECT listing_id FROM favorites WHERE user_id = ?", (uid,))
        fav_ids = {safe_int(x[0]) for x in cur.fetchall()}
        if fav_ids:
            candidates = [r for r in candidates if safe_int(r.get("id")) not in fav_ids]
    except Exception:
        pass

    liked_ids = []
    liked_rows = []
    try:
        cur.execute(
            "SELECT listing_id FROM favorites WHERE user_id = ? ORDER BY created_at DESC LIMIT 50",
            (uid,),
        )
        liked_ids = [safe_int(x[0]) for x in cur.fetchall()]
        if liked_ids:
            ph = ",".join("?" * min(20, len(liked_ids)))
            ids = liked_ids[:20]
            cur.execute(
                f"SELECT id, listing_type, product_type, price, title, description FROM listings WHERE id IN ({ph})",
                ids,
            )
            liked_rows = [dict(x) for x in cur.fetchall()]
    except Exception:
        pass
    conn.close()

    def prepare(item):
        item = dict(item)
        item["files_meta"] = None
        item["is_favorite"] = 1 if safe_int(item.get("is_favorite")) else 0
        return item

    # ≤12 — просто по новизне
    if len(candidates) <= 12:
        feed = [prepare(r) for r in sorted(candidates, key=lambda x: x.get("created_at") or "", reverse=True)]
        return jsonify({
            "listings": feed,
            "algorithm": "newest",
            "debug": {"active_total": len(rows), "feed": len(feed), "mode": "newest"},
        })

    # >12 — алгоритм 4
    def tokenize(text):
        import re as _re
        words = _re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", (text or "").lower())
        stop = {"это", "для", "или", "the", "and", "bot", "бот", "есть", "при", "как", "что"}
        return {w for w in words if w not in stop}

    def interest_score(item):
        if not liked_rows:
            return 0
        best = 0
        item_cat = _normalize_listing_category(item.get("product_type"), item.get("listing_type"))
        item_words = tokenize((item.get("title") or "") + " " + (item.get("description") or ""))
        price = safe_int(item.get("price") or 0)
        for lr in liked_rows:
            s = 0
            if (item.get("listing_type") or "") == (lr.get("listing_type") or ""):
                s += 40
            if item_cat == _normalize_listing_category(lr.get("product_type"), lr.get("listing_type")):
                s += 35
            lp = safe_int(lr.get("price") or 0)
            if price > 0 and lp > 0:
                ratio = min(price, lp) / max(price, lp)
                if ratio >= 0.7:
                    s += 25 * ratio
            lw = tokenize((lr.get("title") or "") + " " + (lr.get("description") or ""))
            if item_words and lw:
                s += 30 * (len(item_words & lw) / (len(item_words | lw) or 1))
            best = max(best, s)
        return best

    liked_set = set(liked_ids)
    pool = [c for c in candidates if safe_int(c["id"]) not in liked_set]
    if not pool:
        pool = list(candidates)

    by_new = sorted(pool, key=lambda x: x.get("created_at") or "", reverse=True)
    by_views = sorted(pool, key=lambda x: (safe_int(x.get("views_count") or 0), x.get("created_at") or ""), reverse=True)
    by_interest = sorted(pool, key=lambda x: (interest_score(x), x.get("created_at") or ""), reverse=True)

    used = set()
    feed = []

    def take(seq, n):
        out = []
        for it in seq:
            i = safe_int(it["id"])
            if i in used:
                continue
            used.add(i)
            out.append(it)
            if len(out) >= n:
                break
        return out

    guard = 0
    while len(feed) < min(100, len(pool)) and len(used) < len(pool) and guard < 80:
        guard += 1
        a = take(by_interest, 2)
        feed.extend(a)
        if len(a) < 2:
            feed.extend(take(by_new, 2 - len(a)))
        feed.extend(take(by_new, 1))
        feed.extend(take(by_views, 1))
        if len(a) == 0 and len(used) >= len(pool):
            break

    for it in by_new:
        if len(feed) >= 100:
            break
        i = safe_int(it["id"])
        if i not in used:
            used.add(i)
            feed.append(it)

    feed = [prepare(r) for r in feed]
    return jsonify({
        "listings": feed,
        "algorithm": "4",
        "debug": {"active_total": len(rows), "after_pinned": len(candidates), "pool": len(pool), "feed": len(feed), "mode": "algo4"},
    })


@app.route("/api/listings/<int:listing_id>", methods=["GET"])
def api_get_listing(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
               l.status, l.listing_type, l.contract_number, l.analytics_consent,
               l.product_type, l.tgstat_url, l.created_at, l.updated_at,
               l.has_private_files, l.files_meta, l.views_count,
               COALESCE(l.users_month, 0) as users_month, COALESCE(l.earnings_month, 0) as earnings_month,
               u.username as seller_username, u.first_name as seller_name, u.last_name as seller_last_name,
               CASE WHEN f.listing_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite
        FROM listings l
        LEFT JOIN users u ON u.user_id = l.seller_id
        LEFT JOIN favorites f ON f.listing_id = l.id AND f.user_id = ?
        WHERE l.id = ?
    ''', (safe_int(user["id"]), listing_id))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404
    data = dict(row)
    uid = safe_int(user["id"])
    is_owner = uid == safe_int(data.get("seller_id"))
    is_staff = get_user_role(uid) >= MODERATOR_ROLE
    # просмотр: +1 если не владелец
    if not is_owner:
        try:
            cursor.execute(
                "UPDATE listings SET views_count = COALESCE(views_count, 0) + 1 WHERE id = ?",
                (listing_id,),
            )
            conn.commit()
            data["views_count"] = safe_int(data.get("views_count") or 0) + 1
        except Exception:
            pass
    # счётчик избранного
    try:
        cursor.execute("SELECT COUNT(*) FROM favorites WHERE listing_id = ?", (listing_id,))
        data["favorites_count"] = safe_int(cursor.fetchone()[0])
    except Exception:
        data["favorites_count"] = 0
    data["views_count"] = safe_int(data.get("views_count") or 0)
    conn.close()
    if not (is_owner or is_staff):
        data["files_meta"] = None
        # чужим не отдаём детальную статистику
        data.pop("favorites_count", None)
        # views можно не показывать чужим
        data.pop("views_count", None)
    else:
        data["files_meta"] = sanitize_files_meta(data.get("files_meta"), allow_content=True)
    return jsonify(data)


@app.route("/api/listings", methods=["POST"])
def api_create_listing():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    if is_user_banned(user["id"]):
        return jsonify({"error": "Аккаунт заблокирован"}), 403
    blocked, reason, until = is_publish_blocked(user["id"])
    if blocked:
        return jsonify({
            "error": "Модерация площадки ограничила публикацию объявлений для вас",
            "code": "publish_blocked",
            "until": until,
            "reason": reason or "",
        }), 403

    data = request.json or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    listing_type = (data.get("listing_type") or "").strip()
    bot_username = (data.get("bot_username") or "").strip().lstrip("@")
    contract_number = (data.get("contract_number") or "").strip()
    analytics_consent = 1 if data.get("analytics_consent") else 0
    product_type = (data.get("product_type") or "").strip()
    tgstat_url = (data.get("tgstat_url") or "").strip()
    price = safe_int(data.get("price", 0))

    if not title:
        return jsonify({"error": "Укажите название"}), 400
    if not description:
        return jsonify({"error": "Укажите описание"}), 400
    if listing_type not in ("partner", "own"):
        return jsonify({"error": "Выберите тип: партнер X-Cod или свой товар"}), 400

    if listing_type == "partner":
        if not contract_number:
            return jsonify({"error": "Укажите номер договора X-Cod"}), 400
        if not analytics_consent:
            return jsonify({"error": "Нужно согласие на передачу данных и аналитики"}), 400
        price = 0
    else:
        if not product_type:
            return jsonify({"error": "Укажите вид товара"}), 400
        pt = product_type.lower()
        if "бот" in pt or "bot" in pt:
            if not tgstat_url:
                return jsonify({"error": "Укажите ссылку на TGStat"}), 400
        if not bot_username:
            return jsonify({"error": "Укажите username"}), 400
        if price < 1:
            return jsonify({"error": "Укажите цену"}), 400

    add_user(user["id"], user.get("username"), user.get("first_name"), user.get("last_name"))
    now = datetime.now(MSK_TZ).isoformat()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO listings (
            seller_id, title, description, price, bot_username, status,
            listing_type, contract_number, analytics_consent, product_type, tgstat_url,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user["id"], title, description, price, bot_username,
        listing_type, contract_number, analytics_consent, product_type, tgstat_url,
        now, now
    ))
    listing_id = cursor.lastrowid
    try:
        import json as _json
        _meta = data.get("files_meta")
        if isinstance(_meta, (list, dict)):
            _meta = _json.dumps(_meta, ensure_ascii=False)
        um = safe_int(data.get("users_month") or 0)
        em = float(data.get("earnings_month") or 0) if data.get("earnings_month") not in (None, "") else 0
        try:
            em = float(em)
        except Exception:
            em = 0
        cursor.execute(
            "UPDATE listings SET has_private_files = ?, files_meta = ?, users_month = ?, earnings_month = ? WHERE id = ?",
            (1 if data.get("has_private_files") else 0, _meta or None, um, em, listing_id)
        )
    except Exception as _fe:
        logger.error(f"listing files_meta: {_fe}")
    conn.commit()
    conn.close()

    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"🆕 <b>Объявление на модерации #{listing_id}</b>\n\n"
                f"<b>{title}</b>\n"
                f"Тип: {'Партнер X-Cod' if listing_type == 'partner' else 'Свой товар'}\n"
                f"От: {user['id']}",
                parse_mode="html"
            )
        except Exception:
            pass

    return jsonify({"status": "ok", "id": listing_id, "moderation": True})


@app.route("/api/my-listings", methods=["GET"])
def api_my_listings():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
               l.status, l.listing_type, l.contract_number, l.analytics_consent,
               l.product_type, l.tgstat_url, l.created_at, l.updated_at,
               l.has_private_files, l.files_meta
        FROM listings l
        WHERE l.seller_id = ? AND l.status != 'deleted'
        ORDER BY l.created_at DESC
        LIMIT 100
    ''', (safe_int(user["id"]),))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"listings": rows})


@app.route("/api/admin/listings", methods=["GET"])
def api_admin_listings():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
               l.status, l.listing_type, l.contract_number, l.analytics_consent,
               l.product_type, l.tgstat_url, l.created_at, l.has_private_files, l.files_meta,
               u.username as seller_username, u.first_name as seller_name, u.last_name as seller_last_name
        FROM listings l
        LEFT JOIN users u ON u.user_id = l.seller_id
        WHERE l.status = 'pending'
        ORDER BY l.created_at ASC
        LIMIT 100
    ''')
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"listings": rows})


@app.route("/api/admin/listings/<int:listing_id>/approve", methods=["POST"])
def api_admin_listing_approve(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    if not can_moderate_content(user["id"]):
        return jsonify({"error": "Вы не на смене, нет доступа к разделу", "code": "not_on_shift"}), 403
    data = request.json or {}
    price = data.get("price")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404
    listing = dict(listing)
    if listing["status"] != "pending":
        conn.close()
        return jsonify({"error": "Объявление уже обработано"}), 400
    now = datetime.now(MSK_TZ).isoformat()
    new_price = listing["price"]
    if listing.get("listing_type") == "partner":
        if price is None or safe_int(price) < 1:
            conn.close()
            return jsonify({"error": "Укажите цену для партнерского товара"}), 400
        new_price = safe_int(price)
    elif price is not None and safe_int(price) >= 1:
        new_price = safe_int(price)
    cursor.execute(
        "UPDATE listings SET status = 'active', price = ?, updated_at = ? WHERE id = ?",
        (new_price, now, listing_id)
    )
    conn.commit()
    conn.close()
    try:
        bot.send_message(
            listing["seller_id"],
            f"✅ <b>Объявление #{listing_id} одобрено</b>\n\n"
            f"<b>{listing['title']}</b>\n"
            f"Цена: {new_price}$",
            parse_mode="html"
        )
    except Exception:
        pass
    return jsonify({"status": "ok", "id": listing_id, "price": new_price})


@app.route("/api/admin/listings/<int:listing_id>/reject", methods=["POST"])
def api_admin_listing_reject(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    if get_user_role(user["id"]) < PANEL_ACCESS_ROLE:
        return jsonify({"error": "Недостаточно прав"}), 403
    if not can_moderate_content(user["id"]):
        return jsonify({"error": "Вы не на смене, нет доступа к разделу", "code": "not_on_shift"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404
    listing = dict(listing)
    now = datetime.now(MSK_TZ).isoformat()
    cursor.execute(
        "UPDATE listings SET status = 'rejected', updated_at = ? WHERE id = ?",
        (now, listing_id)
    )
    conn.commit()
    conn.close()
    try:
        bot.send_message(
            listing["seller_id"],
            f"❌ <b>Объявление #{listing_id} отклонено</b>\n\n"
            f"<b>{listing['title']}</b>",
            parse_mode="html"
        )
    except Exception:
        pass
    return jsonify({"status": "ok", "id": listing_id})


@app.route("/api/favorites", methods=["GET"])
def api_get_favorites():
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT l.id, l.seller_id, l.title, l.description, l.price, l.bot_username,
               l.status, l.listing_type, l.product_type, l.created_at,
               u.username as seller_username, u.first_name as seller_name, u.last_name as seller_last_name,
               1 as is_favorite
        FROM favorites f
        JOIN listings l ON l.id = f.listing_id
        LEFT JOIN users u ON u.user_id = l.seller_id
        WHERE f.user_id = ? AND l.status = 'active'
        ORDER BY f.created_at DESC
    ''', (safe_int(user["id"]),))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return jsonify({"listings": rows})


@app.route("/api/favorites/<int:listing_id>", methods=["POST"])
def api_add_favorite(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM listings WHERE id = ? AND status = ?', (listing_id, 'active'))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404
    now = datetime.now(MSK_TZ).isoformat()
    cursor.execute(
        'INSERT OR IGNORE INTO favorites (user_id, listing_id, created_at) VALUES (?, ?, ?)',
        (user["id"], listing_id, now)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "is_favorite": True})


@app.route("/api/favorites/<int:listing_id>", methods=["DELETE"])
def api_remove_favorite(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM favorites WHERE user_id = ? AND listing_id = ?', (user["id"], listing_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "is_favorite": False})


@app.route("/api/listings/<int:listing_id>", methods=["PUT"])
def api_update_listing(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404

    listing = dict(listing)
    role = get_user_role(user["id"])
    # Редактировать может владелец, админ (5+) или создатель (6)
    if listing["seller_id"] != user["id"] and role < ROLE_CHANGE_ROLE:
        conn.close()
        return jsonify({"error": "Недостаточно прав для редактирования"}), 403

    data = request.json or {}
    title = (data.get("title") or listing["title"]).strip()
    description = data.get("description") if "description" in data else listing["description"]
    price = safe_int(data.get("price", listing["price"]))
    bot_username = data.get("bot_username") if "bot_username" in data else listing["bot_username"]
    status = data.get("status") if "status" in data else listing["status"]

    if not title:
        conn.close()
        return jsonify({"error": "Укажите название"}), 400
    if price < 1:
        conn.close()
        return jsonify({"error": "Цена должна быть больше 0"}), 400

    now = datetime.now(MSK_TZ).isoformat()
    cursor.execute('''
        UPDATE listings
        SET title = ?, description = ?, price = ?, bot_username = ?, status = ?, updated_at = ?
        WHERE id = ?
    ''', (title, description or "", price, (bot_username or "").lstrip("@"), status, now, listing_id))
    # files + resubmit to moderation
    try:
        if "has_private_files" in data:
            cursor.execute("UPDATE listings SET has_private_files = ? WHERE id = ?",
                           (1 if data.get("has_private_files") else 0, listing_id))
        if "files_meta" in data:
            import json as _json
            _fm = data.get("files_meta")
            if isinstance(_fm, (list, dict)):
                _fm = _json.dumps(_fm, ensure_ascii=False)
            cursor.execute("UPDATE listings SET files_meta = ? WHERE id = ?", (_fm, listing_id))
        if data.get("resubmit"):
            cursor.execute("UPDATE listings SET status = 'pending', updated_at = ? WHERE id = ?",
                           (datetime.now(MSK_TZ).isoformat(), listing_id))
    except Exception as _ue:
        logger.error(f"update listing files: {_ue}")
    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "id": listing_id})




@app.route("/api/listings/<int:listing_id>/unpublish", methods=["POST"])
def api_unpublish_listing(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
    listing = cur.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Не найдено"}), 404
    listing = dict(listing)
    if safe_int(listing["seller_id"]) != safe_int(user["id"]) and get_user_role(user["id"]) < ROLE_CHANGE_ROLE:
        conn.close()
        return jsonify({"error": "Нет прав"}), 403
    if listing["status"] != "active":
        conn.close()
        return jsonify({"error": "Снять можно только активное объявление"}), 400
    now = datetime.now(MSK_TZ).isoformat()
    cur.execute("UPDATE listings SET status = 'unpublished', updated_at = ? WHERE id = ?", (now, listing_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": "unpublished"})


@app.route("/api/listings/<int:listing_id>/republish", methods=["POST"])
def api_republish_listing(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401
    blocked, reason, until = is_publish_blocked(user["id"])
    if blocked:
        return jsonify({
            "error": "Модерация площадки ограничила публикацию объявлений для вас",
            "code": "publish_blocked",
            "until": until,
        }), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
    listing = cur.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Не найдено"}), 404
    listing = dict(listing)
    if safe_int(listing["seller_id"]) != safe_int(user["id"]):
        conn.close()
        return jsonify({"error": "Нет прав"}), 403
    if listing.get("status") == "blocked":
        conn.close()
        return jsonify({"error": "Объявление заблокировано модерацией. Можно только удалить."}), 403
    if listing["status"] not in ("unpublished", "rejected", "sold", "completed", "closed"):
        conn.close()
        return jsonify({"error": "Нельзя опубликовать в этом статусе"}), 400
    now = datetime.now(MSK_TZ).isoformat()
    cur.execute("UPDATE listings SET status = 'pending', updated_at = ? WHERE id = ?", (now, listing_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": "pending"})

@app.route("/api/listings/<int:listing_id>", methods=["DELETE"])
def api_delete_listing(listing_id):
    init_data = extract_init_data()
    if not init_data:
        return jsonify({"error": "No initData"}), 401

    user = validate_init_data(init_data)
    if not user:
        return jsonify({"error": "Invalid initData"}), 401

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM listings WHERE id = ?', (listing_id,))
    listing = cursor.fetchone()
    if not listing:
        conn.close()
        return jsonify({"error": "Объявление не найдено"}), 404

    listing = dict(listing)
    role = get_user_role(user["id"])
    if listing["seller_id"] != user["id"] and role < MODERATOR_ROLE:
        conn.close()
        return jsonify({"error": "Недостаточно прав"}), 403

    # продавец не может удалить на возврате; модер+ может удалить любое
    if listing.get("status") == "refund_moderation" and role < MODERATOR_ROLE:
        conn.close()
        return jsonify({"error": "Нельзя удалить объявление во время модерации возврата"}), 400

    cursor.execute("UPDATE listings SET status = 'deleted', updated_at = ? WHERE id = ?", (
        datetime.now(MSK_TZ).isoformat(), listing_id
    ))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ==================== ДЕКОРАТОРЫ ====================

def safe_execute(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка в {func.__name__}: {e}", exc_info=True)
            for arg in args:
                if hasattr(arg, 'chat'):
                    try:
                        bot.send_message(
                            arg.chat.id,
                            f"❌ Произошла ошибка. Мы уже работаем над её исправлением.\n"
                            f"Код ошибки: {str(e)[:50]}"
                        )
                    except:
                        pass
                    break
            return None
    return wrapper


def log_callback(func):
    @wraps(func)
    def wrapper(call, *args, **kwargs):
        try:
            logger.info(f"Callback: {call.data} от пользователя {call.from_user.id}")
            return func(call, *args, **kwargs)
        except Exception as e:
            logger.error(f"Ошибка в callback {call.data}: {e}", exc_info=True)
            try:
                bot.answer_callback_query(
                    call.id,
                    f"❌ Ошибка: {str(e)[:100]}",
                    show_alert=True
                )
            except:
                pass
            return None
    return wrapper


def require_role(min_role_level):
    def decorator(func):
        @wraps(func)
        def wrapper(message, *args, **kwargs):
            user_id = message.from_user.id if hasattr(message, 'from_user') else message.chat.id
            if hasattr(message, 'chat') and hasattr(message.chat, 'id'):
                user_id = message.from_user.id

            user_role = get_user_role(user_id)
            if ROLE_LEVELS.get(user_role, 0) < min_role_level:
                bot.send_message(
                    message.chat.id if hasattr(message, 'chat') else user_id,
                    "❌ У вас недостаточно прав для выполнения этой команды!"
                )
                return None
            return func(message, *args, **kwargs)
        return wrapper
    return decorator


def require_admin(func):
    return require_role(ROLE_CHANGE_ROLE)(func)


def require_panel_access(func):
    return require_role(PANEL_ACCESS_ROLE)(func)


# ==================== БАЗА ДАННЫХ ====================


_original_sqlite_connect = sqlite3.connect

def _sqlite_connect_patched(database, *args, **kwargs):
    if database == DB_PATH or (isinstance(database, str) and database.endswith('.db')):
        kwargs.setdefault('timeout', 30)
        kwargs.setdefault('check_same_thread', False)
        conn = _original_sqlite_connect(database, *args, **kwargs)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=30000')
        except Exception:
            pass
        return conn
    return _original_sqlite_connect(database, *args, **kwargs)

sqlite3.connect = _sqlite_connect_patched

def get_db_connection():
    """SQLite connection safe for bot thread + Flask threads."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
    except Exception:
        pass
    return conn



@safe_execute
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                balance INTEGER DEFAULT 0,
                role INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_active TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bot_name TEXT NOT NULL,
                bot_username TEXT,
                bot_token TEXT,
                description TEXT,
                price INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bot_id INTEGER,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                buyer_id INTEGER NOT NULL,
                bot_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                price INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (buyer_id) REFERENCES users (user_id),
                FOREIGN KEY (seller_id) REFERENCES users (user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                method TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                completed_at TEXT,
                comment TEXT,
                recipient TEXT,
                recipient_name TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                price INTEGER DEFAULT 0,
                bot_username TEXT,
                status TEXT DEFAULT 'pending',
                listing_type TEXT DEFAULT 'own',
                contract_number TEXT,
                analytics_consent INTEGER DEFAULT 0,
                product_type TEXT,
                tgstat_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (seller_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                listing_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, listing_id),
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
                FOREIGN KEY (listing_id) REFERENCES listings (id) ON DELETE CASCADE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                age INTEGER NOT NULL,
                employment TEXT NOT NULL,
                username TEXT,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
            )
        ''')

        cursor.execute("PRAGMA table_info(listings)")
        listing_cols = [column[1] for column in cursor.fetchall()]
        if listing_cols:
            if 'listing_type' not in listing_cols:
                cursor.execute("ALTER TABLE listings ADD COLUMN listing_type TEXT DEFAULT 'own'")
            if 'contract_number' not in listing_cols:
                cursor.execute("ALTER TABLE listings ADD COLUMN contract_number TEXT")
            if 'analytics_consent' not in listing_cols:
                cursor.execute("ALTER TABLE listings ADD COLUMN analytics_consent INTEGER DEFAULT 0")
            if 'product_type' not in listing_cols:
                cursor.execute("ALTER TABLE listings ADD COLUMN product_type TEXT")
            if 'tgstat_url' not in listing_cols:
                cursor.execute("ALTER TABLE listings ADD COLUMN tgstat_url TEXT")


        cursor.execute("PRAGMA table_info(users)")
        _ucols = [c[1] for c in cursor.fetchall()]
        
        cursor.execute("PRAGMA table_info(users)")
        _ucols2 = [c[1] for c in cursor.fetchall()]
        if 'photo_url' not in _ucols2:
            cursor.execute('ALTER TABLE users ADD COLUMN photo_url TEXT')
        cursor.execute("PRAGMA table_info(listings)")
        _lcols = [c[1] for c in cursor.fetchall()]
        if 'has_private_files' not in _lcols:
            cursor.execute('ALTER TABLE listings ADD COLUMN has_private_files INTEGER DEFAULT 0')
        if 'files_meta' not in _lcols:
            cursor.execute('ALTER TABLE listings ADD COLUMN files_meta TEXT')
        if 'views_count' not in _lcols:
            cursor.execute('ALTER TABLE listings ADD COLUMN views_count INTEGER DEFAULT 0')

        cursor.execute("PRAGMA table_info(listings)")
        _lcols_ex = [c[1] for c in cursor.fetchall()]
        if 'users_month' not in _lcols_ex:
            cursor.execute('ALTER TABLE listings ADD COLUMN users_month INTEGER DEFAULT 0')
        if 'earnings_month' not in _lcols_ex:
            cursor.execute('ALTER TABLE listings ADD COLUMN earnings_month REAL DEFAULT 0')

        cursor.execute("PRAGMA table_info(orders)")
        _ocols_rc = [c[1] for c in cursor.fetchall()]
        if 'reject_comment' not in _ocols_rc:
            cursor.execute('ALTER TABLE orders ADD COLUMN reject_comment TEXT')


        if 'rating_seller' not in _ucols:
            cursor.execute('ALTER TABLE users ADD COLUMN rating_seller REAL DEFAULT 5.0')
        if 'rating_buyer' not in _ucols:
            cursor.execute('ALTER TABLE users ADD COLUMN rating_buyer REAL DEFAULT 5.0')
        if 'rating_staff' not in _ucols:
            cursor.execute('ALTER TABLE users ADD COLUMN rating_staff REAL DEFAULT 5.0')

        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'balance' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0')
        if 'last_active' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN last_active TEXT')
        if 'role' not in columns:
            cursor.execute('ALTER TABLE users ADD COLUMN role INTEGER DEFAULT 0')

        cursor.execute("PRAGMA table_info(requests)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'comment' not in columns:
            cursor.execute('ALTER TABLE requests ADD COLUMN comment TEXT')
        if 'recipient' not in columns:
            cursor.execute('ALTER TABLE requests ADD COLUMN recipient TEXT')
        if 'recipient_name' not in columns:
            cursor.execute('ALTER TABLE requests ADD COLUMN recipient_name TEXT')

        now = datetime.now(MSK_TZ).isoformat()

        for user_id, role in MANDATORY_USERS.items():
            cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name, balance, role, created_at, last_active)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, "", "Staff", "", 0, role, now, now))
                logger.info(f"✅ Создан обязательный пользователь {user_id} с ролью {role}")
            else:
                cursor.execute('UPDATE users SET role = ? WHERE user_id = ?', (role, user_id))


        cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL,
                buyer_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT DEFAULT 'awaiting_seller',
                seller_sent_at TEXT,
                review_deadline TEXT,
                buyer_rating INTEGER,
                seller_rating INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_moderation (
                user_id INTEGER PRIMARY KEY,
                publish_blocked_until TEXT,
                publish_reason TEXT,
                is_banned INTEGER DEFAULT 0,
                ban_reason TEXT,
                moderated_by INTEGER,
                updated_at TEXT
            )
        ''')


        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT,
                views_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute("SELECT id FROM news WHERE title = ?", ("Добро пожаловать в X-Cod Exchange",))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO news (title, body, views_count, status, created_at) VALUES (?, ?, 0, 'active', ?)",
                (
                    "Добро пожаловать в X-Cod Exchange",
                    "Платформа для безопасной покупки и продажи Telegram-ботов запущена. Следите за обновлениями!",
                    now,
                ),
            )

        # pinned listings re-seeded via ensure_pinned_listings after commit
        conn.commit()
        conn.close()
        logger.info("✅ База данных успешно инициализирована")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}", exc_info=True)
        return False


init_db()
ensure_pinned_listings()


# ==================== ФУНКЦИИ БАЗЫ ДАННЫХ ====================

@safe_execute
def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT user_id, username, first_name, last_name, balance, role, created_at, last_active, '
            'COALESCE(rating_seller, 5.0) as rating_seller, COALESCE(rating_buyer, 5.0) as rating_buyer, '
            'COALESCE(rating_staff, 5.0) as rating_staff, photo_url '
            'FROM users WHERE user_id = ?',
            (safe_int(user_id),)
        )
        result = cursor.fetchone()
    except Exception:
        cursor.execute(
            'SELECT user_id, username, first_name, last_name, balance, role, created_at, last_active, photo_url '
            'FROM users WHERE user_id = ?',
            (safe_int(user_id),)
        )
        result = cursor.fetchone()
    conn.close()
    if not result:
        return None
    d = dict(result)
    d.setdefault('rating_seller', 5.0)
    d.setdefault('rating_buyer', 5.0)
    d.setdefault('rating_staff', 5.0)
    return d


@safe_execute
def get_user_role(user_id):
    user = get_user(user_id)
    return user['role'] if user else 0


def get_user_moderation(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('SELECT * FROM user_moderation WHERE user_id = ?', (safe_int(user_id),))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None



def ensure_mod_actions_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moderation_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            days INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)



def ensure_user_moderation_schema(cur=None):
    close = False
    if cur is None:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        close = True
    else:
        conn = None
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_moderation (
            user_id INTEGER PRIMARY KEY,
            publish_blocked_until TEXT,
            publish_reason TEXT,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            ban_until TEXT,
            moderated_by INTEGER,
            updated_at TEXT
        )
    """)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(user_moderation)").fetchall()}
    for col, ddl in [
        ("ban_until", "ALTER TABLE user_moderation ADD COLUMN ban_until TEXT"),
        ("ban_reason", "ALTER TABLE user_moderation ADD COLUMN ban_reason TEXT"),
        ("publish_blocked_until", "ALTER TABLE user_moderation ADD COLUMN publish_blocked_until TEXT"),
        ("publish_reason", "ALTER TABLE user_moderation ADD COLUMN publish_reason TEXT"),
        ("is_banned", "ALTER TABLE user_moderation ADD COLUMN is_banned INTEGER DEFAULT 0"),
        ("moderated_by", "ALTER TABLE user_moderation ADD COLUMN moderated_by INTEGER"),
        ("updated_at", "ALTER TABLE user_moderation ADD COLUMN updated_at TEXT"),
    ]:
        if col not in cols:
            try:
                cur.execute(ddl)
            except Exception:
                pass
    if close:
        conn.commit()
        conn.close()


def log_moderation_action(actor_id, target_id, action_type, days=None, reason=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        ensure_mod_actions_schema(cur)
        now = datetime.now(MSK_TZ).isoformat()
        cur.execute(
            "INSERT INTO moderation_actions (actor_id, target_id, action_type, days, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (safe_int(actor_id), safe_int(target_id), action_type, days, reason or "", now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("log_moderation_action: %s", e)



def notify_if_banned(user_id, chat_id=None):
    """Если пользователь забанен — сообщение и True (заблокировать обработку)."""
    if not is_user_banned(user_id):
        return False
    info = get_ban_info(user_id) or {}
    until = info.get("until") or ""
    until_s = until[:19].replace("T", " ") if until else "—"
    text = "❌ Администрация сервиса заблокировала вам аккаунт.\n\nДо разблокировки: {}\nБот и Mini App недоступны.".format(until_s)
    try:
        bot.send_message(chat_id or user_id, text)
    except Exception:
        pass
    return True


def is_user_banned(user_id):
    m = get_user_moderation(user_id)
    if not m or not safe_int(m.get('is_banned')):
        return False
    until = m.get('ban_until')
    if until:
        try:
            # support iso strings
            u = until.replace('Z', '+00:00') if isinstance(until, str) else until
            end = datetime.fromisoformat(u)
            if end.tzinfo is None:
                end = MSK_TZ.localize(end)
            if datetime.now(MSK_TZ) >= end:
                # auto-lift
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cur = conn.cursor()
                    cur.execute("UPDATE user_moderation SET is_banned=0 WHERE user_id=?", (safe_int(user_id),))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
                return False
        except Exception:
            pass
    return True


def get_ban_info(user_id):
    m = get_user_moderation(user_id)
    if not m or not is_user_banned(user_id):
        return None
    return {
        "banned": True,
        "reason": m.get("ban_reason") or "",
        "until": m.get("ban_until"),
    }


def is_publish_blocked(user_id):
    m = get_user_moderation(user_id)
    if not m or not m.get('publish_blocked_until'):
        return False, None, None
    until = m.get('publish_blocked_until')
    try:
        now_s = datetime.now(MSK_TZ).isoformat()
        if until > now_s:
            return True, m.get('publish_reason'), until
    except Exception:
        return True, m.get('publish_reason'), until
    return False, None, None



@safe_execute
def can_change_role(admin_user_id, target_role):
    admin_role = get_user_role(admin_user_id)

    if target_role == 6:
        return admin_role == CREATOR_ASSIGN_ROLE

    if admin_role >= ROLE_CHANGE_ROLE:
        return target_role <= admin_role

    return False


@safe_execute
def set_user_role(user_id, role, admin_user_id=None):
    if user_id in MANDATORY_USERS:
        logger.warning(f"Попытка изменить роль обязательного пользователя {user_id}")
        return False

    if admin_user_id:
        if not can_change_role(admin_user_id, role):
            logger.warning(f"Пользователь {admin_user_id} пытался назначить роль {role} пользователю {user_id}")
            return False

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET role = ? WHERE user_id = ?', (role, safe_int(user_id)))
    conn.commit()
    conn.close()
    logger.info(f"Пользователю {user_id} назначена роль {role}")
    return True


@safe_execute
def add_user(user_id, username=None, first_name=None, last_name=None):
    user_id = safe_int(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    if not cursor.fetchone():
        now = datetime.now(MSK_TZ).isoformat()
        role = MANDATORY_USERS.get(user_id, 0)
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, last_name, balance, role, created_at, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username or "", first_name or "", last_name or "", 0, role, now, now))
        conn.commit()
        logger.info(f"Добавлен новый пользователь: {user_id} с ролью {role}")
        return True
    else:
        cursor.execute('UPDATE users SET last_active = ? WHERE user_id = ?',
                       (datetime.now(MSK_TZ).isoformat(), user_id))
        conn.commit()
        conn.close()
        return False


@safe_execute
def get_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (safe_int(user_id),))
    result = cursor.fetchone()
    conn.close()
    return safe_int(result[0] if result else 0)


@safe_execute
def update_balance(user_id, amount):
    user_id = safe_int(user_id)
    amount = safe_int(amount)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    return True


@safe_execute
def add_transaction(user_id, transaction_type, amount, description="", bot_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now(MSK_TZ).isoformat()
    cursor.execute('''
        INSERT INTO transactions (user_id, bot_id, type, amount, description, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (safe_int(user_id), bot_id, transaction_type, safe_int(amount), description, now))
    conn.commit()
    conn.close()
    return True


@safe_execute
def add_request(user_id, request_type, amount, method="", comment="", recipient="", recipient_name=""):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now(MSK_TZ).isoformat()
    cursor.execute('''
        INSERT INTO requests 
        (user_id, type, amount, method, status, created_at, updated_at, comment, recipient, recipient_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
    safe_int(user_id), request_type, safe_int(amount), method, 'pending', now, now, comment, recipient, recipient_name))
    conn.commit()
    request_id = cursor.lastrowid
    conn.close()
    logger.info(f"Создана заявка #{request_id}: {request_type} {amount}$ для пользователя {user_id}")
    return request_id


@safe_execute
def get_request_by_id(request_id, user_id=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if user_id:
        cursor.execute('''
            SELECT id, user_id, type, amount, method, status, created_at, updated_at, completed_at, 
                   comment, recipient, recipient_name
            FROM requests 
            WHERE id = ? AND user_id = ?
        ''', (safe_int(request_id), safe_int(user_id)))
    else:
        cursor.execute('''
            SELECT id, user_id, type, amount, method, status, created_at, updated_at, completed_at, 
                   comment, recipient, recipient_name
            FROM requests 
            WHERE id = ?
        ''', (safe_int(request_id),))
    result = cursor.fetchone()
    conn.close()
    return dict(result) if result else None


@safe_execute
def update_request_status(request_id, status, comment=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now(MSK_TZ).isoformat()

    if status == 'completed':
        if comment:
            cursor.execute('''
                UPDATE requests 
                SET status = ?, updated_at = ?, completed_at = ?, comment = ? 
                WHERE id = ?
            ''', (status, now, now, comment, safe_int(request_id)))
        else:
            cursor.execute('''
                UPDATE requests 
                SET status = ?, updated_at = ?, completed_at = ? 
                WHERE id = ?
            ''', (status, now, now, safe_int(request_id)))
    else:
        if comment:
            cursor.execute('''
                UPDATE requests 
                SET status = ?, updated_at = ?, comment = ? 
                WHERE id = ?
            ''', (status, now, comment, safe_int(request_id)))
        else:
            cursor.execute('''
                UPDATE requests 
                SET status = ?, updated_at = ? 
                WHERE id = ?
            ''', (status, now, safe_int(request_id)))

    conn.commit()
    conn.close()
    logger.info(f"Обновлен статус заявки #{request_id} на {status}")
    return True


@safe_execute
def get_user_requests(user_id, limit=50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, type, amount, method, status, created_at, updated_at, completed_at, 
               comment, recipient, recipient_name
        FROM requests 
        WHERE user_id = ? AND status != 'cancelled'
        ORDER BY created_at DESC 
        LIMIT ?
    ''', (safe_int(user_id), limit))
    results = cursor.fetchall()
    conn.close()
    return [dict(row) for row in results]


@safe_execute
def get_pending_requests(limit=100):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT r.id, r.user_id, r.type, r.amount, r.method, r.status, r.created_at, r.updated_at,
               r.comment, r.recipient, r.recipient_name,
               u.username as username, u.first_name as first_name, u.last_name as last_name
        FROM requests r
        LEFT JOIN users u ON u.user_id = r.user_id
        WHERE r.status = 'processing'
        ORDER BY r.created_at ASC
        LIMIT ?
    ''', (limit,))
    results = cursor.fetchall()
    conn.close()
    return [dict(row) for row in results]


@safe_execute
def get_all_users(limit=1000):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, username, first_name, last_name, balance, role, created_at, last_active
        FROM users
        ORDER BY role DESC, user_id ASC
        LIMIT ?
    ''', (limit,))
    results = cursor.fetchall()
    conn.close()
    return [dict(row) for row in results]


@safe_execute
def get_staff_users(limit=1000):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT user_id, username, first_name, last_name, balance, role, created_at, last_active
        FROM users
        WHERE role >= ?
        ORDER BY role DESC, user_id ASC
        LIMIT ?
    ''', (STAFF_MIN_ROLE, limit))
    results = cursor.fetchall()
    conn.close()
    return [dict(row) for row in results]


@safe_execute
def get_user_bots(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, bot_name, bot_username, description, price, status, created_at, updated_at '
        'FROM user_bots WHERE user_id = ? ORDER BY created_at DESC',
        (safe_int(user_id),)
    )
    results = cursor.fetchall()
    conn.close()
    return [dict(row) for row in results]


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def safe_int(value, default=0):
    try:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        return int(value)
    except (ValueError, TypeError):
        return default


def get_usd_to_rub():
    try:
        response = requests.get('https://api.exchangerate-api.com/v4/latest/USD', timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data['rates'].get('RUB', 90)
    except:
        pass

    try:
        response = requests.get('https://www.cbr-xml-daily.ru/daily_json.js', timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data['Valute']['USD']['Value']
    except:
        pass

    return 90


def delete_previous_messages(chat_id, user_id, keep_last=False):
    if user_id not in user_messages:
        return

    messages = user_messages[user_id]
    if keep_last and len(messages) > 1:
        messages_to_delete = messages[:-1]
    else:
        messages_to_delete = messages

    for msg_id in messages_to_delete:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logger.debug(f"Не удалось удалить сообщение {msg_id}: {e}")

    if keep_last and len(messages) > 0:
        user_messages[user_id] = [messages[-1]]
    else:
        user_messages[user_id] = []


def save_message(chat_id, user_id, message_id):
    if user_id not in user_messages:
        user_messages[user_id] = []
    user_messages[user_id].append(message_id)

    if len(user_messages[user_id]) > 50:
        user_messages[user_id] = user_messages[user_id][-30:]


# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@bot.message_handler(commands=['start'])
@safe_execute
def send_welcome(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if notify_if_banned(user_id, chat_id):
        return

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)

    add_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    show_subscription_menu(chat_id, user_id)


@bot.message_handler(commands=['menu'])
@safe_execute
def cmd_menu(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)
    show_main_menu(chat_id, user_id)


@bot.message_handler(commands=['balance'])
@safe_execute
def cmd_balance(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)
    show_balance(chat_id, user_id)


@bot.message_handler(commands=['help'])
@safe_execute
def cmd_help(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)
    show_help(chat_id, user_id)


@bot.message_handler(commands=['support'])
@safe_execute
def cmd_support(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)
    show_support(chat_id, user_id)


@bot.message_handler(commands=['panel'])
@require_panel_access
@safe_execute
def admin_panel(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    delete_previous_messages(chat_id, user_id)
    save_message(chat_id, user_id, message.message_id)

    show_admin_panel(chat_id, user_id)


# ==================== МЕНЮ ====================

def show_subscription_menu(chat_id, user_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    subscribe_btn = types.InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_LINK)
    check_btn = types.InlineKeyboardButton("✅ Я подписался! Начать работать", callback_data="check_subscription")
    markup.add(subscribe_btn, check_btn)

    msg = bot.send_message(
        chat_id,
        "🔒 Для использования бота подпишитесь на канал!\n\nПосле подписки нажмите кнопку ниже:",
        reply_markup=markup
    )
    save_message(chat_id, user_id, msg.message_id)


def show_main_menu(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    add_user(user_id)
    user_data[user_id] = user_data.get(user_id, {})
    user_data[user_id]["in_menu"] = True

    markup = types.InlineKeyboardMarkup(row_width=2)
    bots_btn = types.InlineKeyboardButton("🤖 Мои боты", callback_data="my_bots")
    balance_btn = types.InlineKeyboardButton("💵 Баланс", callback_data="balance")
    help_btn = types.InlineKeyboardButton("📄 Помощь", callback_data="info")
    support_btn = types.InlineKeyboardButton("💬 Поддержка", callback_data="support")
    markup.add(bots_btn, balance_btn)
    markup.add(help_btn, support_btn)

    user_role = get_user_role(user_id)
    if user_role >= PANEL_ACCESS_ROLE:
        panel_btn = types.InlineKeyboardButton("🛡 Админ-панель", callback_data="admin_panel")
        markup.add(panel_btn)

    msg_text = (
        f"🗂 Меню\n\n"
        f"<b>X-Cod Exchange</b> - честная и справедливая платформа для продажи и покупки телеграм ботов.\n\n"
        f"• У каждого продавца и покупателя есть свой рейтинг и отзывы\n"
        f"• Мы берем ответственность за каждую сделку"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode='html')
    save_message(chat_id, user_id, msg.message_id)


def show_balance(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    balance = get_balance(user_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    deposit_btn = types.InlineKeyboardButton("💰 Пополнение", callback_data="deposit")
    withdraw_btn = types.InlineKeyboardButton("💳 Вывод", callback_data="withdraw")
    requests_btn = types.InlineKeyboardButton("📋 Мои заявки", callback_data="my_requests")
    back_btn = types.InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="back_to_hub")

    markup.add(deposit_btn, withdraw_btn)
    markup.add(requests_btn)
    markup.add(back_btn)

    msg_text = f"💵 <b>Баланс</b>\n\n<b>Текущий баланс:</b> {balance} $"

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


def show_deposit_menu(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    markup = types.InlineKeyboardMarkup(row_width=1)
    card_btn = types.InlineKeyboardButton("Перевод по СБП", callback_data="deposit_card")
    crypto_btn = types.InlineKeyboardButton("Криптовалюта (USDT)", callback_data="deposit_crypto")
    stars_btn = types.InlineKeyboardButton("Telegram Stars", callback_data="deposit_stars")
    back_btn = types.InlineKeyboardButton("⬅️ Назад к балансу", callback_data="balance")

    markup.add(card_btn, crypto_btn, stars_btn, back_btn)

    msg_text = (
        f"💰 <b>Пополнение баланса</b>\n\n"
        f"Выберите способ пополнения:\n\n"
        f"• <b>Перевод по СБП</b> - быстрый перевод на карту\n"
        f"• <b>Криптовалюта</b> - перевод в USDT (TRC20)\n"
        f"• <b>Telegram Stars</b> - оплата через звезды\n\n"
        f"Минимальная сумма: <b>1$</b>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


def show_withdraw_menu(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    balance = get_balance(user_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    card_btn = types.InlineKeyboardButton("На карту", callback_data="withdraw_card")
    crypto_btn = types.InlineKeyboardButton("Криптовалюта", callback_data="withdraw_crypto")
    back_btn = types.InlineKeyboardButton("⬅️ Назад к балансу", callback_data="balance")

    markup.add(card_btn, crypto_btn)
    markup.add(back_btn)

    msg_text = (
        f"💳 <b>Вывод средств</b>\n\n"
        f"<b>Доступно для вывода:</b> {balance} $\n"
        f"<b>Минимальная сумма:</b> {MIN_WITHDRAW} $\n\n"
        f"Выберите способ вывода:"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


def show_my_bots(chat_id, user_id):
    bots = get_user_bots(user_id)

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="back_to_hub")
    markup.add(back_btn)

    if bots:
        msg_text = f"🤖 <b>Мои боты</b>\n\n<b>Всего:</b> {len(bots)}\n\n"
        for i, bot_data in enumerate(bots[:10], 1):
            status_text = "Активен" if bot_data['status'] == "active" else "Неактивен"
            msg_text += (
                f"{i}. <b>{bot_data['bot_name']}</b>\n"
                f"<b>Цена:</b> {bot_data['price']} монет\n"
                f"<b>Статус:</b> {status_text}\n\n"
            )
        if len(bots) > 10:
            msg_text += f"... и еще {len(bots) - 10} ботов\n\n"
    else:
        msg_text = "📭 <i>У вас пока нет ботов.</i>"

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


def show_my_requests(chat_id, user_id):
    requests_list = get_user_requests(user_id)

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("⬅️ Назад к балансу", callback_data="balance")
    markup.add(back_btn)

    if not requests_list:
        msg_text = "📭 <i>У вас нет активных заявок.</i>"
        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        return

    pending_requests = [r for r in requests_list if r['status'] == 'pending']

    for req in pending_requests:
        type_emoji = "💰" if req['type'] == 'deposit' else "💳"
        type_name = "Пополнение" if req['type'] == 'deposit' else "Вывод"

        method_names = {
            'card': 'СБП',
            'crypto': 'USDT',
            'stars': 'Stars'
        }.get(req['method'], req['method'])

        markup_row = types.InlineKeyboardMarkup(row_width=2)
        confirm_btn = types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_request_{req['id']}")
        cancel_btn = types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_request_{req['id']}")
        markup_row.add(confirm_btn, cancel_btn)

        msg_text = (
            f"{type_emoji} <b>{type_name}</b>\n"
            f"<b>Сумма:</b> {req['amount']} $\n"
            f"<b>Метод:</b> {method_names}\n"
        )

        if req.get('recipient'):
            msg_text += f"<b>Получатель:</b> {req['recipient']}\n"
        if req.get('recipient_name'):
            msg_text += f"<b>Имя:</b> {req['recipient_name']}\n"
        if req.get('comment'):
            msg_text += f"<b>Комментарий:</b> {req['comment']}\n"
        msg_text += "\n"

        bot.send_message(chat_id, msg_text, reply_markup=markup_row, parse_mode="html")

    other_requests = [r for r in requests_list if r['status'] in ['processing', 'completed']]
    if other_requests:
        msg_text = "📊 <b>Остальные заявки:</b>\n\n"
        for req in other_requests[:10]:
            type_emoji = "💰" if req['type'] == 'deposit' else "💳"
            type_name = "Пополнение" if req['type'] == 'deposit' else "Вывод"
            status_names = {'processing': 'В обработке', 'completed': 'Выполнено'}.get(req['status'], 'Неизвестно')

            msg_text += (
                f"{type_emoji} <b>{type_name}</b>\n"
                f"<b>Сумма:</b> {req['amount']} $\n"
                f"<b>Статус:</b> {status_names}\n"
            )
            if req.get('recipient'):
                msg_text += f"<b>Получатель:</b> {req['recipient']}\n"
            msg_text += "\n"

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)


def show_help(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="back_to_hub")
    markup.add(back_btn)

    msg_text = (
        f"📄<b>Помощь</b>\n\n"
        f"<i>Навигатор команд:</i>\n\n"
        f"/menu - 🗂 <i>Главное меню</i>\n"
        f"/mybots - 🤖 <i>Мои Боты</i>\n"
        f"/balance - 💵 <i>Баланс</i>\n"
        f"/help - 📄 <i>Помощь</i>\n"
        f"/support - 💬 <i>Поддержка</i>\n"
        f"/panel - 🛡 <i>Админ-панель</i> (для сотрудников от Корпоративного)"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


def show_support(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="back_to_hub")
    markup.add(back_btn)

    msg_text = (
        f"💬 <b>Поддержка</b>\n\n"
        f"<b>Если у вас возникли вопросы:</b>\n\n"
        f"📢 <b>Напишите нам:</b>\n\n"
        f"   • @support_bot\n"
        f"   • support@email.com\n\n"
        f"⏱ <i>Время ответа: обычно в течение 24 часов.</i>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


# ==================== АДМИН-ПАНЕЛЬ ====================

def show_admin_panel(chat_id, user_id, keep_previous=False):
    if not keep_previous:
        delete_previous_messages(chat_id, user_id)

    user_role = get_user_role(user_id)
    role_name = ROLES.get(user_role, "Неизвестно")

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("🗓️ Рабочий центр", callback_data="admin_workcenter"))
    markup.add(types.InlineKeyboardButton("📈 Трекер должностного роста", callback_data="admin_career"))
    markup.add(types.InlineKeyboardButton("📄 Модерация объявлений", callback_data="admin_listings"))
    markup.add(types.InlineKeyboardButton("💳 Модерация транзакций", callback_data="admin_requests"))
    if user_role >= ROLE_CHANGE_ROLE:
        markup.add(types.InlineKeyboardButton("💼 Модерация заявок на работу", callback_data="admin_jobs"))
    markup.add(types.InlineKeyboardButton("⚠️ Модерация жалоб", callback_data="admin_complaints"))
    if user_role >= ROLE_CHANGE_ROLE:
        markup.add(types.InlineKeyboardButton("👥 Управление сотрудниками", callback_data="admin_users"))
    markup.add(types.InlineKeyboardButton("📊 Аналитика", callback_data="admin_analytics"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub"))

    msg_text = (
        f"🛡 <b>Админ-панель</b>\n\n"
        f"<b>Ваша роль:</b> {role_name}\n"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_users(chat_id, user_id, page=1):
    delete_previous_messages(chat_id, user_id)

    user_role = get_user_role(user_id)
    if user_role < ROLE_CHANGE_ROLE:
        msg = bot.send_message(chat_id, "❌ У вас нет прав для управления пользователями")
        save_message(chat_id, user_id, msg.message_id)
        return

    all_users = get_staff_users(limit=100)
    total_users = len(all_users)
    start_idx = (page - 1) * 10
    end_idx = start_idx + 10
    users = all_users[start_idx:end_idx]

    markup = types.InlineKeyboardMarkup(row_width=2)

    if not users:
        markup.add(types.InlineKeyboardButton(
            "📭 Нет сотрудников",
            callback_data="admin_back"
        ))

    for user in users:
        role_name = ROLES.get(user['role'], "Неизвестно")
        mandatory = "⭐ " if user['user_id'] in MANDATORY_USERS else ""
        username = user['username'] or str(user['user_id'])
        btn_text = f"{mandatory}{role_name} {username[:20]}"
        markup.add(types.InlineKeyboardButton(
            btn_text,
            callback_data=f"admin_user_{user['user_id']}"
        ))

    add_staff_btn = types.InlineKeyboardButton("➕ Добавить сотрудника", callback_data="admin_add_staff")
    markup.add(add_staff_btn)

    nav_btns = []
    if page > 1:
        nav_btns.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_users_page_{page - 1}"))
    if end_idx < total_users:
        nav_btns.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"admin_users_page_{page + 1}"))
    if nav_btns:
        markup.add(*nav_btns)

    back_btn = types.InlineKeyboardButton("⬅️ Назад в админ-панель", callback_data="admin_back")
    markup.add(back_btn)

    msg_text = (
        f"👥 <b>Управление сотрудниками</b>\n\n"
        f"<b>Всего сотрудников:</b> {total_users}\n"
        f"<b>Страница:</b> {page}\n\n"
        f"⭐ - обязательный пользователь (роль нельзя изменить)\n"
        f"<i>Нажмите на сотрудника для изменения роли</i>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_add_staff(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)

    user_role = get_user_role(user_id)
    if user_role < ROLE_CHANGE_ROLE:
        msg = bot.send_message(chat_id, "❌ У вас нет прав для добавления сотрудников")
        save_message(chat_id, user_id, msg.message_id)
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_users")
    markup.add(back_btn)

    msg_text = (
        f"➕ <b>Добавление сотрудника</b>\n\n"
        f"Введите ID пользователя Telegram:\n"
        f"📌 ID можно узнать у бота @userinfobot\n\n"
        f"После ввода ID вы сможете выбрать роль для пользователя.\n"
        f"<i>Минимальная роль сотрудника: Партнер (1)</i>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)

    user_data[user_id]["mode"] = "admin_add_staff_id"


@safe_execute
def process_admin_add_staff_id(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    user_role = get_user_role(user_id)
    if user_role < ROLE_CHANGE_ROLE:
        msg = bot.send_message(chat_id, "❌ У вас нет прав для добавления сотрудников")
        save_message(chat_id, user_id, msg.message_id)
        return

    try:
        target_user_id = int(message.text.strip())
    except ValueError:
        msg = bot.send_message(
            chat_id,
            "❌ Пожалуйста, введите корректный числовой ID пользователя."
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    user_data[user_id]["add_staff_target_id"] = target_user_id

    existing_user = get_user(target_user_id)
    if existing_user:
        user_info = (
            f"<b>Имя:</b> {existing_user['first_name'] or 'Не указано'}\n"
            f"<b>Username:</b> @{existing_user['username'] or 'Не указан'}\n"
            f"<b>Текущая роль:</b> {ROLES.get(existing_user['role'], 'Неизвестно')}"
        )
    else:
        user_info = "⚠️ Пользователь не найден в базе. Он будет создан автоматически."

    markup = types.InlineKeyboardMarkup(row_width=2)

    available_roles = []
    if user_role >= 5:
        available_roles.append((1, "Партнер"))
        available_roles.append((2, "Корпоративный"))
        available_roles.append((3, "Стажер"))
        available_roles.append((4, "Модератор"))
        available_roles.append((5, "Админ"))

    if user_role >= 6:
        available_roles.append((6, "Создатель"))

    for role_id, role_name in available_roles:
        markup.add(types.InlineKeyboardButton(
            f"{role_name}",
            callback_data=f"admin_add_role_{target_user_id}_{role_id}"
        ))

    back_btn = types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_add_staff")
    markup.add(back_btn)

    delete_previous_messages(chat_id, user_id)

    msg_text = (
        f"🔑 <b>Выберите роль для пользователя</b>\n\n"
        f"<b>ID:</b> {target_user_id}\n"
        f"{user_info}\n\n"
        f"Выберите роль, которую хотите назначить:"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)

    user_data[user_id]["mode"] = ""


@safe_execute
def process_admin_add_staff_role(call, chat_id, user_id, target_user_id, role):
    role_name = ROLES.get(role, "Неизвестно")

    admin_role = get_user_role(user_id)
    if not can_change_role(user_id, role):
        bot.answer_callback_query(
            call.id,
            f"❌ У вас нет прав для назначения роли {role_name}",
            show_alert=True
        )
        return

    if role < STAFF_MIN_ROLE:
        bot.answer_callback_query(
            call.id,
            f"❌ Роль {role_name} не является сотрудником (минимальная роль - Партнер)",
            show_alert=True
        )
        return

    existing_user = get_user(target_user_id)
    if not existing_user:
        add_user(target_user_id, "", "Новый пользователь", "")
        existing_user = get_user(target_user_id)

    if target_user_id in MANDATORY_USERS:
        bot.answer_callback_query(
            call.id,
            f"❌ Нельзя изменить роль обязательного пользователя!",
            show_alert=True
        )
        return

    current_role = existing_user['role']
    if current_role >= role:
        bot.answer_callback_query(
            call.id,
            f"❌ Пользователь уже имеет роль {ROLES.get(current_role, 'Неизвестно')}\n"
            f"Его роль выше или равна {role_name}",
            show_alert=True
        )
        return

    set_user_role(target_user_id, role, user_id)

    try:
        bot.send_message(
            target_user_id,
            f"👑 <b>Ваша роль изменена!</b>\n\n"
            f"Вам назначена роль: {role_name}\n"
            f"Теперь вам доступны новые возможности в боте.",
            parse_mode="html"
        )
    except:
        pass

    bot.answer_callback_query(
        call.id,
        f"✅ {role_name} успешно добавлен!",
        show_alert=True
    )

    show_admin_users(chat_id, user_id)


@safe_execute
def show_admin_user_detail(chat_id, user_id, target_user_id):
    delete_previous_messages(chat_id, user_id)

    user = get_user(target_user_id)
    if not user:
        msg = bot.send_message(chat_id, "❌ Пользователь не найден")
        save_message(chat_id, user_id, msg.message_id)
        return

    current_role = user['role']
    current_role_name = ROLES.get(current_role, "Неизвестно")
    is_mandatory = target_user_id in MANDATORY_USERS

    markup = types.InlineKeyboardMarkup(row_width=2)

    admin_role = get_user_role(user_id)

    if admin_role >= ROLE_CHANGE_ROLE and not is_mandatory:
        for role_id, role_name in ROLES.items():
            if can_change_role(user_id, role_id) and role_id != current_role:
                markup.add(types.InlineKeyboardButton(
                    f"{role_name}",
                    callback_data=f"admin_set_role_{target_user_id}_{role_id}"
                ))

    if is_mandatory:
        markup.add(types.InlineKeyboardButton(
            "⭐ Обязательный пользователь",
            callback_data="admin_mandatory_info"
        ))

    if admin_role >= ROLE_CHANGE_ROLE:
        markup.add(types.InlineKeyboardButton(
            "💵 Изменить баланс",
            callback_data=f"admin_balance_{target_user_id}"
        ))

    back_btn = types.InlineKeyboardButton("⬅️ Назад к списку", callback_data="admin_users")
    markup.add(back_btn)

    msg_text = (
        f"👤 <b>Информация о сотруднике</b>\n\n"
        f"<b>ID:</b> {user['user_id']}\n"
        f"<b>Имя:</b> {user['first_name'] or 'Не указано'}\n"
        f"<b>Username:</b> @{user['username'] or 'Не указан'}\n"
        f"<b>Баланс:</b> {user['balance']}$\n"
        f"<b>Текущая роль:</b> {current_role_name}\n"
        f"<b>Обязательный:</b> {'Да' if is_mandatory else 'Нет'}\n"
        f"<b>Зарегистрирован:</b> {user['created_at'][:16] if user['created_at'] else 'Неизвестно'}\n"
        f"<b>Последняя активность:</b> {user['last_active'][:16] if user['last_active'] else 'Неизвестно'}\n\n"
    )

    if is_mandatory:
        msg_text += f"<i>⚠️ Роль обязательного пользователя нельзя изменить</i>"
    elif admin_role >= ROLE_CHANGE_ROLE:
        msg_text += f"<i>Выберите новую роль для сотрудника:</i>"
    else:
        msg_text += f"<i>У вас нет прав для изменения роли</i>"

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)



@safe_execute
def show_admin_workcenter(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📝 Записаться на смену", callback_data="admin_wc_dev"))
    markup.add(types.InlineKeyboardButton("✅ Мои смены", callback_data="admin_wc_dev"))
    markup.add(types.InlineKeyboardButton("📜 История моих смен", callback_data="admin_wc_dev"))
    markup.add(types.InlineKeyboardButton("🗂️ История действий", callback_data="admin_wc_dev"))
    markup.add(types.InlineKeyboardButton("🚫 Список блокировок", callback_data="admin_wc_dev"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"))
    msg = bot.send_message(
        chat_id,
        "🗓️ <b>Рабочий центр</b>\n\nВыберите раздел:",
        reply_markup=markup,
        parse_mode="html"
    )
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_career(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)
    role = get_user_role(user_id)
    role_name = ROLES.get(role, "Неизвестно")
    text = (
        f"📈 <b>Трекер должностного роста</b>\n\n"
        f"<b>Ваша роль:</b> {role_name}\n\n"
        f"<b>Стажёр → Модератор</b>\n"
        f"• 30 дней\n"
        f"• 12 смен\n"
        f"Статус: {'✅ Выполнено' if role >= 4 else '⏳ В процессе'}\n\n"
        f"<b>Модератор → Админ</b>\n"
        f"• 360 дней\n"
        f"• 150 смен\n"
        f"• Рейтинг сотрудника выше 4.75\n"
        f"Статус: {'✅ Выполнено' if role >= 5 else '⏳ В процессе'}\n\n"
        f"<i>Учёт смен и рейтинга подключится с рабочим центром.</i>"
    )
    markup = types.InlineKeyboardMarkup(row_width=1)
    if role in (3, 4):
        markup.add(types.InlineKeyboardButton("📨 Подать заявку на повышение", callback_data="admin_promo_dev"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"))
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_listings(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, seller_id, title, listing_type, price, status, created_at "
        "FROM listings WHERE status = 'pending' ORDER BY created_at ASC LIMIT 30"
    )
    items = [dict(r) for r in cursor.fetchall()]
    conn.close()

    markup = types.InlineKeyboardMarkup(row_width=1)
    if items:
        for l in items:
            tlabel = 'Партнер' if l.get('listing_type') == 'partner' else 'Свой'
            title = (l.get('title') or '')[:25]
            markup.add(types.InlineKeyboardButton(
                f"#{l['id']} {title} ({tlabel})",
                callback_data=f"admin_listing_{l['id']}"
            ))
    else:
        markup.add(types.InlineKeyboardButton("✅ Нет объявлений", callback_data="admin_back"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"))
    msg = bot.send_message(
        chat_id,
        f"📄 <b>Модерация объявлений</b>\n\nВсего: {len(items)}",
        reply_markup=markup,
        parse_mode="html"
    )
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_listing_detail(chat_id, user_id, listing_id):
    delete_previous_messages(chat_id, user_id)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM listings WHERE id = ?', (listing_id,))
    l = cursor.fetchone()
    conn.close()
    if not l:
        msg = bot.send_message(chat_id, "❌ Объявление не найдено")
        save_message(chat_id, user_id, msg.message_id)
        return
    l = dict(l)
    tlabel = 'Партнер X-Cod' if l.get('listing_type') == 'partner' else 'Свой товар'
    text = (
        f"📄 <b>Объявление #{l['id']}</b>\n\n"
        f"<b>Название:</b> {l['title']}\n"
        f"<b>Описание:</b> {l.get('description') or '—'}\n"
        f"<b>Тип:</b> {tlabel}\n"
        f"<b>Продавец:</b> {l['seller_id']}\n"
        f"<b>Цена:</b> {l.get('price') or 0}$\n"
    )
    if l.get('listing_type') == 'partner':
        text += f"<b>Договор:</b> {l.get('contract_number') or '—'}\n"
    else:
        text += f"<b>Вид:</b> {l.get('product_type') or '—'}\n"
        if l.get('bot_username'):
            text += f"<b>Бот:</b> @{l['bot_username']}\n"
        if l.get('tgstat_url'):
            text += f"<b>TGStat:</b> {l['tgstat_url']}\n"

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_lapprove_{listing_id}"))
    markup.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_lreject_{listing_id}"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_listings"))
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_jobs(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM job_applications WHERE status = 'pending' ORDER BY created_at ASC LIMIT 30"
    )
    items = [dict(r) for r in cursor.fetchall()]
    conn.close()
    markup = types.InlineKeyboardMarkup(row_width=1)
    if items:
        for a in items:
            markup.add(types.InlineKeyboardButton(
                f"#{a['id']} {a['full_name'][:28]} ({a['age']})",
                callback_data=f"admin_job_{a['id']}"
            ))
    else:
        markup.add(types.InlineKeyboardButton("✅ Нет заявок", callback_data="admin_back"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"))
    msg = bot.send_message(
        chat_id,
        f"💼 <b>Заявки на работу</b>\n\nВсего: {len(items)}",
        reply_markup=markup,
        parse_mode="html"
    )
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_job_detail(chat_id, user_id, app_id):
    delete_previous_messages(chat_id, user_id)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM job_applications WHERE id = ?', (app_id,))
    a = cursor.fetchone()
    conn.close()
    if not a:
        msg = bot.send_message(chat_id, "❌ Заявка не найдена")
        save_message(chat_id, user_id, msg.message_id)
        return
    a = dict(a)
    labels = {
        "school": "Школьник", "student": "Студент", "working": "Работаю",
        "retired": "На пенсии", "searching": "Окончил обучение, в поисках работы"
    }
    text = (
        f"💼 <b>Заявка на работу #{a['id']}</b>\n\n"
        f"<b>ФИО:</b> {a['full_name']}\n"
        f"<b>Возраст:</b> {a['age']}\n"
        f"<b>Занятость:</b> {labels.get(a.get('employment'), a.get('employment'))}\n"
        f"<b>Username:</b> @{a.get('username') or '—'}\n"
        f"<b>Телефон:</b> {a.get('phone') or '—'}\n"
        f"<b>Почта:</b> {a.get('email') or '—'}\n"
        f"<b>TG ID:</b> {a['user_id']}\n"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"admin_jaccept_{app_id}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_jreject_{app_id}")
    )
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_jobs"))
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)



@safe_execute
def show_admin_requests(chat_id, user_id):
    delete_previous_messages(chat_id, user_id)

    pending_requests = get_pending_requests()

    markup = types.InlineKeyboardMarkup(row_width=1)

    if pending_requests:
        for req in pending_requests[:20]:
            type_emoji = "💰" if req['type'] == 'deposit' else "💳"
            type_name = "Пополнение" if req['type'] == 'deposit' else "Вывод"
            btn_text = f"{type_emoji} #{req['id']} - {req['amount']}$ ({type_name})"
            markup.add(types.InlineKeyboardButton(
                btn_text,
                callback_data=f"admin_request_{req['id']}"
            ))

        if len(pending_requests) > 20:
            markup.add(types.InlineKeyboardButton(
                f"📋 Еще {len(pending_requests) - 20} заявок...",
                callback_data="admin_requests_all"
            ))
    else:
        markup.add(types.InlineKeyboardButton(
            "✅ Нет заявок на рассмотрении",
            callback_data="admin_back"
        ))

    back_btn = types.InlineKeyboardButton("⬅️ Назад в админ-панель", callback_data="admin_back")
    markup.add(back_btn)

    msg_text = (
        f"📋 <b>Заявки на рассмотрение</b>\n\n"
        f"<b>Всего заявок:</b> {len(pending_requests)}\n\n"
        f"<i>Нажмите на заявку для управления</i>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def show_admin_request_detail(chat_id, user_id, request_id):
    delete_previous_messages(chat_id, user_id)

    request = get_request_by_id(request_id)
    if not request:
        msg = bot.send_message(chat_id, "❌ Заявка не найдена")
        save_message(chat_id, user_id, msg.message_id)
        return

    user = get_user(request['user_id'])
    username = f"@{user['username']}" if user and user['username'] else f"ID:{request['user_id']}"

    markup = types.InlineKeyboardMarkup(row_width=2)

    approve_btn = types.InlineKeyboardButton("✅ Одобрить", callback_data=f"admin_approve_{request_id}")
    reject_btn = types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_reject_{request_id}")
    markup.add(approve_btn, reject_btn)

    back_btn = types.InlineKeyboardButton("⬅️ Назад к списку", callback_data="admin_requests")
    markup.add(back_btn)

    type_emoji = "💰" if request['type'] == 'deposit' else "💳"
    type_name = "Пополнение" if request['type'] == 'deposit' else "Вывод"

    msg_text = (
        f"{type_emoji} <b>Заявка #{request['id']}</b>\n\n"
        f"<b>Тип:</b> {type_name}\n"
        f"<b>Пользователь:</b> {username}\n"
        f"<b>Сумма:</b> {request['amount']}$\n"
        f"<b>Метод:</b> {request['method'] or 'Не указан'}\n"
        f"<b>Комментарий:</b> {request['comment'] or 'Нет'}\n"
    )
    if request.get('recipient'):
        msg_text += f"<b>Получатель:</b> {request['recipient']}\n"
    if request.get('recipient_name'):
        msg_text += f"<b>Имя:</b> {request['recipient_name']}\n"
    msg_text += (
        f"\n<b>Создана:</b> {request['created_at'][:16] if request['created_at'] else 'Неизвестно'}\n"
        f"<b>Статус:</b> На рассмотрении\n\n"
        f"<i>Выберите действие:</i>"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


# ==================== ОБРАБОТЧИКИ ВЫВОДА ====================

@safe_execute
def process_withdraw_card(call, chat_id, user_id):
    delete_previous_messages(chat_id, user_id)

    user_data[user_id]["withdraw_method"] = "card"

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("⬅️ Назад к выбору способа", callback_data="withdraw")
    markup.add(back_btn)

    balance = get_balance(user_id)

    msg_text = (
        f"💳 <b>Вывод на карту</b>\n\n"
        f"<b>Доступно для вывода:</b> {balance} $\n"
        f"<b>Минимальная сумма:</b> {MIN_WITHDRAW} $\n\n"
        f"Введите сумму для вывода (в $):"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)

    user_data[user_id]["mode"] = "waiting_withdraw_amount"

    bot.answer_callback_query(call.id, "💳 Введите сумму для вывода")


@safe_execute
def process_withdraw_crypto(call, chat_id, user_id):
    delete_previous_messages(chat_id, user_id)

    user_data[user_id]["withdraw_method"] = "crypto"

    markup = types.InlineKeyboardMarkup(row_width=1)
    back_btn = types.InlineKeyboardButton("⬅️ Назад к выбору способа", callback_data="withdraw")
    markup.add(back_btn)

    balance = get_balance(user_id)

    msg_text = (
        f"₿ <b>Вывод на криптокошелек</b>\n\n"
        f"<b>Доступно для вывода:</b> {balance} $\n"
        f"<b>Минимальная сумма:</b> {MIN_WITHDRAW} $\n\n"
        f"Введите сумму для вывода (в $):"
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)

    user_data[user_id]["mode"] = "waiting_withdraw_amount"

    bot.answer_callback_query(call.id, "₿ Введите сумму для вывода")


@safe_execute
def process_withdraw_amount(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    try:
        amount = float(message.text.strip())

        if amount < MIN_WITHDRAW:
            msg = bot.send_message(
                chat_id,
                f"❌ Минимальная сумма вывода: {MIN_WITHDRAW}$\n"
                f"Пожалуйста, введите сумму больше или равную {MIN_WITHDRAW}$"
            )
            save_message(chat_id, user_id, msg.message_id)
            return

        balance = get_balance(user_id)
        if amount > balance:
            msg = bot.send_message(
                chat_id,
                f"❌ Недостаточно средств!\n"
                f"<b>Ваш баланс:</b> {balance}$\n"
                f"<b>Запрошено:</b> {amount}$"
            )
            save_message(chat_id, user_id, msg.message_id)
            return

        user_data[user_id]["withdraw_amount"] = int(amount)
        user_data[user_id]["mode"] = "waiting_withdraw_recipient"

        method = user_data[user_id].get("withdraw_method", "card")

        if method == "card":
            msg_text = (
                f"💳 <b>Введите реквизиты для вывода</b>\n\n"
                f"<b>Сумма:</b> {amount}$\n\n"
                f"Введите номер карты и инициалы владельца.\n"
                f"📝 Пример: `1234 5678 9012 3456, Иванов И.И.`\n\n"
                f"или введите по отдельности:\n"
                f"1️⃣ Сначала номер карты\n"
                f"2️⃣ Затем инициалы"
            )
        else:
            msg_text = (
                f"₿ <b>Введите реквизиты для вывода</b>\n\n"
                f"<b>Сумма:</b> {amount}$\n\n"
                f"Введите адрес криптокошелька (USDT TRC20):\n"
                f"📝 Пример: `TSM4p8JjU2AqC7Xqy8Zf9gH3kL5nP2rV6w`"
            )

        markup = types.InlineKeyboardMarkup(row_width=1)
        cancel_btn = types.InlineKeyboardButton("❌ Отменить операцию", callback_data="withdraw")
        markup.add(cancel_btn)

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)

    except ValueError:
        msg = bot.send_message(
            chat_id,
            "❌ Пожалуйста, введите число!\nПример: `10` - для вывода 10$",
            parse_mode="Markdown"
        )
        save_message(chat_id, user_id, msg.message_id)


@safe_execute
def process_withdraw_recipient(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    text = message.text.strip()

    if not text:
        msg = bot.send_message(
            chat_id,
            "❌ Реквизиты не могут быть пустыми!\nПожалуйста, введите реквизиты для вывода."
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    method = user_data[user_id].get("withdraw_method", "card")
    amount = user_data[user_id].get("withdraw_amount", 0)

    if amount <= 0:
        msg = bot.send_message(
            chat_id,
            "❌ Ошибка: сумма не указана. Пожалуйста, начните операцию заново."
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    recipient = ""
    recipient_name = ""
    comment = ""

    if method == "card":
        if ',' in text:
            parts = text.split(',', 1)
            recipient = parts[0].strip()
            recipient_name = parts[1].strip() if len(parts) > 1 else ""

            if recipient and recipient_name:
                create_withdraw_request(chat_id, user_id, amount, method, recipient, recipient_name, comment)
                return
        else:
            if user_data[user_id].get("withdraw_step") == "waiting_card_number":
                recipient = text
                user_data[user_id]["withdraw_recipient"] = recipient
                user_data[user_id]["withdraw_step"] = "waiting_card_name"
                user_data[user_id]["mode"] = "waiting_card_name"

                msg_text = (
                    f"📝 <b>Введите инициалы владельца карты</b>\n\n"
                    f"<b>Номер карты:</b> {recipient}\n\n"
                    f"Введите имя владельца (как на карте):\n"
                    f"📝 Пример: `Иванов И.И.`"
                )

                markup = types.InlineKeyboardMarkup(row_width=1)
                cancel_btn = types.InlineKeyboardButton("❌ Отменить операцию", callback_data="withdraw")
                markup.add(cancel_btn)

                msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
                save_message(chat_id, user_id, msg.message_id)
                return

            elif user_data[user_id].get("withdraw_step") == "waiting_card_name":
                recipient_name = text
                recipient = user_data[user_id].get("withdraw_recipient", "")

                if recipient:
                    create_withdraw_request(chat_id, user_id, amount, method, recipient, recipient_name, comment)
                    return
            else:
                recipient = text
                user_data[user_id]["withdraw_recipient"] = recipient
                user_data[user_id]["withdraw_step"] = "waiting_card_name"
                user_data[user_id]["mode"] = "waiting_card_name"

                msg_text = (
                    f"📝 <b>Введите инициалы владельца карты</b>\n\n"
                    f"<b>Номер карты:</b> {recipient}\n\n"
                    f"Введите имя владельца (как на карте):\n"
                    f"📝 Пример: `Иванов И.И.`"
                )

                markup = types.InlineKeyboardMarkup(row_width=1)
                cancel_btn = types.InlineKeyboardButton("❌ Отменить операцию", callback_data="withdraw")
                markup.add(cancel_btn)

                msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
                save_message(chat_id, user_id, msg.message_id)
                return
    else:
        recipient = text

        if not re.match(r'^T[A-Za-z0-9]{33}$', recipient):
            msg = bot.send_message(
                chat_id,
                "❌ Неверный формат адреса USDT TRC20!\n"
                "Адрес должен начинаться с 'T' и содержать 34 символа.\n"
                "📝 Пример: `TSM4p8JjU2AqC7Xqy8Zf9gH3kL5nP2rV6w`"
            )
            save_message(chat_id, user_id, msg.message_id)
            return

        comment = "Вывод USDT TRC20"
        create_withdraw_request(chat_id, user_id, amount, method, recipient, "", comment)


@safe_execute
def create_withdraw_request(chat_id, user_id, amount, method, recipient, recipient_name, comment):
    update_balance(user_id, -amount)

    request_id = add_request(
        user_id,
        'withdraw',
        amount,
        method,
        comment,
        recipient,
        recipient_name
    )

    if request_id:
        add_transaction(user_id, 'withdraw', amount, f"Вывод {amount}$ ({method}) - заявка #{request_id}")
        update_request_status(request_id, 'processing')

        for admin_id in ADMIN_IDS:
            try:
                admin_msg = (
                    f"🆕 <b>Новая заявка на вывод</b>\n\n"
                    f"<b>Заявка:</b> #{request_id}\n"
                    f"<b>Пользователь:</b> {user_id}\n"
                    f"<b>Сумма:</b> {amount}$\n"
                    f"<b>Метод:</b> {'Карта' if method == 'card' else 'Криптовалюта'}\n"
                    f"<b>Получатель:</b> {recipient}\n"
                )
                if recipient_name:
                    admin_msg += f"<b>Имя:</b> {recipient_name}\n"
                if comment:
                    admin_msg += f"<b>Комментарий:</b> {comment}\n"
                admin_msg += f"\n<i>Используйте админ-панель /panel для управления заявкой</i>"

                bot.send_message(admin_id, admin_msg, parse_mode="html")
            except:
                pass

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📋 Мои заявки", callback_data="my_requests"),
            types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub")
        )

        msg_text = (
            f"✅ <b>Заявка на вывод создана!</b>\n\n"
            f"<b>Сумма:</b> {amount}$\n"
            f"<b>Заявка:</b> #{request_id}\n"
            f"<b>Метод:</b> {'Карта' if method == 'card' else 'Криптовалюта'}\n"
            f"<b>Получатель:</b> {recipient}\n"
        )
        if recipient_name:
            msg_text += f"<b>Имя:</b> {recipient_name}\n"
        msg_text += (
            f"\n<b>Статус:</b> В обработке\n\n"
            f"Заявка отправлена на проверку администратору.\n"
            f"Статус можно отслеживать в разделе «Мои заявки»."
        )

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        user_data[user_id]["mode"] = ""
        user_data[user_id]["withdraw_step"] = ""

    else:
        update_balance(user_id, amount)
        msg = bot.send_message(chat_id, "❌ Ошибка создания заявки. Попробуйте позже.")
        save_message(chat_id, user_id, msg.message_id)


# ==================== ОБРАБОТЧИКИ ПОПОЛНЕНИЯ ====================

@safe_execute
def process_deposit_amount(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    try:
        amount = float(message.text.strip())

        if amount < 1:
            msg = bot.send_message(
                chat_id,
                "❌ Минимальная сумма пополнения: 1$\nПожалуйста, введите сумму больше."
            )
            save_message(chat_id, user_id, msg.message_id)
            return

        method = user_data[user_id].get("deposit_method", "card")
        user_data[user_id]["deposit_amount"] = amount

        if method == "stars":
            request_id = add_request(user_id, 'deposit', int(amount), 'stars')
            user_data[user_id]["deposit_request_id"] = request_id

            stars_amount = int(amount * 100)

            delete_previous_messages(chat_id, user_id)

            prices = [types.LabeledPrice(label=f"Пополнение {amount}$", amount=stars_amount)]
            payload = f"deposit_stars_{request_id}_{user_id}_{int(amount)}"

            bot.send_invoice(
                chat_id=chat_id,
                title=f"Пополнение баланса на {amount}$",
                description=f"Оплата {stars_amount} ⭐ Telegram Stars за пополнение баланса на {amount}$ (заявка #{request_id})",
                invoice_payload=payload,
                provider_token="",
                currency="XTR",
                prices=prices
            )

            user_data[user_id]["mode"] = ""

        else:
            user_data[user_id]["mode"] = "waiting_deposit_comment"

            method_names = {
                'card': 'СБП',
                'crypto': 'криптовалюте (USDT TRC20)'
            }.get(method, method)

            usd_to_rub = get_usd_to_rub()
            rub_amount = int(amount * usd_to_rub)

            markup = types.InlineKeyboardMarkup(row_width=1)
            cancel_btn = types.InlineKeyboardButton("❌ Отменить операцию", callback_data="deposit")
            markup.add(cancel_btn)

            msg_text = (
                f"📝 <b>Введите комментарий к платежу</b>\n\n"
                f"<b>Способ:</b> {method_names}\n"
                f"<b>Сумма:</b> {amount}$ (~{rub_amount} ₽)\n\n"
            )

            if method == 'card':
                msg_text += f"Напишите номер транзакции или номер телефона:\n"
                msg_text += f"📌 Пример: `1234567890` или `+79991112233`"
            else:
                msg_text += f"Напишите адрес кошелька, с которого отправлен USDT:\n"
                msg_text += f"📌 Пример: `TSM4p8JjU2AqC7Xqy8Zf9gH3kL5nP2rV6w`"

            msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
            save_message(chat_id, user_id, msg.message_id)

    except ValueError:
        msg = bot.send_message(
            chat_id,
            "❌ Пожалуйста, введите число!\nПример: `10` - для пополнения на 10$",
            parse_mode="Markdown"
        )
        save_message(chat_id, user_id, msg.message_id)


@safe_execute
def process_deposit_comment(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    comment = message.text.strip()

    if not comment:
        msg = bot.send_message(
            chat_id,
            "❌ Комментарий не может быть пустым!\nПожалуйста, введите комментарий."
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    method = user_data[user_id].get("deposit_method", "card")
    amount = user_data[user_id].get("deposit_amount", 0)

    if amount <= 0:
        msg = bot.send_message(
            chat_id,
            "❌ Ошибка: сумма не указана. Пожалуйста, начните операцию заново."
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    request_id = add_request(user_id, 'deposit', int(amount), method, comment)
    user_data[user_id]["deposit_request_id"] = request_id

    usd_to_rub = get_usd_to_rub()
    rub_amount = int(amount * usd_to_rub)

    markup = types.InlineKeyboardMarkup(row_width=2)
    confirm_btn = types.InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_request_{request_id}")
    cancel_btn = types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_request_{request_id}")
    markup.add(confirm_btn, cancel_btn)
    markup.add(types.InlineKeyboardButton("📋 Мои заявки", callback_data="my_requests"))
    markup.add(types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub"))

    delete_previous_messages(chat_id, user_id)

    msg_text = f"💳 <b>Пополнение</b>\n\n"
    msg_text += f"<b>Заявка:</b> #{request_id}\n"
    msg_text += f"<b>Сумма:</b> {amount}$ (~{rub_amount} ₽)\n"
    msg_text += f"<b>Комментарий:</b> {comment}\n\n"

    if method == 'card':
        msg_text += f"<b>Реквизиты для перевода по СБП:</b>\n"
        msg_text += f"📱 Телефон: <code>+79991112233</code>\n"
        msg_text += f"🏦 Банк: <b>Т-Банк</b>\n"
        msg_text += f"👤 Получатель: <b>Иванов И.И.</b>\n\n"
        msg_text += f"<b>Сумма к оплате:</b> {rub_amount} ₽\n\n"
    else:
        msg_text += f"<b>Адрес для перевода USDT (TRC20):</b>\n"
        msg_text += f"<code>TSM4p8JjU2AqC7Xqy8Zf9gH3kL5nP2rV6w</code>\n\n"

    msg_text += f"📌 После оплаты нажмите «✅ Я оплатил».\n"
    msg_text += f"⚠️ Если вы передумали, нажмите «❌ Отменить»."

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)

    user_data[user_id]["mode"] = ""


# ==================== ОБРАБОТЧИКИ ЗАЯВОК ====================

@bot.pre_checkout_query_handler(func=lambda query: True)
@safe_execute
def process_pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@bot.message_handler(content_types=['successful_payment'])
@safe_execute
def process_successful_payment(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    payment = message.successful_payment

    payload = payment.invoice_payload or ""
    if not payload.startswith("deposit_stars_"):
        logger.warning(f"Неизвестный payload успешной оплаты: {payload}")
        return

    try:
        parts = payload.split("_")
        request_id = int(parts[2])
        payload_user_id = int(parts[3])
        amount = int(parts[4])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга payload Stars: {payload} — {e}")
        return

    if payload_user_id != user_id:
        logger.warning(f"Несовпадение user_id в payload Stars: {payload_user_id} vs {user_id}")
        return

    request = get_request_by_id(request_id, user_id)
    if not request:
        logger.error(f"Заявка #{request_id} не найдена при успешной оплате Stars")
        return

    if request['status'] == 'completed':
        logger.info(f"Заявка #{request_id} уже завершена")
        return

    update_balance(user_id, int(amount))
    update_request_status(request_id, 'completed')
    add_transaction(
        user_id,
        'income',
        int(amount),
        f"Пополнение через Stars (заявка #{request_id}, charge_id={payment.telegram_payment_charge_id})"
    )

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💵 Баланс", callback_data="balance"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub")
    )

    msg = bot.send_message(
        chat_id,
        f"✅ <b>Баланс пополнен!</b>\n\n"
        f"<b>Зачислено:</b> {amount}$\n"
        f"<b>Оплачено:</b> {payment.total_amount} ⭐\n"
        f"<b>Заявка:</b> #{request_id}\n\n"
        f"Спасибо за оплату! 🙌",
        reply_markup=markup,
        parse_mode="html"
    )
    save_message(chat_id, user_id, msg.message_id)
    logger.info(f"Успешная оплата Stars: user={user_id}, amount={amount}$, request=#{request_id}")


@safe_execute
def process_confirm_request(call, chat_id, user_id, request_id):
    request = get_request_by_id(request_id, user_id)
    if not request:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена", show_alert=True)
        return

    if request['status'] != 'pending':
        bot.answer_callback_query(call.id, f"❌ Заявка уже {request['status']}", show_alert=True)
        return

    update_request_status(request_id, 'processing')

    for admin_id in ADMIN_IDS:
        try:
            type_emoji = "💰" if request['type'] == 'deposit' else "💳"
            type_name = "Пополнение" if request['type'] == 'deposit' else "Вывод"

            admin_msg = (
                f"🆕 <b>Заявка #{request_id} требует рассмотрения</b>\n\n"
                f"{type_emoji} <b>Тип:</b> {type_name}\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Сумма:</b> {request['amount']}$\n"
                f"<b>Метод:</b> {request['method']}\n"
            )
            if request.get('recipient'):
                admin_msg += f"<b>Получатель:</b> {request['recipient']}\n"
            if request.get('recipient_name'):
                admin_msg += f"<b>Имя:</b> {request['recipient_name']}\n"
            if request.get('comment'):
                admin_msg += f"<b>Комментарий:</b> {request['comment']}\n"
            admin_msg += f"\n<i>Используйте админ-панель /panel для управления заявкой</i>"

            bot.send_message(admin_id, admin_msg, parse_mode="html")
        except:
            pass

    bot.answer_callback_query(
        call.id,
        f"✅ Заявка #{request_id} отправлена на обработку администратору",
        show_alert=True
    )

    try:
        bot.delete_message(chat_id, call.message.message_id)
    except:
        pass

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📋 Мои заявки", callback_data="my_requests"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub")
    )

    msg_text = (
        f"✅ <b>Заявка #{request_id} отправлена на обработку!</b>\n\n"
        f"<b>Сумма:</b> {request['amount']}$\n"
        f"<b>Статус:</b> В обработке\n\n"
        f"Администратор рассмотрит вашу заявку в ближайшее время.\n"
        f"Статус заявки можно отслеживать в разделе «Мои заявки»."
    )

    msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
    save_message(chat_id, user_id, msg.message_id)


@safe_execute
def process_cancel_request(call, chat_id, user_id, request_id):
    request = get_request_by_id(request_id, user_id)
    if not request:
        bot.answer_callback_query(call.id, "❌ Заявка не найдена", show_alert=True)
        return

    if request['status'] != 'pending':
        bot.answer_callback_query(call.id, f"❌ Заявка уже {request['status']}", show_alert=True)
        return

    update_request_status(request_id, 'cancelled')

    bot.answer_callback_query(call.id, "❌ Заявка отменена", show_alert=True)

    try:
        bot.delete_message(chat_id, call.message.message_id)
    except:
        pass

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💰 Пополнение", callback_data="deposit"),
        types.InlineKeyboardButton("🏠 Главное меню", callback_data="back_to_hub")
    )

    msg = bot.send_message(
        chat_id,
        f"❌ <b>Заявка #{request_id} отменена.</b>\n\n"
        f"Если это ошибка, создайте новую заявку.",
        reply_markup=markup,
        parse_mode="html"
    )
    save_message(chat_id, user_id, msg.message_id)


# ==================== АДМИН-ОБРАБОТЧИКИ ЗАЯВОК ====================

@safe_execute
def process_admin_approve(chat_id, admin_id, request_id):
    request = get_request_by_id(request_id)
    if not request:
        bot.send_message(chat_id, "❌ Заявка не найдена")
        return

    if request['status'] != 'processing':
        bot.send_message(chat_id, f"❌ Заявка уже {request['status']}")
        return

    if request['type'] == 'deposit':
        update_balance(request['user_id'], request['amount'])
        add_transaction(
            request['user_id'],
            'income',
            request['amount'],
            f"Пополнение (заявка #{request_id})"
        )

        try:
            bot.send_message(
                request['user_id'],
                f"✅ <b>Заявка #{request_id} одобрена!</b>\n\n"
                f"<b>Баланс пополнен на</b> {request['amount']}$\n"
                f"<b>Заявка:</b> #{request_id}\n\n"
                f"Спасибо за доверие! 🙌",
                parse_mode="html"
            )
        except:
            pass

    elif request['type'] == 'withdraw':
        try:
            msg_text = (
                f"✅ <b>Заявка #{request_id} одобрена!</b>\n\n"
                f"<b>Сумма вывода:</b> {request['amount']}$\n"
                f"<b>Заявка:</b> #{request_id}\n"
            )
            if request.get('recipient'):
                msg_text += f"<b>Получатель:</b> {request['recipient']}\n"
            if request.get('recipient_name'):
                msg_text += f"<b>Имя:</b> {request['recipient_name']}\n"
            msg_text += f"\nСредства отправлены на указанные реквизиты."

            bot.send_message(request['user_id'], msg_text, parse_mode="html")
        except:
            pass

    update_request_status(request_id, 'completed')

    bot.send_message(chat_id, f"✅ Заявка #{request_id} одобрена")


@safe_execute
def process_admin_reject(chat_id, admin_id, request_id):
    request = get_request_by_id(request_id)
    if not request:
        bot.send_message(chat_id, "❌ Заявка не найдена")
        return

    if request['status'] != 'processing':
        bot.send_message(chat_id, f"❌ Заявка уже {request['status']}")
        return

    if request['type'] == 'withdraw':
        update_balance(request['user_id'], request['amount'])
        add_transaction(
            request['user_id'],
            'refund',
            request['amount'],
            f"Возврат средств (отклонена заявка #{request_id})"
        )

    update_request_status(request_id, 'cancelled')

    try:
        bot.send_message(
            request['user_id'],
            f"❌ <b>Заявка #{request_id} отклонена</b>\n\n"
            f"<b>Сумма:</b> {request['amount']}$\n"
            f"<b>Статус:</b> Отклонена\n\n"
            f"К сожалению, ваша заявка была отклонена администратором.\n\n"
            f"Вы можете создать новую заявку.",
            parse_mode="html"
        )
    except:
        pass

    bot.send_message(chat_id, f"❌ Заявка #{request_id} отклонена")


# ==================== CALLBACK ОБРАБОТЧИКИ ====================

@bot.callback_query_handler(func=lambda call: True)
@log_callback
@safe_execute
def handle_callbacks(call):
    if notify_if_banned(call.from_user.id, call.message.chat.id if call.message else call.from_user.id):
        try:
            bot.answer_callback_query(call.id, "Аккаунт заблокирован", show_alert=True)
        except Exception:
            pass
        return
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    data = call.data

    if data == "check_subscription":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_main_menu(chat_id, user_id)
        bot.answer_callback_query(call.id, "✅ Доступ открыт!")
        return

    if data == "back_to_hub":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_main_menu(chat_id, user_id)
        bot.answer_callback_query(call.id, "🏠 Возвращаемся в главное меню")
        return

    if data == "my_bots":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_my_bots(chat_id, user_id)
        bot.answer_callback_query(call.id, "🤖 Список ваших ботов")
        return

    if data == "balance":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_balance(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "deposit":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_deposit_menu(chat_id, user_id)
        bot.answer_callback_query(call.id, "💰 Выберите способ пополнения")
        return

    if data == "deposit_card":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        user_data[user_id]["deposit_method"] = "card"
        user_data[user_id]["mode"] = "waiting_deposit_amount"

        usd_to_rub = get_usd_to_rub()

        markup = types.InlineKeyboardMarkup(row_width=1)
        back_btn = types.InlineKeyboardButton("⬅️ Назад к выбору способа", callback_data="deposit")
        markup.add(back_btn)

        msg_text = (
            f"💳 <b>Пополнение через СБП</b>\n\n"
            f"<b>Курс:</b> 1$ = {usd_to_rub:.2f} ₽\n\n"
            f"Введите сумму пополнения в $:\n"
            f"Минимальная сумма: <b>1$</b>"
        )

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        bot.answer_callback_query(call.id, "💳 Введите сумму пополнения")
        return

    if data == "deposit_crypto":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        user_data[user_id]["deposit_method"] = "crypto"
        user_data[user_id]["mode"] = "waiting_deposit_amount"

        markup = types.InlineKeyboardMarkup(row_width=1)
        back_btn = types.InlineKeyboardButton("⬅️ Назад к выбору способа", callback_data="deposit")
        markup.add(back_btn)

        msg_text = (
            f"₿ <b>Пополнение в криптовалюте</b>\n\n"
            f"Введите сумму пополнения в $:\n"
            f"Минимальная сумма: <b>1$</b>"
        )

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        bot.answer_callback_query(call.id, "₿ Введите сумму пополнения")
        return

    if data == "deposit_stars":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        user_data[user_id]["deposit_method"] = "stars"
        user_data[user_id]["mode"] = "waiting_deposit_amount"

        markup = types.InlineKeyboardMarkup(row_width=1)
        back_btn = types.InlineKeyboardButton("⬅️ Назад к выбору способа", callback_data="deposit")
        markup.add(back_btn)

        msg_text = (
            f"⭐ <b>Пополнение через Telegram Stars</b>\n\n"
            f"Введите сумму пополнения в $:\n"
            f"Минимальная сумма: <b>1$</b>\n"
            f"Курс: 1$ = 100 Stars"
        )

        msg = bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        bot.answer_callback_query(call.id, "⭐ Введите сумму пополнения")
        return

    if data == "withdraw":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_withdraw_menu(chat_id, user_id)
        bot.answer_callback_query(call.id, "💳 Выберите способ вывода")
        return

    if data == "withdraw_card":
        process_withdraw_card(call, chat_id, user_id)
        return

    if data == "withdraw_crypto":
        process_withdraw_crypto(call, chat_id, user_id)
        return

    if data == "my_requests":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_my_requests(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "support":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_support(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "info":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_help(chat_id, user_id)
        bot.answer_callback_query(call.id, "ℹ️ Справка")
        return

    user_role = get_user_role(user_id)
    if data.startswith('admin_') and user_role < PANEL_ACCESS_ROLE:
        bot.answer_callback_query(call.id, "❌ У вас нет прав доступа к админ-панели", show_alert=True)
        return

    if data == "admin_back":
        show_admin_panel(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_panel":
        show_admin_panel(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_users":
        if user_role < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями", show_alert=True)
            return
        show_admin_users(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_users_page_"):
        if user_role < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями", show_alert=True)
            return
        page = int(data.replace("admin_users_page_", ""))
        show_admin_users(chat_id, user_id, page)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_add_staff":
        if user_role < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для добавления сотрудников", show_alert=True)
            return
        show_admin_add_staff(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_add_role_"):
        parts = data.split("_")
        target_user_id = int(parts[3])
        role = int(parts[4])
        process_admin_add_staff_role(call, chat_id, user_id, target_user_id, role)
        return

    if data.startswith("admin_user_"):
        if user_role < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "❌ У вас нет прав для управления пользователями", show_alert=True)
            return
        target_user_id = int(data.replace("admin_user_", ""))
        show_admin_user_detail(chat_id, user_id, target_user_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_set_role_"):
        parts = data.split("_")
        target_user_id = int(parts[3])
        new_role = int(parts[4])

        if not can_change_role(user_id, new_role):
            bot.answer_callback_query(call.id,
                                      f"❌ У вас нет прав для назначения роли {ROLES.get(new_role, 'Неизвестно')}",
                                      show_alert=True)
            return

        if target_user_id in MANDATORY_USERS:
            bot.answer_callback_query(call.id, "❌ Нельзя изменить роль обязательного пользователя", show_alert=True)
            return

        set_user_role(target_user_id, new_role, user_id)
        bot.answer_callback_query(call.id, f"✅ Роль изменена на {ROLES.get(new_role, 'Неизвестно')}", show_alert=True)
        show_admin_user_detail(chat_id, user_id, target_user_id)
        return




    if data == "admin_analytics":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("📤 Расходы", callback_data="admin_an_expenses"))
        markup.add(types.InlineKeyboardButton("📥 Доходы", callback_data="admin_an_income"))
        markup.add(types.InlineKeyboardButton("🥧 Объявления", callback_data="admin_an_listings"))
        markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="admin_back"))
        delete_previous_messages(chat_id, user_id)
        msg = bot.send_message(chat_id, "📊 <b>Аналитика</b>\n\nПодробные отчёты и PDF — в Mini App.\nЗдесь краткая сводка за 30 дней.", reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        bot.answer_callback_query(call.id)
        return

    if data in ("admin_an_expenses", "admin_an_income", "admin_an_listings"):
        kind = {"admin_an_expenses": "expenses", "admin_an_income": "income", "admin_an_listings": "listings"}[data]
        from datetime import timedelta
        date_to = datetime.now(MSK_TZ).date()
        date_from = date_to - timedelta(days=30)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        df, dt = str(date_from), str(date_to) + "T23:59:59"
        if kind == "expenses":
            cur.execute("SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as cnt FROM requests WHERE type='withdraw' AND status='completed' AND created_at>=? AND created_at<=?", (str(date_from), dt))
            row = dict(cur.fetchone())
            text = f"📤 <b>Расходы (30 дн.)</b>\n\nСумма выплат: <b>{row['total']}$</b>\nОпераций: {row['cnt']}\n\nPDF-отчёт — в Mini App → Аналитика."
        elif kind == "income":
            cur.execute("SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as cnt FROM requests WHERE type='deposit' AND status='completed' AND created_at>=? AND created_at<=?", (str(date_from), dt))
            row = dict(cur.fetchone())
            text = f"📥 <b>Доходы (30 дн.)</b>\n\nСумма пополнений: <b>{row['total']}$</b>\nОпераций: {row['cnt']}\n\nPDF-отчёт — в Mini App → Аналитика."
        else:
            cur.execute("SELECT product_type, listing_type FROM listings WHERE created_at>=? AND created_at<=?", (str(date_from), dt))
            cats = {}
            for r in cur.fetchall():
                cat = _normalize_listing_category(r["product_type"], r["listing_type"])
                cats[cat] = cats.get(cat, 0) + 1
            lines = "\n".join([f"• {k}: {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])]) or "• нет данных"
            text = f"🥧 <b>Объявления (30 дн.)</b>\n\n{lines}\n\nДиаграмма и PDF — в Mini App."
        conn.close()
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("⬅️ К аналитике", callback_data="admin_analytics"))
        delete_previous_messages(chat_id, user_id)
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="html")
        save_message(chat_id, user_id, msg.message_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_workcenter":
        show_admin_workcenter(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_career":
        show_admin_career(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_wc_dev" or data == "admin_promo_dev":
        bot.answer_callback_query(call.id, "Пока в разработке", show_alert=True)
        return

    if data == "admin_listings":
        show_admin_listings(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_listing_"):
        lid = int(data.replace("admin_listing_", ""))
        show_admin_listing_detail(chat_id, user_id, lid)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_lapprove_"):
        lid = int(data.replace("admin_lapprove_", ""))
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('SELECT * FROM listings WHERE id = ?', (lid,))
        listing = cur.fetchone()
        if listing:
            listing = dict(listing)
            price = listing.get('price') or 0
            if listing.get('listing_type') == 'partner' and price < 1:
                user_data[user_id] = user_data.get(user_id, {})
                user_data[user_id]["mode"] = "admin_listing_price"
                user_data[user_id]["listing_id"] = lid
                bot.answer_callback_query(call.id)
                msg = bot.send_message(chat_id, "Введите цену для партнерского объявления ($):")
                save_message(chat_id, user_id, msg.message_id)
                conn.close()
                return
            now = datetime.now(MSK_TZ).isoformat()
            cur.execute("UPDATE listings SET status = 'active', updated_at = ? WHERE id = ?", (now, lid))
            conn.commit()
            try:
                bot.send_message(listing['seller_id'], f"✅ Объявление #{lid} одобрено")
            except Exception:
                pass
        conn.close()
        bot.answer_callback_query(call.id, "Одобрено")
        show_admin_listings(chat_id, user_id)
        return

    if data.startswith("admin_lreject_"):
        lid = int(data.replace("admin_lreject_", ""))
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE listings SET status = 'rejected', updated_at = ? WHERE id = ?",
                    (datetime.now(MSK_TZ).isoformat(), lid))
        cur.execute('SELECT seller_id, title FROM listings WHERE id = ?', (lid,))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if row:
            try:
                bot.send_message(row[0], f"❌ Объявление #{lid} отклонено")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "Отклонено")
        show_admin_listings(chat_id, user_id)
        return

    if data == "admin_jobs":
        if get_user_role(user_id) < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
            return
        show_admin_jobs(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data == "admin_complaints":
        bot.answer_callback_query(call.id, "Раздел в разработке", show_alert=True)
        return

    if data.startswith("admin_job_"):
        jid = int(data.replace("admin_job_", ""))
        show_admin_job_detail(chat_id, user_id, jid)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_jaccept_"):
        jid = int(data.replace("admin_jaccept_", ""))
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE job_applications SET status = 'accepted', updated_at = ? WHERE id = ?",
                    (datetime.now(MSK_TZ).isoformat(), jid))
        cur.execute('SELECT user_id FROM job_applications WHERE id = ?', (jid,))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if row:
            try:
                bot.send_message(row[0], f"✅ Заявка на работу #{jid} принята")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "Принято")
        show_admin_jobs(chat_id, user_id)
        return

    if data.startswith("admin_jreject_"):
        jid = int(data.replace("admin_jreject_", ""))
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE job_applications SET status = 'rejected', updated_at = ? WHERE id = ?",
                    (datetime.now(MSK_TZ).isoformat(), jid))
        cur.execute('SELECT user_id FROM job_applications WHERE id = ?', (jid,))
        row = cur.fetchone()
        conn.commit()
        conn.close()
        if row:
            try:
                bot.send_message(row[0], f"❌ Заявка на работу #{jid} отклонена")
            except Exception:
                pass
        bot.answer_callback_query(call.id, "Отклонено")
        show_admin_jobs(chat_id, user_id)
        return

    if data.startswith("admin_balance_"):
        if get_user_role(user_id) < ROLE_CHANGE_ROLE:
            bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
            return
        tid = int(data.replace("admin_balance_", ""))
        user_data[user_id] = user_data.get(user_id, {})
        user_data[user_id]["mode"] = "admin_set_balance"
        user_data[user_id]["balance_target"] = tid
        bot.answer_callback_query(call.id)
        bal = get_balance(tid)
        msg = bot.send_message(
            chat_id,
            f"💵 Текущий баланс пользователя {tid}: <b>{bal}$</b>\n\n"
            f"Введите новый баланс (число):",
            parse_mode="html"
        )
        save_message(chat_id, user_id, msg.message_id)
        return

    if data == "admin_requests":
        show_admin_requests(chat_id, user_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_request_"):
        request_id = int(data.replace("admin_request_", ""))
        show_admin_request_detail(chat_id, user_id, request_id)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("admin_approve_"):
        request_id = int(data.replace("admin_approve_", ""))
        process_admin_approve(chat_id, user_id, request_id)
        show_admin_requests(chat_id, user_id)
        return

    if data.startswith("admin_reject_"):
        request_id = int(data.replace("admin_reject_", ""))
        process_admin_reject(chat_id, user_id, request_id)
        show_admin_requests(chat_id, user_id)
        return

    if data.startswith("confirm_request_"):
        request_id = int(data.replace("confirm_request_", ""))
        process_confirm_request(call, chat_id, user_id, request_id)
        return

    if data.startswith("cancel_request_"):
        request_id = int(data.replace("cancel_request_", ""))
        process_cancel_request(call, chat_id, user_id, request_id)
        return


# ==================== ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ ====================

@bot.message_handler(func=lambda message: True)
@safe_execute
def handle_text(message):
    if notify_if_banned(message.from_user.id, message.chat.id):
        return
    chat_id = message.chat.id
    user_id = message.from_user.id

    try:
        text = message.text.strip()

        if text.startswith('/'):
            return

        delete_previous_messages(chat_id, user_id)
        save_message(chat_id, user_id, message.message_id)

        mode = user_data.get(user_id, {}).get("mode", "")

        if mode == "admin_add_staff_id":
            process_admin_add_staff_id(message)
            return

        if mode == "waiting_deposit_amount":
            process_deposit_amount(message)
            return

        if mode == "waiting_deposit_comment":
            process_deposit_comment(message)
            return

        if mode == "waiting_withdraw_amount":
            process_withdraw_amount(message)
            return

        if mode == "waiting_withdraw_recipient":
            process_withdraw_recipient(message)
            return

        if mode == "waiting_card_name":
            process_withdraw_recipient(message)
            return

        if not user_data.get(user_id, {}).get("in_menu", False):
            show_main_menu(chat_id, user_id)
            return

        text_lower = text.lower()
        if text_lower in ["меню", "menu"]:
            show_main_menu(chat_id, user_id)
        elif text_lower in ["мои боты", "mybots", "боты"]:
            show_my_bots(chat_id, user_id)
        elif text_lower in ["баланс", "balance", "балланс"]:
            show_balance(chat_id, user_id)
        elif text_lower in ["помощь", "help", "помоги"]:
            show_help(chat_id, user_id)
        elif text_lower in ["поддержка", "support", "саппорт"]:
            show_support(chat_id, user_id)
        elif text_lower in ["панель", "panel", "админ"]:
            if get_user_role(user_id) >= PANEL_ACCESS_ROLE:
                show_admin_panel(chat_id, user_id)
            else:
                msg = bot.send_message(
                    chat_id,
                    "❌ У вас нет прав доступа к админ-панели"
                )
                save_message(chat_id, user_id, msg.message_id)
        else:
            markup = types.InlineKeyboardMarkup(row_width=1)
            back_btn = types.InlineKeyboardButton("🏠 Вернуться в главное меню", callback_data="back_to_hub")
            markup.add(back_btn)

            msg = bot.send_message(
                chat_id,
                "❓ **Неизвестная команда**\n\n"
                "Используйте кнопки меню или команды:\n"
                "/menu - Главное меню\n"
                "/mybots - Мои боты\n"
                "/balance - Баланс\n"
                "/help - Помощь\n"
                "/support - Поддержка\n"
                "/panel - Админ-панель",
                reply_markup=markup,
                parse_mode="Markdown"
            )
            save_message(chat_id, user_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка в handle_text: {e}", exc_info=True)
        error_msg = bot.reply_to(message, f"❌ Произошла ошибка. Мы уже работаем над её исправлением.")
        save_message(chat_id, user_id, error_msg.message_id)
        time.sleep(5)
        delete_previous_messages(chat_id, user_id)


# ==================== ЗАПУСК БОТА + API ====================

def run_bot():
    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=30)
        except Exception as e:
            logger.error(f"Ошибка в polling: {e}", exc_info=True)
            print(f"❌ Ошибка: {e}. Перезапуск через 10 секунд...")
            time.sleep(10)


if __name__ == "__main__":
    try:
        bot_info = bot.get_me()
        print(f"🎯 Бот запущен: @{bot_info.username}")
        try:
            bot.delete_webhook(drop_pending_updates=False)
            print("✅ Webhook сброшен, используем polling")
        except Exception as _wh_err:
            print(f"Webhook delete: {_wh_err}")

        # Запускаем бота в отдельном потоке
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        news_thread = threading.Thread(target=news_sync_loop, daemon=True)
        news_thread.start()
        bot_thread.start()

        # Запускаем Flask API (Amvera ожидает порт 80)
        raw_port = os.getenv("PORT") or os.getenv("APP_PORT") or "80"
        if str(raw_port).strip().lower() in ("", "null", "none"):
            raw_port = "80"
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 80
        print(f"🌐 API запущен на порту {port}")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        print(f"❌ Критическая ошибка: {e}")
        sys.exit(1)
