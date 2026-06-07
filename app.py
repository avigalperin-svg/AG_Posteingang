#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AG Posteingang Server
"""
import io, base64, json, datetime, smtplib, os, secrets
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

PORT        = int(os.environ.get('PORT', 10000))
APP_PASSWORD = os.environ.get('APP_PASSWORD', 'sela2024')
CFG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sela_config.json')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
def load_config():
    env = {
        'tg_token':            os.environ.get('TG_TOKEN',''),
        'tg_chat_id':          os.environ.get('TG_CHAT_ID',''),
        'smtp_user':           os.environ.get('SMTP_USER',''),
        'sender_name':         os.environ.get('SENDER_NAME','AG Posteingang'),
        'api_key':             os.environ.get('API_KEY',''),
        'gmail_client_id':     os.environ.get('GMAIL_CLIENT_ID',''),
        'gmail_client_secret': os.environ.get('GMAIL_CLIENT_SECRET',''),
        'gmail_refresh_token': os.environ.get('GMAIL_REFRESH_TOKEN',''),
        'gdrive_in':           os.environ.get('GDRIVE_IN',''),
        'gdrive_out':          os.environ.get('GDRIVE_OUT',''),
    }
    try:
        with open(CFG_FILE) as f:
            local = json.load(f)
        for k,v in env.items():
            if v: local[k] = v
        return local
    except:
        return {k:v for k,v in env.items() if v}

def save_config(data):
    try:
        existing = {}
        try:
            with open(CFG_FILE) as f: existing = json.load(f)
        except: pass
        existing.update(data)
        with open(CFG_FILE,'w') as f: json.dump(existing, f, indent=2)
    except: pass

# ─── PDF STEMPEL ─────────────────────────────────────────────────────────────
def make_stamp(nr, datum, uhrzeit):
    buf = io.BytesIO()
    w, h = 198, 72
    c = rl_canvas.Canvas(buf, pagesize=(w,h))
    c.setStrokeColorRGB(.8,0,0); c.setLineWidth(1.5)
    c.setFillColorRGB(1,.97,.97)
    c.roundRect(.75,.75,w-1.5,h-1.5,4,fill=1,stroke=1)
    c.setFillColorRGB(.8,0,0)
    c.roundRect(.75,h-22,w-1.5,22,4,fill=1,stroke=0)
    c.rect(.75,h-22,w-1.5,11,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont('Helvetica-Bold',9)
    c.drawCentredString(w/2,h-14,'EINGEGANGEN')
    y = h-32
    for lbl,val in [('Datum:',datum),('Uhrzeit:',uhrzeit+' Uhr'),('Eingangs-Nr.:',nr)]:
        c.setFillColorRGB(.3,.3,.3); c.setFont('Helvetica-Bold',7.5)
        c.drawString(6,y,lbl)
        c.setFillColorRGB(.05,.05,.05); c.setFont('Helvetica',7.5)
        c.drawString(70,y,val)
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
        w_pt,h_pt = RL_A4
        iw,ih = img.size
        scale = min(w_pt/iw, h_pt/ih, 1)
        nw,nh = int(iw*scale),int(ih*scale)
        tmp = io.BytesIO()
        img.resize((nw,nh)).save(tmp,format='PNG'); tmp.seek(0)
        c = rl_canvas.Canvas(buf,pagesize=(w_pt,h_pt))
        c.drawImage(ImageReader(tmp),(w_pt-nw)/2,(h_pt-nh)/2,nw,nh)
        c.save(); buf.seek(0); raw = buf.read()
    stamp = PdfReader(io.BytesIO(make_stamp(nr,datum,uhrzeit))).pages[0]
    src = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    for i,page in enumerate(src.pages):
        if i==0:
            pw=float(page.mediabox.width); ph=float(page.mediabox.height)
            page.merge_transformed_page(stamp,(1,0,0,1,pw-212,ph-86))
        writer.add_page(page)
    out = io.BytesIO(); writer.write(out)
    return base64.b64encode(out.getvalue()).decode()

# ─── EMAIL via Gmail API (reines HTTPS Port 443 – kein SMTP) ─────────────────
def send_email(cfg, to, cc, subject, body_text, pdf_b64=None, pdf_name=None):
    """
    Sendet E-Mail über Gmail REST API (HTTPS Port 443).
    Benötigt: gmail_client_id, gmail_client_secret, gmail_refresh_token, smtp_user
    Kein SMTP, kein Port 465/587 – funktioniert auf jedem Hosting.
    """
    cid  = cfg.get('gmail_client_id','')
    csec = cfg.get('gmail_client_secret','')
    rtok = cfg.get('gmail_refresh_token','')
    user = cfg.get('smtp_user','')

    if not cid or not csec or not rtok or not user:
        raise Exception(
            'Gmail API nicht vollständig konfiguriert.\n'
            'Benötigt in Render Environment:\n'
            '  GMAIL_CLIENT_ID\n'
            '  GMAIL_CLIENT_SECRET\n'
            '  GMAIL_REFRESH_TOKEN\n'
            '  SMTP_USER (deine Gmail-Adresse)\n'
            'Anleitung: get_token.py lokal ausführen.'
        )

    # 1. Frischen Access Token holen
    token_data = urllib.parse.urlencode({
        'client_id':     cid,
        'client_secret': csec,
        'refresh_token': rtok,
        'grant_type':    'refresh_token',
    }).encode()
    try:
        token_req = urllib.request.Request(
            'https://oauth2.googleapis.com/token',
            data=token_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=15).read())
    except Exception as e:
        raise Exception(f'Token-Abruf fehlgeschlagen: {e}')

    if 'access_token' not in token_resp:
        raise Exception(
            f'Kein Access Token: {token_resp.get("error_description", token_resp.get("error", "Unbekannt"))}. '
            'Bitte Refresh Token erneuern (get_token.py nochmal ausführen).'
        )
    access_token = token_resp['access_token']

    # 2. E-Mail aufbauen (RFC 2822)
    msg = MIMEMultipart()
    msg['From']    = f"{cfg.get('sender_name','AG Posteingang')} <{user}>"
    msg['To']      = to
    if cc: msg['Cc'] = cc
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    if pdf_b64 and pdf_name:
        pdf_bytes = base64.b64decode(pdf_b64)
        if len(pdf_bytes) < 8 * 1024 * 1024:  # max 8MB
            part = MIMEBase('application', 'pdf')
            part.set_payload(pdf_bytes)
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{pdf_name}"')
            msg.attach(part)

    raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip('=')

    # 3. Via Gmail API senden
    send_payload = json.dumps({'raw': raw_b64}).encode('utf-8')
    try:
        send_req = urllib.request.Request(
            f'https://gmail.googleapis.com/gmail/v1/users/{user}/messages/send',
            data=send_payload,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type':  'application/json',
            }
        )
        send_resp = json.loads(urllib.request.urlopen(send_req, timeout=20).read())
        if 'id' not in send_resp:
            raise Exception(f'Unerwartete Antwort: {send_resp}')
        return True
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            msg_err = err.get('error', {}).get('message', str(e))
        except:
            msg_err = str(e)
        raise Exception(f'Gmail API Fehler: {msg_err}')
    except Exception as e:
        raise Exception(f'Sendefehler: {e}')


# ─── AUSGANGS-PDF ERSTELLEN (E-Mail-Text + Eingangsdokument) ─────────────────
def create_ausgang_pdf(to, cc, subject, body_text, sender_name, sender_email,
                        datum, nr, eingang_pdf_b64=None):
    """
    Erstellt ein PDF das enthält:
    1. Seite 1+: E-Mail-Text (Deckblatt mit Metadaten + Brieftext)
    2. Danach: alle Seiten des gestempelten Eingangsdokuments
    """
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors

    W, H = A4
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)

    # ── Kopfzeile ──
    c.setFillColorRGB(0.094, 0.373, 0.647)  # #185FA5
    c.rect(0, H-25*mm, W, 25*mm, fill=1, stroke=0)
    c.setFillColorRGB(1,1,1)
    c.setFont('Helvetica-Bold', 13)
    c.drawString(15*mm, H-14*mm, 'AG POSTEINGANG')
    c.setFont('Helvetica', 9)
    c.drawString(15*mm, H-20*mm, 'Ausgangskorrespondenz')
    c.setFont('Helvetica', 9)
    c.drawRightString(W-15*mm, H-14*mm, datum)
    c.drawRightString(W-15*mm, H-20*mm, f'Nr.: {nr}')

    # ── Metadaten-Box ──
    c.setFillColorRGB(0.941, 0.945, 0.937)  # #F0EFE9
    c.setStrokeColorRGB(0.8, 0.8, 0.8)
    c.rect(12*mm, H-60*mm, W-24*mm, 30*mm, fill=1, stroke=1)

    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont('Helvetica-Bold', 8)
    meta = [
        ('Von:', f'{sender_name} <{sender_email}>'),
        ('An:', to + (f', {cc}' if cc else '')),
        ('Betreff:', subject),
    ]
    y = H-44*mm
    for label, val in meta:
        c.setFont('Helvetica-Bold', 8)
        c.setFillColorRGB(0.3,0.3,0.3)
        c.drawString(15*mm, y, label)
        c.setFont('Helvetica', 8)
        c.setFillColorRGB(0.1,0.1,0.1)
        # Langer Text umbrechen
        max_w = W - 50*mm
        if c.stringWidth(val, 'Helvetica', 8) > max_w:
            val = val[:80] + '...'
        c.drawString(35*mm, y, val)
        y -= 7*mm

    # ── Trennlinie ──
    c.setStrokeColorRGB(0.094, 0.373, 0.647)
    c.setLineWidth(1)
    c.line(12*mm, H-63*mm, W-12*mm, H-63*mm)

    # ── E-Mail-Text ──
    c.setFont('Helvetica', 9.5)
    c.setFillColorRGB(0.1,0.1,0.1)
    text_obj = c.beginText(15*mm, H-72*mm)
    text_obj.setFont('Helvetica', 9.5)
    text_obj.setLeading(14)

    lines = body_text.split('\n')
    for line in lines:
        # Zeile umbrechen wenn zu lang
        while len(line) > 90:
            text_obj.textLine(line[:90])
            line = '   ' + line[90:]
            # Neue Seite wenn nötig
            if text_obj.getY() < 20*mm:
                c.drawText(text_obj)
                c.showPage()
                # Neue Seite Kopfzeile
                c.setFillColorRGB(0.094, 0.373, 0.647)
                c.rect(0, H-12*mm, W, 12*mm, fill=1, stroke=0)
                c.setFillColorRGB(1,1,1)
                c.setFont('Helvetica-Bold', 9)
                c.drawString(15*mm, H-8*mm, f'AG Posteingang · {subject} · Seite 2')
                text_obj = c.beginText(15*mm, H-22*mm)
                text_obj.setFont('Helvetica', 9.5)
                text_obj.setLeading(14)
        text_obj.textLine(line)
        # Neue Seite wenn nötig
        if text_obj.getY() < 20*mm:
            c.drawText(text_obj)
            c.showPage()
            c.setFillColorRGB(0.094, 0.373, 0.647)
            c.rect(0, H-12*mm, W, 12*mm, fill=1, stroke=0)
            c.setFillColorRGB(1,1,1)
            c.setFont('Helvetica-Bold', 9)
            c.drawString(15*mm, H-8*mm, f'AG Posteingang · {subject}')
            text_obj = c.beginText(15*mm, H-22*mm)
            text_obj.setFont('Helvetica', 9.5)
            text_obj.setLeading(14)

    c.drawText(text_obj)

    # ── Fußzeile letzte Seite ──
    c.setFillColorRGB(0.6,0.6,0.6)
    c.setFont('Helvetica', 7.5)
    c.drawCentredString(W/2, 12*mm, f'AG Posteingang · Ausgang Nr. {nr} · {datum}')
    c.setStrokeColorRGB(0.8,0.8,0.8)
    c.line(12*mm, 15*mm, W-12*mm, 15*mm)

    c.save()
    buf.seek(0)
    email_pdf_bytes = buf.read()

    # ── Eingangsdokument anhängen ──
    if eingang_pdf_b64:
        try:
            writer = PdfWriter()
            # Email-PDF Seiten hinzufügen
            email_reader = PdfReader(io.BytesIO(email_pdf_bytes))
            for page in email_reader.pages:
                writer.add_page(page)
            # Trennseite
            sep_buf = io.BytesIO()
            sep_c = rl_canvas.Canvas(sep_buf, pagesize=A4)
            sep_c.setFillColorRGB(0.94,0.94,0.94)
            sep_c.rect(0, 0, W, H, fill=1, stroke=0)
            sep_c.setFillColorRGB(0.094, 0.373, 0.647)
            sep_c.setFont('Helvetica-Bold', 16)
            sep_c.drawCentredString(W/2, H/2+10*mm, 'EINGANGSSCHREIBEN')
            sep_c.setFont('Helvetica', 11)
            sep_c.setFillColorRGB(0.4,0.4,0.4)
            sep_c.drawCentredString(W/2, H/2-5*mm, f'Eingangs-Nr.: {nr}  ·  {datum}')
            sep_c.save(); sep_buf.seek(0)
            sep_reader = PdfReader(sep_buf)
            writer.add_page(sep_reader.pages[0])
            # Eingangsdokument-Seiten
            eingang_reader = PdfReader(io.BytesIO(base64.b64decode(eingang_pdf_b64)))
            for page in eingang_reader.pages:
                writer.add_page(page)
            out = io.BytesIO()
            writer.write(out)
            return base64.b64encode(out.getvalue()).decode()
        except Exception as e:
            # Fallback: nur Email-PDF
            pass

    return base64.b64encode(email_pdf_bytes).decode()


def gdrive_get_token(cfg):
    """Holt Access Token für Google Drive (gleicher Refresh Token wie Gmail)"""
    data = urllib.parse.urlencode({
        'client_id':     cfg.get('gmail_client_id',''),
        'client_secret': cfg.get('gmail_client_secret',''),
        'refresh_token': cfg.get('gmail_refresh_token',''),
        'grant_type':    'refresh_token',
    }).encode()
    req = urllib.request.Request(
        'https://oauth2.googleapis.com/token',
        data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if 'access_token' not in resp:
        raise Exception(f"Drive Token Fehler: {resp.get('error_description', resp)}")
    return resp['access_token']

def gdrive_upload_pdf(cfg, folder_id, filename, pdf_b64):
    """Lädt ein PDF in einen Google Drive Ordner hoch"""
    if not folder_id:
        return False, 'Kein Ordner konfiguriert'
    # Bereinige Ordner-ID
    folder_id = folder_id.strip().split('?')[0].strip()
    if not cfg.get('gmail_client_id') or not cfg.get('gmail_refresh_token'):
        return False, 'Gmail API nicht konfiguriert'
    try:
        token = gdrive_get_token(cfg)
        pdf_bytes = base64.b64decode(pdf_b64)
        boundary = 'sela_boundary_12345'
        metadata = json.dumps({
            'name': filename,
            'parents': [folder_id],
            'mimeType': 'application/pdf'
        })
        body = (
            f'--{boundary}\r\n'
            f'Content-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{metadata}\r\n'
            f'--{boundary}\r\n'
            f'Content-Type: application/pdf\r\n\r\n'
        ).encode('utf-8') + pdf_bytes + f'\r\n--{boundary}--'.encode('utf-8')
        req = urllib.request.Request(
            'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
            data=body,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': f'multipart/related; boundary={boundary}',
                'Content-Length': str(len(body)),
            }
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if 'id' in resp:
            return True, resp['id']
        return False, str(resp)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err = json.loads(err_body)
            msg = err.get('error', {}).get('message', err_body)
            return False, f'HTTP {e.code}: {msg}'
        except:
            return False, f'HTTP {e.code}: {e.reason}'
    except Exception as e:
        return False, str(e)

def gdrive_upload_text(cfg, folder_id, filename, text_content):
    """Lädt eine Textdatei in Google Drive hoch"""
    if not folder_id:
        return False, 'Kein Ordner konfiguriert'
    folder_id = folder_id.strip().split('?')[0].strip()
    try:
        token = gdrive_get_token(cfg)
        content_bytes = text_content.encode('utf-8')
        boundary = 'sela_txt_boundary_99'
        metadata = json.dumps({
            'name': filename,
            'parents': [folder_id],
            'mimeType': 'text/plain'
        })
        body = (
            f'--{boundary}\r\n'
            f'Content-Type: application/json; charset=UTF-8\r\n\r\n'
            f'{metadata}\r\n'
            f'--{boundary}\r\n'
            f'Content-Type: text/plain; charset=UTF-8\r\n\r\n'
        ).encode('utf-8') + content_bytes + f'\r\n--{boundary}--'.encode('utf-8')
        req = urllib.request.Request(
            'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart',
            data=body,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': f'multipart/related; boundary={boundary}',
            }
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return ('id' in resp), resp.get('id', str(resp))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err = json.loads(err_body)
            msg = err.get('error', {}).get('message', err_body)
            return False, f'HTTP {e.code}: {msg}'
        except:
            return False, f'HTTP {e.code}: {e.reason}'
    except Exception as e:
        return False, str(e)

def gdrive_check_folder(cfg, folder_id):
    """Prüft ob ein Ordner existiert und zugänglich ist"""
    try:
        token = gdrive_get_token(cfg)
        req = urllib.request.Request(
            f'https://www.googleapis.com/drive/v3/files/{folder_id}'
            f'?fields=id,name,mimeType',
            headers={'Authorization': f'Bearer {token}'}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return True, resp
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode('utf-8')
            err = json.loads(err_body)
            return False, f'HTTP {e.code}: {err.get("error",{}).get("message", err_body)}'
        except:
            return False, f'HTTP {e.code}: {e.reason}'
    except Exception as e:
        return False, str(e)


def tg_send(token, chat_id, text):
    data = urllib.parse.urlencode({'chat_id':chat_id,'text':text,'parse_mode':'HTML'}).encode()
    req  = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    return json.loads(urllib.request.urlopen(req,timeout=10).read()).get('ok',False)

# ─── LOGIN PAGE ──────────────────────────────────────────────────────────────
def login_page(error=False):
    err = '<p style="color:#c00;margin-top:12px;font-size:13px">Falscher Zugangscode</p>' if error else ''
    return f"""<!DOCTYPE html><html lang="de"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AG Posteingang</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F0EFE9;
  min-height:100vh;display:flex;align-items:center;justify-content:center}}
.box{{background:#fff;border-radius:14px;padding:44px 40px;width:360px;
  box-shadow:0 4px 28px rgba(0,0,0,.12);text-align:center}}
.icon{{width:56px;height:56px;background:#185FA5;border-radius:14px;font-size:28px;
  display:flex;align-items:center;justify-content:center;margin:0 auto 20px}}
h1{{font-size:22px;font-weight:700;color:#1C1C1A;margin-bottom:6px}}
p{{font-size:13px;color:#6B6A66;margin-bottom:28px}}
input{{width:100%;padding:13px;border:1.5px solid #ddd;border-radius:9px;font-size:15px;
  text-align:center;outline:none;background:#F4F3EE;font-family:inherit;margin-bottom:14px}}
input:focus{{border-color:#185FA5;background:#fff}}
button{{width:100%;padding:13px;background:#185FA5;color:#fff;border:none;border-radius:9px;
  font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}}
button:hover{{background:#0C447C}}
</style></head><body>
<div class="box">
  <div class="icon">📬</div>
  <h1>Posteingang</h1>
  <p>AG Posteingang</p>
  <form method="POST" action="/login">
    <input type="password" name="pw" placeholder="Zugangscode" autofocus/>
    <button type="submit">Anmelden →</button>
  </form>
  {err}
</div>
</body></html>"""

# ─── HAUPT APP ───────────────────────────────────────────────────────────────

APP_PAGE = r"""<!DOCTYPE html>
<html lang="de"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AG Posteingang</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf-lib/1.17.1/pdf-lib.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#F0EFE9;--sf:#fff;--sf2:#F4F3EE;--bd:rgba(0,0,0,.09);--bd2:rgba(0,0,0,.16);
  --tx:#1C1C1A;--tx2:#6B6A66;--tx3:#A0A09C;
  --ac:#185FA5;--acl:#E6F1FB;--acd:#0C447C;
  --gr:#3B6D11;--grl:#EAF3DE;--re:#A32D2D;--rel:#FCEBEB;
  --am:#854F0B;--aml:#FAEEDA;--tg:#1565C0;--tgl:#E3F2FD;
  --r:10px;--rs:7px;--hdr:52px}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  font-size:14px;background:var(--bg);color:var(--tx);height:100vh;overflow:hidden}
/* HEADER */
.hdr{background:var(--sf);border-bottom:.5px solid var(--bd);padding:0 20px;
  height:var(--hdr);display:flex;align-items:center;justify-content:space-between;
  position:fixed;top:0;left:0;right:0;z-index:200;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.logo{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600}
.lbox{width:30px;height:30px;background:var(--ac);border-radius:7px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px}
/* SPLIT LAYOUT */
.layout{display:flex;height:calc(100vh - var(--hdr));margin-top:var(--hdr)}
.panel-left{width:50%;min-width:300px;flex-shrink:0;overflow-y:auto;
  padding:16px 14px 60px;border-right:.5px solid var(--bd);background:var(--bg)}
.panel-right{width:50%;min-width:0;display:flex;flex-direction:column;
  background:#e8e8e4;overflow:hidden}
.prev-hdr{height:44px;background:var(--sf);border-bottom:.5px solid var(--bd);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 16px;flex-shrink:0}
.prev-title{font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px}
.prev-actions{display:flex;gap:6px}
.prev-btn{background:var(--sf2);border:.5px solid var(--bd2);border-radius:var(--rs);
  padding:4px 10px;font-size:12px;cursor:pointer;display:flex;align-items:center;
  gap:4px;color:var(--tx);font-family:inherit}
.prev-btn:hover{background:var(--acl);color:var(--ac);border-color:var(--ac)}
.prev-body{flex:1;overflow:hidden;position:relative;display:flex;
  align-items:center;justify-content:center}
.prev-empty{text-align:center;color:var(--tx3);user-select:none}
.prev-empty i{font-size:52px;margin-bottom:14px;display:block;opacity:.4}
.prev-empty h3{font-size:14px;font-weight:500;margin-bottom:6px;opacity:.6}
.prev-empty p{font-size:12px;opacity:.5}
#prev-iframe{width:100%;height:100%;border:none;display:none;background:#fff}
#prev-img{max-width:100%;max-height:100%;object-fit:contain;display:none;padding:20px}
.prev-foot{background:var(--sf);border-top:.5px solid var(--bd);
  padding:6px 16px;font-size:11px;color:var(--tx2);display:flex;
  gap:14px;align-items:center;flex-shrink:0;min-height:30px;flex-wrap:wrap}
.pf-item{display:flex;align-items:center;gap:4px}
.pf-ok{color:var(--gr)}.pf-fail{color:var(--re)}.pf-info{color:var(--ac)}
/* Mobile */
@media(max-width:900px){
  body{height:auto;overflow:auto}
  .layout{flex-direction:column;height:auto}
  .panel-left{width:100%;border-right:none;border-bottom:.5px solid var(--bd)}
  .panel-right{min-height:60vh;height:60vh}
}
/* TABS */
.tabs{display:flex;border-bottom:.5px solid var(--bd);margin-bottom:12px}
.tab{padding:7px 14px;font-size:13px;cursor:pointer;border:none;
  border-bottom:2px solid transparent;color:var(--tx2);font-weight:500;
  background:none;font-family:inherit;transition:color .15s}
.tab.on{color:var(--ac);border-bottom-color:var(--ac)}
/* CARDS */
.card{background:var(--sf);border:.5px solid var(--bd);border-radius:var(--r);
  padding:14px;margin-bottom:10px}
.ch{display:flex;align-items:flex-start;gap:10px;margin-bottom:12px}
.sn{width:25px;height:25px;border-radius:50%;background:var(--acl);color:var(--acd);
  font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center;
  flex-shrink:0;margin-top:1px}
.ct{font-size:14px;font-weight:600}.cs{font-size:11px;color:var(--tx2);margin-top:2px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.full{grid-column:1/-1}
.fld{display:flex;flex-direction:column;gap:4px}
.fld label{font-size:11px;font-weight:600;color:var(--tx2);letter-spacing:.04em;text-transform:uppercase}
.fld input,.fld select,.fld textarea{width:100%;background:var(--sf2);
  border:.5px solid var(--bd);border-radius:var(--rs);padding:7px 10px;
  font-size:13px;color:var(--tx);font-family:inherit;outline:none;transition:border-color .15s}
.fld input:focus,.fld select:focus,.fld textarea:focus{border-color:var(--ac)}
.fld textarea{min-height:110px;resize:vertical;line-height:1.6}
.req{color:var(--re);margin-left:2px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;
  border-radius:var(--rs);font-size:13px;font-weight:500;cursor:pointer;
  border:none;font-family:inherit;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.98)}
.bp{background:var(--ac);color:#fff}.bp:hover{opacity:.88}
.bp:disabled{opacity:.4;cursor:not-allowed;transform:none}
.bs{background:var(--sf2);color:var(--tx);border:.5px solid var(--bd2)}.bs:hover{opacity:.82}
.brow{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.bdg{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;
  padding:3px 9px;border-radius:20px}
.bok{background:var(--grl);color:var(--gr)}.berr{background:var(--rel);color:var(--re)}
.binfo{background:var(--acl);color:var(--acd)}.bwarn{background:var(--aml);color:var(--am)}
.upz{border:1.5px dashed var(--bd2);border-radius:var(--r);padding:22px 14px;
  text-align:center;cursor:pointer;position:relative;transition:background .15s,border-color .15s}
.upz:hover,.upz.drag{background:var(--sf2);border-color:var(--ac)}
.upz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upi{font-size:26px;color:var(--tx3);margin-bottom:5px}
.upt{font-size:13px;color:var(--tx2);font-weight:500}
.ups{font-size:11px;color:var(--tx3);margin-top:2px}
.fi{display:flex;align-items:center;gap:9px;padding:7px 10px;background:var(--sf2);
  border-radius:var(--rs);margin-top:6px;font-size:13px}
.fi i{color:var(--ac);font-size:17px;flex-shrink:0}
.fn{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fsz{color:var(--tx2);font-size:12px;flex-shrink:0}
.frm{background:none;border:none;cursor:pointer;color:var(--tx3);font-size:15px;
  line-height:1;padding:2px}.frm:hover{color:var(--re)}
.prw{height:4px;background:var(--bd);border-radius:4px;margin:8px 0;overflow:hidden;display:none}
.prb{height:100%;background:var(--ac);border-radius:4px;width:0%;transition:width .4s}
.sprow{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0}
.sp{display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:var(--rs);
  font-size:11px;font-weight:500;border:.5px solid var(--bd);background:var(--sf2);
  color:var(--tx2);transition:color .2s,border-color .2s}
.sp.ok{color:var(--gr);border-color:var(--gr)}
.sp.fail{color:var(--re);border-color:var(--re)}
.sp.run{color:var(--ac);border-color:var(--ac)}
.abox{background:var(--sf2);border:.5px solid var(--bd);border-radius:var(--rs);
  padding:10px;font-size:11px;line-height:1.9;white-space:pre-wrap;
  max-height:160px;overflow-y:auto;margin-top:8px;display:none;font-family:monospace}
.tgprev{background:var(--tgl);border-left:3px solid var(--tg);
  border-radius:0 var(--rs) var(--rs) 0;padding:8px 12px;font-size:11px;
  line-height:1.9;margin-top:8px;display:none;color:var(--tg);font-family:monospace}
.stmp{display:inline-block;border:1.5px solid #CC0000;border-radius:4px;
  overflow:hidden;margin-top:8px;min-width:175px}
.sh{background:#CC0000;color:#fff;font-size:10px;font-weight:700;
  padding:3px 10px;text-align:center;letter-spacing:.08em}
.sb{background:#FFF8F8;padding:5px 10px;font-size:11px;line-height:2;color:#222}
.sb b{min-width:60px;display:inline-block;color:#444}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.dc{border:.5px solid var(--bd);border-radius:var(--r);padding:11px 8px;
  cursor:pointer;text-align:center;transition:border-color .15s,background .15s,transform .1s}
.dc:hover{background:var(--sf2);border-color:var(--ac)}
.dc:active{transform:scale(.97)}
.dc.sel{border:1.5px solid var(--ac);background:var(--acl)}
.dci{font-size:20px;color:var(--ac);margin-bottom:4px}
.dcl{font-size:12px;font-weight:600}.dcs{font-size:10px;color:var(--tx2);margin-top:2px}
.subp{margin-top:12px;display:none;padding-top:12px;border-top:.5px solid var(--bd)}
.li{display:flex;align-items:flex-start;gap:9px;padding:6px 0;
  border-bottom:.5px solid var(--bd);font-size:12px}
.li:last-child{border-bottom:none}
.lt{color:var(--tx3);font-size:10px;flex-shrink:0;min-width:42px;margin-top:1px;font-family:monospace}
.lic{font-size:14px;flex-shrink:0;margin-top:1px}
.done{display:flex;align-items:center;gap:9px;padding:11px;background:var(--grl);
  border-radius:var(--rs);color:var(--gr);font-size:13px;font-weight:500;margin-top:11px}
@media(max-width:520px){.g2{grid-template-columns:1fr}.dgrid{grid-template-columns:1fr}}
</style></head><body>

<header class="hdr">
  <div class="logo">
    <div class="lbox"><i class="ti ti-mailbox"></i></div>
    AG Posteingang
  </div>
  <a href="/logout" style="font-size:12px;color:var(--tx2);text-decoration:none;
    display:flex;align-items:center;gap:5px;padding:5px 10px;border-radius:var(--rs)">
    <i class="ti ti-logout"></i> Abmelden
  </a>
</header>

<div class="layout">

<!-- ══ LINKE SEITE: WORKFLOW ══ -->
<div class="panel-left">
<div class="tabs">
  <button class="tab on" onclick="showTab('wf',this)">Workflow</button>
  <button class="tab" onclick="showTab('cfg',this)">Konfiguration</button>
  <button class="tab" onclick="showTab('log',this)">Protokoll</button>
</div>

<div id="tab-wf">
<div class="card">
  <div class="ch"><div class="sn">1</div>
    <div><div class="ct">Posteingangsscan hochladen</div>
    <div class="cs">PDF oder Bild des gescannten Briefes</div></div>
  </div>
  <div class="upz" id="dz">
    <input type="file" id="fi" accept=".pdf,image/jpeg,image/png,image/webp"
      multiple onchange="addFiles(this.files)"/>
    <div class="upi"><i class="ti ti-cloud-upload"></i></div>
    <div class="upt">Datei hierher ziehen oder klicken</div>
    <div class="ups">PDF · JPG · PNG · max. 15 MB</div>
  </div>
  <div id="flist"></div>
</div>

<div class="card" id="c2" style="display:none">
  <div class="ch"><div class="sn">2</div>
    <div><div class="ct">Eingangsstempel & KI-Analyse</div>
    <div class="cs">Stempel auf PDF · Claude analysiert · Telegram · Drive</div></div>
  </div>
  <div class="sprow">
    <div class="sp" id="sp1"><i class="ti ti-stamp" style="font-size:12px"></i> Stempel</div>
    <div class="sp" id="sp2"><i class="ti ti-cpu" style="font-size:12px"></i> KI-Analyse</div>
    <div class="sp" id="sp3"><i class="ti ti-brand-telegram" style="font-size:12px"></i> Telegram</div>
    <div class="sp" id="sp4"><i class="ti ti-brand-google-drive" style="font-size:12px"></i> Drive</div>
  </div>
  <div class="prw" id="prw"><div class="prb" id="prb"></div></div>
  <div id="stmp-prev"></div>
  <div class="abox" id="abox"></div>
  <div class="tgprev" id="tgp"></div>
  <div class="brow">
    <button class="btn bp" id="btn2" onclick="runAll()">
      <i class="ti ti-bolt"></i> Stempel · Analysieren · Telegram
    </button>
  </div>
</div>

<div class="card" id="c3" style="display:none">
  <div class="ch"><div class="sn">3</div>
    <div><div class="ct">Deine Entscheidung</div>
    <div class="cs">Was soll mit diesem Schreiben geschehen?</div></div>
  </div>
  <div class="dgrid">
    <div class="dc" id="d-wv" onclick="selD('wv')">
      <div class="dci"><i class="ti ti-calendar-event"></i></div>
      <div class="dcl">Wiedervorlage</div><div class="dcs">Datum & Priorität</div>
    </div>
    <div class="dc" id="d-ma" onclick="selD('ma')">
      <div class="dci"><i class="ti ti-mail-forward"></i></div>
      <div class="dcl">Antwort per E-Mail</div><div class="dcs">Claude formuliert · senden</div>
    </div>
    <div class="dc" id="d-ab" onclick="selD('ab')">
      <div class="dci"><i class="ti ti-folder-check"></i></div>
      <div class="dcl">Nur ablegen</div><div class="dcs">Kein weiterer Schritt</div>
    </div>
    <div class="dc" id="d-fw" onclick="selD('fw')">
      <div class="dci"><i class="ti ti-send"></i></div>
      <div class="dcl">Weiterleiten</div><div class="dcs">E-Mail an Kollegen</div>
    </div>
  </div>
  <div class="subp" id="p-wv">
    <div class="g2">
      <div class="fld"><label>Datum</label><input type="date" id="wv-d"/></div>
      <div class="fld"><label>Priorität</label>
        <select id="wv-p"><option>Normal</option><option>Dringend</option><option>Niedrig</option></select>
      </div>
      <div class="fld full"><label>Notiz</label>
        <input type="text" id="wv-n" placeholder="Interne Notiz..."/></div>
    </div>
  </div>
  <div class="subp" id="p-ma">
    <div class="g2">
      <div class="fld full"><label>Empfänger-E-Mail <span class="req">*</span></label>
        <input type="email" id="m-to" placeholder="empfaenger@beispiel.de"/></div>
      <div class="fld full"><label>CC (optional)</label>
        <input type="email" id="m-cc" placeholder="kopie@beispiel.de"/></div>
      <div class="fld full"><label>Betreff <span class="req">*</span></label>
        <input type="text" id="m-su" placeholder="Re: ..."/></div>
      <div class="fld full">
        <label>E-Mail-Text <span class="req">*</span>
          <span style="font-weight:400;text-transform:none;font-size:11px">— von Claude vorformuliert</span></label>
        <textarea id="m-bo" placeholder="Klicke auf Entwurf generieren..."></textarea>
      </div>
    </div>
    <div class="brow">
      <button class="btn bs" onclick="genDraft()">
        <i class="ti ti-wand"></i> Entwurf generieren</button>
      <span id="dst"></span>
    </div>
  </div>
  <div class="subp" id="p-fw">
    <div class="g2">
      <div class="fld full"><label>Weiterleiten an <span class="req">*</span></label>
        <input type="email" id="fw-to" placeholder="kollege@sela-holding.de"/></div>
      <div class="fld full"><label>Hinweis</label>
        <input type="text" id="fw-n" placeholder="Bitte bearbeiten..."/></div>
    </div>
  </div>
  <div class="brow" style="margin-top:14px">
    <button class="btn bp" id="btn3" onclick="execDec()">
      <i class="ti ti-check"></i> Ausführen & abschließen
    </button>
  </div>
  <div id="donebox"></div>
</div>
<div id="cnew" style="display:none;margin-top:4px">
  <button class="btn bs" onclick="resetAll()">
    <i class="ti ti-plus"></i> Nächstes Schreiben
  </button>
</div>
</div><!-- /tab-wf -->

<!-- KONFIGURATION -->
<div id="tab-cfg" style="display:none">
<div class="card">
  <div class="ch"><div class="sn"><i class="ti ti-settings" style="font-size:11px"></i></div>
    <div><div class="ct">Einstellungen</div><div class="cs">Sicher auf dem Server gespeichert</div></div>
  </div>
  <div class="g2">
    <div class="fld full"><label>Claude API-Key <span class="req">*</span></label>
      <input type="password" id="c-ak" placeholder="sk-ant-api03-..."/></div>
    <div class="fld full"><label>Telegram Bot-Token <span class="req">*</span></label>
      <input type="password" id="c-tt" placeholder="123456789:AAF..."/></div>
    <div class="fld full"><label>Telegram Chat-ID <span class="req">*</span></label>
      <input type="text" id="c-tc" placeholder="987654321"/></div>
    <div class="fld full"><label>Gmail-Adresse (Absender) <span class="req">*</span></label>
      <input type="email" id="c-mu" placeholder="buero@gmail.com"/></div>
    <div class="fld full"><label>Gmail Client ID <span class="req">*</span></label>
      <input type="text" id="c-ci" placeholder="xxxxxxxxx.apps.googleusercontent.com"/></div>
    <div class="fld full"><label>Gmail Client Secret <span class="req">*</span></label>
      <input type="password" id="c-cs" placeholder="GOCSPX-..."/></div>
    <div class="fld full"><label>Gmail Refresh Token <span class="req">*</span></label>
      <input type="password" id="c-rt" placeholder="1//0g..."/></div>
    <div style="background:var(--acl);border-radius:var(--rs);padding:9px 12px;font-size:11px;color:var(--acd);margin-top:4px">
      <i class="ti ti-info-circle"></i> <b>Client ID, Secret & Refresh Token</b> aus <code>get_token.py</code>
      <a href="#" onclick="showTokenHelp()" style="color:var(--ac);font-weight:500;margin-left:4px">Anleitung</a>
    </div>
    <div id="token-help" style="display:none;grid-column:1/-1;background:var(--sf2);border-radius:var(--rs);padding:10px;font-size:11px;line-height:2;border:.5px solid var(--bd)">
      <b>1</b> – console.cloud.google.com → Neues Projekt → Gmail API + Drive API aktivieren<br>
      <b>2</b> – OAuth-Zustimmungsbildschirm → Extern → deine E-Mail als Testnutzer<br>
      <b>3</b> – Anmeldedaten → OAuth-Client-ID → Desktop → Client ID & Secret kopieren<br>
      <b>4</b> – <code>get_token.py</code> ausführen → Refresh Token kopieren
    </div>
    <div class="fld full" style="margin-top:10px;padding-top:10px;border-top:.5px solid var(--bd)">
      <label><i class="ti ti-brand-google-drive" style="color:var(--ac)"></i> Posteingang-Ordner-ID</label>
      <input type="text" id="c-gi" placeholder="ID aus Drive-URL nach /folders/"/>
    </div>
    <div class="fld full">
      <label><i class="ti ti-brand-google-drive" style="color:var(--ac)"></i> Postausgang-Ordner-ID</label>
      <input type="text" id="c-go" placeholder="ID aus Drive-URL nach /folders/"/>
    </div>
    <div class="fld full"><label>Absender-Name</label>
      <input type="text" id="c-mn" placeholder="AG Posteingang"/></div>
  </div>
  <div class="brow">
    <button class="btn bp" onclick="saveCfg()"><i class="ti ti-device-floppy"></i> Speichern</button>
    <button class="btn bs" onclick="testTG()"><i class="ti ti-brand-telegram"></i> Telegram</button>
    <button class="btn bs" onclick="testMail()"><i class="ti ti-mail"></i> E-Mail</button>
    <button class="btn bs" onclick="testDrive()"><i class="ti ti-brand-google-drive"></i> Drive</button>
    <button class="btn bs" onclick="debugDrive()"><i class="ti ti-bug"></i> Drive Debug</button>
    <span id="cfgmsg"></span>
  </div>
</div>
</div>

<!-- PROTOKOLL -->
<div id="tab-log" style="display:none">
<div class="card">
  <div class="ch"><div class="sn"><i class="ti ti-list" style="font-size:11px"></i></div>
    <div><div class="ct">Aktivitätsprotokoll</div></div>
  </div>
  <div id="logbox"></div>
  <div class="brow" style="margin-top:8px">
    <button class="btn bs" onclick="$('logbox').innerHTML='';lg('ti-trash','var(--tx2)','Protokoll geleert')">
      <i class="ti ti-trash"></i> Leeren</button>
  </div>
</div>
</div>

</div><!-- /panel-left -->

<!-- ══ RECHTE SEITE: DOKUMENTVORSCHAU ══ -->
<div class="panel-right">
  <div class="prev-hdr">
    <div class="prev-title">
      <i class="ti ti-eye" style="color:var(--ac)"></i>
      <span>Dokumentvorschau</span>
      <span id="prev-filename" style="color:var(--tx2);font-weight:400;font-size:12px"></span>
    </div>
    <div class="prev-actions">
      <button class="prev-btn" id="prev-toggle" onclick="togglePreview()" style="display:none">
        <i class="ti ti-arrows-maximize"></i> Vollbild
      </button>
      <button class="prev-btn" id="prev-download" onclick="downloadPrev()" style="display:none">
        <i class="ti ti-download"></i> Download
      </button>
    </div>
  </div>
  <div class="prev-body" id="prev-body">
    <div class="prev-empty" id="prev-empty">
      <i class="ti ti-file-description"></i>
      <h3>Kein Dokument geladen</h3>
      <p>Scan hochladen um Vorschau zu sehen</p>
    </div>
    <iframe id="prev-iframe" title="Dokumentvorschau"></iframe>
    <img id="prev-img" alt="Dokumentvorschau"/>
  </div>
  <div class="prev-foot" id="prev-foot">
    <span style="color:var(--tx3);font-size:11px">Warte auf Dokument...</span>
  </div>
</div>

</div><!-- /layout -->

<script>
// ═══ STATE & UTILS ════
let files=[],analysis='',curD=null,nr='',stPdfB64='',origB64='',origMime='';
const $=id=>document.getElementById(id);
const pad=n=>String(n).padStart(2,'0');
const now=()=>{const d=new Date();return pad(d.getHours())+':'+pad(d.getMinutes())};
const tod=()=>{const d=new Date();return pad(d.getDate())+'.'+pad(d.getMonth()+1)+'.'+d.getFullYear()};
const setP=p=>{$('prw').style.display='block';$('prb').style.width=p+'%'};
const setSP=(id,c)=>{const e=$(id);e.className='sp';if(c)e.classList.add(c)};
const J={'Content-Type':'application/json'};

function lg(icon,color,text){
  const d=document.createElement('div');d.className='li';
  d.innerHTML=`<span class="lt">${now()}</span><i class="ti ${icon} lic" style="color:${color}"></i><span>${text}</span>`;
  $('logbox').appendChild(d);
  d.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function showTab(n,btn){
  ['wf','cfg','log'].forEach(t=>{const e=$('tab-'+t);if(e)e.style.display='none'});
  $('tab-'+n).style.display='block';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  if(btn)btn.classList.add('on');
}

// ═══ DOKUMENTVORSCHAU ════
function showPreview(b64, mime, filename, isStamped){
  const iframe=$('prev-iframe');
  const img=$('prev-img');
  const empty=$('prev-empty');
  const foot=$('prev-foot');

  empty.style.display='none';
  $('prev-filename').textContent = filename ? '· '+filename : '';
  $('prev-toggle').style.display='flex';
  $('prev-download').style.display='flex';

  if(mime==='application/pdf' || (!mime && b64)){
    iframe.style.display='block';
    img.style.display='none';
    iframe.src='data:application/pdf;base64,'+b64;
  } else {
    img.style.display='block';
    iframe.style.display='none';
    img.src='data:'+mime+';base64,'+b64;
  }

  // Footer info
  const stamp = isStamped
    ? `<span class="pf-item pf-ok"><i class="ti ti-stamp"></i> Gestempelt</span>`
    : `<span class="pf-item pf-info"><i class="ti ti-file"></i> Original</span>`;
  foot.innerHTML = stamp +
    `<span class="pf-item"><i class="ti ti-file-type-pdf"></i> ${filename||'Dokument'}</span>`;
}

function togglePreview(){
  const body=$('prev-body');
  const isFullscreen=document.fullscreenElement;
  if(!isFullscreen){
    body.requestFullscreen && body.requestFullscreen();
    $('prev-toggle').innerHTML='<i class="ti ti-arrows-minimize"></i> Beenden';
  } else {
    document.exitFullscreen && document.exitFullscreen();
    $('prev-toggle').innerHTML='<i class="ti ti-arrows-maximize"></i> Vollbild';
  }
}

function downloadPrev(){
  if(!stPdfB64 && !origB64) return;
  const b64=stPdfB64||origB64;
  const a=document.createElement('a');
  a.href='data:application/pdf;base64,'+b64;
  a.download=`Posteingang_${nr||'Dokument'}.pdf`;
  a.click();
}

// ═══ CONFIG ════
async function loadCfg(){
  try{
    const r=await fetch('/config');if(!r.ok)return;
    const c=await r.json();
    if(c.smtp_user)$('c-mu').value=c.smtp_user;
    if(c.tg_chat_id)$('c-tc').value=c.tg_chat_id;
    if(c.sender_name)$('c-mn').value=c.sender_name;
    if(c.gdrive_in)$('c-gi').value=c.gdrive_in;
    if(c.gdrive_out)$('c-go').value=c.gdrive_out;
    if(c.has_api_key)$('c-ak').placeholder='✓ gesetzt';
    if(c.has_tg_token)$('c-tt').placeholder='✓ gesetzt';
    if(c.has_gmail_client_id)$('c-ci').placeholder='✓ gesetzt';
    if(c.has_gmail_client_secret)$('c-cs').placeholder='✓ gesetzt';
    if(c.has_gmail_refresh_token)$('c-rt').placeholder='✓ gesetzt';
  }catch(e){}
}
async function saveCfg(){
  const body={
    api_key:$('c-ak').value.trim(),tg_token:$('c-tt').value.trim(),
    tg_chat_id:$('c-tc').value.trim(),smtp_user:$('c-mu').value.trim(),
    gmail_client_id:$('c-ci').value.trim(),gmail_client_secret:$('c-cs').value.trim(),
    gmail_refresh_token:$('c-rt').value.trim(),
    sender_name:$('c-mn').value.trim()||'AG Posteingang',
    gdrive_in:$('c-gi').value.trim(),gdrive_out:$('c-go').value.trim()
  };
  Object.keys(body).forEach(k=>{if(!body[k])delete body[k]});
  const r=await fetch('/config/save',{method:'POST',headers:J,body:JSON.stringify(body)});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok
    ?'<span class="bdg bok"><i class="ti ti-check"></i> Gespeichert</span>'
    :'<span class="bdg berr">Fehler</span>';
  setTimeout(()=>$('cfgmsg').innerHTML='',3000);
}
function showTokenHelp(){const h=$('token-help');h.style.display=h.style.display==='block'?'none':'block'}
async function testTG(){
  const r=await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:'✅ Telegram-Test OK!'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Telegram OK!</span>':`<span class="bdg berr">${d.error||'Fehler'}</span>`;
  setTimeout(()=>$('cfgmsg').innerHTML='',4000);
}
async function testMail(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Sende...</span>';
  const cfg=await(await fetch('/config')).json();
  const to=cfg.smtp_user||$('c-mu').value;
  if(!to){$('cfgmsg').innerHTML='<span class="bdg berr">Gmail-Adresse eintragen</span>';return}
  const r=await fetch('/mail',{method:'POST',headers:J,
    body:JSON.stringify({to,subject:'Test – AG Posteingang',body:'Verbindungstest OK.'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Mail gesendet!</span>':`<span class="bdg berr">${d.error||'Fehler'}</span>`;
  setTimeout(()=>$('cfgmsg').innerHTML='',5000);
}
async function debugDrive(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Debug läuft...</span>';
  try{
    const r=await fetch('/drive/debug',{method:'POST',headers:J,body:JSON.stringify({})});
    const d=await r.json();
    let msg='DRIVE DEBUG:\n\n';
    msg+=`Client ID Länge: ${d.client_id_laenge||0} Zeichen\n`;
    msg+=`Client Secret Länge: ${d.client_secret_laenge||0} Zeichen\n`;
    msg+=`Refresh Token Länge: ${d.refresh_token_laenge||0} Zeichen\n`;
    msg+=`Refresh Token Start: ${d.refresh_token_start||'LEER'}\n`;
    msg+=`Token hat Leerzeichen/Zeilenumbrüche: ${d.refresh_token_leerzeichen?'JA ← PROBLEM!':'Nein'}\n\n`;
    if(d.token_fehler) msg+=`TOKEN FEHLER: ${JSON.stringify(d.token_fehler)}\n\n`;
    msg+=`Token: ${d.token||'FEHLER'}\n`;
    if(d.scope) msg+=`Scope: ${d.scope}\n`;
    msg+=`Konto: ${d.konto||'unbekannt'}\n`;
    msg+=`Drive-Zugriff: ${d.drive_zugriff||'FEHLER'}\n`;
    msg+=`Dateien gefunden: ${d.dateien_gefunden??'–'}\n\n`;
    if(d.Posteingang) msg+=`Posteingang (${d.Posteingang.id}):\n  ${d.Posteingang.ok?'✓ OK':'✗ '+d.Posteingang.info}\n\n`;
    if(d.Postausgang) msg+=`Postausgang (${d.Postausgang.id}):\n  ${d.Postausgang.ok?'✓ OK':'✗ '+d.Postausgang.info}\n\n`;
    if(d.fehler) msg+=`FEHLER: ${d.fehler}`;
    alert(msg);
    $('cfgmsg').innerHTML='<span class="bdg binfo">Debug abgeschlossen</span>';
    setTimeout(()=>$('cfgmsg').innerHTML='',4000);
  }catch(e){
    alert('Debug-Fehler: '+e.message);
  }
}
async function testDrive(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Prüfe Drive-Ordner...</span>';
  try{
    // Erst Ordner prüfen
    const cr=await fetch('/drive/check',{method:'POST',headers:J,body:JSON.stringify({})});
    const cd=await cr.json();
    let msg='';
    let allOk=true;
    for(const [label,info] of Object.entries(cd.results||{})){
      if(info.ok){
        msg+=`✓ ${label}: "${info.info?.name||info.folder_id}" `;
      } else {
        msg+=`✗ ${label}: ${info.info} `;
        allOk=false;
      }
    }
    if(!allOk){
      $('cfgmsg').innerHTML=`<span class="bdg berr">${msg}</span>`;
      setTimeout(()=>$('cfgmsg').innerHTML='',8000);
      return;
    }
    // Ordner OK - Testdatei hochladen
    $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Lade Testdatei hoch...</span>';
    const r=await fetch('/drive/save',{method:'POST',headers:J,
      body:JSON.stringify({folder:'in',filename:'Drive_Test.txt',
        text:`Drive-Verbindungstest\nDatum: ${tod()} ${now()}\nAG Posteingang`})});
    const d=await r.json();
    $('cfgmsg').innerHTML=d.ok
      ?`<span class="bdg bok"><i class="ti ti-check"></i> Drive OK! ${msg}</span>`
      :`<span class="bdg berr">Upload-Fehler: ${d.error||d.msg}</span>`;
    setTimeout(()=>$('cfgmsg').innerHTML='',8000);
  }catch(e){
    $('cfgmsg').innerHTML=`<span class="bdg berr">Fehler: ${e.message}</span>`;
    setTimeout(()=>$('cfgmsg').innerHTML='',6000);
  }
}

// ═══ DATEIEN ════
function addFiles(fl){
  Array.from(fl).forEach(f=>{if(!files.find(x=>x.name===f.name))files.push(f)});
  renderFiles();
  if(files.length){
    $('c2').style.display='block';
    // Sofort im Vorschau-Fenster anzeigen (Original)
    const f=files[0];
    toB64(f).then(b64=>{
      origB64=b64; origMime=f.type;
      showPreview(b64, f.type, f.name, false);
    });
  }
}
function renderFiles(){
  $('flist').innerHTML=files.map((f,i)=>`
    <div class="fi">
      <i class="ti ${f.type.includes('pdf')?'ti-file-type-pdf':'ti-photo'}"></i>
      <span class="fn">${f.name}</span>
      <span class="fsz">${(f.size/1024).toFixed(0)} KB</span>
      <button class="frm" onclick="rmF(${i})"><i class="ti ti-x"></i></button>
    </div>`).join('');
}
function rmF(i){
  files.splice(i,1);renderFiles();
  if(!files.length){
    $('c2').style.display='none';
    $('prev-iframe').style.display='none';
    $('prev-img').style.display='none';
    $('prev-empty').style.display='block';
    $('prev-filename').textContent='';
    $('prev-toggle').style.display='none';
    $('prev-download').style.display='none';
    $('prev-foot').innerHTML='<span style="color:var(--tx3);font-size:11px">Warte auf Dokument...</span>';
  }
}
async function toB64(f){
  return new Promise((res,rej)=>{
    const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f);
  });
}

// ═══ STEMPEL (pdf-lib) ════
async function stampBrowser(rawB64,isImg,mime,einNr,datum,uhrzeit){
  const {PDFDocument,rgb,StandardFonts}=PDFLib;
  let doc;
  if(isImg){
    doc=await PDFDocument.create();
    const bytes=Uint8Array.from(atob(rawB64),c=>c.charCodeAt(0));
    const img=(mime==='image/jpeg'||mime==='image/jpg')?await doc.embedJpg(bytes):await doc.embedPng(bytes);
    const {width:iw,height:ih}=img.scale(1);
    const sc=Math.min(595/iw,842/ih,1);
    const pg=doc.addPage([595,842]);
    pg.drawImage(img,{x:(595-iw*sc)/2,y:(842-ih*sc)/2,width:iw*sc,height:ih*sc});
  } else {
    doc=await PDFDocument.load(Uint8Array.from(atob(rawB64),c=>c.charCodeAt(0)),{ignoreEncryption:true});
  }
  const bold=await doc.embedFont(StandardFonts.HelveticaBold);
  const reg=await doc.embedFont(StandardFonts.Helvetica);
  const pg=doc.getPages()[0];
  const {width:pw,height:ph}=pg.getSize();
  const sw=200,sh=72,mx=14;
  const sx=pw-sw-mx,sy=ph-sh-mx;
  pg.drawRectangle({x:sx,y:sy,width:sw,height:sh,color:rgb(1,.97,.97),borderColor:rgb(.8,0,0),borderWidth:1.5});
  pg.drawRectangle({x:sx,y:sy+sh-22,width:sw,height:22,color:rgb(.8,0,0)});
  pg.drawText('EINGEGANGEN',{x:sx+sw/2-bold.widthOfTextAtSize('EINGEGANGEN',9)/2,y:sy+sh-15,size:9,font:bold,color:rgb(1,1,1)});
  let ry=sy+sh-34;
  for(const [l,v] of [['Datum:',datum],['Uhrzeit:',uhrzeit+' Uhr'],['Eingangs-Nr.:',einNr]]){
    pg.drawText(l,{x:sx+7,y:ry,size:7.5,font:bold,color:rgb(.25,.25,.25)});
    pg.drawText(v,{x:sx+68,y:ry,size:7.5,font:reg,color:rgb(.05,.05,.05)});
    ry-=14;
  }
  const out=await doc.save();
  return btoa(String.fromCharCode(...new Uint8Array(out)));
}

// ═══ CLAUDE API ════
async function callClaude(messages){
  const cfg=await(await fetch('/config')).json();
  if(!cfg.api_key&&!cfg.has_api_key)throw new Error('API-Key fehlt!');
  const r=await fetch('https://api.anthropic.com/v1/messages',{
    method:'POST',
    headers:{'Content-Type':'application/json','x-api-key':cfg.api_key||'',
      'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},
    body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:1400,messages})
  });
  if(!r.ok){const e=await r.json();throw new Error(e.error?.message||'API '+r.status)}
  return (await r.json()).content?.[0]?.text||'';
}

// ═══ HAUPTWORKFLOW ════
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
    const isImg=f.type.startsWith('image/');

    // 1. STEMPEL
    lg('ti-stamp','var(--ac)','Setze Eingangsstempel...');
    stPdfB64=await stampBrowser(rawB64,isImg,f.type,nr,datum,uhrzeit);
    setSP('sp1','ok');
    lg('ti-stamp','var(--gr)','Stempel gesetzt ✓');
    setP(22);

    // Gestempeltes PDF sofort in Vorschau rechts
    showPreview(stPdfB64,'application/pdf',f.name,true);

    $('stmp-prev').innerHTML=`
      <div style="display:flex;align-items:center;gap:12px;margin-top:8px;flex-wrap:wrap">
        <div class="stmp">
          <div class="sh">EINGEGANGEN</div>
          <div class="sb"><b>Datum:</b>${datum}<br><b>Uhrzeit:</b>${uhrzeit} Uhr<br><b>Nr.:</b>${nr}</div>
        </div>
        <a href="data:application/pdf;base64,${stPdfB64}" download="Posteingang_${nr}.pdf"
           style="display:inline-flex;align-items:center;gap:5px;color:var(--ac);font-size:12px;
             font-weight:500;padding:7px 12px;background:var(--acl);border-radius:var(--rs);
             border:.5px solid var(--ac);text-decoration:none">
          <i class="ti ti-download"></i> PDF herunterladen
        </a>
      </div>`;

    // Drive Posteingang - läuft über Server /stamp Endpunkt
    // Status wird nach dem Stamp-Aufruf angezeigt
    lg('ti-brand-google-drive','var(--ac)','Speichere in Drive Posteingang...');setSP('sp4','run');
    // Server stempelt UND speichert in Drive gleichzeitig
    const sr=await fetch('/stamp',{method:'POST',headers:J,
      body:JSON.stringify({pdf_b64:rawB64,mime:f.type,nr})});
    const sd=await sr.json();
    if(sd.ok){
      if(sd.drive_ok){setSP('sp4','ok');lg('ti-brand-google-drive','var(--gr)','Posteingang in Drive gespeichert ✓');}
      else if(sd.drive_msg&&sd.drive_msg!=='Drive nicht konfiguriert'){setSP('sp4','fail');lg('ti-alert-triangle','var(--am)','Drive Posteingang: '+sd.drive_msg);}
      else{setSP('sp4','ok');} // Drive nicht konfiguriert ist kein Fehler
    }

    // 2. KI-ANALYSE
    lg('ti-cpu','var(--ac)','KI-Analyse...');setSP('sp2','run');
    const mc=[];
    if(isImg)mc.push({type:'image',source:{type:'base64',media_type:f.type,data:rawB64}});
    else mc.push({type:'document',source:{type:'base64',media_type:'application/pdf',data:rawB64}});
    mc.push({type:'text',text:`Analysiere dieses Eingangsschreiben der AG Posteingang. Antworte NUR mit:\n\nABSENDER: [Name/Firma/Behörde]\nDATUM: [Briefdatum TT.MM.JJJJ]\nBETREFF: [1-2 Sätze worum es geht]\nDRINGLICHKEIT: [Hoch / Mittel / Niedrig]\nFRIST: [Datum oder "keine Frist"]\nBETRAG: [Betrag oder "–"]\nEMPFEHLUNG: [Wiedervorlage / Antwort / Ablage / Weiterleiten]\nHINWEIS: [Wichtige Details]`});
    setP(45);
    analysis=await callClaude([{role:'user',content:mc}]);
    setSP('sp2','ok');lg('ti-file-text','var(--gr)','KI-Analyse abgeschlossen ✓');
    const ab=$('abox');ab.style.display='block';
    ab.textContent=`╔══ EINGANGSPROTOKOLL ══════════════════╗\n  Nr.:     ${nr}\n  Eingang: ${datum}  ${uhrzeit} Uhr\n  Datei:   ${f.name}\n╚═══════════════════════════════════════╝\n\n`+analysis;
    setP(65);

    // 3. TELEGRAM
    lg('ti-brand-telegram','var(--ac)','Sende Telegram...');setSP('sp3','run');
    const tgTxt=`📬 <b>POSTEINGANG – AG</b>\n━━━━━━━━━━━━━━━━━━━\n<b>Nr.:</b>      ${nr}\n<b>Eingang:</b> ${datum} · ${uhrzeit} Uhr\n<b>Datei:</b>   ${f.name}\n━━━━━━━━━━━━━━━━━━━\n${analysis}\n━━━━━━━━━━━━━━━━━━━\n<b>Bitte Entscheidung in der App treffen!</b>`;
    $('tgp').style.display='block';$('tgp').textContent=tgTxt.replace(/<[^>]+>/g,'');
    const tr=await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:tgTxt})});
    const td=await tr.json();
    if(td.ok){setSP('sp3','ok');lg('ti-brand-telegram','var(--tg)','Telegram gesendet ✓');}
    else{setSP('sp3','fail');lg('ti-alert-triangle','var(--am)','Telegram: '+td.error);}
    setP(100);
    $('c3').style.display='block';
  }catch(e){
    lg('ti-x','var(--re)','Fehler: '+e.message);
    ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,'fail'));
    alert('Fehler: '+e.message);
  }
  btn.disabled=false;btn.innerHTML='<i class="ti ti-refresh"></i> Erneut';
}

// ═══ ENTSCHEIDUNG ════
function selD(t){
  curD=t;
  ['wv','ma','ab','fw'].forEach(x=>{$('d-'+x).classList.remove('sel');const p=$('p-'+x);if(p)p.style.display='none'});
  $('d-'+t).classList.add('sel');const pp=$('p-'+t);if(pp)pp.style.display='block';
}

async function genDraft(){
  if(!analysis){alert('Bitte erst analysieren.');return}
  const ds=$('dst');ds.innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Generiere...</span>';$('m-bo').value='';
  try{
    const cfg=await(await fetch('/config')).json();
    const t=await callClaude([{role:'user',content:
      `Verfasse professionelle deutsche Antwort der AG Posteingang.\n\nBriefanalyse:\n${analysis}\n\nErste Zeile: "BETREFF: [Betreff]"\nDann vollständiger Brief ab "Sehr geehrte..." – sachlich, höflich.\nAbsender: ${cfg.sender_name||'AG Posteingang'}`
    }]);
    const lines=t.split('\n');
    const bl=lines.find(l=>l.startsWith('BETREFF:'));
    if(bl)$('m-su').value=bl.replace('BETREFF:','').trim();
    const bs=lines.findIndex(l=>/^(Sehr geehrte|Sehr geehrter|Guten)/.test(l.trim()));
    $('m-bo').value=bs>=0?lines.slice(bs).join('\n'):t;
    ds.innerHTML='<span class="bdg bok"><i class="ti ti-check"></i> Entwurf fertig!</span>';
    lg('ti-wand','var(--ac)','Entwurf generiert ✓');
  }catch(e){ds.innerHTML=`<span class="bdg berr">${e.message}</span>`}
}

async function execDec(){
  if(!curD){alert('Bitte Entscheidung treffen.');return}
  const btn=$('btn3');btn.disabled=true;btn.innerHTML='<i class="ti ti-loader"></i> Läuft...';
  const datum=tod(),uhrzeit=now();
  try{
    if(curD==='wv'){
      const d=$('wv-d').value,p=$('wv-p').value,n=$('wv-n').value;
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({
        text:`📅 <b>Wiedervorlage</b>\nNr.: ${nr}\nDatum: ${d||'–'}\nPriorität: ${p}\nNotiz: ${n||'–'}`})});
      lg('ti-calendar-event','var(--gr)',`Wiedervorlage: ${d} · ${p} ✓`);
      showDone('Wiedervorlage eingetragen · Telegram ✓');

    }else if(curD==='ma'){
      const to=$('m-to').value.trim(),cc=$('m-cc').value.trim();
      const su=$('m-su').value.trim()||'Antwort – AG Posteingang';
      const bo=$('m-bo').value.trim();
      if(!to){alert('Empfänger-E-Mail!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      if(!bo){alert('E-Mail-Text eingeben!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      lg('ti-mail','var(--ac)',`Sende E-Mail an ${to}...`);
      const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),30000);
      try{
        const er=await fetch('/mail',{method:'POST',headers:J,signal:ctrl.signal,
          body:JSON.stringify({to,cc,subject:su,body:bo,nr,
            pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
        clearTimeout(timer);
        const ed=await er.json();
        if(!ed.ok)throw new Error(ed.error);
        if(ed.drive_ok){
          lg('ti-brand-google-drive','var(--gr)','Ausgangs-PDF in Drive gespeichert ✓');
        } else if(ed.drive_msg&&ed.drive_msg!=='Drive nicht konfiguriert'){
          lg('ti-alert-triangle','var(--am)','Drive Ausgang: '+ed.drive_msg);
        }
      }catch(e){clearTimeout(timer);if(e.name==='AbortError')throw new Error('Timeout – Gmail-Konfiguration prüfen');throw e}
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({
        text:`✉️ <b>E-Mail gesendet</b>\nNr.: ${nr}\nAn: ${to}\nBetreff: ${su}`})});
      lg('ti-mail','var(--gr)',`E-Mail gesendet an ${to} ✓`);
      showDone(`E-Mail gesendet · ${to} · Drive · Telegram ✓`);

    }else if(curD==='ab'){
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:`📁 <b>Abgelegt</b>\nNr.: ${nr}\n${datum}`})});
      lg('ti-folder-check','var(--gr)','Abgelegt · Telegram ✓');
      showDone('Abgelegt · Telegram ✓');

    }else if(curD==='fw'){
      const fto=$('fw-to').value.trim();
      if(!fto){alert('Empfänger!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      const fn=$('fw-n').value.trim();
      const cfg=await(await fetch('/config')).json();
      const fwb=`Weitergeleitet von AG Posteingang\n\nHinweis: ${fn||'–'}\nNr.: ${nr} · ${datum}\n\n${analysis}\n\n--\n${cfg.sender_name||'AG Posteingang'}`;
      const er=await fetch('/mail',{method:'POST',headers:J,
        body:JSON.stringify({to:fto,subject:`Weiterleitung: Eingangspost ${nr}`,body:fwb,nr,
          pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
      const ed=await er.json();
      if(!ed.ok)throw new Error(ed.error);
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:`➡️ <b>Weitergeleitet</b>\nNr.: ${nr}\nAn: ${fto}`})});
      lg('ti-send','var(--gr)',`Weitergeleitet an ${fto} ✓`);
      showDone(`Weitergeleitet · ${fto} · Telegram ✓`);
    }
    $('cnew').style.display='block';
  }catch(e){lg('ti-x','var(--re)','Fehler: '+e.message);alert('Fehler: '+e.message)}
  btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen & abschließen';
}

function showDone(msg){
  $('donebox').innerHTML=`<div class="done"><i class="ti ti-circle-check"></i> ${msg} · ${tod()} ${now()}</div>`;
}
function resetAll(){
  files=[];analysis='';curD=null;nr='';stPdfB64='';origB64='';origMime='';
  renderFiles();
  ['c2','c3','cnew'].forEach(id=>$(id).style.display='none');
  ['abox','tgp'].forEach(id=>{$(id).style.display='none'});
  $('prw').style.display='none';$('prb').style.width='0%';
  $('stmp-prev').innerHTML='';$('donebox').innerHTML='';
  ['wv','ma','ab','fw'].forEach(t=>{$('d-'+t).classList.remove('sel');const p=$('p-'+t);if(p)p.style.display='none'});
  ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,null));
  // Preview zurücksetzen
  $('prev-iframe').style.display='none';$('prev-iframe').src='';
  $('prev-img').style.display='none';
  $('prev-empty').style.display='block';
  $('prev-filename').textContent='';
  $('prev-toggle').style.display='none';$('prev-download').style.display='none';
  $('prev-foot').innerHTML='<span style="color:var(--tx3);font-size:11px">Warte auf Dokument...</span>';
  lg('ti-plus','var(--ac)','Neues Schreiben');
}

const dz=$('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag')});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');addFiles(e.dataTransfer.files)});

loadCfg();
lg('ti-rocket','var(--ac)',`Posteingang Cloud · AG Posteingang · ${tod()}`);
</script>
</body></html>"""


# ─── HTTP HANDLER ────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')

    def send_html(self, content, code=200):
        body=content.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',len(body))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        body=json.dumps(data,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',len(body))
        self.send_cors()
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send_response(302)
        self.send_header('Location',url)
        self.send_cors()
        self.end_headers()

    def get_body(self):
        n=int(self.headers.get('Content-Length',0))
        return json.loads(self.rfile.read(n)) if n else {}

    def get_form(self):
        n=int(self.headers.get('Content-Length',0))
        raw=self.rfile.read(n).decode('utf-8')
        return {k:v[0] for k,v in urllib.parse.parse_qs(raw).items()}

    def cookie_ok(self):
        cookie=self.headers.get('Cookie','')
        for part in cookie.split(';'):
            if part.strip().startswith('sela_auth='):
                return part.strip().split('=',1)[1]==APP_PASSWORD
        return False

    def do_OPTIONS(self):
        self.send_response(200);self.send_cors();self.end_headers()

    def do_GET(self):
        if self.path=='/':
            if self.cookie_ok():self.redirect('/app')
            else:self.send_html(login_page())
        elif self.path=='/app':
            if not self.cookie_ok():self.redirect('/')
            else:self.send_html(APP_PAGE)
        elif self.path=='/logout':
            self.send_response(302)
            self.send_header('Location','/')
            self.send_header('Set-Cookie','sela_auth=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT')
            self.end_headers()
        elif self.path=='/config':
            if not self.cookie_ok():self.send_json({'error':'unauthorized'},401);return
            cfg=load_config()
            self.send_json({
                'smtp_user':               cfg.get('smtp_user',''),
                'tg_chat_id':              cfg.get('tg_chat_id',''),
                'sender_name':             cfg.get('sender_name','AG Posteingang'),
                'api_key':                 cfg.get('api_key',''),
                'gdrive_in':               cfg.get('gdrive_in',''),
                'gdrive_out':              cfg.get('gdrive_out',''),
                'has_api_key':             bool(cfg.get('api_key')),
                'has_tg_token':            bool(cfg.get('tg_token')),
                'has_gmail_client_id':     bool(cfg.get('gmail_client_id')),
                'has_gmail_client_secret': bool(cfg.get('gmail_client_secret')),
                'has_gmail_refresh_token': bool(cfg.get('gmail_refresh_token')),
            })
        elif self.path=='/ping':
            self.send_json({'ok':True})
        else:
            self.send_json({'error':'not found'},404)

    def do_POST(self):
        # Login (kein Auth nötig)
        if self.path=='/login':
            form=self.get_form()
            if form.get('pw')==APP_PASSWORD:
                self.send_response(302)
                self.send_header('Location','/app')
                self.send_header('Set-Cookie',
                    f'sela_auth={APP_PASSWORD}; Path=/; HttpOnly; SameSite=Lax')
                self.end_headers()
            else:
                self.send_html(login_page(error=True))
            return

        # Ab hier Auth prüfen
        if not self.cookie_ok():
            self.send_json({'error':'unauthorized'},401);return

        b=self.get_body()

        if self.path=='/config/save':
            save_config(b);self.send_json({'ok':True})

        elif self.path=='/stamp':
            if not HAVE_PDF:
                self.send_json({'ok':False,'error':'PDF-Bibliotheken fehlen'});return
            n=datetime.datetime.now()
            nr=b.get('nr','PE-'+n.strftime('%d%m%y%H%M'))
            dat=n.strftime('%d.%m.%Y');uhr=n.strftime('%H:%M')
            try:
                stamped=do_stamp(b['pdf_b64'],b.get('mime',''),nr,dat,uhr)
                # Automatisch in Google Drive Posteingang speichern
                cfg=load_config()
                drive_ok=False; drive_msg='Drive nicht konfiguriert'
                if cfg.get('gdrive_in'):
                    fname=f"Posteingang_{nr}_{dat.replace('.','')}.pdf"
                    drive_ok,drive_msg=gdrive_upload_pdf(cfg,cfg['gdrive_in'],fname,stamped)
                self.send_json({'ok':True,'pdf_b64':stamped,'nr':nr,'datum':dat,'uhrzeit':uhr,
                    'drive_ok':drive_ok,'drive_msg':str(drive_msg)})
            except Exception as e:
                self.send_json({'ok':False,'error':str(e)})

        elif self.path=='/mail':
            cfg=load_config()
            try:
                to      = b.get('to','')
                cc      = b.get('cc','')
                subject = b.get('subject','Antwort – AG Posteingang')
                body    = b.get('body','')
                nr      = b.get('nr','')
                eingang_pdf_b64 = b.get('pdf_b64')  # gestempeltes Eingangsdokument

                # E-Mail senden (mit gestempeltem PDF als Anhang)
                send_email(cfg, to, cc, subject, body, eingang_pdf_b64,
                           f'Posteingang_{nr}.pdf' if nr else 'Posteingang.pdf')

                # Ausgangs-PDF erstellen (E-Mail-Text + Eingangsdokument)
                n = datetime.datetime.now()
                datum = n.strftime('%d.%m.%Y')
                ausgang_pdf_b64 = create_ausgang_pdf(
                    to=to, cc=cc, subject=subject, body_text=body,
                    sender_name=cfg.get('sender_name','AG Posteingang'),
                    sender_email=cfg.get('smtp_user',''),
                    datum=datum, nr=nr,
                    eingang_pdf_b64=eingang_pdf_b64
                )

                # Ausgangs-PDF in Drive Postausgang speichern
                drive_ok=False; drive_msg='Drive nicht konfiguriert'
                if cfg.get('gdrive_out'):
                    fname=f"Ausgang_{nr}_{datum.replace('.','')}.pdf"
                    drive_ok,drive_msg=gdrive_upload_pdf(cfg,cfg['gdrive_out'].strip().split('?')[0],fname,ausgang_pdf_b64)

                self.send_json({'ok':True,'drive_ok':drive_ok,'drive_msg':str(drive_msg),
                                'ausgang_pdf_b64':ausgang_pdf_b64})
            except Exception as e:
                self.send_json({'ok':False,'error':str(e)})

        elif self.path=='/drive/debug':
            # Vollständiger Debug: Token-Info + Drive-Zugriff prüfen
            cfg=load_config()
            result={}
            try:
                # 0. Zeige Credential-Infos (ohne Passwörter)
                cid = cfg.get('gmail_client_id','')
                csec = cfg.get('gmail_client_secret','')
                rtok = cfg.get('gmail_refresh_token','')
                result['client_id_laenge'] = len(cid)
                result['client_secret_laenge'] = len(csec)
                result['refresh_token_laenge'] = len(rtok)
                result['refresh_token_start'] = rtok[:8] if rtok else 'LEER'
                result['refresh_token_leerzeichen'] = ' ' in rtok or '\n' in rtok or '\r' in rtok

                # 1. Token holen - zeige genaue Fehlerdetails
                data = urllib.parse.urlencode({
                    'client_id': cid,
                    'client_secret': csec,
                    'refresh_token': rtok.strip(),
                    'grant_type': 'refresh_token',
                }).encode()
                req = urllib.request.Request(
                    'https://oauth2.googleapis.com/token',
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )
                try:
                    token_resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
                    if 'access_token' not in token_resp:
                        result['token_fehler'] = token_resp
                        self.send_json(result)
                        return
                    token = token_resp['access_token']
                    result['token'] = 'OK'
                    result['token_typ'] = token_resp.get('token_type','')
                    result['scope'] = token_resp.get('scope','')
                    result['hat_drive_scope'] = 'drive' in token_resp.get('scope','')
                    result['hat_gmail_scope'] = 'gmail' in token_resp.get('scope','')
                except urllib.error.HTTPError as e:
                    err = json.loads(e.read())
                    result['token_fehler'] = err
                    self.send_json(result)
                    return

                # 2. Konto prüfen
                req2=urllib.request.Request(
                    'https://www.googleapis.com/oauth2/v3/userinfo',
                    headers={'Authorization':f'Bearer {token}'}
                )
                userinfo=json.loads(urllib.request.urlopen(req2,timeout=10).read())
                result['konto']=userinfo.get('email','unbekannt')

                # 3. Drive-Zugriff
                req3=urllib.request.Request(
                    'https://www.googleapis.com/drive/v3/files?pageSize=3&fields=files(id,name)',
                    headers={'Authorization':f'Bearer {token}'}
                )
                files=json.loads(urllib.request.urlopen(req3,timeout=10).read())
                result['drive_zugriff']='OK'
                result['dateien_gefunden']=len(files.get('files',[]))

                # 4. Ordner prüfen
                for key,label in [('gdrive_in','Posteingang'),('gdrive_out','Postausgang')]:
                    fid=cfg.get(key,'').strip()
                    if fid:
                        ok,info=gdrive_check_folder(cfg,fid)
                        result[label]={'ok':ok,'id':fid,'info':str(info)}
                    else:
                        result[label]={'ok':False,'info':'Keine ID'}

            except Exception as e:
                result['fehler']=str(e)
            self.send_json(result)

        elif self.path=='/drive/check':
            # Prüft ob Ordner erreichbar ist
            cfg=load_config()
            results={}
            for key,label in [('gdrive_in','Posteingang'),('gdrive_out','Postausgang')]:
                fid=cfg.get(key,'')
                if fid:
                    ok,info=gdrive_check_folder(cfg,fid)
                    results[label]={'ok':ok,'folder_id':fid,'info':str(info)}
                else:
                    results[label]={'ok':False,'info':'Keine Ordner-ID konfiguriert'}
            self.send_json({'ok':True,'results':results})

        elif self.path=='/drive/save':
            cfg=load_config()
            folder=b.get('folder','in')
            folder_id=cfg.get('gdrive_in' if folder=='in' else 'gdrive_out','').strip()
            # Bereinige Ordner-ID - entferne alles ab '?'
            if '?' in folder_id: folder_id=folder_id.split('?')[0].strip()
            fname=b.get('filename','dokument.pdf')
            if not folder_id:
                self.send_json({'ok':False,'msg':f'Ordner-ID für {folder} nicht konfiguriert'});return
            try:
                if b.get('pdf_b64'):
                    ok,msg=gdrive_upload_pdf(cfg,folder_id,fname,b['pdf_b64'])
                else:
                    ok,msg=gdrive_upload_text(cfg,folder_id,fname,b.get('text',''))
                self.send_json({'ok':ok,'msg':str(msg)})
            except Exception as e:
                self.send_json({'ok':False,'error':str(e)})

        elif self.path=='/tg':
            cfg=load_config()
            tok=cfg.get('tg_token','');cid=cfg.get('tg_chat_id','')
            if not tok or not cid:
                self.send_json({'ok':False,'error':'Telegram nicht konfiguriert'});return
            try:
                ok=tg_send(tok,cid,b['text'])
                self.send_json({'ok':ok})
            except Exception as e:
                self.send_json({'ok':False,'error':str(e)})
        else:
            self.send_json({'error':'unknown'},404)

if __name__=='__main__':
    srv=HTTPServer(('0.0.0.0',PORT),H)
    print(f"Server läuft auf Port {PORT}")
    srv.serve_forever()
