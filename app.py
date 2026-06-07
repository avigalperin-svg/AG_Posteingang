#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sela Holding – Posteingang Server
Cloud-Version für Render.com
Zugriff von überall: https://sela-posteingang.onrender.com
"""
import io, base64, json, datetime, smtplib, os, hashlib, secrets
import urllib.request, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4 as RL_A4
    from pypdf import PdfReader, PdfWriter
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False

PORT = int(os.environ.get('PORT', 10000))

# ─── PASSWORTSCHUTZ ──────────────────────────────────────────────────────────
# Zugangscode für die App (wird in Render als Umgebungsvariable gesetzt)
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'sela2024')

# ─── KONFIGURATION AUS UMGEBUNGSVARIABLEN ────────────────────────────────────
def get_env_config():
    """Liest Konfiguration aus Render-Umgebungsvariablen"""
    return {
        'tg_token':    os.environ.get('TG_TOKEN', ''),
        'tg_chat_id':  os.environ.get('TG_CHAT_ID', ''),
        'smtp_user':   os.environ.get('SMTP_USER', ''),
        'smtp_pass':   os.environ.get('SMTP_PASS', ''),
        'sender_name': os.environ.get('SENDER_NAME', 'Sela Holding Berlin'),
        'api_key':     os.environ.get('API_KEY', ''),
    }

# Lokale config-Datei als Fallback (für lokale Nutzung)
CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sela_config.json')

def load_config():
    env = get_env_config()
    # Umgebungsvariablen haben Vorrang, lokale Datei als Fallback
    if any(env.values()):
        try:
            local = json.load(open(CFG_FILE)) if os.path.exists(CFG_FILE) else {}
            for k, v in env.items():
                if v: local[k] = v
            return local
        except:
            return env
    try:
        with open(CFG_FILE) as f: return json.load(f)
    except:
        return {}

def save_config(data):
    try:
        with open(CFG_FILE, 'w') as f: json.dump(data, f, indent=2)
    except:
        pass  # Auf Render ist Dateisystem read-only – ignorieren

# ─── SESSION-TOKENS ──────────────────────────────────────────────────────────
valid_sessions = set()

def create_session():
    token = secrets.token_hex(32)
    valid_sessions.add(token)
    return token

def check_session(token):
    return token in valid_sessions

# ─── PDF STEMPEL ─────────────────────────────────────────────────────────────
def make_stamp(nr, datum, uhrzeit):
    buf = io.BytesIO()
    w, h = 198, 72
    c = rl_canvas.Canvas(buf, pagesize=(w, h))
    c.setStrokeColorRGB(0.80, 0, 0); c.setLineWidth(1.5)
    c.setFillColorRGB(1.0, 0.97, 0.97)
    c.roundRect(0.75, 0.75, w-1.5, h-1.5, 4, fill=1, stroke=1)
    c.setFillColorRGB(0.80, 0, 0)
    c.roundRect(0.75, h-22, w-1.5, 22, 4, fill=1, stroke=0)
    c.rect(0.75, h-22, w-1.5, 11, fill=1, stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold', 9)
    c.drawCentredString(w/2, h-14, 'EINGEGANGEN')
    fields = [('Datum:', datum), ('Uhrzeit:', uhrzeit+' Uhr'), ('Eingangs-Nr.:', nr)]
    y = h - 32
    for label, val in fields:
        c.setFillColorRGB(0.3,0.3,0.3); c.setFont('Helvetica-Bold', 7.5)
        c.drawString(6, y, label)
        c.setFillColorRGB(0.05,0.05,0.05); c.setFont('Helvetica', 7.5)
        c.drawString(70, y, val)
        y -= 14
    c.save(); buf.seek(0)
    return buf.read()

def do_stamp(pdf_b64, mime, nr, datum, uhrzeit):
    raw = base64.b64decode(pdf_b64)
    if not mime or 'pdf' not in mime:
        from PIL import Image as PILImage
        from reportlab.lib.utils import ImageReader
        img = PILImage.open(io.BytesIO(raw))
        buf = io.BytesIO()
        w_pt, h_pt = RL_A4
        iw, ih = img.size
        scale = min(w_pt/iw, h_pt/ih, 1)
        nw, nh = int(iw*scale), int(ih*scale)
        resized = img.resize((nw, nh), PILImage.LANCZOS)
        tmp = io.BytesIO(); resized.save(tmp, format='PNG'); tmp.seek(0)
        c = rl_canvas.Canvas(buf, pagesize=(w_pt, h_pt))
        c.drawImage(ImageReader(tmp), (w_pt-nw)/2, (h_pt-nh)/2, nw, nh)
        c.save(); buf.seek(0); raw = buf.read()

    stamp_bytes = make_stamp(nr, datum, uhrzeit)
    stamp_page = PdfReader(io.BytesIO(stamp_bytes)).pages[0]
    src = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    for i, page in enumerate(src.pages):
        if i == 0:
            pw = float(page.mediabox.width)
            ph = float(page.mediabox.height)
            page.merge_transformed_page(stamp_page, (1,0,0,1, pw-212, ph-86))
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return base64.b64encode(out.getvalue()).decode()

# ─── EMAIL ───────────────────────────────────────────────────────────────────
def send_email(cfg, to, cc, subject, body_text, pdf_b64=None, pdf_name=None):
    msg = MIMEMultipart()
    msg['From']    = f"{cfg.get('sender_name','Sela Holding')} <{cfg['smtp_user']}>"
    msg['To']      = to
    if cc: msg['Cc'] = cc
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    if pdf_b64 and pdf_name:
        part = MIMEBase('application','pdf')
        part.set_payload(base64.b64decode(pdf_b64))
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{pdf_name}"')
        msg.attach(part)
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(cfg['smtp_user'], cfg['smtp_pass'])
        rcpts = [to] + ([cc] if cc else [])
        s.sendmail(cfg['smtp_user'], rcpts, msg.as_bytes())
    return True

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def tg_send(token, chat_id, text):
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({'chat_id':chat_id,'text':text,'parse_mode':'HTML'}).encode()
    req  = urllib.request.Request(url, data=data)
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read()).get('ok', False)

# ─── LOGIN HTML ──────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Posteingang – Sela Holding</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#F0EFE9;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#fff;border-radius:12px;padding:40px 36px;width:340px;
  box-shadow:0 4px 24px rgba(0,0,0,0.10);text-align:center}
.logo{width:48px;height:48px;background:#185FA5;border-radius:12px;
  display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:24px}
h1{font-size:20px;font-weight:600;margin-bottom:6px}
p{font-size:13px;color:#6B6A66;margin-bottom:24px}
input{width:100%;padding:10px 13px;border:0.5px solid rgba(0,0,0,0.16);
  border-radius:8px;font-size:14px;font-family:inherit;outline:none;margin-bottom:12px;
  background:#F4F3EE;text-align:center;letter-spacing:2px}
input:focus{border-color:#185FA5}
button{width:100%;padding:11px;background:#185FA5;color:#fff;border:none;
  border-radius:8px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
button:hover{opacity:0.88}
.err{color:#A32D2D;font-size:13px;margin-top:10px;display:none}
</style>
</head>
<body>
<div class="box">
  <div class="logo">📬</div>
  <h1>Posteingang</h1>
  <p>Sela Holding Berlin</p>
  <input type="password" id="pw" placeholder="Zugangscode" onkeydown="if(event.key==='Enter')login()"/>
  <button onclick="login()">Anmelden</button>
  <div class="err" id="err">Falscher Zugangscode</div>
</div>
<script>
async function login(){
  const pw = document.getElementById('pw').value;
  const r = await fetch('/auth/login', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: pw})});
  const d = await r.json();
  if(d.ok){
    localStorage.setItem('sela_session', d.token);
    window.location.href = '/app';
  } else {
    const e = document.getElementById('err');
    e.style.display='block';
    document.getElementById('pw').value='';
    setTimeout(()=>e.style.display='none', 3000);
  }
}
// Auto-redirect if already logged in
const t = localStorage.getItem('sela_session');
if(t) fetch('/auth/check', {headers:{'X-Session':t}})
  .then(r=>r.json()).then(d=>{ if(d.ok) window.location.href='/app'; });
</script>
</body>
</html>"""

# ─── HAUPT-APP HTML ──────────────────────────────────────────────────────────
APP_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Posteingang – Sela Holding</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#F0EFE9;--sf:#fff;--sf2:#F4F3EE;--bd:rgba(0,0,0,.09);--bd2:rgba(0,0,0,.16);--tx:#1C1C1A;--tx2:#6B6A66;--tx3:#A0A09C;--ac:#185FA5;--acl:#E6F1FB;--acd:#0C447C;--gr:#3B6D11;--grl:#EAF3DE;--re:#A32D2D;--rel:#FCEBEB;--am:#854F0B;--aml:#FAEEDA;--tg:#1565C0;--tgl:#E3F2FD;--r:10px;--rs:7px}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;font-size:14px;background:var(--bg);color:var(--tx);min-height:100vh}
.hdr{background:var(--sf);border-bottom:.5px solid var(--bd);padding:0 20px;height:52px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:99;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.logo{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600}
.lbox{width:30px;height:30px;background:var(--ac);border-radius:7px;display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px}
.logout{font-size:12px;color:var(--tx2);cursor:pointer;background:none;border:none;display:flex;align-items:center;gap:5px;font-family:inherit;padding:5px 10px;border-radius:var(--rs)}
.logout:hover{background:var(--sf2)}
.wrap{max-width:680px;margin:0 auto;padding:18px 14px 60px}
.tabs{display:flex;border-bottom:.5px solid var(--bd);margin-bottom:14px}
.tab{padding:8px 15px;font-size:13px;cursor:pointer;border:none;border-bottom:2px solid transparent;color:var(--tx2);font-weight:500;background:none;font-family:inherit;transition:color .15s}
.tab.on{color:var(--ac);border-bottom-color:var(--ac)}
.card{background:var(--sf);border:.5px solid var(--bd);border-radius:var(--r);padding:18px;margin-bottom:12px}
.ch{display:flex;align-items:flex-start;gap:11px;margin-bottom:14px}
.sn{width:27px;height:27px;border-radius:50%;background:var(--acl);color:var(--acd);font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px}
.ct{font-size:15px;font-weight:600}.cs{font-size:12px;color:var(--tx2);margin-top:2px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.full{grid-column:1/-1}
.fld{display:flex;flex-direction:column;gap:4px}
.fld label{font-size:11px;font-weight:600;color:var(--tx2);letter-spacing:.04em;text-transform:uppercase}
.fld input,.fld select,.fld textarea{width:100%;background:var(--sf2);border:.5px solid var(--bd);border-radius:var(--rs);padding:8px 11px;font-size:13px;color:var(--tx);font-family:inherit;outline:none;transition:border-color .15s}
.fld input:focus,.fld select:focus,.fld textarea:focus{border-color:var(--ac)}
.fld textarea{min-height:130px;resize:vertical;line-height:1.6}
.req{color:var(--re);margin-left:2px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 17px;border-radius:var(--rs);font-size:13px;font-weight:500;cursor:pointer;border:none;font-family:inherit;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.98)}
.bp{background:var(--ac);color:#fff}.bp:hover{opacity:.88}.bp:disabled{opacity:.4;cursor:not-allowed;transform:none}
.bs{background:var(--sf2);color:var(--tx);border:.5px solid var(--bd2)}.bs:hover{opacity:.82}
.brow{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
.bdg{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px}
.bok{background:var(--grl);color:var(--gr)}.berr{background:var(--rel);color:var(--re)}.binfo{background:var(--acl);color:var(--acd)}.bwarn{background:var(--aml);color:var(--am)}
.upz{border:1.5px dashed var(--bd2);border-radius:var(--r);padding:28px 16px;text-align:center;cursor:pointer;position:relative;transition:background .15s,border-color .15s}
.upz:hover,.upz.drag{background:var(--sf2);border-color:var(--ac)}
.upz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upi{font-size:30px;color:var(--tx3);margin-bottom:6px}.upt{font-size:14px;color:var(--tx2);font-weight:500}.ups{font-size:12px;color:var(--tx3);margin-top:3px}
.fi{display:flex;align-items:center;gap:9px;padding:8px 11px;background:var(--sf2);border-radius:var(--rs);margin-top:7px;font-size:13px}
.fi i{color:var(--ac);font-size:18px;flex-shrink:0}.fn{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.fsz{color:var(--tx2);font-size:12px;flex-shrink:0}
.frm{background:none;border:none;cursor:pointer;color:var(--tx3);font-size:16px;line-height:1;padding:2px}.frm:hover{color:var(--re)}
.prw{height:4px;background:var(--bd);border-radius:4px;margin:10px 0;overflow:hidden;display:none}.prb{height:100%;background:var(--ac);border-radius:4px;width:0%;transition:width .4s}
.sprow{display:flex;gap:7px;flex-wrap:wrap;margin:8px 0}
.sp{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:var(--rs);font-size:12px;font-weight:500;border:.5px solid var(--bd);background:var(--sf2);color:var(--tx2);transition:color .2s,border-color .2s}
.sp.ok{color:var(--gr);border-color:var(--gr)}.sp.fail{color:var(--re);border-color:var(--re)}.sp.run{color:var(--ac);border-color:var(--ac)}
.abox{background:var(--sf2);border:.5px solid var(--bd);border-radius:var(--rs);padding:12px;font-size:12px;line-height:1.9;white-space:pre-wrap;max-height:200px;overflow-y:auto;margin-top:10px;display:none;font-family:monospace}
.tgprev{background:var(--tgl);border-left:3px solid var(--tg);border-radius:0 var(--rs) var(--rs) 0;padding:10px 14px;font-size:12px;line-height:1.9;margin-top:10px;display:none;color:var(--tg);font-family:monospace}
.pdfframe{width:100%;height:320px;border:.5px solid var(--bd);border-radius:var(--rs);margin-top:10px;display:none}
.stmp{display:inline-block;border:1.5px solid #CC0000;border-radius:4px;overflow:hidden;margin-top:10px;min-width:190px}
.sh{background:#CC0000;color:#fff;font-size:10px;font-weight:700;padding:3px 10px;text-align:center;letter-spacing:.08em}
.sb{background:#FFF8F8;padding:7px 10px;font-size:11px;line-height:2;color:#222}.sb b{min-width:62px;display:inline-block;color:#444}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:12px}
.dc{border:.5px solid var(--bd);border-radius:var(--r);padding:14px 10px;cursor:pointer;text-align:center;transition:border-color .15s,background .15s,transform .1s}
.dc:hover{background:var(--sf2);border-color:var(--ac)}.dc:active{transform:scale(.97)}.dc.sel{border:1.5px solid var(--ac);background:var(--acl)}
.dci{font-size:22px;color:var(--ac);margin-bottom:5px}.dcl{font-size:13px;font-weight:600}.dcs{font-size:11px;color:var(--tx2);margin-top:2px}
.subp{margin-top:13px;display:none;padding-top:13px;border-top:.5px solid var(--bd)}
.li{display:flex;align-items:flex-start;gap:9px;padding:7px 0;border-bottom:.5px solid var(--bd);font-size:12px}.li:last-child{border-bottom:none}
.lt{color:var(--tx3);font-size:10px;flex-shrink:0;min-width:42px;margin-top:1px;font-family:monospace}.lic{font-size:14px;flex-shrink:0;margin-top:1px}
.done{display:flex;align-items:center;gap:9px;padding:13px;background:var(--grl);border-radius:var(--rs);color:var(--gr);font-size:13px;font-weight:500;margin-top:13px}
.hbox{background:var(--sf2);border-radius:var(--rs);padding:12px;font-size:12px;line-height:2;margin-top:8px;display:none;border:.5px solid var(--bd)}
.hbox code{background:var(--sf);padding:1px 5px;border-radius:4px;font-family:monospace;font-size:11px;border:.5px solid var(--bd);word-break:break-all}
.tl{font-size:12px;color:var(--ac);cursor:pointer;background:none;border:none;padding:0;display:inline-flex;align-items:center;gap:4px;margin-top:7px;font-family:inherit}
@media(max-width:520px){.g2{grid-template-columns:1fr}.dgrid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header class="hdr">
  <div class="logo">
    <div class="lbox"><i class="ti ti-mailbox"></i></div>
    Posteingang · Sela Holding
  </div>
  <button class="logout" onclick="logout()"><i class="ti ti-logout"></i> Abmelden</button>
</header>
<div class="wrap">
<div class="tabs">
  <button class="tab on" onclick="showTab('wf',this)">Workflow</button>
  <button class="tab" onclick="showTab('cfg',this)">Konfiguration</button>
  <button class="tab" onclick="showTab('log',this)">Protokoll</button>
</div>

<div id="tab-wf">
<div class="card">
  <div class="ch"><div class="sn">1</div>
    <div><div class="ct">Posteingangsscan hochladen</div><div class="cs">PDF oder Bild des gescannten Briefes</div></div>
  </div>
  <div class="upz" id="dz">
    <input type="file" id="fi" accept=".pdf,image/jpeg,image/png,image/webp" multiple onchange="addFiles(this.files)"/>
    <div class="upi"><i class="ti ti-cloud-upload"></i></div>
    <div class="upt">Datei hierher ziehen oder klicken</div>
    <div class="ups">PDF · JPG · PNG · max. 15 MB</div>
  </div>
  <div id="flist"></div>
</div>

<div class="card" id="c2" style="display:none">
  <div class="ch"><div class="sn">2</div>
    <div><div class="ct">Eingangsstempel & KI-Analyse</div><div class="cs">Stempel auf PDF · Claude analysiert · Telegram-Benachrichtigung</div></div>
  </div>
  <div class="sprow">
    <div class="sp" id="sp1"><i class="ti ti-stamp" style="font-size:13px"></i> Stempel</div>
    <div class="sp" id="sp2"><i class="ti ti-cpu" style="font-size:13px"></i> KI-Analyse</div>
    <div class="sp" id="sp3"><i class="ti ti-brand-telegram" style="font-size:13px"></i> Telegram</div>
    <div class="sp" id="sp4"><i class="ti ti-mail" style="font-size:13px"></i> E-Mail</div>
  </div>
  <div class="prw" id="prw"><div class="prb" id="prb"></div></div>
  <div id="stmp-prev"></div>
  <iframe id="pdfframe" class="pdfframe"></iframe>
  <div class="abox" id="abox"></div>
  <div class="tgprev" id="tgp"></div>
  <div class="brow">
    <button class="btn bp" id="btn2" onclick="runAll()">
      <i class="ti ti-bolt"></i> Stempel · Analysieren · Telegram senden
    </button>
  </div>
</div>

<div class="card" id="c3" style="display:none">
  <div class="ch"><div class="sn">3</div>
    <div><div class="ct">Deine Entscheidung</div><div class="cs">Was soll mit diesem Schreiben geschehen?</div></div>
  </div>
  <div class="dgrid">
    <div class="dc" id="d-wv" onclick="selD('wv')">
      <div class="dci"><i class="ti ti-calendar-event"></i></div><div class="dcl">Wiedervorlage</div><div class="dcs">Datum & Priorität</div>
    </div>
    <div class="dc" id="d-ma" onclick="selD('ma')">
      <div class="dci"><i class="ti ti-mail-forward"></i></div><div class="dcl">Antwort per E-Mail</div><div class="dcs">Claude formuliert · direkt senden</div>
    </div>
    <div class="dc" id="d-ab" onclick="selD('ab')">
      <div class="dci"><i class="ti ti-folder-check"></i></div><div class="dcl">Nur ablegen</div><div class="dcs">Kein weiterer Schritt</div>
    </div>
    <div class="dc" id="d-fw" onclick="selD('fw')">
      <div class="dci"><i class="ti ti-send"></i></div><div class="dcl">Weiterleiten</div><div class="dcs">E-Mail an Kollegen</div>
    </div>
  </div>

  <div class="subp" id="p-wv">
    <div class="g2">
      <div class="fld"><label>Datum</label><input type="date" id="wv-d"/></div>
      <div class="fld"><label>Priorität</label>
        <select id="wv-p"><option>Normal</option><option>Dringend</option><option>Niedrig</option></select>
      </div>
      <div class="fld full"><label>Notiz</label><input type="text" id="wv-n" placeholder="Interne Notiz..."/></div>
    </div>
  </div>

  <div class="subp" id="p-ma">
    <div class="g2">
      <div class="fld full"><label>Empfänger-E-Mail<span class="req">*</span></label><input type="email" id="m-to" placeholder="empfaenger@beispiel.de"/></div>
      <div class="fld full"><label>CC (optional)</label><input type="email" id="m-cc" placeholder="kopie@beispiel.de"/></div>
      <div class="fld full"><label>Betreff<span class="req">*</span></label><input type="text" id="m-su" placeholder="Re: ..."/></div>
      <div class="fld full">
        <label>E-Mail-Text<span class="req">*</span> <span style="font-weight:400;text-transform:none;font-size:11px">— von Claude vorformuliert, bitte prüfen</span></label>
        <textarea id="m-bo" placeholder="Klicke auf 'Entwurf generieren'..."></textarea>
      </div>
    </div>
    <div class="brow">
      <button class="btn bs" onclick="genDraft()"><i class="ti ti-wand"></i> Entwurf generieren</button>
      <span id="dst"></span>
    </div>
  </div>

  <div class="subp" id="p-fw">
    <div class="g2">
      <div class="fld full"><label>Weiterleiten an<span class="req">*</span></label><input type="email" id="fw-to" placeholder="kollege@sela-holding.de"/></div>
      <div class="fld full"><label>Hinweis</label><input type="text" id="fw-n" placeholder="Bitte bearbeiten..."/></div>
    </div>
  </div>

  <div class="brow" style="margin-top:16px">
    <button class="btn bp" id="btn3" onclick="execDec()"><i class="ti ti-check"></i> Ausführen & abschließen</button>
  </div>
  <div id="donebox"></div>
</div>

<div id="cnew" style="display:none;margin-top:4px">
  <button class="btn bs" onclick="resetAll()"><i class="ti ti-plus"></i> Nächstes Schreiben</button>
</div>
</div>

<div id="tab-cfg" style="display:none">
<div class="card">
  <div class="ch"><div class="sn"><i class="ti ti-settings" style="font-size:12px"></i></div>
    <div><div class="ct">App-Einstellungen</div><div class="cs">Werden sicher auf dem Server gespeichert</div></div>
  </div>
  <div class="g2">
    <div class="fld full"><label>Claude API-Key<span class="req">*</span></label><input type="password" id="c-ak" placeholder="sk-ant-api03-..."/></div>
    <div class="fld full"><label>Telegram Bot-Token<span class="req">*</span></label><input type="password" id="c-tt" placeholder="123456789:AAF..."/></div>
    <div class="fld full"><label>Telegram Chat-ID<span class="req">*</span></label><input type="text" id="c-tc" placeholder="987654321"/></div>
    <div class="fld full"><label>Gmail-Adresse<span class="req">*</span></label><input type="email" id="c-mu" placeholder="buero@gmail.com"/></div>
    <div class="fld full"><label>Gmail App-Passwort<span class="req">*</span></label><input type="password" id="c-mp" placeholder="xxxx xxxx xxxx xxxx"/></div>
    <div class="fld full"><label>Absender-Name</label><input type="text" id="c-mn" placeholder="Sela Holding Berlin"/></div>
  </div>
  <div class="brow">
    <button class="btn bp" onclick="saveCfg()"><i class="ti ti-device-floppy"></i> Speichern</button>
    <button class="btn bs" onclick="testTG()"><i class="ti ti-brand-telegram"></i> Telegram testen</button>
    <button class="btn bs" onclick="testMail()"><i class="ti ti-mail"></i> E-Mail testen</button>
    <span id="cfgmsg"></span>
  </div>
</div>
</div>

<div id="tab-log" style="display:none">
<div class="card">
  <div class="ch"><div class="sn"><i class="ti ti-list" style="font-size:12px"></i></div>
    <div><div class="ct">Aktivitätsprotokoll</div></div>
  </div>
  <div id="logbox"></div>
  <div class="brow" style="margin-top:10px">
    <button class="btn bs" onclick="$('logbox').innerHTML='';lg('ti-trash','var(--tx2)','Protokoll geleert')"><i class="ti ti-trash"></i> Leeren</button>
  </div>
</div>
</div>
</div>

<script>
let files=[],analysis='',curD=null,nr='',stPdfB64='';
const $=id=>document.getElementById(id);
const pad=n=>String(n).padStart(2,'0');
const now=()=>{const d=new Date();return pad(d.getHours())+':'+pad(d.getMinutes())};
const tod=()=>{const d=new Date();return pad(d.getDate())+'.'+pad(d.getMonth()+1)+'.'+d.getFullYear()};
const setP=p=>{$('prw').style.display='block';$('prb').style.width=p+'%'};
const setSP=(id,c)=>{const e=$(id);e.className='sp';if(c)e.classList.add(c)};
const tog=id=>{const e=$(id);e.style.display=e.style.display==='block'?'none':'block'};
const sess=()=>localStorage.getItem('sela_session')||'';
const hdr=()=>({'Content-Type':'application/json','X-Session':sess()});

function logout(){localStorage.removeItem('sela_session');window.location.href='/';}

function lg(icon,color,text){
  const d=document.createElement('div');d.className='li';
  d.innerHTML=`<span class="lt">${now()}</span><i class="ti ${icon} lic" style="color:${color}"></i><span>${text}</span>`;
  $('logbox').appendChild(d);
}
function showTab(n,btn){
  ['wf','cfg','log'].forEach(t=>{const e=$('tab-'+t);if(e)e.style.display='none'});
  $('tab-'+n).style.display='block';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  if(btn)btn.classList.add('on');
}

async function loadCfg(){
  try{
    const r=await fetch('/config',{headers:hdr()});
    if(r.status===401){window.location.href='/';return;}
    const c=await r.json();
    if(c.smtp_user)$('c-mu').value=c.smtp_user;
    if(c.tg_chat_id)$('c-tc').value=c.tg_chat_id;
    if(c.sender_name)$('c-mn').value=c.sender_name;
    if(c.api_key_set)$('c-ak').placeholder='● gesetzt (Render ENV)';
    if(c.smtp_pass_set)$('c-mp').placeholder='● gesetzt (Render ENV)';
    if(c.tg_token_set)$('c-tt').placeholder='● gesetzt (Render ENV)';
  }catch(e){}
}
async function saveCfg(){
  const c={
    api_key:$('c-ak').value.trim(), tg_token:$('c-tt').value.trim(),
    tg_chat_id:$('c-tc').value.trim(), smtp_user:$('c-mu').value.trim(),
    smtp_pass:$('c-mp').value.trim(), sender_name:$('c-mn').value.trim()||'Sela Holding Berlin'
  };
  const r=await fetch('/config/save',{method:'POST',headers:hdr(),body:JSON.stringify(c)});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Gespeichert</span>':'<span class="bdg berr">Fehler</span>';
  setTimeout(()=>$('cfgmsg').innerHTML='',3000);
  lg('ti-device-floppy','var(--gr)','Konfiguration gespeichert');
}
async function testTG(){
  const r=await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:'✅ Telegram-Test OK – Sela Holding Posteingang bereit!'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Telegram OK!</span>':'<span class="bdg berr">'+d.error+'</span>';
  setTimeout(()=>$('cfgmsg').innerHTML='',4000);
}
async function testMail(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Sende...</span>';
  const c=await(await fetch('/config',{headers:hdr()})).json();
  const r=await fetch('/email/send',{method:'POST',headers:hdr(),
    body:JSON.stringify({to:c.smtp_user||$('c-mu').value,subject:'Test – Sela Holding Posteingang',body:'Verbindungstest erfolgreich.'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Test-Mail gesendet!</span>':'<span class="bdg berr">'+d.error+'</span>';
  setTimeout(()=>$('cfgmsg').innerHTML='',5000);
}

function addFiles(fl){
  Array.from(fl).forEach(f=>{if(!files.find(x=>x.name===f.name))files.push(f)});
  renderFiles();if(files.length)$('c2').style.display='block';
}
function renderFiles(){
  $('flist').innerHTML=files.map((f,i)=>`
    <div class="fi"><i class="ti ${f.type.includes('pdf')?'ti-file-type-pdf':'ti-photo'}"></i>
    <span class="fn">${f.name}</span><span class="fsz">${(f.size/1024).toFixed(0)} KB</span>
    <button class="frm" onclick="rmF(${i})"><i class="ti ti-x"></i></button></div>`).join('');
}
function rmF(i){files.splice(i,1);renderFiles();if(!files.length)$('c2').style.display='none'}
async function toB64(f){
  return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f)});
}

async function runAll(){
  if(!files.length){alert('Bitte Datei hochladen.');return}
  const btn=$('btn2');btn.disabled=true;btn.innerHTML='<i class="ti ti-loader"></i> Läuft...';
  ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,'run'));
  const n=new Date();
  nr='PE-'+n.getTime().toString().slice(-6);
  const datum=tod(),uhrzeit=now();
  lg('ti-bolt','var(--ac)',`Gestartet: ${files[0].name} [${nr}]`);
  setP(5);
  try{
    const f=files[0];
    const rawB64=await toB64(f);

    // 1. STEMPEL via Server
    lg('ti-stamp','var(--ac)','Stempel wird gesetzt...');
    const sr=await fetch('/stamp',{method:'POST',headers:hdr(),
      body:JSON.stringify({pdf_b64:rawB64,mime:f.type,nr})});
    if(sr.status===401){window.location.href='/';return}
    const sd=await sr.json();
    if(!sd.ok)throw new Error('Stempel: '+sd.error);
    stPdfB64=sd.pdf_b64;
    setSP('sp1','ok');
    lg('ti-stamp','var(--gr)',`Stempel gesetzt: ${sd.datum} ${sd.uhrzeit} ✓`);
    setP(22);

    $('stmp-prev').innerHTML=`
      <div style="display:flex;align-items:center;gap:14px;margin-top:10px;flex-wrap:wrap">
        <div class="stmp">
          <div class="sh">EINGEGANGEN</div>
          <div class="sb"><b>Datum:</b>${sd.datum}<br><b>Uhrzeit:</b>${sd.uhrzeit} Uhr<br><b>Nr.:</b>${nr}</div>
        </div>
        <a href="data:application/pdf;base64,${stPdfB64}" download="Posteingang_${nr}.pdf"
           style="display:inline-flex;align-items:center;gap:6px;color:var(--ac);font-size:13px;font-weight:500;padding:8px 14px;background:var(--acl);border-radius:var(--rs);border:.5px solid var(--ac);text-decoration:none">
          <i class="ti ti-download"></i> PDF herunterladen
        </a>
      </div>`;
    $('pdfframe').src='data:application/pdf;base64,'+stPdfB64;
    $('pdfframe').style.display='block';

    // 2. KI-ANALYSE
    lg('ti-cpu','var(--ac)','KI-Analyse...');setSP('sp2','run');
    const cfg=await(await fetch('/config',{headers:hdr()})).json();
    if(!cfg.api_key&&!cfg.api_key_set)throw new Error('API-Key fehlt – bitte in Konfiguration eintragen!');
    const mc=[];
    if(f.type.startsWith('image/'))mc.push({type:'image',source:{type:'base64',media_type:f.type,data:rawB64}});
    else mc.push({type:'document',source:{type:'base64',media_type:'application/pdf',data:rawB64}});
    mc.push({type:'text',text:`Analysiere dieses Eingangsschreiben der Sela Holding Berlin. Antworte NUR mit diesen Feldern:\n\nABSENDER: [Name/Firma/Behörde]\nDATUM: [Briefdatum TT.MM.JJJJ]\nBETREFF: [1-2 Sätze worum es geht]\nDRINGLICHKEIT: [Hoch / Mittel / Niedrig]\nFRIST: [Datum oder "keine Frist"]\nBETRAG: [Betrag oder "–"]\nEMPFEHLUNG: [Wiedervorlage / Antwort / Ablage / Weiterleiten]\nHINWEIS: [Wichtige Details]`});

    setP(45);
    const apiKey=cfg.api_key||'';
    const ar=await fetch('https://api.anthropic.com/v1/messages',{method:'POST',
      headers:{'Content-Type':'application/json','x-api-key':apiKey,'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},
      body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:1200,messages:[{role:'user',content:mc}]})});
    if(!ar.ok){const e=await ar.json();throw new Error('Claude: '+(e.error?.message||ar.status))}
    const ad=await ar.json();
    analysis=ad.content?.[0]?.text||'';
    setSP('sp2','ok');lg('ti-file-text','var(--gr)','KI-Analyse abgeschlossen ✓');
    const abox=$('abox');abox.style.display='block';
    abox.textContent=`╔══ EINGANGSPROTOKOLL ══════════════════════╗\n  Nr.:     ${nr}\n  Eingang: ${datum}  ${uhrzeit} Uhr\n  Datei:   ${f.name}\n╚═══════════════════════════════════════════╝\n\n${analysis}`;
    setP(65);

    // 3. TELEGRAM
    lg('ti-brand-telegram','var(--ac)','Sende Telegram...');setSP('sp3','run');
    const tgTxt=`📬 <b>POSTEINGANG – Sela Holding</b>\n━━━━━━━━━━━━━━━━━━━━\n<b>Nr.:</b>      ${nr}\n<b>Eingang:</b> ${datum} · ${uhrzeit} Uhr\n<b>Datei:</b>   ${f.name}\n━━━━━━━━━━━━━━━━━━━━\n${analysis}\n━━━━━━━━━━━━━━━━━━━━\n<b>Bitte Entscheidung in der App treffen!</b>`;
    $('tgp').style.display='block';$('tgp').textContent=tgTxt.replace(/<[^>]+>/g,'');
    const tr=await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:tgTxt})});
    const td=await tr.json();
    if(td.ok){setSP('sp3','ok');lg('ti-brand-telegram','var(--tg)','Telegram gesendet ✓');}
    else{setSP('sp3','fail');lg('ti-alert-triangle','var(--am)','Telegram: '+td.error);}
    setSP('sp4','ok');setP(100);
    $('c3').style.display='block';$('c3').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){
    lg('ti-x','var(--re)','Fehler: '+e.message);
    ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,'fail'));
    alert('Fehler: '+e.message);
  }
  btn.disabled=false;btn.innerHTML='<i class="ti ti-refresh"></i> Erneut';
}

function selD(t){
  curD=t;['wv','ma','ab','fw'].forEach(x=>{$('d-'+x).classList.remove('sel');const p=$('p-'+x);if(p)p.style.display='none'});
  $('d-'+t).classList.add('sel');const pp=$('p-'+t);if(pp)pp.style.display='block';
}

async function genDraft(){
  if(!analysis){alert('Bitte erst analysieren.');return}
  const ds=$('dst');ds.innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Generiere...</span>';$('m-bo').value='';
  try{
    const cfg=await(await fetch('/config',{headers:hdr()})).json();
    const rr=await fetch('https://api.anthropic.com/v1/messages',{method:'POST',
      headers:{'Content-Type':'application/json','x-api-key':cfg.api_key||'','anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},
      body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:1200,messages:[{role:'user',content:`Verfasse professionelle deutsche Antwort der Sela Holding Berlin.\n\nBriefanalyse:\n${analysis}\n\nErste Zeile: "BETREFF: [Betreff]"\nDann vollständiger Brief ab "Sehr geehrte..." – sachlich, höflich.\nAbsender: ${cfg.sender_name||'Sela Holding Berlin'}`}]})});
    const d=await rr.json();const t=d.content?.[0]?.text||'';
    const lines=t.split('\n');
    const bl=lines.find(l=>l.startsWith('BETREFF:'));
    if(bl)$('m-su').value=bl.replace('BETREFF:','').trim();
    const bs=lines.findIndex(l=>/^(Sehr geehrte|Sehr geehrter|Guten)/.test(l.trim()));
    $('m-bo').value=bs>=0?lines.slice(bs).join('\n'):t;
    ds.innerHTML='<span class="bdg bok"><i class="ti ti-check"></i> Entwurf fertig – bitte prüfen!</span>';
    lg('ti-wand','var(--ac)','Entwurf generiert ✓');
  }catch(e){ds.innerHTML='<span class="bdg berr">'+e.message+'</span>'}
}

async function execDec(){
  if(!curD){alert('Bitte Entscheidung treffen.');return}
  const btn=$('btn3');btn.disabled=true;btn.innerHTML='<i class="ti ti-loader"></i> Läuft...';
  const datum=tod(),uhrzeit=now();
  try{
    if(curD==='wv'){
      const d=$('wv-d').value,p=$('wv-p').value,n=$('wv-n').value;
      await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:`📅 <b>Wiedervorlage</b>\nNr.: ${nr}\nDatum: ${d||'–'}\nPriorität: ${p}\nNotiz: ${n||'–'}`})});
      lg('ti-calendar-event','var(--gr)',`Wiedervorlage: ${d} · ${p} ✓`);
      showDone('Wiedervorlage eingetragen · Telegram ✓');

    }else if(curD==='ma'){
      const to=$('m-to').value.trim(),cc=$('m-cc').value.trim();
      const su=$('m-su').value.trim()||'Antwort – Sela Holding';
      const bo=$('m-bo').value.trim();
      if(!to){alert('Empfänger-E-Mail eingeben!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      if(!bo){alert('E-Mail-Text eingeben!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      const er=await fetch('/email/send',{method:'POST',headers:hdr(),
        body:JSON.stringify({to,cc,subject:su,body:bo,pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
      const ed=await er.json();
      if(!ed.ok)throw new Error(ed.error);
      await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:`✉️ <b>E-Mail gesendet</b>\nNr.: ${nr}\nAn: ${to}\nBetreff: ${su}`})});
      lg('ti-mail','var(--gr)',`E-Mail gesendet an ${to} ✓`);
      showDone(`E-Mail gesendet an ${to} · Telegram ✓`);

    }else if(curD==='ab'){
      await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:`📁 <b>Abgelegt</b>\nNr.: ${nr}\n${datum}`})});
      lg('ti-folder-check','var(--gr)','Abgelegt · Telegram ✓');
      showDone('Abgelegt · Telegram ✓');

    }else if(curD==='fw'){
      const fto=$('fw-to').value.trim();
      if(!fto){alert('Empfänger eingeben!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      const fn=$('fw-n').value.trim();
      const cfg=await(await fetch('/config',{headers:hdr()})).json();
      const fwb=`Weitergeleitet von Sela Holding Berlin\n\nHinweis: ${fn||'–'}\nEingangs-Nr.: ${nr} · ${datum}\n\n${analysis}\n\n--\n${cfg.sender_name||'Sela Holding Berlin'}`;
      const er=await fetch('/email/send',{method:'POST',headers:hdr(),
        body:JSON.stringify({to:fto,subject:`Weiterleitung: Eingangspost ${nr}`,body:fwb,pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
      const ed=await er.json();
      if(!ed.ok)throw new Error(ed.error);
      await fetch('/telegram/send',{method:'POST',headers:hdr(),body:JSON.stringify({text:`➡️ <b>Weitergeleitet</b>\nNr.: ${nr}\nAn: ${fto}`})});
      lg('ti-send','var(--gr)',`Weitergeleitet an ${fto} ✓`);
      showDone(`Weitergeleitet an ${fto} · Telegram ✓`);
    }
    $('cnew').style.display='block';
  }catch(e){lg('ti-x','var(--re)','Fehler: '+e.message);alert('Fehler: '+e.message);}
  btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen & abschließen';
}

function showDone(msg){
  $('donebox').innerHTML=`<div class="done"><i class="ti ti-circle-check"></i> ${msg} · ${tod()} ${now()}</div>`;
}
function resetAll(){
  files=[];analysis='';curD=null;nr='';stPdfB64='';
  renderFiles();
  ['c2','c3','cnew'].forEach(id=>$(id).style.display='none');
  $('abox').style.display='none';$('tgp').style.display='none';$('pdfframe').style.display='none';$('pdfframe').src='';
  $('prw').style.display='none';$('prb').style.width='0%';$('stmp-prev').innerHTML='';$('donebox').innerHTML='';
  ['wv','ma','ab','fw'].forEach(t=>{$('d-'+t).classList.remove('sel');const p=$('p-'+t);if(p)p.style.display='none'});
  ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,null));
  window.scrollTo({top:0,behavior:'smooth'});
  lg('ti-plus','var(--ac)','Neues Schreiben');
}

const dz=$('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag')});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');addFiles(e.dataTransfer.files)});

// Session prüfen
fetch('/auth/check',{headers:{'X-Session':sess()}}).then(r=>r.json()).then(d=>{
  if(!d.ok){window.location.href='/';}else{loadCfg();}
});
lg('ti-rocket','var(--ac)',`Posteingang Cloud-Version · ${tod()}`);
</script>
</body>
</html>"""

# ─── HTTP HANDLER ────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Session')

    def ok(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def session_ok(self):
        return check_session(self.headers.get('X-Session',''))

    def do_OPTIONS(self):
        self.send_response(200); self.cors(); self.end_headers()

    def do_GET(self):
        if self.path == '/':
            self.html(LOGIN_HTML)
        elif self.path in ('/app', '/app/'):
            if not self.session_ok():
                self.send_response(302)
                self.send_header('Location', '/')
                self.end_headers()
            else:
                self.html(APP_HTML)
        elif self.path == '/ping':
            self.ok({'ok': True})
        elif self.path == '/config':
            if not self.session_ok(): self.ok({'error':'unauthorized'}, 401); return
            cfg = load_config()
            # Sende keine Passwörter zurück, aber zeige ob gesetzt
            safe = {k:v for k,v in cfg.items() if 'pass' not in k and k != 'api_key' and k != 'tg_token'}
            safe['api_key_set']    = bool(cfg.get('api_key') or os.environ.get('API_KEY'))
            safe['smtp_pass_set']  = bool(cfg.get('smtp_pass') or os.environ.get('SMTP_PASS'))
            safe['tg_token_set']   = bool(cfg.get('tg_token') or os.environ.get('TG_TOKEN'))
            safe['api_key']        = cfg.get('api_key','')  # für direkte Browser-Nutzung nötig
            self.ok(safe)
        else:
            self.ok({'error':'not found'}, 404)

    def do_POST(self):
        b = self.body()

        # Login – kein Session-Check
        if self.path == '/auth/login':
            if b.get('password') == APP_PASSWORD:
                token = create_session()
                self.ok({'ok': True, 'token': token})
            else:
                self.ok({'ok': False, 'error': 'Falscher Zugangscode'})
            return

        if self.path == '/auth/check':
            self.ok({'ok': self.session_ok()})
            return

        # Ab hier Session prüfen
        if not self.session_ok():
            self.ok({'error':'unauthorized'}, 401); return

        if self.path == '/config/save':
            save_config(b); self.ok({'ok': True})

        elif self.path == '/stamp':
            if not HAVE_PDF:
                self.ok({'ok':False,'error':'PDF-Bibliotheken nicht installiert'}); return
            now = datetime.datetime.now()
            nr  = b.get('nr','PE-'+now.strftime('%d%m%y%H%M'))
            dat = now.strftime('%d.%m.%Y')
            uhr = now.strftime('%H:%M')
            try:
                stamped = do_stamp(b['pdf_b64'], b.get('mime',''), nr, dat, uhr)
                self.ok({'ok':True,'pdf_b64':stamped,'nr':nr,'datum':dat,'uhrzeit':uhr})
            except Exception as e:
                self.ok({'ok':False,'error':str(e)})

        elif self.path == '/email/send':
            cfg = load_config()
            if not cfg.get('smtp_user') or not cfg.get('smtp_pass'):
                self.ok({'ok':False,'error':'Gmail-Zugangsdaten fehlen – bitte in Konfiguration eintragen'}); return
            try:
                send_email(cfg,b['to'],b.get('cc',''),b.get('subject',''),b.get('body',''),b.get('pdf_b64'),b.get('pdf_name'))
                self.ok({'ok':True})
            except Exception as e:
                self.ok({'ok':False,'error':str(e)})

        elif self.path == '/telegram/send':
            cfg = load_config()
            tok = cfg.get('tg_token','')
            cid = cfg.get('tg_chat_id','')
            try:
                ok = tg_send(tok, cid, b['text'])
                self.ok({'ok': ok})
            except Exception as e:
                self.ok({'ok':False,'error':str(e)})
        else:
            self.ok({'error':'unknown'}, 404)

# ─── START ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    srv = HTTPServer(('0.0.0.0', PORT), H)
    print(f"Sela Holding Posteingang Server läuft auf Port {PORT}")
    print(f"PDF-Stempel: {'JA' if HAVE_PDF else 'NEIN'}")
    srv.serve_forever()
