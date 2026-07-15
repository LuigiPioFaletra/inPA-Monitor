import os
import re
import time
import json
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

URLS_FILE = "urls.json"
STATE_FILE = "state.json"
HEARTBEAT_FILE = "heartbeat.json"
HEARTBEAT_DAYS = 7

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5

# Numero di controlli consecutivi in errore prima di inviare una notifica
# Telegram. Serve a non fare spam per un singolo timeout/errore di rete
# transitorio, ma a segnalare comunque un problema persistente (es. un
# concorso_id ormai scaduto che il sito reindirizza altrove).
ERROR_NOTIFY_THRESHOLD = 2


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default if default is not None else {}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def concorso_id_from_url(url):
    qs = parse_qs(urlparse(url).query)
    values = qs.get("concorso_id")
    return values[0] if values else None


def fetch_html(url):
    """Scarica la pagina con qualche retry, per non trattare un errore
    di rete temporaneo come se il concorso fosse sparito.

    Ritorna (html, url_finale): il portale inPA a volte, quando un
    concorso_id non e' piu' valido, non risponde con un 404 ma reindirizza
    (HTTP 200) verso una pagina generica di un altro concorso. Controlliamo
    l'URL finale per accorgercene, invece di calcolare tranquillamente
    l'hash di una pagina che non c'entra nulla con quella richiesta.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text, r.url
        except requests.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS)
    raise last_error


def extract_signals(html):
    """
    Estrae SOLO le informazioni che indicano un vero aggiornamento del
    bando, invece di fare l'hash dell'intera pagina.

    La versione precedente calcolava lo sha256 di tutto il testo visibile
    (meno script/style/header/footer/nav): qualunque differenza anche
    minima e non legata al contenuto del bando faceva scattare un falso
    allarme, com'e' successo con "Concorso 2".

    Qui isoliamo invece:
      - la lista dei blocchi "Aggiornamento del gg.mm.aaaa: ..."
      - lo "Stato" del concorso (Aperto/Chiuso/...)
      - l'elenco di allegati pubblicati (nome file + data di pubblicazione)

    Non tutte le pagine hanno tutti e tre i campi (alcuni bandi non hanno
    mai un "Aggiornamento del", altri non hanno affatto una sezione
    allegati): la funzione ritorna liste/valori vuoti in quei casi, il che
    va bene, significa solo che quel segnale non e' disponibile per quel
    concorso.
    """
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(["script", "style", "header", "footer", "nav", "noscript"]):
        tag.decompose()

    # Il portale inPA espone un link di accessibilita' "Salta al contenuto"
    # che punta a #content: usiamo quel contenitore come perimetro del
    # contenuto reale del bando, se esiste.
    main = soup.find(id="content") or soup

    # I singoli nodi di testo possono contenere newline interni: li
    # normalizziamo a spazi singoli, altrimenti le regex sotto (che usano
    # ".", senza DOTALL) fallirebbero nell'attraversare gli "a-capo".
    full_text = re.sub(r"\s+", " ", " ".join(main.stripped_strings)).strip()

    aggiornamenti = re.findall(
        r"Aggiornamento del?\s*\d{2}\.\d{2}\.\d{4}.*?"
        r"(?=Aggiornamento del?\s*\d{2}\.\d{2}\.\d{4}|"
        r"Area geografica:|Valutazione:|Stato:|Bando/Avviso e Allegati:|$)",
        full_text,
    )
    aggiornamenti = sorted({a.strip() for a in aggiornamenti})

    stato_match = re.search(r"Stato:\s*(\S+)", full_text)
    stato = stato_match.group(1) if stato_match else None

    allegati = sorted({
        a.get_text(strip=True)
        for a in main.find_all("a")
        if "Pubblicato il" in a.get_text()
    })

    return {
        "stato": stato,
        "aggiornamenti": aggiornamenti,
        "allegati": allegati,
    }


def digest(signal):
    payload = json.dumps(signal, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def describe_change(old_signal, new_signal):
    """Prova a spiegare in una riga cosa e' cambiato davvero, invece di un
    generico 'qualcosa e' cambiato'."""
    if not old_signal:
        return "Prima rilevazione di questo concorso"

    old_agg = set(old_signal.get("aggiornamenti", []))
    new_agg = set(new_signal.get("aggiornamenti", []))
    nuovi_agg = new_agg - old_agg

    old_all = set(old_signal.get("allegati", []))
    new_all = set(new_signal.get("allegati", []))
    nuovi_all = new_all - old_all

    parti = []
    if nuovi_agg:
        parti.append(f"{len(nuovi_agg)} nuovo/i aggiornamento/i testuale/i")
    if nuovi_all:
        parti.append(f"{len(nuovi_all)} nuovo/i allegato/i")
    if old_signal.get("stato") != new_signal.get("stato"):
        parti.append(f"stato: {old_signal.get('stato')} -> {new_signal.get('stato')}")

    return ", ".join(parti) if parti else "contenuto modificato"


def telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram non configurato (BOT_TOKEN/CHAT_ID mancanti)")
        return

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    ).raise_for_status()


def heartbeat_due():
    """
    Ritorna True se è il momento di inviare un heartbeat.
    """
    data = load_json(HEARTBEAT_FILE, {})

    last = data.get("last_sent")

    if not last:
        return True

    try:
        last = datetime.fromisoformat(last).date()
    except ValueError:
        return True

    return (datetime.now().date() - last) >= timedelta(days=HEARTBEAT_DAYS)


def mark_heartbeat_sent():
    """
    Aggiorna heartbeat.json mantenendo anche la data
    del primo heartbeat inviato.
    """
    now = datetime.now().date().isoformat()

    data = load_json(HEARTBEAT_FILE, {})

    if "started" not in data:
        data["started"] = now

    data["last_sent"] = now

    save_json(HEARTBEAT_FILE, data)


def heartbeat_info():
    """
    Restituisce:
      - data di avvio del monitor
      - giorni trascorsi dall'ultimo heartbeat
    """
    data = load_json(HEARTBEAT_FILE, {})

    today = datetime.now().date()

    started = data.get("started")
    last = data.get("last_sent")

    try:
        started_date = datetime.fromisoformat(started).date()
    except Exception:
        started_date = today

    try:
        last_date = datetime.fromisoformat(last).date()
    except Exception:
        last_date = today

    return (
        started_date.strftime("%d/%m/%Y"),
        (today - last_date).days
    )


def check_one(concorso, old_state):
    """Controlla un singolo concorso e ritorna (nuova_entry_di_stato,
    evento), dove evento e' uno tra:
      ("change", motivo)   -> il bando e' stato aggiornato
      ("error_start", msg) -> nuovo errore che ha appena superato la soglia
      ("recovered", None)  -> il concorso e' tornato a funzionare dopo un errore
      (None, None)         -> nessuna novita' da segnalare
    """
    cid = concorso["id"]
    url = concorso["url"]

    old_entry = old_state.get(cid, {})

    try:
        html, final_url = fetch_html(url)

        final_cid = concorso_id_from_url(final_url)
        if final_cid != cid:
            raise ValueError(
                f"la pagina ha reindirizzato altrove (concorso_id atteso "
                f"'{cid}', ottenuto '{final_cid}'): il bando e' probabilmente "
                f"scaduto/rimosso dal portale inPA"
            )

        signal = extract_signals(html)
        current_hash = digest(signal)

        new_entry = {
            "hash": current_hash,
            "signal": signal,
            "url": url,
            "checked": datetime.now().isoformat(),
            "consecutive_errors": 0,
            "error_notified": False,
        }

        event, motivo = None, None

        old_hash = old_entry.get("hash")
        if old_hash is not None and old_hash != current_hash:
            event, motivo = "change", describe_change(old_entry.get("signal"), signal)
        elif old_entry.get("error_notified"):
            # Era in errore ed era gia' stato segnalato: avvisiamo che e' tornato ok
            event = "recovered"

        return new_entry, (event, motivo)

    except Exception as e:
        consecutive_errors = old_entry.get("consecutive_errors", 0) + 1
        already_notified = old_entry.get("error_notified", False)

        new_entry = dict(old_entry)  # conserva hash/signal precedenti, se presenti
        new_entry.update({
            "url": url,
            "checked": datetime.now().isoformat(),
            "consecutive_errors": consecutive_errors,
            "last_error": str(e),
        })

        event, motivo = None, None
        if consecutive_errors >= ERROR_NOTIFY_THRESHOLD and not already_notified:
            new_entry["error_notified"] = True
            event, motivo = "error_start", str(e)
        else:
            new_entry["error_notified"] = already_notified

        return new_entry, (event, motivo)


def main():
    print(f"Controllo inPA: {datetime.now():%d/%m/%Y %H:%M}")

    concorsi = load_json(URLS_FILE, [])
    old_state = load_json(STATE_FILE, {})
    new_state = {}
    changes = []
    error_starts = []
    recoveries = []

    for c in concorsi:
        cid = c["id"]
        label = c.get("label", cid)

        new_entry, (event, motivo) = check_one(c, old_state)
        new_state[cid] = new_entry

        if event == "change":
            changes.append((label, c["url"], motivo))
            print(f"OK {label} (aggiornato: {motivo})")
        elif event == "error_start":
            error_starts.append((label, c["url"], motivo))
            print(f"ERRORE {label}: {motivo}")
        elif event == "recovered":
            recoveries.append((label, c["url"]))
            print(f"OK {label} (tornato disponibile)")
        else:
            status = "in errore" if new_entry.get("consecutive_errors") else "nessuna modifica"
            print(f"OK {label} ({status})")

    save_json(STATE_FILE, new_state)

    if changes:
        msg = "Aggiornamento concorsi inPA\n\n"
        msg += "\n\n".join(
            f"- {label}\n  {motivo}\n  {url}"
            for label, url, motivo in changes
        )
        telegram(msg)
        print("Notifica di aggiornamento inviata")

    if error_starts:
        msg = "Attenzione: problemi nel controllo di alcuni concorsi inPA\n\n"
        msg += "\n\n".join(
            f"- {label}\n  {motivo}\n  {url}"
            for label, url, motivo in error_starts
        )
        telegram(msg)
        print("Notifica di errore inviata")

    if recoveries:
        msg = "I seguenti concorsi sono tornati raggiungibili:\n\n"
        msg += "\n".join(f"- {label}\n  {url}" for label, url in recoveries)
        telegram(msg)
        print("Notifica di ripristino inviata")

    if not changes and not error_starts and not recoveries:
        print("Nessuna modifica")

        if heartbeat_due():
            started, days = heartbeat_info()
            telegram(
                "✅ inPA Monitor operativo\n\n"
                f"Ultimo controllo: {datetime.now():%d/%m/%Y %H:%M}\n\n"
                f"Concorsi monitorati: {len(concorsi)}\n"
                f"Ultimo heartbeat: {days} giorni fa\n\n"
                f"Monitor operativo da: {started}\n\n"
                "Nessuna modifica rilevata."
            )
            mark_heartbeat_sent()
            print("Heartbeat inviato")


if __name__ == "__main__":
    main()
