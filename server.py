"""КОД КРАСОТЫ: persistent buyer links and reliable mobile PDF reader."""
import os, io, re, json, html, hmac, base64, hashlib, secrets, threading, urllib.parse, urllib.request, logging
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parent
HOST = os.getenv('HOST', '0.0.0.0')
PORT = int(os.getenv('PORT', '8080'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
DEMO_MODE = os.getenv('DEMO_MODE', '1') == '1'
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://kod-krasoty.onrender.com').rstrip('/')
SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_SECRET_KEY = os.getenv('SUPABASE_SECRET_KEY', '')
# Keep this secret unchanged after deployment: it signs existing v2 links.
TOKEN_SIGNING_KEY = os.getenv('TOKEN_SIGNING_KEY', SUPABASE_SECRET_KEY)
STORAGE_BUCKET = 'beauty-guides'
STORAGE_FILE = 'guide.pdf'
PAY = 'https://pay.kaspi.kz/pay/w6fc4b8h'
PRICE = 4990
_pdf_lock = threading.Lock()
_pdf_bytes = None


def supabase_request(method, endpoint, payload=None, *, raw=False):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError('Missing Supabase configuration')
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
    return body if raw else (json.loads(body.decode('utf-8')) if body else [])


def orders_get(filters='', limit=200):
    return supabase_request('GET', '/rest/v1/orders?select=*&' + filters + ('&' if filters else '') + 'limit=' + str(limit))


def find_order(oid):
    rows = orders_get('order_number=eq.' + urllib.parse.quote(oid, safe=''), 2)
    return rows[0] if rows else None


def new_order(name, contact):
    oid = 'KK-' + secrets.token_hex(3).upper()
    record = {'order_number': oid, 'customer_name': name, 'customer_contact': contact, 'amount': PRICE, 'status': 'pending', 'access_token': None}
    if not supabase_request('POST', '/rest/v1/orders', record):
        raise RuntimeError('Order was not saved')
    return oid


def update_order(row_id, fields):
    endpoint = '/rest/v1/orders?id=eq.' + urllib.parse.quote(str(row_id), safe='')
    if not supabase_request('PATCH', endpoint, fields):
        raise RuntimeError('Order update failed')


def permanent_token(oid):
    message = ('kod-krasoty:reader:v2:' + oid).encode('utf-8')
    sig = hmac.new(TOKEN_SIGNING_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return 'v2.' + oid + '.' + sig


def permanent_link(oid):
    return PUBLIC_BASE_URL + '/read?token=' + urllib.parse.quote(permanent_token(oid), safe='')


def find_by_token(token):
    if re.fullmatch(r'v2\.KK-[A-F0-9]{6}\.[a-f0-9]{64}', token):
        oid = token.split('.')[1]
        if not hmac.compare_digest(token, permanent_token(oid)):
            return None
        row = find_order(oid)
        return row if row and row.get('status') == 'paid' else None
    # Legacy links remain valid while their hashes are stored in Supabase.
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
                data = supabase_request('GET', path, raw=True)
                if len(data) > 60 * 1024 * 1024 or not data.startswith(b'%PDF-'):
                    raise RuntimeError('Invalid PDF in Supabase storage')
                _pdf_bytes = data
    return _pdf_bytes


CSS = '''*{box-sizing:border-box}body{margin:0;background:#f6f3eb;color:#173d34;font:16px/1.5 Arial,sans-serif}main{max-width:900px;margin:20px auto;padding:16px;background:white;border:1px solid #dce4d9;border-radius:12px}h1{font:clamp(26px,7vw,42px) Georgia,serif;overflow-wrap:anywhere}input,button,textarea{font:inherit;padding:12px;margin:8px 0;width:100%;max-width:100%}button,.btn{background:#173d34;color:white;border:0;border-radius:7px;cursor:pointer;padding:12px;display:inline-block;text-decoration:none;text-align:center;overflow-wrap:anywhere}a{color:#173d34}.muted{color:#65766d}.reader-nav{position:sticky;top:0;z-index:5;background:#fff;padding:12px 4px;border-bottom:1px solid #ddd;display:flex;align-items:center;justify-content:space-between;gap:10px}.sheet{margin:14px 0;min-height:100px;background:#f5f5f5;position:relative}.sheet img{display:block;width:100%;height:auto;min-height:40px}.sheet .status{padding:30px 10px;text-align:center;color:#53685c}.sheet button{width:auto;margin:8px auto;display:block}.reader{max-width:850px;margin:auto}textarea{min-height:110px} @media(max-width:600px){main{margin:0;padding:10px;border:0;border-radius:0}.reader-nav{font-size:14px}}'''


def page(title, body, script=''):
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>' + CSS + '</style></head><body><main>' + body + '</main>' + script + '</body></html>').encode('utf-8')


# Image loading is controlled by JavaScript, with explicit retries and a manual retry button.
# No loading="lazy": this avoids the mobile WebKit blank-page issue observed with long image readers.
READER_SCRIPT = '''<script>
(function(){
  const pages=[...document.querySelectorAll('.sheet')];
  let next=0, active=0;
  const limit=2;
  function run(){
    while(active<limit && next<pages.length){load(pages[next++]);}
  }
  function load(box){
    active++;
    const img=box.querySelector('img'), status=box.querySelector('.status');
    const url=img.dataset.src;
    let finished=false;
    const timeout=setTimeout(()=>done(false),25000);
    function done(ok){
      if(finished)return;
      finished=true;clearTimeout(timeout);active--;
      if(ok){img.hidden=false;status.remove();}
      else{status.textContent='Страница не загрузилась. ';const b=document.createElement('button');b.textContent='Повторить загрузку';b.onclick=()=>{b.remove();status.textContent='Загрузка страницы…';load(box)};status.appendChild(b);}
      run();
    }
    img.onload=()=>done(true);
    img.onerror=()=>done(false);
    img.src=url;
    if(img.complete && img.naturalWidth>0)done(true);
  }
  run();
})();
</script>'''


class Handler(BaseHTTPRequestHandler):
    def send_data(self, status, body, mime='text/html; charset=utf-8', extra=None):
        self.send_response(status)
        headers = {'Content-Type': mime, 'Content-Length': str(len(body)), 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}
        headers.update(extra or {})
        for key, value in headers.items(): self.send_header(key, value)
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def error_page(self):
        self.send_data(503, page('Временно недоступно', '<h1>Сервис временно недоступен</h1><p>Попробуйте позднее. Повторно оплачивать не нужно.</p>'))

    def form(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=8192: raise ValueError('Invalid form size')
        return urllib.parse.parse_qs(self.rfile.read(size).decode('utf-8'),keep_blank_values=True)

    def authenticated(self):
        if not ADMIN_PASSWORD: return False
        try:
            scheme, encoded=self.headers.get('Authorization','').split(' ',1)
            if scheme.lower()!='basic': return False
            user,pw=base64.b64decode(encoded,validate=True).decode('utf-8').split(':',1)
            return hmac.compare_digest(user,'admin') and hmac.compare_digest(pw,ADMIN_PASSWORD)
        except (ValueError,UnicodeDecodeError): return False

    def require_admin(self):
        if self.authenticated(): return True
        self.send_data(401,page('Вход','<h1>Вход администратора</h1>'),extra={'WWW-Authenticate':'Basic realm="Kod Krasoty Admin"'})
        return False

    def safe_admin_post(self):
        origin=self.headers.get('Origin','')
        fetch_site=self.headers.get('Sec-Fetch-Site','')
        expected=urllib.parse.urlsplit(PUBLIC_BASE_URL)
        if origin and origin!='null':
            actual=urllib.parse.urlsplit(origin)
            valid=actual.scheme==expected.scheme and actual.netloc==expected.netloc
        else: valid=fetch_site=='same-origin'
        if not valid:
            self.send_data(403,b'Forbidden','text/plain');return False
        return self.require_admin()

    def do_GET(self):
        u=urllib.parse.urlsplit(self.path);p=u.path;q=urllib.parse.parse_qs(u.query)
        try:
            if p in ('/','/cover.png','/reader-demo.html'):
                filename={'/':'index.html','/cover.png':'cover.png','/reader-demo.html':'reader-demo.html'}[p]
                self.send_data(200,(ROOT/filename).read_bytes(),'image/png' if p=='/cover.png' else 'text/html; charset=utf-8');return
            if p=='/order':
                if DEMO_MODE:
                    self.send_data(200,page('Демонстрация','<h1>Сайт готовится к запуску</h1><p>Оплата временно отключена.</p><a href="/">Вернуться</a>'));return
                body='<h1>КОД КРАСОТЫ</h1><p>Цена: <b>4 990 ₸</b>. Доступ после ручной проверки оплаты.</p><form method="post" action="/order"><label>Имя<input name="name" maxlength="80" required></label><label>Email или телефон<input name="contact" maxlength="120" required></label><button>Создать заявку</button></form>'
                self.send_data(200,page('Заявка',body));return
            if p=='/admin':
                if not self.require_admin():return
                rows=orders_get('order=created_at.desc',200)
                body='<h1>Заявки</h1><p>Оплата один раз — постоянная ссылка для повторного чтения, пока доступ не отозван. Проверяйте поступление денег в Kaspi Pay.</p>'
                for r in rows:
                    oid=html.escape(str(r['order_number']),quote=True)
                    name=html.escape(str(r['customer_name']))
                    contact=html.escape(str(r['customer_contact']))
                    status=r.get('status')
                    body+='<section style="border-top:1px solid #ddd;padding:14px 0"><b>'+name+'</b> — '+contact+'<p>Заказ '+oid+' · '+html.escape(str(status))+'</p>'
                    if status=='pending': body+='<form method="post" action="/admin/approve"><input type="hidden" name="id" value="'+oid+'"><button>Подтвердить оплату и создать ссылку</button></form>'
                    if status=='paid':
                        link=html.escape(permanent_link(str(r['order_number'])),quote=True)
                        body+='<p><a class="btn" href="'+link+'">Открыть книгу</a></p><label>Постоянная ссылка покупателя<textarea readonly>'+link+'</textarea></label><form method="post" action="/admin/revoke"><input type="hidden" name="id" value="'+oid+'"><button>Отозвать доступ</button></form>'
                    body+='</section>'
                self.send_data(200,page('Администрирование',body));return
            if p=='/read' or p.startswith('/page/'):
                token=q.get('token',[''])[0]
                r=find_by_token(token)
                if not r:
                    self.send_data(403,page('Доступ закрыт','<h1>Нет доступа</h1><p>Ссылка недействительна или доступ отозван.</p>'));return
                pdf_bytes=get_pdf_bytes()
                if p=='/read':
                    with fitz.open(stream=pdf_bytes,filetype='pdf') as doc: count=len(doc)
                    name=html.escape(str(r['customer_name']));oid=html.escape(str(r['order_number']))
                    body='<div class="reader"><h1>КОД КРАСОТЫ</h1><p>Персональный доступ: '+name+' · заказ '+oid+'</p><p class="muted">Для личного чтения. Если страница не загрузилась, нажмите «Повторить загрузку».</p><div class="reader-nav"><b>Моё руководство</b><span>Страниц: '+str(count)+'</span></div>'
                    for i in range(count):
                        src='/page/'+str(i+1)+'?token='+urllib.parse.quote(token,safe='')
                        body+='<section class="sheet" id="page-'+str(i+1)+'"><div class="status">Загрузка страницы '+str(i+1)+'…</div><img hidden draggable="false" alt="Страница '+str(i+1)+'" data-src="'+html.escape(src,quote=True)+'"></section>'
                    self.send_data(200,page('Моё руководство',body+'</div>',READER_SCRIPT));return
                try: i=int(p.rsplit('/',1)[1])-1
                except ValueError:
                    self.send_data(404,b'Not found','text/plain');return
                with fitz.open(stream=pdf_bytes,filetype='pdf') as doc:
                    if not 0<=i<len(doc):
                        self.send_data(404,b'Not found','text/plain');return
                    # JPEG is broadly supported by iPhone Safari and in-app browsers.
                    pix=doc[i].get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
                    im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                out=io.BytesIO();im.save(out,format='JPEG',quality=85,optimize=True)
                self.send_data(200,out.getvalue(),'image/jpeg');return
            self.send_data(404,page('Не найдено','<h1>Страница не найдена</h1>'))
        except Exception:
            logging.exception('GET failed: %s',p)
            self.error_page()

    def do_POST(self):
        p=urllib.parse.urlsplit(self.path).path
        if p not in ('/order','/admin/approve','/admin/revoke'):
            self.send_data(404,b'Not found','text/plain');return
        if p.startswith('/admin/') and not self.safe_admin_post():return
        try: f=self.form()
        except (ValueError,UnicodeDecodeError):
            self.send_data(400,b'Invalid request','text/plain');return
        try:
            if p=='/order':
                if DEMO_MODE:
                    self.send_data(403,page('Недоступно','<p>Заявки пока отключены.</p>'));return
                name=f.get('name',[''])[0].strip();contact=f.get('contact',[''])[0].strip()
                if not (2<=len(name)<=80 and 5<=len(contact)<=120):
                    self.send_data(400,page('Ошибка','<p>Проверьте имя и контакт.</p>'));return
                oid=new_order(name,contact)
                body='<h1>Заявка №'+html.escape(oid)+'</h1><p>Сумма: <b>4 990 ₸</b>.</p><p>Перед оплатой проверьте получателя в Kaspi. После оплаты сообщите номер заявки. Доступ откроется после проверки платежа.</p><p><a class="btn" href="'+html.escape(PAY,quote=True)+'" rel="noreferrer">Перейти к Kaspi Pay</a></p>'
                self.send_data(200,page('Заявка создана',body));return
            oid=f.get('id',[''])[0].strip()
            if not re.fullmatch(r'KK-[A-F0-9]{6}',oid):
                self.send_data(400,b'Invalid order ID','text/plain');return
            r=find_order(oid)
            if not r:
                self.send_data(404,b'Unknown order','text/plain');return
            if p=='/admin/approve':
                if r['status']!='pending':
                    self.send_data(409,page('Уже обработан','<p>Этот заказ уже обработан.</p>'));return
                token=secrets.token_urlsafe(36)
                update_order(r['id'],{'status':'paid','access_token':hashlib.sha256(token.encode('utf-8')).hexdigest()})
                link=permanent_link(oid)
                body='<h1>Ссылка покупателя</h1><p>Её можно снова скопировать из админки без повторной оплаты.</p><textarea readonly>'+html.escape(link)+'</textarea><p><a href="/admin">К заказам</a></p>'
                self.send_data(200,page('Доступ создан',body));return
            update_order(r['id'],{'status':'revoked','access_token':None})
            self.send_data(200,page('Доступ отозван','<h1>Доступ отозван</h1><a href="/admin">К заказам</a>'))
        except Exception:
            logging.exception('POST failed: %s',p)
            self.error_page()


if __name__=='__main__':
    if not ADMIN_PASSWORD or len(ADMIN_PASSWORD)<14: raise SystemExit('Set ADMIN_PASSWORD with at least 14 characters')
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY: raise SystemExit('Set SUPABASE_URL and SUPABASE_SECRET_KEY')
    if not PUBLIC_BASE_URL.startswith('https://'): raise SystemExit('PUBLIC_BASE_URL must use HTTPS')
    logging.basicConfig(level=logging.INFO)
    print('Starting Kod Krasoty on %s:%d (demo=%s)'%(HOST,PORT,DEMO_MODE),flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
