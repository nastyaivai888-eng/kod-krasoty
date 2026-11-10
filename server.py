"""КОД КРАСОТЫ — protected PDF reader, manual payments and Supabase orders."""
import os
import io
import re
import json
import html
import hmac
import base64
import hashlib
import secrets
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8080'))
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
DEMO_MODE = os.environ.get('DEMO_MODE', '1') == '1'
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'https://kod-krasoty.onrender.com').rstrip('/')
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_SECRET_KEY = os.environ.get('SUPABASE_SECRET_KEY', '')
STORAGE_BUCKET = 'beauty-guides'
STORAGE_FILE = 'guide.pdf'
PAY = 'https://pay.kaspi.kz/pay/w6fc4b8h'
PRICE = 4990
_pdf_lock = threading.Lock()
_pdf_bytes = None


def supabase_request(method, endpoint, payload=None, *, raw=False):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError('Missing Supabase URL or server secret key')
    headers = {'apikey': SUPABASE_SECRET_KEY, 'Authorization': 'Bearer ' + SUPABASE_SECRET_KEY}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if method != 'GET':
        headers['Prefer'] = 'return=representation'
    req = urllib.request.Request(SUPABASE_URL + endpoint, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=35) as response:
        body = response.read()
    return body if raw else json.loads(body.decode('utf-8')) if body else []


def orders_get(filters='', limit=200):
    return supabase_request('GET', '/rest/v1/orders?select=*&' + filters + ('&' if filters else '') + 'limit=' + str(limit))


def new_order(name, contact):
    oid = 'KK-' + secrets.token_hex(3).upper()
    data = {'order_number': oid, 'customer_name': name, 'customer_contact': contact,
            'amount': PRICE, 'status': 'pending', 'access_token': None}
    if not supabase_request('POST', '/rest/v1/orders', data):
        raise RuntimeError('Supabase did not return a saved order')
    return oid


def update_order(row_id, fields):
    endpoint = '/rest/v1/orders?id=eq.' + urllib.parse.quote(str(row_id), safe='')
    if not supabase_request('PATCH', endpoint, fields):
        raise RuntimeError('Supabase did not confirm updated order')


def find_order(order_no):
    rows = orders_get('order_number=eq.' + urllib.parse.quote(order_no, safe=''), 2)
    return rows[0] if rows else None


def find_by_token(token):
    if not 40 <= len(token) <= 150:
        return None
    digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    rows = orders_get('access_token=eq.' + digest, 2)
    return rows[0] if rows and rows[0].get('status') == 'paid' else None


def get_pdf_bytes():
    global _pdf_bytes
    if _pdf_bytes is None:
        with _pdf_lock:
            if _pdf_bytes is None:
                path = '/storage/v1/object/authenticated/' + urllib.parse.quote(STORAGE_BUCKET) + '/' + urllib.parse.quote(STORAGE_FILE)
                document = supabase_request('GET', path, raw=True)
                if len(document) > 60 * 1024 * 1024 or not document.startswith(b'%PDF-'):
                    raise RuntimeError('Invalid PDF in private Supabase storage')
                _pdf_bytes = document
    return _pdf_bytes


def page(title, body):
    style = '''body{margin:0;background:#f7f3e9;color:#173d34;font:16px Arial,sans-serif}
main{max-width:760px;margin:24px auto;padding:20px;background:#fff;border:1px solid #dce4d9;border-radius:14px}
h1{font:clamp(28px,8vw,44px) Georgia;overflow-wrap:anywhere}input,button,textarea{font:inherit;padding:12px;margin:8px 0;width:100%;box-sizing:border-box}
button,.btn{background:#173d34;color:white;border:0;border-radius:6px;cursor:pointer;padding:13px;display:inline-block;text-decoration:none;box-sizing:border-box;max-width:100%;text-align:center;overflow-wrap:anywhere}a{color:#173d34}.muted{color:#65766d}img{max-width:100%;height:auto}footer,footer *{box-sizing:border-box;max-width:100%;overflow-wrap:anywhere}
.reader{max-width:900px;margin:auto}.reader img{display:block;width:100%;height:auto;margin:0 auto 14px}.reader .sheet{margin:0 0 16px;padding:0}.reader-nav{position:sticky;top:0;background:#fff;padding:10px 0;border-bottom:1px solid #dce4d9;z-index:2}'''
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
            + html.escape(title) + '</title><style>' + style + '</style></head><body><main>' + body + '</main></body></html>').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    def send_data(self, status, body, mime='text/html; charset=utf-8', extra=None):
        self.send_response(status)
        headers = {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'no-store, private',
                   'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                   'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}
        headers.update(extra or {})
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def error_page(self, status=503):
        self.send_data(status, page('Временно недоступно', '<h1>Сервис временно недоступен</h1><p>Попробуйте позднее. Оплату пока не выполняйте.</p>'))

    def form(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 8192:
            raise ValueError('Invalid form size')
        return urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'), keep_blank_values=True)

    def authenticated(self):
        if not ADMIN_PASSWORD:
            return False
        try:
            scheme, encoded = self.headers.get('Authorization', '').split(' ', 1)
            if scheme.lower() != 'basic':
                return False
            user, pw = base64.b64decode(encoded).decode('utf-8').split(':', 1)
            return hmac.compare_digest(user, 'admin') and hmac.compare_digest(pw, ADMIN_PASSWORD)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            return False

    def require_admin(self):
        if self.authenticated():
            return True
        self.send_data(401, page('Вход', '<h1>Вход администратора</h1>'),
                       extra={'WWW-Authenticate': 'Basic realm="Kod Krasoty Admin"'})
        return False

    def safe_admin_post(self):
        origin = self.headers.get('Origin', '')
        fetch_site = self.headers.get('Sec-Fetch-Site', '')
        expected = urllib.parse.urlsplit(PUBLIC_BASE_URL)
        if origin and origin != 'null':
            actual = urllib.parse.urlsplit(origin)
            valid = actual.scheme == expected.scheme and actual.netloc == expected.netloc
        else:
            valid = fetch_site == 'same-origin'
        if not valid:
            self.send_data(403, b'Forbidden', 'text/plain')
            return False
        return self.require_admin()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if p in ('/', '/cover.png', '/reader-demo.html'):
                filename = {'/': 'index.html', '/cover.png': 'cover.png', '/reader-demo.html': 'reader-demo.html'}[p]
                mime = 'image/png' if p == '/cover.png' else 'text/html; charset=utf-8'
                self.send_data(200, (ROOT / filename).read_bytes(), mime)
                return
            if p == '/order':
                if DEMO_MODE:
                    self.send_data(200, page('Демонстрация', '<h1>Сайт готовится к запуску</h1><p>Оплата временно отключена.</p><a href="/">Вернуться</a>'))
                    return
                body = '''<h1>КОД КРАСОТЫ</h1><p>Цена: <b>4 990 ₸</b>. Заполните заявку перед оплатой. Доступ выдаётся после ручной проверки поступления платежа.</p><form method="post" action="/order"><label>Имя<input name="name" maxlength="80" required></label><label>Email или телефон для связи<input name="contact" maxlength="120" required></label><button>Создать заявку</button></form><p class="muted">Не указывайте пароли и банковские реквизиты.</p>'''
                self.send_data(200, page('Заявка', body))
                return
            if p == '/admin':
                if not self.require_admin():
                    return
                rows = orders_get('order=created_at.desc', 200)
                body = '<h1>Заявки</h1><p>Подтверждайте оплату только по фактическому поступлению в Kaspi Pay, не по скриншоту.</p>'
                for r in rows:
                    oid = html.escape(str(r['order_number']), quote=True)
                    name = html.escape(str(r['customer_name']))
                    contact = html.escape(str(r['customer_contact']))
                    status = r.get('status')
                    body += '<div style="border-top:1px solid #ddd;padding:14px 0"><b>' + name + '</b> — ' + contact + '<br>Заказ ' + oid + ' · ' + html.escape(str(status))
                    if status == 'pending':
                        body += '<form method="post" action="/admin/approve"><input type="hidden" name="id" value="' + oid + '"><button>Подтвердить оплату и создать ссылку</button></form>'
                    if status == 'paid':
                        body += '<form method="post" action="/admin/revoke"><input type="hidden" name="id" value="' + oid + '"><button>Отозвать доступ</button></form>'
                    body += '</div>'
                self.send_data(200, page('Администрирование', body))
                return
            if p == '/read' or p.startswith('/page/'):
                token = q.get('token', [''])[0]
                r = find_by_token(token)
                if not r:
                    self.send_data(403, page('Доступ закрыт', '<h1>Нет доступа</h1><p>Ссылка недействительна или доступ отозван.</p>'))
                    return
                pdf_bytes = get_pdf_bytes()
                if p == '/read':
                    with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
                        count = len(doc)
                    name = html.escape(str(r['customer_name']))
                    oid = html.escape(str(r['order_number']))
                    body = '<div class="reader"><h1>КОД КРАСОТЫ</h1><p>Персональный доступ: ' + name + ' · заказ ' + oid + '</p><p class="muted">Только для личного чтения.</p><div class="reader-nav">Страниц: ' + str(count) + '</div>'
                    for i in range(count):
                        body += '<div class="sheet" id="page-' + str(i+1) + '"><img loading="lazy" draggable="false" alt="Страница ' + str(i+1) + '" src="/page/' + str(i+1) + '?token=' + urllib.parse.quote(token, safe='') + '"></div>'
                    self.send_data(200, page('Моё руководство', body + '</div>'))
                    return
                try:
                    i = int(p.rsplit('/', 1)[1]) - 1
                except ValueError:
                    self.send_data(404, b'Not found', 'text/plain')
                    return
                with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
                    if not 0 <= i < len(doc):
                        self.send_data(404, b'Not found', 'text/plain')
                        return
                    pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
                    im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                out = io.BytesIO()
                im.save(out, format='WEBP', quality=88)
                self.send_data(200, out.getvalue(), 'image/webp')
                return
            self.send_data(404, page('Не найдено', '<h1>Страница не найдена</h1>'))
        except Exception as exc:
            print('Request failed: ' + type(exc).__name__ + ': ' + str(exc)[:160], flush=True)
            self.error_page()

    def do_POST(self):
        p = urllib.parse.urlsplit(self.path).path
        if p not in ('/order', '/admin/approve', '/admin/revoke'):
            self.send_data(404, b'Not found', 'text/plain')
            return
        if p.startswith('/admin/') and not self.safe_admin_post():
            return
        try:
            f = self.form()
        except (ValueError, UnicodeDecodeError):
            self.send_data(400, b'Invalid request', 'text/plain')
            return
        try:
            if p == '/order':
                if DEMO_MODE:
                    self.send_data(403, page('Недоступно', '<p>Заявки пока отключены.</p>'))
                    return
                name = f.get('name', [''])[0].strip()
                contact = f.get('contact', [''])[0].strip()
                if not (2 <= len(name) <= 80 and 5 <= len(contact) <= 120):
                    self.send_data(400, page('Ошибка', '<p>Проверьте имя и контакт.</p>'))
                    return
                oid = new_order(name, contact)
                body = '<h1>Заявка №' + html.escape(oid) + '</h1><p>Сумма: <b>4 990 ₸</b>.</p><p>Перед оплатой проверьте получателя и сумму в Kaspi. После оплаты сообщите продавцу номер заявки <b>' + html.escape(oid) + '</b>. Доступ выдаётся после проверки платежа.</p><p><a class="btn" href="' + html.escape(PAY, quote=True) + '" rel="noreferrer">Перейти к Kaspi Pay</a></p><p class="muted">Оплата сама по себе не открывает доступ автоматически.</p>'
                self.send_data(200, page('Заявка создана', body))
                return
            oid = f.get('id', [''])[0].strip()
            if not re.fullmatch(r'KK-[A-F0-9]{6}', oid):
                self.send_data(400, b'Invalid order ID', 'text/plain')
                return
            r = find_order(oid)
            if not r:
                self.send_data(404, b'Unknown order', 'text/plain')
                return
            if p == '/admin/approve':
                if r['status'] != 'pending':
                    self.send_data(409, page('Уже обработан', '<p>Этот заказ уже обработан.</p>'))
                    return
                token = secrets.token_urlsafe(36)
                digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
                update_order(r['id'], {'status': 'paid', 'access_token': digest})
                link = PUBLIC_BASE_URL + '/read?token=' + urllib.parse.quote(token, safe='')
                body = '<h1>Ссылка покупателя</h1><p>Скопируйте ссылку и отправьте только после проверки поступления оплаты. Она показывается один раз.</p><textarea readonly style="height:130px">' + html.escape(link) + '</textarea><p><a href="/admin">К заказам</a></p>'
                self.send_data(200, page('Доступ создан', body))
                return
            update_order(r['id'], {'status': 'revoked', 'access_token': None})
            self.send_data(200, page('Доступ отозван', '<h1>Доступ отозван</h1><a href="/admin">К заказам</a>'))
        except Exception as exc:
            print('Order processing failed: ' + type(exc).__name__ + ': ' + str(exc)[:160], flush=True)
            self.error_page()


if __name__ == '__main__':
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD) < 14:
        raise SystemExit('Set an ADMIN_PASSWORD of at least 14 characters')
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise SystemExit('Set SUPABASE_URL and SUPABASE_SECRET_KEY in Render')
    if not PUBLIC_BASE_URL.startswith('https://'):
        raise SystemExit('PUBLIC_BASE_URL must be an HTTPS URL')
    print('Starting Kod Krasoty on %s:%d (demo=%s)' % (HOST, PORT, DEMO_MODE), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
