"""
ReviewAgent — Gestione automatica recensioni Google Business
Deploy: Render.com (cron job)
"""

import os
import json
import smtplib
import requests
from openai import OpenAI
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# ── Configurazione (legge da variabili d'ambiente su Render) ──────────────────
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
LOCATION_ID        = os.environ["GOOGLE_LOCATION_ID"]      # es. locations/123456789
ALERT_EMAIL_TO     = os.environ["ALERT_EMAIL_TO"]          # tua email
SMTP_USER          = os.environ["SMTP_USER"]               # gmail mittente
SMTP_PASSWORD      = os.environ["SMTP_PASSWORD"]           # app password Gmail
GOOGLE_CREDS_JSON  = os.environ["GOOGLE_CREDENTIALS_JSON"] # JSON intero come stringa

SCOPES = ["https://www.googleapis.com/auth/business.manage"]

# ── Google Auth ───────────────────────────────────────────────────────────────
def get_access_token():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token

# ── Fetch recensioni ──────────────────────────────────────────────────────────
def fetch_reviews(token):
    url = f"https://mybusiness.googleapis.com/v4/{LOCATION_ID}/reviews"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("reviews", [])

# ── Analisi sentiment + risposta con OpenAI ───────────────────────────────────
def analyze_and_reply(review_text: str, star_rating: str):
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Sentiment
    sentiment_msg = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=10,
        messages=[{"role": "user", "content":
            f"Rispondi SOLO con POSITIVE, NEGATIVE o NEUTRAL.\n"
            f"Stelle: {star_rating}\nTesto: {review_text}"}]
    )
    sentiment = sentiment_msg.choices[0].message.content.strip().upper()
    if sentiment not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
        sentiment = "NEUTRAL"

    # Bozza risposta
    tone = "calorosa e grata" if sentiment == "POSITIVE" else "empatica e propositiva"
    reply_msg = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=200,
        messages=[{"role": "user", "content":
            f"Sei il responsabile clienti di un'azienda italiana. "
            f"Scrivi una risposta {tone} a questa recensione. "
            f"Massimo 70 parole. Non usare virgolette all'inizio.\n"
            f"Recensione: {review_text}"}]
    )
    reply = reply_msg.choices[0].message.content.strip()
    return sentiment, reply

# ── Pubblica risposta su Google ───────────────────────────────────────────────
def post_reply(review_name: str, reply_text: str, token: str):
    url = f"https://mybusiness.googleapis.com/v4/{review_name}/reply"
    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"comment": reply_text}
    )
    resp.raise_for_status()
    print(f"✓ Risposta pubblicata: {review_name}")

# ── Email alert per recensioni negative ──────────────────────────────────────
def send_alert_email(reviewer_name: str, stars: str, review_text: str, reply_draft: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ Recensione negativa da {reviewer_name} ({stars}★)"
    msg["From"]    = SMTP_USER
    msg["To"]      = ALERT_EMAIL_TO

    body = f"""
⚠️  NUOVA RECENSIONE NEGATIVA — Azione richiesta
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Autore:  {reviewer_name}
Stelle:  {stars} ★
Data:    {datetime.now().strftime("%d/%m/%Y %H:%M")}

TESTO RECENSIONE:
"{review_text}"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOZZA RISPOSTA (generata da AI):
{reply_draft}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👉 Vai su Google Business Profile per rispondere:
https://business.google.com
"""
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print(f"📧 Alert email inviata per recensione di {reviewer_name}")

# ── Stato locale (evita di rielaborare recensioni già viste) ──────────────────
STATE_FILE = "/tmp/processed_reviews.json"

def load_processed():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_processed(ids: set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(ids), f)

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    print(f"\n🤖 ReviewAgent avviato — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    token = get_access_token()
    reviews = fetch_reviews(token)
    processed = load_processed()

    new_count = 0
    for review in reviews:
        review_id = review.get("reviewId", "")
        if review_id in processed:
            continue

        reviewer  = review.get("reviewer", {}).get("displayName", "Anonimo")
        stars     = review.get("starRating", "?")
        text      = review.get("comment", "")

        if not text:
            processed.add(review_id)
            continue

        print(f"\n📝 Recensione da {reviewer} ({stars}★): {text[:60]}...")
        sentiment, reply = analyze_and_reply(text, stars)
        print(f"   Sentiment: {sentiment}")

        if sentiment == "POSITIVE":
            post_reply(review["name"], reply, token)
        else:
            # NEGATIVE o NEUTRAL → alert email + bozza
            send_alert_email(reviewer, stars, text, reply)

        processed.add(review_id)
        new_count += 1

    save_processed(processed)
    print(f"\n✅ Completato. Recensioni elaborate: {new_count}")

if __name__ == "__main__":
    run()
