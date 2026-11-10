"""Minimal manual-payment protected reader prototype. Python 3.10+, PyMuPDF, Pillow.
Run: ADMIN_PASSWORD='strong random password' python server.py
Use HTTPS reverse proxy in production; this is a prototype, not a security audit.
"""
import os, sqlite3, secrets, hashlib, hmac, json, html, io, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from pathlib import Path
from datetime import datetime, timezone
import fitz
from PIL import Image, ImageDraw, ImageFont
ROOT=Path(__file__).resolve().parent
PDF=ROOT/'private'/'guide.pdf'
DB=ROOT/'private'/'orders.sqlite3'
ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD','')
HOST=os.environ.get('HOST','127.0.0.1'); PORT=int(os.environ.get('PORT','8080'))
PAY='https://pay.kaspi.kz/pay/w6fc4b8h'
DEMO_MODE=os.environ.get('DEMO_MODE','1')=='1'
PUBLIC_BASE_URL=os.environ.get('PUBLIC_BASE_URL','').rstrip('/')
DB.parent.mkdir(exist_ok=True)
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
 c.execute('CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL, created INTEGER NOT NULL, paid INTEGER NOT NULL DEFAULT 0, tokenhash TEXT, approved INTEGER, revoked INTEGER NOT NULL DEFAULT 0)');c.commit();return c
def page(title,content):
 return ('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+html.escape(title)+'</title><style>body{margin:0;background:#f7f3e9;color:#173d34;font:16px Arial,sans-serif}main{max-width:760px;margin:45px auto;padding:28px;background:#fff;border:1px solid #dce4d9;border-radius:14px}h1{font:36px Georgia}input,button{font:inherit;padding:12px;margin:8px 0;width:100%;box-sizing:border-box}button,.btn{background:#173d34;color:white;border:0;border-radius:6px;cursor:pointer;padding:12px;display:inline-block;text-decoration:none}a{color:#173d34}.muted{color:#65766d}img{max-width:100%}</style><main>'+content+'</main></html>').encode()
class Handler(BaseHTTPRequestHandler):
 def send(self,status,body,typ='text/html; charset=utf-8',extra=None):
  self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
  for k,v in (extra or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(body)
 def form(self):
  n=int(self.headers.get('Content-Length','0'))
  if n>8192:raise ValueError('Request too large')
  return urllib.parse.parse_qs(self.rfile.read(n).decode(),keep_blank_values=True)
 def admin(self):
  if not ADMIN_PASSWORD:return False
  a=self.headers.get('Authorization','')
  # HTTP Basic supported for local demo only; HTTPS required when deployed
  import base64
  try:
   user,pwd=base64.b64decode(a.split(' ',1)[1]).decode().split(':',1)
   return hmac.compare_digest(user,'admin') and hmac.compare_digest(pwd,ADMIN_PASSWORD)
  except Exception:return False
 def require_admin(self):
  if self.admin():return True
  self.send(401,page('Вход','<h1>Вход администратора</h1><p>Требуется пароль администратора.</p>'),extra={'WWW-Authenticate':'Basic realm="Kod Krasoty Admin"'});return False
 def do_GET(self):
  u=urllib.parse.urlsplit(self.path);p=u.path;q=urllib.parse.parse_qs(u.query)
  if p=='/':
   src=(ROOT/'index.html').read_bytes();self.send(200,src);return
  if p=='/cover.png':self.send(200,(ROOT/'cover.png').read_bytes(),'image/png');return
  if p=='/order':
   if DEMO_MODE:
    self.send(200,page('Демонстрация','<h1>Сайт готовится к запуску</h1><p>Оплата временно отключена. Пока проверяем безопасность и юридические реквизиты продавца.</p><p><a href="/">Вернуться</a></p>'));return
   self.send(200,page('Заявка','<h1>КОД КРАСОТЫ</h1><p>Цена: <b>4 990 ₸</b>. Заполни заявку перед оплатой. Доступ выдаётся после ручной проверки поступления платежа.</p><form method="post" action="/order"><label>Имя<input name="name" maxlength="80" required></label><label>Email или телефон для связи<input name="contact" maxlength="120" required></label><button>Создать заявку</button></form><p class="muted">Не указывай пароли и банковские реквизиты. Перед оплатой уточни реквизиты продавца и условия покупки.</p>'));return
  if p=='/admin':
   if not self.require_admin():return
   c=conn();rows=c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 200').fetchall();c.close()
   body='<h1>Заявки</h1><p>Подтверждай оплату только по фактическому поступлению в Kaspi Pay. Не по скриншоту покупателя.</p>'
   for r in rows:
    body+=f'<div style="border-top:1px solid #ddd;padding:14px 0"><b>{html.escape(r["name"])}</b> — {html.escape(r["contact"])}<br>Заказ {r["id"]} · '+('Оплачен' if r['paid'] else 'Ожидает оплаты')+(' · Отозван' if r['revoked'] else '')
    if not r['paid']:body+=f'<form method="post" action="/admin/approve"><input type="hidden" name="id" value="{r["id"]}"><button>Подтвердить оплату и создать ссылку</button></form>'
    if r['paid'] and not r['revoked']:body+=f'<form method="post" action="/admin/revoke"><input type="hidden" name="id" value="{r["id"]}"><button>Отозвать доступ</button></form>'
    body+='</div>'
   self.send(200,page('Администрирование',body));return
  if p=='/read':
   token=q.get('token',[''])[0];r=self.lookup(token)
   if not r:self.send(403,page('Доступ закрыт','<h1>Нет доступа</h1><p>Ссылка неверна или доступ отозван.</p>'));return
   if not PDF.exists():self.send(503,page('Материал готовится','<h1>Материал ещё не опубликован</h1>'));return
   with fitz.open(PDF) as d:count=len(d)
   body=f'<h1>КОД КРАСОТЫ</h1><p>Персональный доступ: {html.escape(r["name"])} · заказ {r["id"]}</p><p class="muted">Для личного чтения. Распространение запрещено. Скриншоты технически невозможно исключить полностью.</p>'
   for i in range(count):body+=f'<p><img loading="lazy" draggable="false" alt="Страница {i+1}" src="/page/{i+1}?token={urllib.parse.quote(token)}"></p>'
   self.send(200,page('Моё руководство',body));return
  if p.startswith('/page/'):
   token=q.get('token',[''])[0];r=self.lookup(token)
   if not r or not PDF.exists():self.send(403,b'Forbidden','text/plain');return
   try:i=int(p.rsplit('/',1)[1])-1
   except: self.send(404,b'Not found','text/plain');return
   with fitz.open(PDF) as doc:
    if not 0<=i<len(doc):self.send(404,b'Not found','text/plain');return
    pix=doc[i].get_pixmap(matrix=fitz.Matrix(1.35,1.35),alpha=False)
    im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
   draw=ImageDraw.Draw(im,'RGBA');watermark=f'ЛИЧНЫЙ ДОСТУП • {r["id"]} • {r["name"][:28]}'
   fontpath='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
   font=ImageFont.truetype(fontpath,22) if os.path.exists(fontpath) else ImageFont.load_default()
   for y in range(120,im.height,320):
    draw.text((22,y),watermark,font=font,fill=(20,65,50,75))
   out=io.BytesIO();im.save(out,format='WEBP',quality=78);self.send(200,out.getvalue(),'image/webp');return
  self.send(404,page('Не найдено','<h1>Страница не найдена</h1>'))
 def lookup(self,token):
  if len(token)<40:return None
  c=conn();r=c.execute('SELECT * FROM orders WHERE tokenhash=? AND paid=1 AND revoked=0',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone();c.close();return r
 def do_POST(self):
  p=urllib.parse.urlsplit(self.path).path
  try:f=self.form()
  except:self.send(400,b'Invalid request','text/plain');return
  if p=='/order':
   if DEMO_MODE:self.send(403,page('Недоступно','<p>В демонстрационном режиме заказы отключены.</p>'));return
   name=f.get('name',[''])[0].strip();contact=f.get('contact',[''])[0].strip()
   if not (2<=len(name)<=80 and 5<=len(contact)<=120):self.send(400,page('Ошибка','<p>Проверь имя и контакт.</p>'));return
   oid='КК-'+secrets.token_hex(3).upper();c=conn();c.execute('INSERT INTO orders(id,name,contact,created) VALUES(?,?,?,?)',(oid,name,contact,int(time.time())));c.commit();c.close()
   body=f'<h1>Заявка №{oid}</h1><p>Сумма: <b>4 990 ₸</b>.</p><p>Перед оплатой проверь имя продавца и сумму на странице Kaspi. После оплаты сообщи продавцу номер заявки <b>{oid}</b>. Доступ выдаётся после проверки платежа.</p><p><a class="btn" href="{PAY}" rel="noreferrer">Перейти к Kaspi Pay</a></p><p class="muted">Оплата сама по себе не открывает доступ автоматически. Не оплачивай, если продавец ещё не подтвердил готовность к выдаче.</p>'
   self.send(200,page('Заявка создана',body));return
  if p in ('/admin/approve','/admin/revoke'):
   if not self.require_admin():return
   oid=f.get('id',[''])[0];c=conn();r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
   if not r:c.close();self.send(404,b'Unknown order','text/plain');return
   if p.endswith('approve'):
    token=secrets.token_urlsafe(36);c.execute('UPDATE orders SET paid=1, tokenhash=?, approved=?, revoked=0 WHERE id=?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()),oid));c.commit();c.close()
    # Show raw token once. Admin copies URL to buyer. Token cannot be recovered from DB.
    link=f'{PUBLIC_BASE_URL or "http://localhost:8080"}/read?token={token}'
    self.send(200,page('Доступ создан','<h1>Ссылка покупательницы</h1><p>Скопируй и передай только покупательнице. Ссылка показывается один раз:</p><textarea style="width:100%;height:140px">'+html.escape(link)+'</textarea><p><a href="/admin">К заказам</a></p>'));return
   c.execute('UPDATE orders SET revoked=1 WHERE id=?',(oid,));c.commit();c.close();self.send(200,page('Отозвано','<h1>Доступ отозван</h1><a href="/admin">К заказам</a>'));return
  self.send(404,b'Not found','text/plain')
if __name__=='__main__':
 if not ADMIN_PASSWORD or len(ADMIN_PASSWORD)<14:raise SystemExit('Set ADMIN_PASSWORD to a strong password of at least 14 characters')
 print(f'Render-ready preview on http://{HOST}:{PORT} | DEMO_MODE={DEMO_MODE}')
 ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
