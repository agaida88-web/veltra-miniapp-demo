#!/usr/bin/env python3
"""VELTRA: demo-grade, audit-friendly TRC20 portal.
No private keys and no automated outbound transfers. Python 3.11+ stdlib only.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = os.environ.get('SITE_DB_PATH', str(ROOT / 'data.sqlite3'))
HOST = os.environ.get('SITE_HOST', '127.0.0.1')
PORT = int(os.environ.get('SITE_PORT') or os.environ.get('PORT', '8080'))
RECEIVE_ADDRESS = os.environ.get('TRON_RECEIVE_ADDRESS','').strip()
PAYOUT_ADDRESS = os.environ.get('TRON_PAYOUT_ADDRESS', RECEIVE_ADDRESS).strip()
TRON_API = os.environ.get('TRON_API_URL', 'https://api.trongrid.io').rstrip('/')
TRON_KEY = os.environ.get('TRON_API_KEY', '').strip()
COOKIE_SECURE = os.environ.get('COOKIE_SECURE','0') == '1'
MIN_CONFIRMATIONS = max(1,int(os.environ.get('MIN_CONFIRMATIONS','20')))
USDT_CONTRACT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
TRANSFER_TOPIC = 'ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
DB_LOCK = threading.RLock()
# Telegram webhook is optional. Keep the bot token only in Render Environment.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_APP_URL = os.environ.get('TELEGRAM_APP_URL', 'https://veltra-miniapp-demo.onrender.com/').strip()
TELEGRAM_WEBHOOK_URL = os.environ.get('TELEGRAM_WEBHOOK_URL', 'https://veltra-miniapp-demo.onrender.com/telegram/webhook').strip()


def telegram_call(method, payload):
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError('Telegram bot is not configured')
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + TELEGRAM_BOT_TOKEN + '/' + method,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise ValueError('Telegram API request failed')
    return result


def telegram_webhook_secret():
    # Deterministic secret: avoids storing a second credential in Render.
    return hmac.new(TELEGRAM_BOT_TOKEN.encode(), b'veltra-telegram-webhook-v1', hashlib.sha256).hexdigest()


def telegram_handle_update(update):
    message = update.get('message') if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return
    chat = message.get('chat') or {}
    if chat.get('type') != 'private' or not isinstance(chat.get('id'), int):
        return
    command = str(message.get('text') or '').split(' ', 1)[0].split('@', 1)[0]
    if command not in ('/start', '/help'):
        return
    telegram_call('sendMessage', {
        'chat_id': chat['id'],
        'text': 'Добро пожаловать в VELTRA! 🚀\\n\\nОткройте демонстрационное приложение кнопкой ниже. Реальные переводы и торговля здесь не подключены.',
        'reply_markup': {
            'inline_keyboard': [[{
                'text': '🚀 Открыть VELTRA',
                'web_app': {'url': TELEGRAM_APP_URL},
            }]],
        },
    })


def telegram_register_webhook():
    if not TELEGRAM_BOT_TOKEN:
        print('Telegram bot: disabled (set TELEGRAM_BOT_TOKEN in Render Environment)')
        return
    if not TELEGRAM_APP_URL.startswith('https://') or not TELEGRAM_WEBHOOK_URL.startswith('https://'):
        print('Telegram webhook: invalid HTTPS URL configuration')
        return
    try:
        telegram_call('setWebhook', {
            'url': TELEGRAM_WEBHOOK_URL,
            'secret_token': telegram_webhook_secret(),
            'allowed_updates': ['message'],
            'drop_pending_updates': False,
        })
        print('Telegram webhook registered successfully')
    except Exception as exc:
        # Never print exception details: urllib errors may include the tokenized URL.
        print('Telegram webhook registration failed:', type(exc).__name__)


ENABLE_CORRECTIONS = os.environ.get('ENABLE_MANUAL_CORRECTIONS','0') == '1'



def tron_decode(addr):
    if not isinstance(addr,str) or len(addr)!=34 or not addr.startswith('T'): raise ValueError('Некорректный TRON-адрес')
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    v=0
    for ch in addr:
        if ch not in alphabet: raise ValueError('Некорректный TRON-адрес')
        v=v*58+alphabet.index(ch)
    raw=v.to_bytes(25,'big')
    if raw[0]!=0x41 or hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]!=raw[21:]:
        raise ValueError('Ошибка контрольной суммы TRON-адреса')
    return raw[:21]


def parse_amount(x):
    try: d=Decimal(str(x))
    except (InvalidOperation,ValueError): raise ValueError('Укажите корректную сумму')
    if not d.is_finite() or d<Decimal('1') or d>Decimal('1000000') or d.as_tuple().exponent<-6:
        raise ValueError('Сумма: от 1 до 1 000 000 USDT, максимум 6 знаков после запятой')
    return int(d*1_000_000)


def amt(n): return format(Decimal(n)/1_000_000,'f')


def hash_password(password,salt=None):
    salt=salt or secrets.token_bytes(16)
    key=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,250000)
    return f'pbkdf2_sha256$250000${salt.hex()}${key.hex()}'


def password_check(password,encoded):
    try:
        _,it,s,k=encoded.split('$')
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(s),int(it))
        return hmac.compare_digest(actual,bytes.fromhex(k))
    except Exception: return False


def conn():
    c=sqlite3.connect(DB_PATH,timeout=30, isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    return c


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True,exist_ok=True)
    with DB_LOCK, conn() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, passhash TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount_micro INTEGER NOT NULL, txid TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'awaiting_txid', created_at INTEGER NOT NULL, expires INTEGER NOT NULL, verified_at INTEGER, sender TEXT);
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), amount_micro INTEGER NOT NULL, address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', txid TEXT UNIQUE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, amount_micro INTEGER NOT NULL, ref TEXT NOT NULL UNIQUE, txid TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL, created_at INTEGER NOT NULL);
        ''')
        # Backward-compatible additions for existing development databases.
        for column,ddl in [
            ('phone','TEXT'),('withdraw_hash','TEXT'),('disabled','INTEGER NOT NULL DEFAULT 0')
        ]:
            if column not in [x['name'] for x in c.execute('PRAGMA table_info(users)')]:
                c.execute(f'ALTER TABLE users ADD COLUMN {column} {ddl}')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS password_resets(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), token_hash TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, created_by INTEGER NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS bot_trades(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), symbol TEXT NOT NULL, side TEXT NOT NULL, order_ref TEXT NOT NULL, entry_price TEXT NOT NULL, exit_price TEXT NOT NULL, volume TEXT NOT NULL, pnl_micro INTEGER NOT NULL, closed_at INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'operator_import', created_by INTEGER NOT NULL, created_at INTEGER NOT NULL, UNIQUE(user_id,order_ref));
        CREATE INDEX IF NOT EXISTS idx_trades_uid_closed ON bot_trades(user_id,closed_at);
        ''')
        admin_email=os.environ.get('ADMIN_EMAIL','').lower().strip()
        admin_password=os.environ.get('ADMIN_PASSWORD','')
        if admin_email and admin_password and len(admin_password)>=12:
            existing=c.execute('SELECT id FROM users WHERE email=?',(admin_email,)).fetchone()
            if existing: c.execute('UPDATE users SET admin=1 WHERE id=?',(existing['id'],))
            else: c.execute('INSERT INTO users(email,name,passhash,admin,created_at) VALUES(?,?,?,?,?)', (admin_email,'Администратор',hash_password(admin_password),1,int(time.time())))


def api_post(path,body):
    req=urllib.request.Request(TRON_API+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**({'TRON-PRO-API-KEY':TRON_KEY} if TRON_KEY else {})},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=12) as resp: return json.load(resp)
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        raise ValueError('Нет ответа от TRON API: '+str(exc)[:100])


def verify_transfer(txid,to_address, expected_micro, from_address=None, not_before=None):
    """Server-side proof: SUCCESS receipt, exact contract/event, address, amount, time, confirmations.
    Use raw integer token units (USDT decimals=6). Never trust browser-supplied status.
    """
    if not re.fullmatch(r'[a-fA-F0-9]{64}',txid or ''): raise ValueError('Некорректный TXID')
    tron_decode(to_address)
    if from_address: tron_decode(from_address)
    tx=api_post('/wallet/gettransactioninfobyid',{'value':txid})
    if not tx or tx.get('receipt',{}).get('result')!='SUCCESS' or not tx.get('blockNumber'):
        raise ValueError('Транзакция ещё не подтверждена или завершилась ошибкой')
    if not_before and int(tx.get('blockTimeStamp',0)) < (int(not_before)-300)*1000:
        raise ValueError('Перевод сделан до создания заявки')
    now=api_post('/wallet/getnowblock',{})
    block=int(now.get('block_header',{}).get('raw_data',{}).get('number',0))
    if block-int(tx['blockNumber'])+1 < MIN_CONFIRMATIONS: raise ValueError('Недостаточно подтверждений сети')
    token_hex=tron_decode(USDT_CONTRACT)[1:].hex()
    dest_hex=tron_decode(to_address)[1:].hex()
    src_hex=tron_decode(from_address)[1:].hex() if from_address else None
    matched=0
    for log in tx.get('log',[]):
        topics=log.get('topics',[])
        contract=(log.get('address','').lower().removeprefix('41')).removeprefix('0x')
        if contract!=token_hex or len(topics)<3 or topics[0].lower().removeprefix('0x')!=TRANSFER_TOPIC: continue
        frm=topics[1].lower()[-40:]; dest=topics[2].lower()[-40:]
        if dest==dest_hex and (src_hex is None or frm==src_hex):
            try: matched+=int(log['data'],16)
            except (ValueError,KeyError): pass
    if matched!=expected_micro:
        raise ValueError('Сумма, получатель, отправитель или контракт USDT не совпадают с заявкой')
    return {'amount_micro':matched,'block':int(tx['blockNumber'])}


def add_audit(c,actor,action,details):
    c.execute('INSERT INTO audits(actor_id,action,details,created_at) VALUES(?,?,?,?)',(actor,action,json.dumps(details,ensure_ascii=False),int(time.time())))


def balance(c,user_id):
    return int(c.execute('SELECT COALESCE(SUM(amount_micro),0) FROM ledger WHERE user_id=?',(user_id,)).fetchone()[0])


def available(c,user_id):
    locked=int(c.execute("SELECT COALESCE(SUM(amount_micro),0) FROM withdrawals WHERE user_id=? AND status='pending'",(user_id,)).fetchone()[0])
    return balance(c,user_id)-locked


def trade_pnl_total(c,user_id):
    return int(c.execute('SELECT COALESCE(SUM(pnl_micro),0) FROM bot_trades WHERE user_id=?',(user_id,)).fetchone()[0])

def trade_pnl_today(c,user_id):
    # UTC reporting day; show source and timezone explicitly in interface.
    t=int(time.time());utcday=t-(t%86400)
    return int(c.execute('SELECT COALESCE(SUM(pnl_micro),0) FROM bot_trades WHERE user_id=? AND closed_at>=?',(user_id,utcday)).fetchone()[0])

class Handler(BaseHTTPRequestHandler):
    server_version='VELTRA/1.0'
    def log_message(self, fmt,*args):
        print('%s %s' % (self.address_string(),fmt%args))
    def send_json(self,body,status=200,cookie=None):
        raw=json.dumps(body,ensure_ascii=False,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY'); self.send_header('Referrer-Policy','no-referrer')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(raw)
    def path_parts(self): return urlparse(self.path).path.rstrip('/').split('/')
    def auth(self,c):
        cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
        v=cookie.get('sid'); token=v.value if v else ''
        if not re.fullmatch('[a-f0-9]{64}',token): return None
        row=c.execute('SELECT u.id,u.name,u.email,u.phone,u.admin,u.disabled,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
        return row if row and not row['disabled'] else None
    def load_json(self):
        if int(self.headers.get('Content-Length','0'))>10000: raise ValueError('Слишком большой запрос')
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        try: result=json.loads(raw)
        except ValueError: raise ValueError('Ожидался JSON')
        if not isinstance(result,dict): raise ValueError('Ожидался объект JSON')
        return result
    def csrf(self,u):
        if not u or not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),u['csrf']): raise PermissionError('Требуется вход и защитный токен')
        origin=self.headers.get('Origin')
        if origin and urlparse(origin).netloc!=self.headers.get('Host'): raise PermissionError('Некорректный источник запроса')
    def do_GET(self):
        try:self.route('GET')
        except Exception as e:self.error(e)
    def do_POST(self):
        try:self.route('POST')
        except Exception as e:self.error(e)
    def error(self,e):
        if isinstance(e,PermissionError):return self.send_json({'error':str(e)},403)
        if isinstance(e,ValueError):return self.send_json({'error':str(e)},400)
        if isinstance(e,sqlite3.IntegrityError):return self.send_json({'error':'Дублирующая операция или адрес электронной почты'},409)
        print('SERVER ERROR:',repr(e)); self.send_json({'error':'Ошибка сервера'},500)
    def route(self,method):
        path=urlparse(self.path).path
        if path == '/telegram/webhook':
            if method != 'POST':
                return self.send_json({'error':'Method not allowed'},405)
            if not TELEGRAM_BOT_TOKEN:
                return self.send_json({'error':'Telegram bot not configured'},503)
            token = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if not hmac.compare_digest(token, telegram_webhook_secret()):
                return self.send_json({'error':'Unauthorized'},403)
            update = self.load_json()
            telegram_handle_update(update)
            return self.send_json({'ok':True})
        if method=='GET' and not path.startswith('/api/'):
            safe=path.lstrip('/') or 'index.html'
            staticroot=(ROOT/'static').resolve();p=(staticroot/safe).resolve()
            if not p.is_relative_to(staticroot) or not p.is_file():return self.send_json({'error':'Не найдено'},404)
            raw=p.read_bytes();self.send_response(200); self.send_header('Content-Type',(mimetypes.guess_type(str(p))[0] or 'application/octet-stream')+('; charset=utf-8' if p.suffix in ('.html','.css','.js','.json') else ''));self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','public, max-age=300' if p.suffix in ('.png','.css','.js') else 'no-store');self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(raw);return
        if method=='GET' and path=='/api/config':return self.send_json({'receive_address':RECEIVE_ADDRESS,'deposits_enabled':bool(RECEIVE_ADDRESS),'min_confirmations':MIN_CONFIRMATIONS,'network':'TRON (TRC-20)','token':'USDT','demo':True})
        with DB_LOCK, conn() as c:
            u=self.auth(c)
            if method=='POST' and path in ('/api/register','/api/login'):
                data=self.load_json();email=str(data.get('email','')).strip().lower();passwd=str(data.get('password',''))
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>240:raise ValueError('Введите корректную почту')
                if path=='/api/register':
                    name=str(data.get('name','')).strip()
                    phone=re.sub(r'[\s()\-]','',str(data.get('phone','')).strip())
                    wdpass=str(data.get('withdraw_password',''))
                    if not (2<=len(name)<=80):raise ValueError('Укажите имя длиной от 2 до 80 символов')
                    if not re.fullmatch(r'\+?[0-9]{10,15}',phone):raise ValueError('Укажите номер телефона с кодом страны')
                    if len(passwd)<12 or len(wdpass)<10 or passwd==wdpass:raise ValueError('Пароль: минимум 12 символов; пароль вывода: минимум 10 и должен отличаться')
                    c.execute('INSERT INTO users(email,name,phone,passhash,withdraw_hash,created_at) VALUES(?,?,?,?,?,?)',(email,name,phone,hash_password(passwd),hash_password(wdpass),int(time.time())))
                row=c.execute('SELECT id,passhash,disabled FROM users WHERE email=?',(email,)).fetchone()
                if not row or not password_check(passwd,row['passhash']):raise PermissionError('Неверный логин или пароль')
                if row['disabled']:raise PermissionError('Доступ к аккаунту приостановлен. Обратитесь к администратору.')
                token=secrets.token_hex(32); csrf=secrets.token_hex(24)
                c.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['id'],csrf,int(time.time())+86400))
                cookie='sid='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400'+('; Secure' if COOKIE_SECURE else '')
                return self.send_json({'ok':True,'csrf':csrf},cookie=cookie)
            if method=='GET' and path=='/api/me':
                if not u:return self.send_json({'authenticated':False})
                return self.send_json({'authenticated':True,'id':u['id'],'uid':f"VLT-{u['id']:08d}",'name':u['name'],'email':u['email'],'phone':u['phone'] or '', 'admin':bool(u['admin']),'csrf':u['csrf'],'balance':amt(balance(c,u['id'])),'available':amt(available(c,u['id'])),'locked':amt(balance(c,u['id'])-available(c,u['id'])),'earnings_today':amt(trade_pnl_today(c,u['id'])),'earnings_total':amt(trade_pnl_total(c,u['id']))})
            if method=='POST' and path=='/api/password-reset':
                data=self.load_json(); token=str(data.get('token','')).strip(); passwd=str(data.get('password','')); wdpass=str(data.get('withdraw_password',''))
                if len(passwd)<12 or len(wdpass)<10 or passwd==wdpass:raise ValueError('Укажите два разных надёжных пароля')
                if not re.fullmatch(r'[a-f0-9]{64}',token):raise ValueError('Некорректная ссылка восстановления')
                row=c.execute('SELECT * FROM password_resets WHERE token_hash=? AND used=0 AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
                if not row:raise ValueError('Ссылка недействительна или устарела')
                c.execute('BEGIN IMMEDIATE')
                c.execute('UPDATE users SET passhash=?,withdraw_hash=? WHERE id=?',(hash_password(passwd),hash_password(wdpass),row['user_id']))
                c.execute('UPDATE password_resets SET used=1 WHERE id=?',(row['id'],))
                c.execute('DELETE FROM sessions WHERE user_id=?',(row['user_id'],))
                add_audit(c,row['user_id'],'credentials_reset',{'user':row['user_id']})
                c.execute('COMMIT');return self.send_json({'ok':True})
            if not u:raise PermissionError('Войдите в аккаунт')
            if method=='POST': self.csrf(u)
            if method=='POST' and path=='/api/logout':
                sid=SimpleCookie();sid.load(self.headers.get('Cookie',''));t=sid.get('sid');
                if t:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(t.value.encode()).hexdigest(),))
                return self.send_json({'ok':True},cookie='sid=; Path=/; Max-Age=0; SameSite=Strict')
            if method=='GET' and path=='/api/activity':
                deps=[dict(x) for x in c.execute('SELECT id,amount_micro,txid,status,created_at,expires FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 100',(u['id'],)).fetchall()]
                wd=[dict(x) for x in c.execute('SELECT id,amount_micro,address,txid,status,created_at FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 100',(u['id'],)).fetchall()]
                led=[dict(x) for x in c.execute('SELECT kind,amount_micro,txid,created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 100',(u['id'],)).fetchall()]
                return self.send_json({'deposits':deps,'withdrawals':wd,'ledger':led})
            if method=='POST' and path=='/api/deposits':
                if not RECEIVE_ADDRESS:raise ValueError('Оператор ещё не настроил TRC-20-кошелёк')
                tron_decode(RECEIVE_ADDRESS)
                data=self.load_json();base=parse_amount(data.get('amount'))
                if base>999_999_000_000:raise ValueError('Слишком большая сумма')
                # Distinct, short-lived fractional payment amount for attribution to one shared address.
                # Operational review is still necessary; not an ownership signature.
                for i in range(20):
                    suffix=secrets.randbelow(89999)+1001
                    exact=base+suffix
                    if not c.execute("SELECT 1 FROM deposits WHERE amount_micro=? AND status IN ('awaiting_txid','awaiting_review') AND expires>?",(exact,int(time.time()))).fetchone():break
                else:raise ValueError('Не удалось создать платёжную заявку')
                now=int(time.time());cur=c.execute('INSERT INTO deposits(user_id,amount_micro,status,created_at,expires) VALUES(?,?,?,?,?)',(u['id'],exact,'awaiting_txid',now,now+86400))
                add_audit(c,u['id'],'deposit_created',{'id':cur.lastrowid,'micro':exact})
                return self.send_json({'id':cur.lastrowid,'amount':amt(exact),'receive_address':RECEIVE_ADDRESS,'expires':now+86400})
            if method=='POST' and re.fullmatch('/api/deposits/\\d+/txid',path):
                did=int(path.split('/')[3]);d=self.load_json();txid=str(d.get('txid','')).strip().lower()
                if not re.fullmatch('[a-f0-9]{64}',txid):raise ValueError('TXID должен содержать 64 шестнадцатеричных символа')
                row=c.execute('SELECT * FROM deposits WHERE id=? AND user_id=?',(did,u['id'])).fetchone()
                if not row or row['status'] not in ('awaiting_txid','awaiting_review'):raise ValueError('Заявка не найдена или закрыта')
                if int(time.time())>row['expires']:raise ValueError('Срок заявки истёк, создайте новую')
                c.execute('UPDATE deposits SET txid=?,status=? WHERE id=?',(txid,'awaiting_review',did))
                return self.send_json({'ok':True,'status':'awaiting_review'})
            if method=='POST' and path=='/api/withdrawals':
                data=self.load_json();n=parse_amount(data.get('amount'));dest=str(data.get('address','')).strip();tron_decode(dest)
                p=c.execute('SELECT withdraw_hash FROM users WHERE id=?',(u['id'],)).fetchone()
                if not p['withdraw_hash'] or not password_check(str(data.get('withdraw_password','')),p['withdraw_hash']):raise PermissionError('Неверный пароль подтверждения вывода')
                c.execute('BEGIN IMMEDIATE')
                if n>available(c,u['id']):raise ValueError('Недостаточно доступных подтверждённых средств')
                now=int(time.time());cur=c.execute('INSERT INTO withdrawals(user_id,amount_micro,address,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',(u['id'],n,dest,'pending',now,now))
                add_audit(c,u['id'],'withdraw_requested',{'id':cur.lastrowid,'amount':n,'address':dest});c.execute('COMMIT')
                return self.send_json({'ok':True,'id':cur.lastrowid})
            if method=='POST' and path=='/api/account/passwords':
                data=self.load_json();old=str(data.get('current_password',''))
                p=c.execute('SELECT passhash FROM users WHERE id=?',(u['id'],)).fetchone()
                if not password_check(old,p['passhash']):raise PermissionError('Неверный текущий пароль входа')
                newpass=str(data.get('password',''));newwd=str(data.get('withdraw_password',''))
                if len(newpass)<12 or len(newwd)<10 or newpass==newwd:raise ValueError('Новые пароли должны отличаться (12 и 10+ символов)')
                c.execute('UPDATE users SET passhash=?,withdraw_hash=? WHERE id=?',(hash_password(newpass),hash_password(newwd),u['id']))
                c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
                add_audit(c,u['id'],'self_credentials_changed',{})
                return self.send_json({'ok':True,'relogin':True},cookie='sid=; Path=/; Max-Age=0; SameSite=Strict')
            if method=='GET' and path=='/api/trades':
                rows=[dict(x) for x in c.execute('SELECT symbol,side,order_ref,entry_price,exit_price,volume,pnl_micro,closed_at,source FROM bot_trades WHERE user_id=? ORDER BY closed_at DESC LIMIT 250',(u['id'],)).fetchall()]
                return self.send_json({'trades':rows,'source_notice':'Импорт оператора; сделки не подтверждаются автоматически брокером'})
            if not u['admin']:raise PermissionError('Только для администратора')
            if method=='POST' and re.fullmatch(r'/api/admin/users/\d+/reset',path):
                target=int(path.split('/')[4]);data=self.load_json()
                if not c.execute('SELECT id FROM users WHERE id=?',(target,)).fetchone():raise ValueError('Пользователь не найден')
                token=secrets.token_hex(32);now=int(time.time())
                c.execute('UPDATE password_resets SET used=1 WHERE user_id=?',(target,))
                c.execute('INSERT INTO password_resets(user_id,token_hash,expires,created_by,created_at) VALUES(?,?,?,?,?)',(target,hashlib.sha256(token.encode()).hexdigest(),now+1800,u['id'],now))
                add_audit(c,u['id'],'password_reset_issued',{'target_id':target})
                return self.send_json({'ok':True,'token':token,'expires':now+1800})
            if method=='POST' and re.fullmatch(r'/api/admin/users/\d+/edit',path):
                target=int(path.split('/')[4]);d=self.load_json()
                name=str(d.get('name','')).strip();phone=re.sub(r'[\s()\-]','',str(d.get('phone','')).strip());email=str(d.get('email','')).strip().lower()
                if len(name)<2 or len(name)>80:raise ValueError('Имя: 2–80 символов')
                if not re.fullmatch(r'\+?[0-9]{10,15}',phone):raise ValueError('Некорректный телефон')
                if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>240:raise ValueError('Некорректная почта')
                if not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():raise ValueError('Пользователь не найден')
                c.execute('UPDATE users SET name=?,phone=?,email=? WHERE id=?',(name,phone,email,target))
                add_audit(c,u['id'],'user_profile_edited',{'target_id':target,'name':name,'email':email,'phone':phone})
                return self.send_json({'ok':True})
            if method=='POST' and re.fullmatch(r'/api/admin/users/\d+/status',path):
                target=int(path.split('/')[4]);data=self.load_json()
                if target==u['id']:raise ValueError('Нельзя менять статус своего аккаунта')
                disabled=1 if data.get('disabled') is True else 0
                changed=c.execute('UPDATE users SET disabled=? WHERE id=?',(disabled,target))
                if changed.rowcount!=1:raise ValueError('Пользователь не найден')
                if disabled:c.execute('DELETE FROM sessions WHERE user_id=?',(target,))
                add_audit(c,u['id'],'user_status_change',{'target_id':target,'disabled':bool(disabled)})
                return self.send_json({'ok':True})
            if method=='POST' and path=='/api/admin/corrections':
                if not ENABLE_CORRECTIONS:raise PermissionError('Ручные корректировки отключены в настройках сервера')
                data=self.load_json();target=int(data.get('user_id',0));direction=str(data.get('direction',''))
                value=parse_amount(data.get('amount'));reason=str(data.get('reason','')).strip()
                if direction not in ('credit','debit') or len(reason)<16 or len(reason)>400:raise ValueError('Укажите направление и подробную причину (16–400 символов)')
                if value>1000*1_000_000:raise ValueError('Максимальная корректировка: 1000 USDT')
                c.execute('BEGIN IMMEDIATE')
                if not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():raise ValueError('Пользователь не найден')
                signed=value if direction=='credit' else -value
                if signed<0 and available(c,target)<value:raise ValueError('Нельзя списать больше доступного остатка')
                ref='correction:'+secrets.token_hex(12)
                c.execute('INSERT INTO ledger(user_id,kind,amount_micro,ref,created_at) VALUES(?,?,?,?,?)',(target,'correction',signed,ref,int(time.time())))
                add_audit(c,u['id'],'ledger_correction',{'target_id':target,'amount_micro':signed,'reason':reason,'ref':ref})
                c.execute('COMMIT');return self.send_json({'ok':True})
            if method=='POST' and path=='/api/admin/trades':
                data=self.load_json();target=int(data.get('user_id',0));order_ref=str(data.get('order_ref','')).strip();symbol=str(data.get('symbol',''));side=str(data.get('side',''))
                if symbol not in ('XAUUSD','BTCUSD') or side not in ('BUY','SELL'):raise ValueError('Допустимы BTCUSD/XAUUSD и BUY/SELL')
                if not re.fullmatch(r'[a-zA-Z0-9._:-]{3,72}',order_ref):raise ValueError('Не указан идентификатор ордера брокера')
                entry=str(data.get('entry_price',''));ex=str(data.get('exit_price',''));volume=str(data.get('volume',''))
                for q in (entry,ex,volume):
                    try:
                        if Decimal(q)<=0 or not Decimal(q).is_finite():raise ValueError()
                    except Exception:raise ValueError('Укажите корректные числовые параметры сделки')
                pnl=Decimal(str(data.get('pnl','0')))
                if not pnl.is_finite() or abs(pnl)>Decimal('100000'):raise ValueError('Некорректный PnL')
                closed=int(data.get('closed_at',0))
                if closed<1500000000 or closed>int(time.time())+120:raise ValueError('Некорректное время закрытия')
                if not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():raise ValueError('Пользователь не найден')
                c.execute('INSERT INTO bot_trades(user_id,symbol,side,order_ref,entry_price,exit_price,volume,pnl_micro,closed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(target,symbol,side,order_ref,entry,ex,volume,int(pnl*1_000_000),closed,u['id'],int(time.time())))
                add_audit(c,u['id'],'trade_imported',{'target_id':target,'order_ref':order_ref,'source':'operator_import'})
                return self.send_json({'ok':True,'notice':'Результат записан в журнал советника, но не зачислен на баланс'})
            if method=='GET' and path=='/api/admin/overview':
                users=[]
                for r in c.execute('SELECT id,name,email,phone,disabled,created_at FROM users ORDER BY id DESC LIMIT 300').fetchall():
                    d=dict(r);d['uid']=f"VLT-{d['id']:08d}";d['balance']=amt(balance(c,d['id']));d['available']=amt(available(c,d['id']));users.append(d)
                deps=[dict(r) for r in c.execute('SELECT d.*,u.email FROM deposits d JOIN users u ON u.id=d.user_id ORDER BY d.id DESC LIMIT 200').fetchall()]
                wds=[dict(r) for r in c.execute('SELECT w.*,u.email FROM withdrawals w JOIN users u ON u.id=w.user_id ORDER BY w.id DESC LIMIT 200').fetchall()]
                audits=[dict(r) for r in c.execute('SELECT action,details,created_at FROM audits ORDER BY id DESC LIMIT 60').fetchall()]
                return self.send_json({'users':users,'deposits':deps,'withdrawals':wds,'audits':audits,'manual_corrections_enabled':ENABLE_CORRECTIONS})
            if method=='POST' and re.fullmatch('/api/admin/deposits/\\d+/verify',path):
                did=int(path.split('/')[4]);r=c.execute('SELECT * FROM deposits WHERE id=?',(did,)).fetchone()
                if not r or r['status']!='awaiting_review' or not r['txid']:raise ValueError('Заявка не готова к проверке')
                if int(time.time())>r['expires']+3600: raise ValueError('Заявка просрочена; требуется отдельное урегулирование')
                # Network call happens BEFORE DB update; repeated approvals guarded by ledger ref uniqueness.
                verify_transfer(r['txid'],RECEIVE_ADDRESS,int(r['amount_micro']),not_before=r['created_at'])
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT * FROM deposits WHERE id=?',(did,)).fetchone()
                if r['status']!='awaiting_review':raise ValueError('Уже обработано')
                c.execute('INSERT INTO ledger(user_id,kind,amount_micro,ref,txid,created_at) VALUES(?,?,?,?,?,?)',(r['user_id'],'deposit',r['amount_micro'],f'deposit:{did}',r['txid'],int(time.time())))
                c.execute('UPDATE deposits SET status=?,verified_at=? WHERE id=?',('confirmed',int(time.time()),did))
                add_audit(c,u['id'],'deposit_verified',{'id':did,'txid':r['txid']});c.execute('COMMIT')
                return self.send_json({'ok':True})
            if method=='POST' and re.fullmatch('/api/admin/withdrawals/\\d+/reject',path):
                wid=int(path.split('/')[4]);c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()
                if not r or r['status']!='pending':raise ValueError('Заявка уже обработана')
                c.execute('UPDATE withdrawals SET status=?,updated_at=? WHERE id=?',('rejected',int(time.time()),wid));add_audit(c,u['id'],'withdraw_rejected',{'id':wid});c.execute('COMMIT');return self.send_json({'ok':True})
            if method=='POST' and re.fullmatch('/api/admin/withdrawals/\\d+/confirm',path):
                wid=int(path.split('/')[4]);data=self.load_json();txid=str(data.get('txid','')).lower().strip()
                r=c.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()
                if not r or r['status']!='pending':raise ValueError('Нет ожидающей заявки')
                if not PAYOUT_ADDRESS:raise ValueError('Настройте TRON_PAYOUT_ADDRESS')
                verify_transfer(txid,r['address'],int(r['amount_micro']),from_address=PAYOUT_ADDRESS,not_before=r['created_at'])
                c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM withdrawals WHERE id=?',(wid,)).fetchone()
                if r['status']!='pending':raise ValueError('Уже обработано')
                c.execute('INSERT INTO ledger(user_id,kind,amount_micro,ref,txid,created_at) VALUES(?,?,?,?,?,?)',(r['user_id'],'withdrawal',-int(r['amount_micro']),f'withdrawal:{wid}',txid,int(time.time())))
                c.execute('UPDATE withdrawals SET status=?,txid=?,updated_at=? WHERE id=?',('paid',txid,int(time.time()),wid))
                add_audit(c,u['id'],'withdraw_paid',{'id':wid,'txid':txid});c.execute('COMMIT');return self.send_json({'ok':True})
            return self.send_json({'error':'Маршрут не найден'},404)


def main():
    if RECEIVE_ADDRESS:tron_decode(RECEIVE_ADDRESS)
    if PAYOUT_ADDRESS:tron_decode(PAYOUT_ADDRESS)
    init_db();print(f'VELTRA: http://{HOST}:{PORT} | DB={DB_PATH} | wallet={RECEIVE_ADDRESS or "not set"}')
    telegram_register_webhook()
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()

if __name__=='__main__':main()
