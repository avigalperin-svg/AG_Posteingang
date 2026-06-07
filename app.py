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

def gdrive_check_duplicate(cfg, folder_id, filename, pdf_b64):
    """
    Prüft ob eine Datei bereits im Ordner existiert.
    Prüft: 1. Gleicher Dateiname, 2. Gleicher Inhalt (MD5-Hash)
    Gibt zurück: (is_duplicate, reason)
    """
    import hashlib
    folder_id = folder_id.strip().split('?')[0].strip()
    if not folder_id:
        return False, ''
    try:
        token = gdrive_get_token(cfg)
        # MD5 des neuen Dokuments
        pdf_bytes = base64.b64decode(pdf_b64)
        new_md5 = hashlib.md5(pdf_bytes).hexdigest()
        new_size = len(pdf_bytes)

        # 1. Gleicher Dateiname im Ordner?
        name_base = filename.rsplit('.',1)[0] if '.' in filename else filename
        # Suche nach ähnlichen Namen (ohne Datum/Nr-Suffix)
        query = f"'{folder_id}' in parents and trashed = false and mimeType = 'application/pdf'"
        url = 'https://www.googleapis.com/drive/v3/files?' + urllib.parse.urlencode({
            'q': query,
            'fields': 'files(id,name,size,md5Checksum)',
            'pageSize': '100'
        })
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        existing = resp.get('files', [])

        for f in existing:
            # Exakt gleicher Dateiname
            if f.get('name','') == filename:
                return True, f'Dateiname bereits vorhanden: {filename}'
            # Gleicher MD5-Hash (identischer Inhalt)
            if f.get('md5Checksum','') == new_md5:
                return True, f'Identischer Inhalt bereits vorhanden als: {f["name"]}'
            # Gleiche Dateigröße ± 100 Bytes (sehr ähnlich)
            existing_size = int(f.get('size', 0))
            if existing_size > 0 and abs(existing_size - new_size) < 100:
                return True, f'Sehr ähnliche Datei vorhanden: {f["name"]} ({existing_size} Bytes)'

        return False, ''
    except Exception as e:
        # Bei Fehler kein Duplikat annehmen → Upload fortsetzen
        return False, ''

def gdrive_upload_pdf(cfg, folder_id, filename, pdf_b64, check_duplicate=True):
    """Lädt ein PDF in einen Google Drive Ordner hoch — mit Duplikat-Erkennung"""
    if not folder_id:
        return False, 'Kein Ordner konfiguriert'
    folder_id = folder_id.strip().split('?')[0].strip()
    if not cfg.get('gmail_client_id') or not cfg.get('gmail_refresh_token'):
        return False, 'Gmail API nicht konfiguriert'

    # Duplikat-Prüfung
    if check_duplicate:
        is_dup, dup_reason = gdrive_check_duplicate(cfg, folder_id, filename, pdf_b64)
        if is_dup:
            return False, f'DUPLIKAT: {dup_reason}'

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


# ─── DRIVE POLLING ───────────────────────────────────────────────────────────
def gdrive_list_new_files(cfg, since_iso, folder_id=None):
    """Listet neue Dateien im Drive-Ordner seit einem Zeitpunkt — filtert Duplikate"""
    fid = (folder_id or cfg.get('gdrive_poll') or cfg.get('gdrive_in','')).strip().split('?')[0]
    if not fid:
        return []
    try:
        token = gdrive_get_token(cfg)
        try:
            dt = datetime.datetime.fromisoformat(since_iso.replace('Z','+00:00'))
            since_rfc = dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        except:
            since_rfc = '2020-01-01T00:00:00.000Z'

        query = f"'{fid}' in parents and createdTime > '{since_rfc}' and trashed = false"
        url = 'https://www.googleapis.com/drive/v3/files?' + urllib.parse.urlencode({
            'q': query,
            'fields': 'files(id,name,mimeType,createdTime,md5Checksum,size)',
            'orderBy': 'createdTime desc',
            'pageSize': '10'
        })
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        files = resp.get('files', [])

        # Duplikat-Filter: bereits verarbeitete File-IDs aus lokalem Cache
        processed_ids = set(cfg.get('processed_file_ids', []))
        new_files = []
        for f in files:
            if f['id'] not in processed_ids:
                new_files.append(f)

        return new_files
    except Exception as e:
        return []

def gdrive_download_file(cfg, file_id):
    """Lädt eine Datei aus Drive herunter als base64"""
    try:
        token = gdrive_get_token(cfg)
        req = urllib.request.Request(
            f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
            headers={'Authorization': f'Bearer {token}'}
        )
        data = urllib.request.urlopen(req, timeout=30).read()
        return base64.b64encode(data).decode()
    except:
        return None

# ─── SAMMELDOKUMENT TRENNUNG ─────────────────────────────────────────────────
def split_pdf_document(pdf_b64, filename, cfg):
    """
    Trennt ein Sammeldokument in einzelne Dokumente.
    Gibt part_data mit base64 zurück damit der Browser die Queue befüllen kann.
    """
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)

        if total_pages <= 1:
            return {'ok': True, 'parts': 1, 'filenames': [filename], 'part_data': []}

        base_name = filename.replace('.pdf','').replace('.PDF','')
        n = datetime.datetime.now()
        dat = n.strftime('%d%m%Y')
        filenames = []
        part_data = []

        # Heuristik: Trenne bei jeder Seite die deutlich weniger Text hat (Deckblatt)
        # Einfache Regel: max 3 Seiten pro Dokument, oder bei 1-2 seitigen PDFs 1 Seite je Teil
        if total_pages <= 4:
            chunk_size = 1  # Jede Seite = eigenes Dokument
        elif total_pages <= 9:
            chunk_size = 2
        else:
            chunk_size = 3

        for i in range(0, total_pages, chunk_size):
            writer = PdfWriter()
            for j in range(i, min(i + chunk_size, total_pages)):
                writer.add_page(reader.pages[j])
            out = io.BytesIO()
            writer.write(out)
            part_b64 = base64.b64encode(out.getvalue()).decode()
            part_num = len(part_data) + 1
            part_name = f"{base_name}_Dok{part_num:02d}_{dat}.pdf"
            filenames.append(part_name)
            part_data.append({'name': part_name, 'b64': part_b64, 'mime': 'application/pdf'})

            # Auch in Drive speichern
            if cfg.get('gdrive_in'):
                try:
                    gdrive_upload_pdf(cfg, cfg['gdrive_in'].strip().split('?')[0],
                                      part_name, part_b64)
                except:
                    pass

        return {
            'ok': True,
            'parts': len(part_data),
            'filenames': filenames,
            'part_data': part_data  # base64 der einzelnen Teile für Browser-Queue
        }
    except Exception as e:
        return {'ok': False, 'error': str(e), 'parts': 1, 'part_data': []}

# ─── HAUPT APP ─────────────────────────────────────────────────────
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
.hdr{background:var(--sf);border-bottom:.5px solid var(--bd);padding:0 16px;
  height:var(--hdr);display:flex;align-items:center;justify-content:space-between;
  position:fixed;top:0;left:0;right:0;z-index:200;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.logo{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:600}
.lbox{width:30px;height:30px;background:var(--ac);border-radius:7px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:16px}
.hdr-r{display:flex;align-items:center;gap:10px}
.hdr-btn{background:none;border:.5px solid var(--bd2);border-radius:var(--rs);
  padding:4px 10px;font-size:12px;cursor:pointer;display:flex;align-items:center;
  gap:5px;color:var(--tx2);font-family:inherit}
.hdr-btn:hover{background:var(--sf2)}
.layout{display:flex;height:calc(100vh - var(--hdr));margin-top:var(--hdr)}
.panel-left{width:50%;flex-shrink:0;overflow-y:auto;padding:14px 12px 60px;
  border-right:.5px solid var(--bd);background:var(--bg)}
.panel-right{width:50%;display:flex;flex-direction:column;background:#e8e8e4;overflow:hidden}
.prev-hdr{height:42px;background:var(--sf);border-bottom:.5px solid var(--bd);
  display:flex;align-items:center;justify-content:space-between;padding:0 14px;flex-shrink:0}
.prev-title{font-size:13px;font-weight:600;display:flex;align-items:center;gap:7px}
.prev-actions{display:flex;gap:5px}
.prev-btn{background:var(--sf2);border:.5px solid var(--bd2);border-radius:var(--rs);
  padding:3px 9px;font-size:12px;cursor:pointer;display:flex;align-items:center;
  gap:4px;color:var(--tx);font-family:inherit}
.prev-btn:hover{background:var(--acl);color:var(--ac)}
.prev-body{flex:1;overflow:hidden;position:relative;display:flex;align-items:center;justify-content:center}
.prev-empty{text-align:center;color:var(--tx3)}
.prev-empty i{font-size:48px;margin-bottom:12px;display:block;opacity:.35}
.prev-empty p{font-size:13px;opacity:.5}
#prev-iframe{width:100%;height:100%;border:none;display:none}
#prev-img{max-width:100%;max-height:100%;object-fit:contain;display:none;padding:16px}
.prev-foot{background:var(--sf);border-top:.5px solid var(--bd);padding:5px 14px;
  font-size:11px;color:var(--tx2);display:flex;gap:12px;align-items:center;
  flex-shrink:0;min-height:28px;flex-wrap:wrap}
@media(max-width:900px){body{height:auto;overflow:auto}.layout{flex-direction:column;height:auto}
  .panel-left{width:100%;border-right:none}.panel-right{min-height:55vh;height:55vh}}
/* TABS */
.tabs{display:flex;border-bottom:.5px solid var(--bd);margin-bottom:12px}
.tab{padding:7px 13px;font-size:13px;cursor:pointer;border:none;border-bottom:2px solid transparent;
  color:var(--tx2);font-weight:500;background:none;font-family:inherit;transition:color .15s}
.tab.on{color:var(--ac);border-bottom-color:var(--ac)}
/* CARDS */
.card{background:var(--sf);border:.5px solid var(--bd);border-radius:var(--r);padding:14px;margin-bottom:10px}
.ch{display:flex;align-items:flex-start;gap:10px;margin-bottom:12px}
.sn{width:24px;height:24px;border-radius:50%;background:var(--acl);color:var(--acd);
  font-size:11px;font-weight:600;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px}
.ct{font-size:14px;font-weight:600}.cs{font-size:11px;color:var(--tx2);margin-top:2px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px}
.full{grid-column:1/-1}
.fld{display:flex;flex-direction:column;gap:3px}
.fld label{font-size:10px;font-weight:600;color:var(--tx2);letter-spacing:.04em;text-transform:uppercase}
.fld input,.fld select,.fld textarea{width:100%;background:var(--sf2);border:.5px solid var(--bd);
  border-radius:var(--rs);padding:7px 10px;font-size:12px;color:var(--tx);font-family:inherit;outline:none;transition:border-color .15s}
.fld input:focus,.fld select:focus,.fld textarea:focus{border-color:var(--ac)}
.fld textarea{min-height:100px;resize:vertical;line-height:1.6}
.req{color:var(--re);margin-left:2px}
.btn{display:inline-flex;align-items:center;gap:5px;padding:7px 14px;border-radius:var(--rs);
  font-size:12px;font-weight:500;cursor:pointer;border:none;font-family:inherit;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.98)}
.bp{background:var(--ac);color:#fff}.bp:hover{opacity:.88}.bp:disabled{opacity:.4;cursor:not-allowed;transform:none}
.bs{background:var(--sf2);color:var(--tx);border:.5px solid var(--bd2)}.bs:hover{opacity:.82}
.brow{display:flex;gap:7px;margin-top:10px;flex-wrap:wrap;align-items:center}
.bdg{display:inline-flex;align-items:center;gap:3px;font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px}
.bok{background:var(--grl);color:var(--gr)}.berr{background:var(--rel);color:var(--re)}
.binfo{background:var(--acl);color:var(--acd)}.bwarn{background:var(--aml);color:var(--am)}
.upz{border:1.5px dashed var(--bd2);border-radius:var(--r);padding:20px 14px;text-align:center;
  cursor:pointer;position:relative;transition:background .15s,border-color .15s}
.upz:hover,.upz.drag{background:var(--sf2);border-color:var(--ac)}
.upz input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upi{font-size:26px;color:var(--tx3);margin-bottom:5px}
.upt{font-size:13px;color:var(--tx2);font-weight:500}.ups{font-size:11px;color:var(--tx3);margin-top:2px}
.fi{display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--sf2);
  border-radius:var(--rs);margin-top:6px;font-size:12px}
.fi i{color:var(--ac);font-size:16px;flex-shrink:0}
.fn{flex:1;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fsz{color:var(--tx2);font-size:11px;flex-shrink:0}
.frm{background:none;border:none;cursor:pointer;color:var(--tx3);font-size:14px;line-height:1;padding:2px}
.frm:hover{color:var(--re)}
.prw{height:3px;background:var(--bd);border-radius:3px;margin:8px 0;overflow:hidden;display:none}
.prb{height:100%;background:var(--ac);border-radius:3px;width:0%;transition:width .4s}
.sprow{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.sp{display:flex;align-items:center;gap:4px;padding:3px 9px;border-radius:var(--rs);
  font-size:11px;font-weight:500;border:.5px solid var(--bd);background:var(--sf2);color:var(--tx2)}
.sp.ok{color:var(--gr);border-color:var(--gr)}.sp.fail{color:var(--re);border-color:var(--re)}.sp.run{color:var(--ac);border-color:var(--ac)}
/* INDEXIERUNG */
.idx-box{background:var(--sf2);border:.5px solid var(--bd);border-radius:var(--rs);padding:10px;margin-top:8px;display:none}
.idx-title{font-size:11px;font-weight:600;color:var(--tx2);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;display:flex;align-items:center;gap:5px}
.idx-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.idx-item{background:var(--sf);border-radius:var(--rs);padding:6px 9px}
.idx-label{font-size:10px;color:var(--tx3);margin-bottom:2px}
.idx-val{font-size:12px;font-weight:500;color:var(--tx)}
/* DOC TYPE BADGE */
.dtype{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;margin-top:6px}
.dtype-rechnung{background:#FFF3E0;color:#E65100}
.dtype-mahnung{background:#FFEBEE;color:#C62828}
.dtype-klage{background:#FCE4EC;color:#880E4F}
.dtype-avis{background:#E8F5E9;color:#1B5E20}
.dtype-auftrag{background:#E3F2FD;color:#0D47A1}
.dtype-brief{background:#F3E5F5;color:#4A148C}
.dtype-sonstige{background:#F5F5F5;color:#424242}
/* TAGS */
.tags-wrap{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.tag{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;background:var(--acl);
  color:var(--acd);border-radius:20px;font-size:11px;font-weight:500;cursor:pointer}
.tag:hover{background:var(--rel);color:var(--re)}
.tag-input{border:.5px solid var(--bd);border-radius:20px;padding:2px 8px;font-size:11px;
  background:var(--sf2);outline:none;width:100px;font-family:inherit}
/* ANALYSE BOX */
.abox{background:var(--sf2);border:.5px solid var(--bd);border-radius:var(--rs);padding:10px;
  font-size:11px;line-height:1.9;white-space:pre-wrap;max-height:160px;overflow-y:auto;margin-top:8px;display:none;font-family:monospace}
.tgprev{background:var(--tgl);border-left:3px solid var(--tg);border-radius:0 var(--rs) var(--rs) 0;
  padding:8px 12px;font-size:11px;line-height:1.9;margin-top:8px;display:none;color:var(--tg);font-family:monospace}
.stmp{display:inline-block;border:1.5px solid #CC0000;border-radius:4px;overflow:hidden;margin-top:8px;min-width:170px}
.sh{background:#CC0000;color:#fff;font-size:9px;font-weight:700;padding:2px 8px;text-align:center;letter-spacing:.08em}
.sb{background:#FFF8F8;padding:5px 8px;font-size:10px;line-height:2;color:#222}
.sb b{min-width:55px;display:inline-block;color:#444}
.dgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.dc{border:.5px solid var(--bd);border-radius:var(--r);padding:11px 8px;cursor:pointer;text-align:center;transition:border-color .15s,background .15s}
.dc:hover{background:var(--sf2);border-color:var(--ac)}.dc.sel{border:1.5px solid var(--ac);background:var(--acl)}
.dci{font-size:20px;color:var(--ac);margin-bottom:4px}.dcl{font-size:12px;font-weight:600}.dcs{font-size:10px;color:var(--tx2);margin-top:2px}
.subp{margin-top:12px;display:none;padding-top:12px;border-top:.5px solid var(--bd)}
.li{display:flex;align-items:flex-start;gap:8px;padding:6px 0;border-bottom:.5px solid var(--bd);font-size:12px}
.li:last-child{border-bottom:none}
.lt{color:var(--tx3);font-size:10px;flex-shrink:0;min-width:40px;margin-top:1px;font-family:monospace}
.lic{font-size:13px;flex-shrink:0;margin-top:1px}
.done{display:flex;align-items:center;gap:8px;padding:11px;background:var(--grl);border-radius:var(--rs);color:var(--gr);font-size:12px;font-weight:500;margin-top:11px}
/* LANG SELECTOR */
.lang-row{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.lang-btn{padding:3px 10px;border-radius:20px;border:.5px solid var(--bd2);background:var(--sf2);
  font-size:11px;font-weight:500;cursor:pointer;color:var(--tx2);font-family:inherit}
.lang-btn.sel{background:var(--acl);color:var(--ac);border-color:var(--ac)}
/* SAMMELDOKUMENT */
.multi-doc{background:var(--aml);border:.5px solid var(--am);border-radius:var(--rs);
  padding:8px 12px;font-size:12px;color:var(--am);margin-top:8px;display:none}
/* DRIVE POLL */
.poll-status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--tx2)}
.poll-dot{width:7px;height:7px;border-radius:50%;background:var(--tx3)}
.poll-dot.active{background:var(--gr);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
@media(max-width:520px){.g2,.g3,.dgrid{grid-template-columns:1fr}}
</style></head><body>

<header class="hdr">
  <div class="logo">
    <div class="lbox"><i class="ti ti-mailbox"></i></div>
    AG Posteingang
  </div>
  <div class="hdr-r">
    <div class="poll-status">
      <div class="poll-dot" id="poll-dot"></div>
      <span id="poll-txt">Drive-Überwachung</span>
    </div>
    <button class="hdr-btn" onclick="pollDrive(true)">
      <i class="ti ti-refresh"></i> Drive prüfen
    </button>
    <a href="/logout" class="hdr-btn"><i class="ti ti-logout"></i> Abmelden</a>
  </div>
</header>

<div class="layout">

<!-- ══ LINKS: WORKFLOW ══ -->
<div class="panel-left">
<div class="tabs">
  <button class="tab on" onclick="showTab('wf',this)">Workflow</button>
  <button class="tab" onclick="showTab('arch',this)">Archiv</button>
  <button class="tab" onclick="showTab('cfg',this)">Konfiguration</button>
  <button class="tab" onclick="showTab('log',this)">Protokoll</button>
</div>

<!-- WORKFLOW TAB -->
<div id="tab-wf">

<div class="card">
  <div class="ch"><div class="sn">1</div>
    <div><div class="ct">Posteingangsscan hochladen</div>
    <div class="cs">PDF oder Bild · oder automatisch aus Google Drive</div></div>
  </div>
  <div class="upz" id="dz">
    <input type="file" id="fi" accept=".pdf,image/jpeg,image/png,image/webp" multiple onchange="addFiles(this.files)"/>
    <div class="upi"><i class="ti ti-cloud-upload"></i></div>
    <div class="upt">Datei hierher ziehen oder klicken</div>
    <div class="ups">PDF · JPG · PNG · max. 15 MB</div>
  </div>
  <div id="flist"></div>
  <!-- Sammeldokument-Warnung -->
  <div class="multi-doc" id="multi-doc">
    <i class="ti ti-files"></i> <b>Sammeldokument erkannt</b> — dieses PDF enthält möglicherweise mehrere Schreiben.
    <button class="btn bs" style="margin-top:6px;font-size:11px" onclick="splitDoc()">
      <i class="ti ti-scissors"></i> Automatisch trennen
    </button>
  </div>
</div>

<div class="card" id="c2" style="display:none">
  <div class="ch"><div class="sn">2</div>
    <div><div class="ct">Stempel · Erkennung · Klassifizierung</div>
    <div class="cs">Stempel · Dokumentenart · Indexierung · Telegram · Drive</div></div>
  </div>
  <div class="sprow">
    <div class="sp" id="sp1"><i class="ti ti-stamp" style="font-size:12px"></i> Stempel</div>
    <div class="sp" id="sp2"><i class="ti ti-cpu" style="font-size:12px"></i> KI-Analyse</div>
    <div class="sp" id="sp3"><i class="ti ti-brand-telegram" style="font-size:12px"></i> Telegram</div>
    <div class="sp" id="sp4"><i class="ti ti-brand-google-drive" style="font-size:12px"></i> Drive</div>
  </div>
  <div class="prw" id="prw"><div class="prb" id="prb"></div></div>
  <div id="stmp-prev"></div>

  <!-- Dokumentenart Badge -->
  <div id="dtype-badge"></div>

  <!-- Indexierungs-Box -->
  <div class="idx-box" id="idx-box">
    <div class="idx-title"><i class="ti ti-table"></i> Automatische Indexierung</div>
    <div class="idx-grid" id="idx-grid"></div>
    <!-- Tags -->
    <div style="margin-top:8px">
      <div class="idx-label" style="margin-bottom:4px">TAGS & LABELS</div>
      <div class="tags-wrap" id="tags-wrap">
        <input class="tag-input" id="tag-input" placeholder="+ Tag hinzufügen" onkeydown="addTag(event)"/>
      </div>
    </div>
    <!-- Sprache -->
    <div style="margin-top:8px">
      <div class="idx-label">ERKANNTE SPRACHE / ANTWORTSPRACHE</div>
      <div class="lang-row" id="lang-row">
        <span id="lang-detected" style="font-size:12px;color:var(--tx2)"></span>
        <span style="font-size:11px;color:var(--tx3)">Antwort auf:</span>
        <button class="lang-btn sel" id="lang-de" onclick="setLang('de')">🇩🇪 Deutsch</button>
        <button class="lang-btn" id="lang-en" onclick="setLang('en')">🇬🇧 English</button>
        <button class="lang-btn" id="lang-ru" onclick="setLang('ru')">🇷🇺 Русский</button>
        <button class="lang-btn" id="lang-orig" onclick="setLang('orig')">📄 Original</button>
      </div>
    </div>
  </div>

  <div class="abox" id="abox"></div>
  <div class="tgprev" id="tgp"></div>
  <div class="brow">
    <button class="btn bp" id="btn2" onclick="runAll()">
      <i class="ti ti-bolt"></i> Stempel · Analysieren · Telegram · Drive
    </button>
  </div>
</div>

<div class="card" id="c3" style="display:none">
  <div class="ch"><div class="sn">3</div>
    <div><div class="ct">Bearbeitungsschritt</div>
    <div class="cs">Was soll mit diesem Schreiben geschehen?</div></div>
  </div>
  <div class="dgrid">
    <div class="dc" id="d-wv" onclick="selD('wv')">
      <div class="dci"><i class="ti ti-calendar-event"></i></div>
      <div class="dcl">Wiedervorlage</div><div class="dcs">Datum & Priorität</div>
    </div>
    <div class="dc" id="d-ma" onclick="selD('ma')">
      <div class="dci"><i class="ti ti-mail-forward"></i></div>
      <div class="dcl">E-Mail Antwort</div><div class="dcs">KI formuliert · mehrsprachig</div>
    </div>
    <div class="dc" id="d-ab" onclick="selD('ab')">
      <div class="dci"><i class="ti ti-folder-check"></i></div>
      <div class="dcl">Ablegen</div><div class="dcs">Drive-Archivierung</div>
    </div>
    <div class="dc" id="d-fw" onclick="selD('fw')">
      <div class="dci"><i class="ti ti-send"></i></div>
      <div class="dcl">Weiterleiten</div><div class="dcs">E-Mail an Kollegen</div>
    </div>
  </div>

  <!-- WIEDERVORLAGE -->
  <div class="subp" id="p-wv">
    <div class="g2">
      <div class="fld"><label>Datum</label><input type="date" id="wv-d"/></div>
      <div class="fld"><label>Priorität</label>
        <select id="wv-p"><option>Normal</option><option>Dringend</option><option>Niedrig</option></select>
      </div>
      <div class="fld full"><label>Notiz</label><input type="text" id="wv-n" placeholder="Interne Notiz..."/></div>
    </div>
  </div>

  <!-- E-MAIL -->
  <div class="subp" id="p-ma">
    <div class="g2">
      <div class="fld full"><label>Empfänger <span class="req">*</span></label>
        <input type="email" id="m-to" placeholder="empfaenger@beispiel.de"/></div>
      <div class="fld full"><label>CC</label>
        <input type="email" id="m-cc" placeholder="kopie@beispiel.de"/></div>
      <div class="fld full"><label>Betreff <span class="req">*</span></label>
        <input type="text" id="m-su" placeholder="Re: ..."/></div>
      <div class="fld full">
        <label>E-Mail-Text <span class="req">*</span>
          <span style="font-weight:400;text-transform:none;font-size:10px"> — KI-Entwurf, bitte prüfen</span>
        </label>
        <textarea id="m-bo" placeholder="Klicke auf Entwurf generieren..."></textarea>
      </div>
    </div>
    <!-- Sprache für Antwort -->
    <div style="margin-top:8px;font-size:11px;color:var(--tx2)">
      Antwortsprache:
      <button class="lang-btn sel" id="rep-de" onclick="setRepLang('de')" style="margin-left:4px">🇩🇪 DE</button>
      <button class="lang-btn" id="rep-en" onclick="setRepLang('en')">🇬🇧 EN</button>
      <button class="lang-btn" id="rep-ru" onclick="setRepLang('ru')">🇷🇺 RU</button>
      <button class="lang-btn" id="rep-orig" onclick="setRepLang('orig')">📄 Original</button>
    </div>
    <div class="brow">
      <button class="btn bs" onclick="genDraft()"><i class="ti ti-wand"></i> Entwurf generieren</button>
      <span id="dst"></span>
    </div>
  </div>

  <!-- WEITERLEITEN -->
  <div class="subp" id="p-fw">
    <div class="g2">
      <div class="fld full"><label>Weiterleiten an <span class="req">*</span></label>
        <input type="email" id="fw-to" placeholder="kollege@domain.de"/></div>
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

<!-- WARTESCHLANGE ANZEIGE -->
<div id="queue-bar" style="display:none;margin-top:8px">
  <div style="background:var(--acl);border:.5px solid var(--ac);border-radius:var(--rs);padding:10px 14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
      <span style="font-size:12px;font-weight:600;color:var(--acd)">
        <i class="ti ti-list-numbers"></i> Dokumenten-Warteschlange
      </span>
      <span id="queue-count" style="font-size:11px;color:var(--tx2)"></span>
    </div>
    <div id="queue-list" style="display:flex;flex-direction:column;gap:4px"></div>
    <div class="brow" style="margin-top:8px">
      <button class="btn bp" id="btn-next" onclick="processNext()">
        <i class="ti ti-player-skip-forward"></i> Nächstes Dokument bearbeiten
      </button>
      <button class="btn bs" onclick="clearQueue()">
        <i class="ti ti-x"></i> Warteschlange leeren
      </button>
    </div>
  </div>
</div>

<div id="cnew" style="display:none;margin-top:4px">
  <button class="btn bs" onclick="resetAll()"><i class="ti ti-plus"></i> Neues Schreiben</button>
</div>
</div><!-- /tab-wf -->

<!-- ARCHIV TAB -->
<div id="tab-arch" style="display:none">
<div class="card">
  <div class="ch"><div class="sn"><i class="ti ti-archive" style="font-size:11px"></i></div>
    <div><div class="ct">Archiv & Suche</div><div class="cs">Alle verarbeiteten Dokumente</div></div>
  </div>
  <div class="g2" style="margin-bottom:10px">
    <div class="fld full">
      <label>Suche</label>
      <input type="text" id="arch-q" placeholder="Versender, Betreff, Betrag, Tag..." onkeyup="searchArch()"/>
    </div>
    <div class="fld"><label>Dokumentenart</label>
      <select id="arch-type" onchange="searchArch()">
        <option value="">Alle</option>
        <option>Rechnung</option><option>Mahnung</option><option>Klage</option>
        <option>Avis</option><option>Auftragsbestätigung</option><option>Brief</option><option>Sonstige</option>
      </select>
    </div>
    <div class="fld"><label>Zeitraum</label>
      <select id="arch-period" onchange="searchArch()">
        <option value="">Alle</option>
        <option value="7">Letzte 7 Tage</option>
        <option value="30">Letzte 30 Tage</option>
        <option value="90">Letzte 90 Tage</option>
      </select>
    </div>
  </div>
  <div id="arch-list">
    <div style="text-align:center;color:var(--tx3);padding:20px;font-size:13px">
      <i class="ti ti-database" style="font-size:28px;display:block;margin-bottom:8px;opacity:.4"></i>
      Noch keine archivierten Dokumente
    </div>
  </div>
</div>
</div>

<!-- KONFIGURATION TAB -->
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
    <div class="fld full"><label>Gmail-Adresse <span class="req">*</span></label>
      <input type="email" id="c-mu" placeholder="buero@gmail.com"/></div>
    <div class="fld full"><label>Gmail Client ID <span class="req">*</span></label>
      <input type="text" id="c-ci" placeholder="xxxxxxxxx.apps.googleusercontent.com"/></div>
    <div class="fld full"><label>Gmail Client Secret <span class="req">*</span></label>
      <input type="password" id="c-cs" placeholder="GOCSPX-..."/></div>
    <div class="fld full"><label>Gmail Refresh Token <span class="req">*</span></label>
      <input type="password" id="c-rt" placeholder="1//0g..."/></div>
    <div class="fld full"><label><i class="ti ti-brand-google-drive" style="color:var(--ac)"></i> Posteingang-Ordner-ID</label>
      <input type="text" id="c-gi" placeholder="Google Drive Ordner-ID"/></div>
    <div class="fld full"><label><i class="ti ti-brand-google-drive" style="color:var(--ac)"></i> Postausgang-Ordner-ID</label>
      <input type="text" id="c-go" placeholder="Google Drive Ordner-ID"/></div>
    <div class="fld full"><label><i class="ti ti-brand-google-drive" style="color:var(--ac)"></i> Drive-Überwachung Ordner-ID</label>
      <input type="text" id="c-gp" placeholder="Ordner der auf neue Dokumente überwacht wird"/></div>
    <div class="fld full"><label>Absender-Name</label>
      <input type="text" id="c-mn" placeholder="AG Posteingang"/></div>
    <div class="fld full"><label>Standard-Antwortsprache</label>
      <select id="c-lang">
        <option value="de">Deutsch</option>
        <option value="en">English</option>
        <option value="ru">Русский</option>
        <option value="orig">Sprache des Eingangs</option>
      </select>
    </div>
  </div>
  <!-- Dokumentenarten -->
  <div style="margin-top:12px;padding-top:12px;border-top:.5px solid var(--bd)">
    <div style="font-size:11px;font-weight:600;color:var(--tx2);text-transform:uppercase;margin-bottom:8px">
      Dokumentenarten (Stammdaten)
    </div>
    <div id="dtypes-list" class="tags-wrap"></div>
    <div style="margin-top:6px;display:flex;gap:6px">
      <input type="text" id="dtype-new" placeholder="Neue Dokumentenart..." style="font-size:12px;padding:5px 9px;border:.5px solid var(--bd);border-radius:var(--rs);background:var(--sf2);outline:none;flex:1"/>
      <button class="btn bs" onclick="addDtype()"><i class="ti ti-plus"></i></button>
    </div>
  </div>
  <div class="brow">
    <button class="btn bp" onclick="saveCfg()"><i class="ti ti-device-floppy"></i> Speichern</button>
    <button class="btn bs" onclick="testTG()"><i class="ti ti-brand-telegram"></i> Telegram</button>
    <button class="btn bs" onclick="testMail()"><i class="ti ti-mail"></i> E-Mail</button>
    <button class="btn bs" onclick="testDrive()"><i class="ti ti-brand-google-drive"></i> Drive</button>
    <button class="btn bs" onclick="debugDrive()"><i class="ti ti-bug"></i> Debug</button>
    <span id="cfgmsg"></span>
  </div>
</div>
</div>

<!-- PROTOKOLL TAB -->
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

<!-- ══ RECHTS: DOKUMENTVORSCHAU ══ -->
<div class="panel-right">
  <div class="prev-hdr">
    <div class="prev-title">
      <i class="ti ti-eye" style="color:var(--ac)"></i>
      <span>Dokumentvorschau</span>
      <span id="prev-filename" style="color:var(--tx2);font-weight:400;font-size:11px"></span>
    </div>
    <div class="prev-actions">
      <button class="prev-btn" id="prev-toggle" onclick="toggleFullscreen()" style="display:none">
        <i class="ti ti-arrows-maximize"></i>
      </button>
      <button class="prev-btn" id="prev-dl" onclick="downloadPrev()" style="display:none">
        <i class="ti ti-download"></i> Download
      </button>
    </div>
  </div>
  <div class="prev-body" id="prev-body">
    <div class="prev-empty" id="prev-empty">
      <i class="ti ti-file-description"></i>
      <p>Scan hochladen um Vorschau zu sehen</p>
    </div>
    <iframe id="prev-iframe" title="Vorschau"></iframe>
    <img id="prev-img" alt="Vorschau"/>
  </div>
  <div class="prev-foot" id="prev-foot">
    <span style="color:var(--tx3);font-size:11px">Kein Dokument geladen</span>
  </div>
</div>

</div><!-- /layout -->

<script>
// ═══ STATE ════
let files=[],analysis='',curD=null,nr='',stPdfB64='',origB64='',origMime='';
let tags=[],repLang='de',detectedLang='de';
let archive=JSON.parse(localStorage.getItem('agp_archive')||'[]');
let docTypes=JSON.parse(localStorage.getItem('agp_dtypes')||'["Rechnung","Mahnung","Klage","Avis","Auftragsbestätigung","Brief","Sonstige"]');
let indexData={};
let pollTimer=null;

// ═══ DOKUMENTEN-WARTESCHLANGE ════
let docQueue=[]; // [{name, b64, mime, index, total}]
let queueActive=false;

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
  $('logbox').appendChild(d);d.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function showTab(n,btn){
  ['wf','arch','cfg','log'].forEach(t=>{const e=$('tab-'+t);if(e)e.style.display='none'});
  $('tab-'+n).style.display='block';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  if(btn)btn.classList.add('on');
}

// ═══ VORSCHAU ════
function showPreview(b64,mime,filename,isStamped){
  const iframe=$('prev-iframe'),img=$('prev-img'),empty=$('prev-empty');
  empty.style.display='none';
  $('prev-filename').textContent=filename?'· '+filename:'';
  $('prev-toggle').style.display='flex';$('prev-dl').style.display='flex';
  if(mime==='application/pdf'||!mime.startsWith('image/')){
    iframe.style.display='block';img.style.display='none';
    iframe.src='data:application/pdf;base64,'+b64;
  } else {
    img.style.display='block';iframe.style.display='none';
    img.src='data:'+mime+';base64,'+b64;
  }
  $('prev-foot').innerHTML=
    `<span style="color:${isStamped?'var(--gr)':'var(--ac)'}">
      <i class="ti ti-${isStamped?'stamp':'file'}"></i> ${isStamped?'Gestempelt':'Original'}
    </span>
    <span><i class="ti ti-file-type-pdf"></i> ${filename||'Dokument'}</span>`;
}
function toggleFullscreen(){
  if(!document.fullscreenElement)$('prev-body').requestFullscreen?.();
  else document.exitFullscreen?.();
}
function downloadPrev(){
  if(!stPdfB64&&!origB64)return;
  const a=document.createElement('a');
  a.href='data:application/pdf;base64,'+(stPdfB64||origB64);
  a.download=`Posteingang_${nr||'Dokument'}.pdf`;a.click();
}

// ═══ DOKUMENTENARTEN ════
// ═══ WARTESCHLANGE ════
function addToQueue(items){
  // items = [{name, b64, mime}]
  items.forEach((item,i)=>{
    docQueue.push({...item, index:docQueue.length+1, total:docQueue.length+items.length});
  });
  renderQueue();
}

function renderQueue(){
  const bar=$('queue-bar');
  if(docQueue.length===0){bar.style.display='none';return;}
  bar.style.display='block';
  $('queue-count').textContent=`${docQueue.length} Dokument(e) wartend`;
  $('queue-list').innerHTML=docQueue.map((d,i)=>`
    <div style="display:flex;align-items:center;gap:8px;padding:5px 8px;
      background:${i===0?'var(--acl)':'var(--sf2)'};border-radius:var(--rs);font-size:12px;
      border:.5px solid ${i===0?'var(--ac)':'var(--bd)'}">
      <span style="width:20px;height:20px;border-radius:50%;background:${i===0?'var(--ac)':'var(--bd2)'};
        color:${i===0?'#fff':'var(--tx2)'};display:flex;align-items:center;justify-content:center;
        font-size:10px;font-weight:600;flex-shrink:0">${i+1}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
        font-weight:${i===0?'600':'400'}">${d.name}</span>
      ${i===0?'<span style="font-size:10px;color:var(--ac);font-weight:500">← Aktuell</span>':''}
      <button onclick="removeFromQueue(${i})" style="background:none;border:none;cursor:pointer;
        color:var(--tx3);font-size:13px;padding:1px"><i class="ti ti-x"></i></button>
    </div>`).join('');
}

function removeFromQueue(i){
  docQueue.splice(i,1);
  renderQueue();
}

function clearQueue(){
  if(!confirm('Warteschlange wirklich leeren?'))return;
  docQueue=[];renderQueue();
  lg('ti-trash','var(--tx2)','Warteschlange geleert');
}

async function processNext(){
  if(docQueue.length===0){$('queue-bar').style.display='none';return;}
  const next=docQueue.shift(); // erstes Element nehmen
  renderQueue();
  lg('ti-player-skip-forward','var(--ac)',
    `Starte Dokument ${next.name} (noch ${docQueue.length} in der Warteschlange)`);
  // Reset und neues Dokument laden
  resetAll();
  // Datei aus base64 wiederherstellen
  const mime=next.mime||'application/pdf';
  const bytes=Uint8Array.from(atob(next.b64),c=>c.charCodeAt(0));
  const blob=new Blob([bytes],{type:mime});
  const file=new File([blob],next.name,{type:mime});
  addFiles([file]);
  // Automatisch starten
  await new Promise(r=>setTimeout(r,500));
  await runAll();
}

function renderDtypes(){
  $('dtypes-list').innerHTML=docTypes.map(t=>`
    <span class="tag" onclick="removeDtype('${t}')">
      ${t} <i class="ti ti-x" style="font-size:10px"></i>
    </span>`).join('');
}
function addDtype(){
  const v=$('dtype-new').value.trim();
  if(v&&!docTypes.includes(v)){docTypes.push(v);localStorage.setItem('agp_dtypes',JSON.stringify(docTypes));renderDtypes();}
  $('dtype-new').value='';
  // Update selects
  updateDtypeSelects();
}
function removeDtype(t){
  docTypes=docTypes.filter(x=>x!==t);
  localStorage.setItem('agp_dtypes',JSON.stringify(docTypes));
  renderDtypes();updateDtypeSelects();
}
function updateDtypeSelects(){
  const sel=$('arch-type');
  const cur=sel.value;
  sel.innerHTML='<option value="">Alle</option>'+docTypes.map(t=>`<option>${t}</option>`).join('');
  sel.value=cur;
}

// ═══ TAGS ════
function addTag(e){
  if(e.key==='Enter'){
    const v=e.target.value.trim();
    if(v&&!tags.includes(v)){tags.push(v);renderTags();}
    e.target.value='';
  }
}
function removeTag(t){tags=tags.filter(x=>x!==t);renderTags();}
function renderTags(){
  const wrap=$('tags-wrap');
  wrap.innerHTML=tags.map(t=>`
    <span class="tag" onclick="removeTag('${t}')">
      ${t} <i class="ti ti-x" style="font-size:10px"></i>
    </span>`).join('');
  const inp=document.createElement('input');
  inp.className='tag-input';inp.id='tag-input';inp.placeholder='+ Tag hinzufügen';
  inp.addEventListener('keydown',addTag);
  wrap.appendChild(inp);
}

// ═══ SPRACHE ════
function setLang(lang){
  repLang=lang;
  ['de','en','ru','orig'].forEach(l=>{
    const b=$('lang-'+l);if(b)b.classList.toggle('sel',l===lang);
  });
}
function setRepLang(lang){
  repLang=lang;
  ['de','en','ru','orig'].forEach(l=>{
    const b=$('rep-'+l);if(b)b.classList.toggle('sel',l===lang);
  });
}
const LANG_NAMES={de:'Deutsch',en:'English',ru:'Русский',orig:'Original'};

// ═══ DOKUMENTENART ERKENNUNG ════
const DOCTYPE_COLORS={
  'Rechnung':'dtype-rechnung','Mahnung':'dtype-mahnung','Klage':'dtype-klage',
  'Avis':'dtype-avis','Auftragsbestätigung':'dtype-auftrag','Brief':'dtype-brief',
  'Sonstige':'dtype-sonstige'
};
function showDocType(dtype){
  const cls=DOCTYPE_COLORS[dtype]||'dtype-sonstige';
  $('dtype-badge').innerHTML=`<span class="dtype ${cls}"><i class="ti ti-file-description"></i> ${dtype}</span>`;
}

// ═══ INDEXIERUNG ════
function showIndexData(data){
  indexData=data;
  const fields=[
    ['ABSENDER','absender'],['DOKUMENTENART','dtype'],['DATUM','datum'],
    ['BETRAG','betrag'],['BELEGNUMMER','belegnummer'],['ZAHLUNGSZIEL','zahlungsziel'],
    ['SKONTOSATZ','skonto'],['FRIST','frist'],['DRINGLICHKEIT','dringlichkeit']
  ];
  $('idx-grid').innerHTML=fields.map(([label,key])=>{
    const val=data[key]||'–';
    if(val==='–')return'';
    return`<div class="idx-item"><div class="idx-label">${label}</div><div class="idx-val">${val}</div></div>`;
  }).filter(Boolean).join('');
  $('idx-box').style.display='block';
  // Sprache anzeigen
  if(data.sprache){
    detectedLang=data.sprache_code||'de';
    $('lang-detected').textContent=`Erkannt: ${data.sprache}`;
  }
  // Auto-Tags vorschlagen
  if(data.tags_vorschlag){
    data.tags_vorschlag.split(',').forEach(t=>{
      t=t.trim();if(t&&!tags.includes(t))tags.push(t);
    });
    renderTags();
  }
  showDocType(data.dtype||'Sonstige');
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
    if(c.gdrive_poll)$('c-gp').value=c.gdrive_poll;
    if(c.default_lang)$('c-lang').value=c.default_lang;
    if(c.has_api_key)$('c-ak').placeholder='✓ gesetzt';
    if(c.has_tg_token)$('c-tt').placeholder='✓ gesetzt';
    if(c.has_gmail_client_id)$('c-ci').placeholder='✓ gesetzt';
    if(c.has_gmail_client_secret)$('c-cs').placeholder='✓ gesetzt';
    if(c.has_gmail_refresh_token)$('c-rt').placeholder='✓ gesetzt';
    repLang=c.default_lang||'de';
    setRepLang(repLang);
  }catch(e){}
}
async function saveCfg(){
  const body={
    api_key:$('c-ak').value.trim(),tg_token:$('c-tt').value.trim(),
    tg_chat_id:$('c-tc').value.trim(),smtp_user:$('c-mu').value.trim(),
    gmail_client_id:$('c-ci').value.trim(),gmail_client_secret:$('c-cs').value.trim(),
    gmail_refresh_token:$('c-rt').value.trim(),
    sender_name:$('c-mn').value.trim()||'AG Posteingang',
    gdrive_in:$('c-gi').value.trim(),gdrive_out:$('c-go').value.trim(),
    gdrive_poll:$('c-gp').value.trim(),
    default_lang:$('c-lang').value
  };
  Object.keys(body).forEach(k=>{if(!body[k])delete body[k]});
  const r=await fetch('/config/save',{method:'POST',headers:J,body:JSON.stringify(body)});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok"><i class="ti ti-check"></i> Gespeichert</span>':'<span class="bdg berr">Fehler</span>';
  setTimeout(()=>$('cfgmsg').innerHTML='',3000);
}
async function testTG(){
  const r=await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:'✅ Telegram-Test OK!'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok">Telegram OK!</span>':`<span class="bdg berr">${d.error||'Fehler'}</span>`;
  setTimeout(()=>$('cfgmsg').innerHTML='',4000);
}
async function testMail(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Sende...</span>';
  const cfg=await(await fetch('/config')).json();
  const to=cfg.smtp_user||$('c-mu').value;
  if(!to){$('cfgmsg').innerHTML='<span class="bdg berr">Gmail-Adresse eintragen</span>';return}
  const r=await fetch('/mail',{method:'POST',headers:J,body:JSON.stringify({to,subject:'Test AG Posteingang',body:'Verbindungstest OK.'})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok">Mail gesendet!</span>':`<span class="bdg berr">${d.error||'Fehler'}</span>`;
  setTimeout(()=>$('cfgmsg').innerHTML='',5000);
}
async function testDrive(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Teste...</span>';
  const r=await fetch('/drive/save',{method:'POST',headers:J,body:JSON.stringify({folder:'in',filename:'Test.txt',text:`Test ${tod()} ${now()}`})});
  const d=await r.json();
  $('cfgmsg').innerHTML=d.ok?'<span class="bdg bok">Drive OK!</span>':`<span class="bdg berr">${d.error||d.msg}</span>`;
  setTimeout(()=>$('cfgmsg').innerHTML='',6000);
}
async function debugDrive(){
  $('cfgmsg').innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Debug...</span>';
  const r=await fetch('/drive/debug',{method:'POST',headers:J,body:JSON.stringify({})});
  const d=await r.json();
  let msg='DRIVE DEBUG:\n\n';
  msg+=`Client ID: ${d.client_id_laenge||0} Zeichen\n`;
  msg+=`Refresh Token: ${d.refresh_token_laenge||0} Zeichen | Start: ${d.refresh_token_start||'LEER'}\n`;
  msg+=`Leerzeichen: ${d.refresh_token_leerzeichen?'JA ← PROBLEM!':'Nein'}\n\n`;
  if(d.token_fehler)msg+=`TOKEN FEHLER: ${JSON.stringify(d.token_fehler)}\n\n`;
  msg+=`Token: ${d.token||'FEHLER'}\nScope: ${d.scope||'–'}\nKonto: ${d.konto||'–'}\nDrive: ${d.drive_zugriff||'FEHLER'}\n\n`;
  if(d.Posteingang)msg+=`Posteingang: ${d.Posteingang.ok?'✓ OK':'✗ '+d.Posteingang.info}\n`;
  if(d.Postausgang)msg+=`Postausgang: ${d.Postausgang.ok?'✓ OK':'✗ '+d.Postausgang.info}\n`;
  if(d.fehler)msg+=`\nFEHLER: ${d.fehler}`;
  alert(msg);
  $('cfgmsg').innerHTML='';
}

// ═══ DATEIEN ════
function addFiles(fl){
  Array.from(fl).forEach(f=>{if(!files.find(x=>x.name===f.name))files.push(f)});
  renderFiles();
  if(files.length){
    $('c2').style.display='block';
    const f=files[0];
    toB64(f).then(b64=>{origB64=b64;origMime=f.type;showPreview(b64,f.type,f.name,false);});
    // Sammeldokument-Check (einfach: PDF > 500KB könnte mehrere Dokumente enthalten)
    if(f.type==='application/pdf'&&f.size>500*1024){
      $('multi-doc').style.display='block';
    }
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
    $('prev-iframe').style.display='none';$('prev-img').style.display='none';
    $('prev-empty').style.display='block';$('prev-filename').textContent='';
    $('prev-toggle').style.display='none';$('prev-dl').style.display='none';
    $('multi-doc').style.display='none';
    $('prev-foot').innerHTML='<span style="color:var(--tx3);font-size:11px">Kein Dokument</span>';
  }
}
async function toB64(f){
  return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(r.result.split(',')[1]);r.onerror=rej;r.readAsDataURL(f)});
}

// ═══ SAMMELDOKUMENT TRENNUNG ════
async function splitDoc(){
  if(!files.length)return;
  lg('ti-scissors','var(--ac)','Trenne Sammeldokument...');
  const f=files[0];const b64=await toB64(f);
  const r=await fetch('/doc/split',{method:'POST',headers:J,
    body:JSON.stringify({pdf_b64:b64,filename:f.name})});
  const d=await r.json();
  if(d.ok&&d.parts>1){
    lg('ti-files','var(--gr)',`${d.parts} Dokumente erkannt und getrennt ✓`);
    $('multi-doc').style.display='none';
    // Alle getrennten Teile in die Warteschlange
    const queueItems=d.part_data||[];
    if(queueItems.length>0){
      // Erstes Dokument sofort laden, Rest in Queue
      const first=queueItems.shift();
      if(queueItems.length>0){
        addToQueue(queueItems);
        lg('ti-list-numbers','var(--ac)',
          `${queueItems.length} weitere Dokumente in Warteschlange eingereiht`);
      }
      // Erstes direkt laden
      resetAll();
      const bytes=Uint8Array.from(atob(first.b64),c=>c.charCodeAt(0));
      const blob=new Blob([bytes],{type:'application/pdf'});
      const file=new File([blob],first.name,{type:'application/pdf'});
      addFiles([file]);
      lg('ti-file','var(--ac)',`Bearbeite zuerst: ${first.name}`);
    } else {
      alert(`${d.parts} Dokumente wurden in Drive gespeichert:\n${d.filenames?.join('\n')||''}`);
    }
  } else {
    lg('ti-info-circle','var(--am)','Kein Sammeldokument erkannt – wird als einzelnes Dokument verarbeitet');
    $('multi-doc').style.display='none';
  }
}

// ═══ STEMPEL (pdf-lib) ════
async function stampBrowser(rawB64,isImg,mime,einNr,datum,uhrzeit){
  const {PDFDocument,rgb,StandardFonts}=PDFLib;
  let doc;
  if(isImg){
    doc=await PDFDocument.create();
    const bytes=Uint8Array.from(atob(rawB64),c=>c.charCodeAt(0));
    const img=(mime==='image/jpeg'||mime==='image/jpg')?await doc.embedJpg(bytes):await doc.embedPng(bytes);
    const {width:iw,height:ih}=img.scale(1);const sc=Math.min(595/iw,842/ih,1);
    const pg=doc.addPage([595,842]);
    pg.drawImage(img,{x:(595-iw*sc)/2,y:(842-ih*sc)/2,width:iw*sc,height:ih*sc});
  } else {
    doc=await PDFDocument.load(Uint8Array.from(atob(rawB64),c=>c.charCodeAt(0)),{ignoreEncryption:true});
  }
  const bold=await doc.embedFont(StandardFonts.HelveticaBold);
  const reg=await doc.embedFont(StandardFonts.Helvetica);
  const pg=doc.getPages()[0];
  const {width:pw,height:ph}=pg.getSize();
  const sw=200,sh=72,mx=14,sx=pw-sw-mx,sy=ph-sh-mx;
  pg.drawRectangle({x:sx,y:sy,width:sw,height:sh,color:rgb(1,.97,.97),borderColor:rgb(.8,0,0),borderWidth:1.5});
  pg.drawRectangle({x:sx,y:sy+sh-22,width:sw,height:22,color:rgb(.8,0,0)});
  pg.drawText('EINGEGANGEN',{x:sx+sw/2-bold.widthOfTextAtSize('EINGEGANGEN',9)/2,y:sy+sh-15,size:9,font:bold,color:rgb(1,1,1)});
  let ry=sy+sh-34;
  for(const [l,v] of [['Datum:',datum],['Uhrzeit:',uhrzeit+' Uhr'],['Eingangs-Nr.:',einNr]]){
    pg.drawText(l,{x:sx+7,y:ry,size:7.5,font:bold,color:rgb(.25,.25,.25)});
    pg.drawText(v,{x:sx+68,y:ry,size:7.5,font:reg,color:rgb(.05,.05,.05)});
    ry-=14;
  }
  return btoa(String.fromCharCode(...new Uint8Array(await doc.save())));
}

// ═══ CLAUDE API ════
async function callClaude(messages){
  const cfg=await(await fetch('/config')).json();
  if(!cfg.api_key&&!cfg.has_api_key)throw new Error('API-Key fehlt!');
  const r=await fetch('https://api.anthropic.com/v1/messages',{
    method:'POST',
    headers:{'Content-Type':'application/json','x-api-key':cfg.api_key||'',
      'anthropic-version':'2023-06-01','anthropic-dangerous-direct-browser-access':'true'},
    body:JSON.stringify({model:'claude-sonnet-4-6',max_tokens:1800,messages})
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
    const f=files[0];const rawB64=await toB64(f);const isImg=f.type.startsWith('image/');

    // 1. STEMPEL
    lg('ti-stamp','var(--ac)','Setze Eingangsstempel...');
    stPdfB64=await stampBrowser(rawB64,isImg,f.type,nr,datum,uhrzeit);
    setSP('sp1','ok');lg('ti-stamp','var(--gr)','Stempel gesetzt ✓');setP(18);
    showPreview(stPdfB64,'application/pdf',f.name,true);
    $('stmp-prev').innerHTML=`
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap">
        <div class="stmp">
          <div class="sh">EINGEGANGEN</div>
          <div class="sb"><b>Datum:</b>${datum}<br><b>Uhrzeit:</b>${uhrzeit} Uhr<br><b>Nr.:</b>${nr}</div>
        </div>
        <a href="data:application/pdf;base64,${stPdfB64}" download="Posteingang_${nr}.pdf"
           style="display:inline-flex;align-items:center;gap:5px;color:var(--ac);font-size:11px;
             font-weight:500;padding:6px 11px;background:var(--acl);border-radius:var(--rs);
             border:.5px solid var(--ac);text-decoration:none">
          <i class="ti ti-download"></i> PDF
        </a>
      </div>`;

    // Server: Stempel + Drive Posteingang
    const sr=await fetch('/stamp',{method:'POST',headers:J,body:JSON.stringify({pdf_b64:rawB64,mime:f.type,nr})});
    const sd=await sr.json();
    if(sd.ok){
      if(sd.drive_ok){
        setSP('sp4','ok');lg('ti-brand-google-drive','var(--gr)','Posteingang in Drive ✓');
      } else if(sd.drive_msg&&sd.drive_msg.startsWith('DUPLIKAT')){
        setSP('sp4','fail');
        lg('ti-copy-off','var(--re)','⚠️ '+sd.drive_msg+' – nicht doppelt gespeichert');
        $('stmp-prev').insertAdjacentHTML('beforeend',
          `<div style="background:var(--rel);border:.5px solid var(--re);border-radius:var(--rs);
            padding:8px 12px;font-size:12px;color:var(--re);margin-top:8px;display:flex;align-items:center;gap:6px">
            <i class="ti ti-copy-off"></i>
            <b>Duplikat erkannt</b> — ${sd.drive_msg.replace('DUPLIKAT: ','')}
          </div>`);
      } else if(sd.drive_msg&&sd.drive_msg!=='Drive nicht konfiguriert'){
        setSP('sp4','fail');lg('ti-alert-triangle','var(--am)','Drive: '+sd.drive_msg);
      }
    }
    setP(30);

    // 2. KI-ANALYSE (erweitert: Dokumentenart + Indexierung + Sprache)
    lg('ti-cpu','var(--ac)','KI-Analyse & Klassifizierung...');setSP('sp2','run');
    const dtypeList=docTypes.join(', ');
    const mc=[];
    if(isImg)mc.push({type:'image',source:{type:'base64',media_type:f.type,data:rawB64}});
    else mc.push({type:'document',source:{type:'base64',media_type:'application/pdf',data:rawB64}});
    mc.push({type:'text',text:`Analysiere dieses Eingangsschreiben. Antworte NUR mit diesen Feldern (JSON-Format):

{
  "absender": "Name/Firma/Behörde",
  "dtype": "Dokumentenart aus: ${dtypeList}",
  "datum": "Briefdatum TT.MM.JJJJ oder leer",
  "betreff": "1-2 Sätze worum es geht",
  "betrag": "Nettobetrag als Zahl oder leer",
  "belegnummer": "Rechnungs-/Belegnummer oder leer",
  "zahlungsziel": "Datum oder leer",
  "skonto": "Skontosatz in % oder leer",
  "frist": "Fristdatum oder leer",
  "dringlichkeit": "Hoch oder Mittel oder Niedrig",
  "empfehlung": "Wiedervorlage oder Antwort oder Ablage oder Weiterleiten",
  "hinweis": "Wichtige Details",
  "sprache": "Sprache des Dokuments auf Deutsch, z.B. Deutsch, Englisch, Russisch",
  "sprache_code": "de oder en oder ru oder andere",
  "tags_vorschlag": "kommaseparierte Tags die passen, z.B. Finanzamt,Steuer",
  "ist_sammlung": false,
  "anzahl_dokumente": 1
}

Antworte NUR mit dem JSON-Objekt, kein Text davor oder danach.`});

    setP(50);
    const rawAnalysis=await callClaude([{role:'user',content:mc}]);
    // JSON parsen
    let parsed={};
    try{
      const clean=rawAnalysis.replace(/```json|```/g,'').trim();
      parsed=JSON.parse(clean);
    }catch(e){
      // Fallback: Text-Analyse
      parsed={betreff:rawAnalysis,dtype:'Sonstige',dringlichkeit:'Mittel',empfehlung:'Ablage'};
    }
    analysis=JSON.stringify(parsed,null,2);
    indexData=parsed;
    showIndexData(parsed);
    setSP('sp2','ok');lg('ti-file-text','var(--gr)',`Analyse: ${parsed.dtype||'Sonstige'} · ${parsed.dringlichkeit||'Mittel'} ✓`);

    // Sammeldokument-Hinweis
    if(parsed.ist_sammlung||parsed.anzahl_dokumente>1){
      $('multi-doc').style.display='block';
    }

    const abox=$('abox');abox.style.display='block';
    abox.textContent=
      `╔══ EINGANGSPROTOKOLL ══════════════════╗\n`+
      `  Nr.:     ${nr}\n`+
      `  Eingang: ${datum}  ${uhrzeit} Uhr\n`+
      `  Datei:   ${f.name}\n`+
      `  Art:     ${parsed.dtype||'–'}\n`+
      `  Absender: ${parsed.absender||'–'}\n`+
      `╚═══════════════════════════════════════╝\n\n`+
      `Betreff: ${parsed.betreff||'–'}\n`+
      `Betrag: ${parsed.betrag||'–'} | Frist: ${parsed.frist||parsed.zahlungsziel||'–'}\n`+
      `Empfehlung: ${parsed.empfehlung||'–'} | Sprache: ${parsed.sprache||'–'}`;
    setP(65);

    // 3. TELEGRAM
    lg('ti-brand-telegram','var(--ac)','Sende Telegram...');setSP('sp3','run');
    const tgTxt=
      `📬 <b>POSTEINGANG – AG</b>\n`+
      `━━━━━━━━━━━━━━━━━━━━\n`+
      `<b>Nr.:</b>      ${nr}\n`+
      `<b>Eingang:</b> ${datum} · ${uhrzeit} Uhr\n`+
      `<b>Datei:</b>   ${f.name}\n`+
      `<b>Art:</b>     ${parsed.dtype||'–'}\n`+
      `<b>Von:</b>     ${parsed.absender||'–'}\n`+
      `<b>Betreff:</b> ${parsed.betreff||'–'}\n`+
      (parsed.betrag?`<b>Betrag:</b>  ${parsed.betrag}\n`:'')+
      (parsed.frist?`<b>Frist:</b>   ${parsed.frist}\n`:'')+
      `<b>Prio:</b>    ${parsed.dringlichkeit||'–'}\n`+
      `━━━━━━━━━━━━━━━━━━━━\n`+
      `<b>Empfehlung:</b> ${parsed.empfehlung||'–'}\n`+
      `<b>Hinweis:</b> ${parsed.hinweis||'–'}`;
    $('tgp').style.display='block';$('tgp').textContent=tgTxt.replace(/<[^>]+>/g,'');
    const tr=await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:tgTxt})});
    const td=await tr.json();
    if(td.ok){setSP('sp3','ok');lg('ti-brand-telegram','var(--tg)','Telegram ✓');}
    else{setSP('sp3','fail');lg('ti-alert-triangle','var(--am)','Telegram: '+td.error);}
    setP(100);

    // Archivieren
    addToArchive({nr,datum,uhrzeit,filename:f.name,...parsed,tags:[...tags],status:'Neu'});

    $('c3').style.display='block';
  }catch(e){
    lg('ti-x','var(--re)','Fehler: '+e.message);
    ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,'fail'));
    alert('Fehler: '+e.message);
  }
  btn.disabled=false;btn.innerHTML='<i class="ti ti-refresh"></i> Erneut';
}

// ═══ ARCHIV ════
function addToArchive(entry){
  archive.unshift(entry);
  if(archive.length>500)archive=archive.slice(0,500);
  localStorage.setItem('agp_archive',JSON.stringify(archive));
  renderArchive(archive);
}
function renderArchive(items){
  const list=$('arch-list');
  if(!items.length){
    list.innerHTML='<div style="text-align:center;color:var(--tx3);padding:20px;font-size:13px"><i class="ti ti-database" style="font-size:28px;display:block;margin-bottom:8px;opacity:.4"></i>Keine Dokumente gefunden</div>';
    return;
  }
  list.innerHTML=items.map(e=>{
    const cls=DOCTYPE_COLORS[e.dtype]||'dtype-sonstige';
    return`<div style="border:.5px solid var(--bd);border-radius:var(--rs);padding:10px;margin-bottom:7px;background:var(--sf)">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px">
        <div>
          <span class="dtype ${cls}" style="font-size:10px;padding:1px 7px">${e.dtype||'–'}</span>
          <span style="font-size:11px;color:var(--tx2);margin-left:6px">${e.datum||''} ${e.uhrzeit||''}</span>
          <span style="font-size:10px;color:var(--tx3);margin-left:6px">[${e.nr}]</span>
        </div>
        <span style="font-size:11px;font-weight:500;color:${e.dringlichkeit==='Hoch'?'var(--re)':e.dringlichkeit==='Niedrig'?'var(--tx3)':'var(--tx)'}">${e.dringlichkeit||''}</span>
      </div>
      <div style="font-size:12px;font-weight:500;margin-top:4px">${e.absender||'–'}</div>
      <div style="font-size:11px;color:var(--tx2);margin-top:2px">${e.betreff||'–'}</div>
      ${e.betrag?`<div style="font-size:11px;color:var(--am);margin-top:2px">💶 ${e.betrag}</div>`:''}
      ${e.tags?.length?`<div class="tags-wrap" style="margin-top:4px">${e.tags.map(t=>`<span class="tag" style="cursor:default">${t}</span>`).join('')}</div>`:''}
    </div>`;
  }).join('');
}
function searchArch(){
  const q=($('arch-q').value||'').toLowerCase();
  const type=$('arch-type').value;
  const days=parseInt($('arch-period').value)||0;
  const cutoff=days?new Date(Date.now()-days*86400000):null;
  renderArchive(archive.filter(e=>{
    if(type&&e.dtype!==type)return false;
    if(cutoff){
      const parts=(e.datum||'').split('.');
      if(parts.length===3){
        const d=new Date(parts[2],parts[1]-1,parts[0]);
        if(d<cutoff)return false;
      }
    }
    if(q){
      const hay=JSON.stringify(e).toLowerCase();
      if(!hay.includes(q))return false;
    }
    return true;
  }));
}

// ═══ ENTSCHEIDUNG ════
function selD(t){
  curD=t;
  ['wv','ma','ab','fw'].forEach(x=>{$('d-'+x).classList.remove('sel');const p=$('p-'+x);if(p)p.style.display='none'});
  $('d-'+t).classList.add('sel');const pp=$('p-'+t);if(pp)pp.style.display='block';
}

async function genDraft(){
  if(!analysis){alert('Bitte erst analysieren.');return}
  const ds=$('dst');ds.innerHTML='<span class="bdg binfo"><i class="ti ti-loader"></i> Generiere...</span>';
  $('m-bo').value='';
  try{
    const cfg=await(await fetch('/config')).json();
    const parsed=JSON.parse(analysis);

    // Sprache bestimmen
    const langMap={de:'Deutsch',en:'English',ru:'Русский',orig:parsed.sprache||'Deutsch'};
    const targetLang=langMap[repLang]||'Deutsch';
    const origLang=parsed.sprache||'Deutsch';

    const prompt=`Du bist Assistent der AG Posteingang.

Briefanalyse:
${JSON.stringify(parsed,null,2)}

Aufgabe: Verfasse eine professionelle Antwort.
${repLang==='orig'||targetLang===origLang
  ? `Schreibe auf ${origLang}.`
  : `Das Originaldokument ist auf ${origLang}. Schreibe die Antwort auf ${targetLang}. Falls nötig, übersetze den Inhalt entsprechend.`
}

Erste Zeile exakt: "BETREFF: [passender Betreff]"
Dann vollständiger Brief ab "Sehr geehrte..." / "Dear..." / "Уважаемые..." (je nach Sprache).
Sachlich, professionell, korrekte Briefkonventionen.
Absender: ${cfg.sender_name||'AG Posteingang'}`;

    const t=await callClaude([{role:'user',content:prompt}]);
    const lines=t.split('\n');
    const bl=lines.find(l=>l.startsWith('BETREFF:')||l.startsWith('SUBJECT:')||l.startsWith('ТЕМА:'));
    if(bl)$('m-su').value=bl.replace(/^(BETREFF:|SUBJECT:|ТЕМА:)/,'').trim();
    const bs=lines.findIndex(l=>/^(Sehr geehrte|Dear|Уважаем|Guten|Hallo)/.test(l.trim()));
    $('m-bo').value=bs>=0?lines.slice(bs).join('\n'):t;
    ds.innerHTML=`<span class="bdg bok"><i class="ti ti-check"></i> Entwurf auf ${targetLang} fertig!</span>`;
    lg('ti-wand','var(--ac)',`Entwurf auf ${targetLang} generiert ✓`);
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
        text:`📅 <b>Wiedervorlage</b>\nNr.: ${nr}\nDatum: ${d||'–'}\nPriorität: ${p}\nNotiz: ${n||'–'}\n${indexData.absender?'Von: '+indexData.absender:''}`})});
      // Status aktualisieren
      updateArchiveStatus(nr,'Wiedervorlage: '+d);
      lg('ti-calendar-event','var(--gr)',`Wiedervorlage: ${d} · ${p} ✓`);
      showDone('Wiedervorlage eingetragen · Telegram ✓');

    }else if(curD==='ma'){
      const to=$('m-to').value.trim(),cc=$('m-cc').value.trim();
      const su=$('m-su').value.trim()||'Antwort – AG Posteingang';
      const bo=$('m-bo').value.trim();
      if(!to){alert('Empfänger!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      if(!bo){alert('E-Mail-Text!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      lg('ti-mail','var(--ac)',`Sende E-Mail an ${to}...`);
      const ctrl=new AbortController();const timer=setTimeout(()=>ctrl.abort(),30000);
      try{
        const er=await fetch('/mail',{method:'POST',headers:J,signal:ctrl.signal,
          body:JSON.stringify({to,cc,subject:su,body:bo,nr,
            pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
        clearTimeout(timer);
        const ed=await er.json();
        if(!ed.ok)throw new Error(ed.error);
        if(ed.drive_ok)lg('ti-brand-google-drive','var(--gr)','Ausgangs-PDF in Drive ✓');
        else if(ed.drive_msg&&ed.drive_msg!=='Drive nicht konfiguriert')lg('ti-alert-triangle','var(--am)','Drive: '+ed.drive_msg);
      }catch(e){clearTimeout(timer);if(e.name==='AbortError')throw new Error('Timeout');throw e}
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({
        text:`✉️ <b>E-Mail gesendet</b>\nNr.: ${nr}\nAn: ${to}\nBetreff: ${su}`})});
      updateArchiveStatus(nr,'E-Mail gesendet an '+to);
      lg('ti-mail','var(--gr)',`E-Mail gesendet an ${to} ✓`);
      showDone(`E-Mail gesendet · ${to} · Drive · Telegram ✓`);

    }else if(curD==='ab'){
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({
        text:`📁 <b>Abgelegt</b>\nNr.: ${nr}\nVon: ${indexData.absender||'–'}\n${datum}`})});
      updateArchiveStatus(nr,'Abgelegt');
      lg('ti-folder-check','var(--gr)','Abgelegt ✓');
      showDone('Abgelegt · Telegram ✓');

    }else if(curD==='fw'){
      const fto=$('fw-to').value.trim();
      if(!fto){alert('Empfänger!');btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen';return}
      const fn=$('fw-n').value.trim();
      const cfg=await(await fetch('/config')).json();
      const fwb=`Weitergeleitet von AG Posteingang\n\nHinweis: ${fn||'–'}\nNr.: ${nr} · ${datum}\n\nVon: ${indexData.absender||'–'}\nBetreff: ${indexData.betreff||'–'}\n\n--\n${cfg.sender_name||'AG Posteingang'}`;
      const er=await fetch('/mail',{method:'POST',headers:J,
        body:JSON.stringify({to:fto,subject:`Weiterleitung: ${indexData.absender||'Eingang'} ${nr}`,body:fwb,nr,
          pdf_b64:stPdfB64,pdf_name:`Posteingang_${nr}.pdf`})});
      const ed=await er.json();
      if(!ed.ok)throw new Error(ed.error);
      await fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({text:`➡️ <b>Weitergeleitet</b>\nNr.: ${nr}\nAn: ${fto}`})});
      updateArchiveStatus(nr,'Weitergeleitet an '+fto);
      lg('ti-send','var(--gr)',`Weitergeleitet an ${fto} ✓`);
      showDone(`Weitergeleitet · ${fto} · Telegram ✓`);
    }
    $('cnew').style.display='block';
    // Warteschlange prüfen
    if(docQueue.length>0){
      $('queue-bar').style.display='block';
      renderQueue();
      // Automatisch nach 3 Sekunden nächstes starten
      setTimeout(()=>{
        if(docQueue.length>0){
          lg('ti-player-skip-forward','var(--ac)',
            `Starte automatisch nächstes Dokument in 3 Sekunden... (${docQueue.length} verbleibend)`);
          setTimeout(processNext,3000);
        }
      },500);
    }
  }catch(e){lg('ti-x','var(--re)','Fehler: '+e.message);alert('Fehler: '+e.message)}
  btn.disabled=false;btn.innerHTML='<i class="ti ti-check"></i> Ausführen & abschließen';
}

function updateArchiveStatus(nr,status){
  const idx=archive.findIndex(e=>e.nr===nr);
  if(idx>=0){archive[idx].status=status;localStorage.setItem('agp_archive',JSON.stringify(archive));}
}
function showDone(msg){
  $('donebox').innerHTML=`<div class="done"><i class="ti ti-circle-check"></i> ${msg} · ${tod()} ${now()}</div>`;
}
function resetAll(){
  files=[];analysis='';curD=null;nr='';stPdfB64='';origB64='';origMime='';tags=[];indexData={};
  renderFiles();
  ['c2','c3'].forEach(id=>$(id).style.display='none');
  // cnew nur ausblenden wenn Queue leer
  if(docQueue.length===0)$('cnew').style.display='none';
  ['abox','tgp','multi-doc'].forEach(id=>{$(id).style.display='none'});
  $('prw').style.display='none';$('prb').style.width='0%';
  $('stmp-prev').innerHTML='';$('donebox').innerHTML='';$('dtype-badge').innerHTML='';$('idx-box').style.display='none';
  ['wv','ma','ab','fw'].forEach(t=>{$('d-'+t).classList.remove('sel');const p=$('p-'+t);if(p)p.style.display='none'});
  ['sp1','sp2','sp3','sp4'].forEach(id=>setSP(id,null));
  $('prev-iframe').style.display='none';$('prev-iframe').src='';$('prev-img').style.display='none';
  $('prev-empty').style.display='block';$('prev-filename').textContent='';
  $('prev-toggle').style.display='none';$('prev-dl').style.display='none';
  $('prev-foot').innerHTML='<span style="color:var(--tx3);font-size:11px">Kein Dokument</span>';
  window.scrollTo({top:0,behavior:'smooth'});
  lg('ti-plus','var(--ac)',docQueue.length>0?`Neues Schreiben (${docQueue.length} in Warteschlange)`:'Neues Schreiben');
}

// ═══ GOOGLE DRIVE POLLING ════
let pollLastChecked=new Date(0);
async function pollDrive(manual=false){
  const cfg=await(await fetch('/config')).json();
  if(!cfg.gdrive_poll&&!cfg.gdrive_in)return;
  if(manual)lg('ti-refresh','var(--ac)','Prüfe Google Drive auf neue Dokumente...');
  try{
    const r=await fetch('/drive/poll',{method:'POST',headers:J,
      body:JSON.stringify({since:pollLastChecked.toISOString()})});
    const d=await r.json();
    if(d.ok&&d.new_files?.length){
      lg('ti-mail-opened','var(--gr)',`${d.new_files.length} neue Datei(en) in Drive gefunden!`);
      d.new_files.forEach(f=>{
        lg('ti-file','var(--ac)',`Neue Datei: ${f.name}`);
        // Benachrichtigung via Telegram
        fetch('/tg',{method:'POST',headers:J,body:JSON.stringify({
          text:`📥 <b>Neue Post in Drive</b>\nDatei: ${f.name}\nBitte in AG Posteingang bearbeiten.`})});
      });
      // Erste neue Datei automatisch laden
      if(d.new_files[0]?.pdf_b64){
        const blob=new Blob([Uint8Array.from(atob(d.new_files[0].pdf_b64),c=>c.charCodeAt(0))],{type:'application/pdf'});
        const file=new File([blob],d.new_files[0].name,{type:'application/pdf'});
        addFiles([file]);
        lg('ti-arrow-down','var(--gr)',`Datei "${d.new_files[0].name}" automatisch geladen`);
      }
    } else if(manual){
      lg('ti-circle-check','var(--gr)','Keine neuen Dokumente in Drive');
    }
    pollLastChecked=new Date();
    $('poll-dot').classList.add('active');
    $('poll-txt').textContent='Drive aktiv';
  }catch(e){
    if(manual)lg('ti-alert-triangle','var(--am)','Drive-Polling: '+e.message);
  }
}

// Drag & Drop
const dz=$('dz');
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag')});
dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');addFiles(e.dataTransfer.files)});

// Init
loadCfg();
renderDtypes();
updateDtypeSelects();
renderArchive(archive);
// Drive alle 5 Minuten prüfen
pollTimer=setInterval(()=>pollDrive(false),5*60*1000);
lg('ti-rocket','var(--ac)',`AG Posteingang v2.0 · ${tod()} · Punkte 1-5 aktiv`);
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
                'gdrive_poll':             cfg.get('gdrive_poll',''),
                'default_lang':            cfg.get('default_lang','de'),
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

        elif self.path=='/drive/poll':
            cfg=load_config()
            since=b.get('since','2020-01-01T00:00:00Z')
            try:
                files=gdrive_list_new_files(cfg,since)
                result_files=[]
                processed_ids=set(cfg.get('processed_file_ids',[]))
                for f in files[:5]:
                    if 'pdf' in f.get('mimeType','').lower() or f['name'].lower().endswith('.pdf'):
                        # Duplikat-Check über File-ID
                        if f['id'] in processed_ids:
                            continue
                        pdf_b64=gdrive_download_file(cfg,f['id'])
                        if pdf_b64:
                            result_files.append({'name':f['name'],'id':f['id'],'pdf_b64':pdf_b64})
                            processed_ids.add(f['id'])
                # Verarbeitete IDs speichern (max 1000)
                if result_files:
                    ids_list=list(processed_ids)[-1000:]
                    save_config({**cfg,'processed_file_ids':ids_list})
                self.send_json({'ok':True,'new_files':result_files})
            except Exception as e:
                self.send_json({'ok':False,'error':str(e),'new_files':[]})

        elif self.path=='/doc/split':
            # Trennt Sammeldokument
            cfg=load_config()
            try:
                result=split_pdf_document(b.get('pdf_b64',''),b.get('filename','dokument.pdf'),cfg)
                self.send_json(result)
            except Exception as e:
                self.send_json({'ok':False,'error':str(e),'parts':1})

        else:
            self.send_json({'error':'unknown'},404)

if __name__=='__main__':
    srv=HTTPServer(('0.0.0.0',PORT),H)
    print(f"Server läuft auf Port {PORT}")
    srv.serve_forever()
