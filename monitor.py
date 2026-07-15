"""
monitor.py
----------
Monitora il canale Telegram "Concorsi pubblici" e invia una notifica,
tramite un bot Telegram personale, solo per i post che riguardano:
  - concorsi che si svolgono in Sicilia, oppure
  - concorsi nazionali con posti disponibili anche in Sicilia

Pensato per essere eseguito da GitHub Actions secondo un cron
(vedi .github/workflows/monitor.yml), sul modello di inPA-Monitor:
nessun processo persistente, lo stato tra un'esecuzione e l'altra
viene mantenuto in state.json, che il workflow ricommitta nel repo.

Tutte le credenziali arrivano da variabili d'ambiente (GitHub Secrets),
non da un file di configurazione locale.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

STATE_FILE = Path(__file__).parent / "state.json"

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TELEGRAM_SESSION = os.environ["TELEGRAM_SESSION"]
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "concorsipubblicin")
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# Province e riferimenti siciliani da cercare nel testo del post
SICILIA_KEYWORDS = [
    r"\bsicilia\b",
    r"\bpalermo\b",
    r"\bcatania\b",
    r"\bmessina\b",
    r"\bsiracusa\b",
    r"\btrapani\b",
    r"\bagrigento\b",
    r"\bcaltanissetta\b",
    r"\benna\b",
    r"\bragusa\b",
]

# Frasi tipiche delle "anticipazioni" (concorso non ancora bandito)
ANTICIPAZIONE_KEYWORDS = [
    r"in arrivo",
    r"prossimamente",
    r"a breve",
    r"sar[aà] pubblicato",
    r"si prevede",
    r"annunciat[oa]",
    r"in programma",
]

SICILIA_RE = re.compile("|".join(SICILIA_KEYWORDS), re.IGNORECASE)
ANTICIPAZIONE_RE = re.compile("|".join(ANTICIPAZIONE_KEYWORDS), re.IGNORECASE)


def load_last_id() -> int:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("last_id", 0)
    return 0


def save_last_id(last_id: int) -> None:
    STATE_FILE.write_text(json.dumps({"last_id": last_id}, indent=2))


def is_interesting(text: str) -> bool:
    """Vero se il post riguarda la Sicilia e non è una semplice anticipazione."""
    if not text:
        return False
    if not SICILIA_RE.search(text):
        return False
    if ANTICIPAZIONE_RE.search(text):
        return False
    return True


def send_notification(text: str, link: str = None) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    message = text if not link else f"{text}\n\n{link}"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    last_id = load_last_id()
    max_id_seen = last_id
    found = 0

    # IMPORTANTE: non usare "with TelegramClient(...) as client", perché
    # internamente chiama .start(), che se la sessione non è valida prova
    # un login INTERATTIVO (chiede numero di telefono a schermo) — su
    # GitHub Actions questo blocca il job fino al timeout, senza errore
    # chiaro. Ci connettiamo manualmente e verifichiamo l'autenticazione
    # prima di procedere, così un problema di sessione fallisce subito
    # con un messaggio chiaro invece di restare appeso.
    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)
    client.connect()

    if not client.is_user_authorized():
        print(
            "ERRORE: la sessione Telethon (TELEGRAM_SESSION) non è valida o "
            "è scaduta. Rigenerala eseguendo generate_session.py in locale "
            "e aggiorna il secret su GitHub.",
            file=sys.stderr,
        )
        client.disconnect()
        sys.exit(1)

    try:
        channel = client.get_entity(CHANNEL_USERNAME)

        messages = list(client.iter_messages(channel, min_id=last_id, limit=200))
        messages.reverse()  # dal più vecchio al più recente

        for msg in messages:
            if msg.id > max_id_seen:
                max_id_seen = msg.id

            text = msg.message or ""
            if is_interesting(text):
                link = None
                if getattr(channel, "username", None):
                    link = f"https://t.me/{channel.username}/{msg.id}"
                send_notification(text, link)
                found += 1
    finally:
        client.disconnect()

    if max_id_seen != last_id:
        save_last_id(max_id_seen)

    print(f"Controllati i messaggi nuovi, trovati {found} rilevanti per la Sicilia")


if __name__ == "__main__":
    main()
