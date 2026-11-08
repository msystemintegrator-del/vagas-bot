#!/usr/bin/env python3
"""
vagas_whatsapp.py
Recolhe vagas NOVAS de feeds RSS de sites de emprego moçambicanos e gera
mensagens prontas a colar no teu Canal de WhatsApp.

- Só usa a biblioteca padrão do Python (3.9+), sem instalar nada.
- Guarda as vagas já vistas em seen.json (nunca repete).
- Entrega: ficheiros em ./saida/, e (opcional) Telegram e/ou e-mail.

Uso:
    python vagas_whatsapp.py                # corrida normal
    python vagas_whatsapp.py --dry-run      # mostra, não grava estado
    python vagas_whatsapp.py --hours 72     # janela de antiguidade
    python vagas_whatsapp.py --feed "Teste=file:///caminho/feed.xml"
"""
import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

# ----------------------------- CONFIGURAÇÃO ---------------------------------
# (nome, url do feed RSS). O primeiro foi confirmado; os outros são tentativas:
# se o site não tiver feed, o script avisa e continua com os restantes.
FEEDS = [
    ("MMO Emprego", "https://emprego.mmo.co.mz/feed"),
    ("Emprego.co.mz", "https://emprego.co.mz/feed"),
    ("Vaga de Emprego Moçambique", "https://www.vagademprego.com/feed"),
]

MAX_AGE_HOURS = 48          # ignora vagas mais antigas que isto
MAX_ITEMS = 60              # máximo de vagas por corrida
CHUNK_LIMIT = 3500          # caracteres por mensagem (seguro para WhatsApp/Telegram)
DEDUPE_TITLES_ACROSS_SITES = True   # evita a mesma vaga vinda de 2 sites
KEEP_DAYS = 90              # quanto tempo lembrar vagas vistas

CAT = timezone(timedelta(hours=2))  # Maputo (UTC+2)
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen.json"
OUT_DIR = HERE / "saida"

# Rodapé das mensagens (edita à vontade). {canal} só aparece se definires CANAL_LINK.
FOOTER = (
    "\n———\n"
    "⚠️ Nunca pague para ser contratado(a). Confirme sempre a vaga na fonte oficial.\n"
    "💼 CV profissional, carta e coaching: *Integra Career Solutions* — +258 879 600 405"
)

UA = "Mozilla/5.0 (compatible; VagasBot/1.0)"
ATOM = "{http://www.w3.org/2005/Atom}"


# ------------------------------- UTILITÁRIOS --------------------------------
def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def clean_link(url):
    """Remove parâmetros de tracking e fragmentos."""
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def norm_title(t):
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"^vagas?\s+(para|de)?\s*", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def link_id(url):
    return hashlib.sha1(clean_link(url).lower().encode()).hexdigest()[:16]


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def child_text(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter("item"):                      # RSS 2.0
        items.append({
            "title": clean_text(child_text(it, "title")),
            "link": child_text(it, "link"),
            "date": parse_date(child_text(it, "pubDate")),
            "desc": clean_text(child_text(it, "description")),
        })
    for en in root.iter(ATOM + "entry"):              # Atom
        link = ""
        for l in en.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href")
                break
        items.append({
            "title": clean_text(child_text(en, ATOM + "title")),
            "link": link,
            "date": parse_date(child_text(en, ATOM + "published") or child_text(en, ATOM + "updated")),
            "desc": clean_text(child_text(en, ATOM + "summary")),
        })
    return [i for i in items if i["title"] and i["link"]]


def guess_company(desc):
    """Tenta extrair a empresa de frases como 'A Sasol pretende recrutar...'."""
    m = re.match(r"^(?:The\s+)?(?:[OA]s?\s+)?(.{2,60}?)\s+(?:pretende|pretendem|procura|recruta)", desc or "")
    if m:
        c = m.group(1).strip(" ,.-")
        if 1 < len(c) <= 60 and not c.lower().startswith(("vaga", "procura")):
            return c
    return ""


# --------------------------------- ESTADO -----------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": {}}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


# ------------------------------ MENSAGENS -----------------------------------
def build_messages(items, today):
    canal = os.environ.get("CANAL_LINK", "").strip()
    footer = FOOTER + (f"\n📲 Siga o canal: {canal}" if canal else "")
    header = (f"📢 *VAGAS DE EMPREGO — {today:%d/%m/%Y}*\n"
              f"🇲🇿 {len(items)} nova(s) oportunidade(s) em Moçambique\n")

    blocks = []
    for n, it in enumerate(items, 1):
        line = f"\n*{n}. {it['title']}*"
        company = guess_company(it["desc"])
        if company:
            line += f"\n🏢 {company}"
        line += f"\n🔗 {clean_link(it['link'])}\n"
        blocks.append(line)

    budget = CHUNK_LIMIT - len(header) - len(footer) - 60
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > budget:
            chunks.append(cur)
            cur = ""
        cur += b
    if cur:
        chunks.append(cur)

    total = len(chunks)
    msgs = []
    for i, body in enumerate(chunks, 1):
        head = header if i == 1 else f"📢 *VAGAS DE EMPREGO — parte {i}/{total}*\n"
        if i == 1 and total > 1:
            head += f"(mensagem 1 de {total})\n"
        msgs.append(head + body + (footer if i == total else ""))
    return msgs


# -------------------------------- ENTREGA -----------------------------------
def send_telegram(msgs):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return None
    for m in msgs:
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": m, "disable_web_page_preview": "true"
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                raise RuntimeError(f"Telegram devolveu {r.status}")
    return True


def send_email(msgs, today):
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not (user and pw):
        return None
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    em = EmailMessage()
    em["Subject"] = f"Vagas de emprego {today:%d/%m/%Y} — prontas para o WhatsApp"
    em["From"], em["To"] = user, to
    em.set_content(("\n\n" + "=" * 30 + "\n\n").join(msgs))
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(em)
    return True


# ---------------------------------- MAIN ------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="não grava estado nem envia")
    ap.add_argument("--hours", type=int, default=int(os.environ.get("MAX_AGE_HOURS", MAX_AGE_HOURS)))
    ap.add_argument("--feed", action="append", metavar="NOME=URL", help="substitui a lista de feeds")
    args = ap.parse_args()

    feeds = FEEDS
    if args.feed:
        feeds = [tuple(f.split("=", 1)) for f in args.feed]

    state = load_state()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours)

    fresh, seen_titles = [], {}
    for name, url in feeds:
        try:
            items = parse_feed(http_get(url))
        except Exception as e:  # um site em baixo não pára os outros
            print(f"[aviso] {name}: não consegui ler o feed ({e})", file=sys.stderr)
            continue
        new_here = 0
        for it in items:
            lid = link_id(it["link"])
            if lid in state["seen"]:
                continue
            if it["date"] and it["date"] < cutoff:
                continue
            tk = norm_title(it["title"])
            if DEDUPE_TITLES_ACROSS_SITES and seen_titles.get(tk, name) != name:
                state["seen"][lid] = now.isoformat()   # duplicada de outro site
                continue
            seen_titles.setdefault(tk, name)
            it.update(source=name, id=lid)
            fresh.append(it)
            new_here += 1
        print(f"[ok] {name}: {new_here} nova(s)", file=sys.stderr)

    if not fresh:
        print("Sem vagas novas.")
        if not args.dry_run:
            save_state(state)
        return 0

    fresh.sort(key=lambda i: i["date"] or now, reverse=True)
    fresh = fresh[:MAX_ITEMS]
    today = datetime.now(CAT)
    msgs = build_messages(fresh, today)

    print("\n\n".join(msgs))
    if args.dry_run:
        return 0

    OUT_DIR.mkdir(exist_ok=True)
    stamp = today.strftime("%Y-%m-%d_%H%M")
    for i, m in enumerate(msgs, 1):
        (OUT_DIR / f"vagas_{stamp}_parte{i}.txt").write_text(m, encoding="utf-8")

    ok = True
    for label, fn in (("Telegram", lambda: send_telegram(msgs)),
                      ("E-mail", lambda: send_email(msgs, today))):
        try:
            r = fn()
            if r:
                print(f"[ok] {label} enviado", file=sys.stderr)
        except Exception as e:
            ok = False
            print(f"[erro] {label}: {e}", file=sys.stderr)

    if not ok:   # não marcar como vistas se a entrega falhou: tenta de novo na próxima
        return 1
    for it in fresh:
        state["seen"][it["id"]] = now.isoformat()
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
