# inPA Monitor

Automatic monitoring of job postings/notices published on the [inPA](https://www.inpa.gov.it/) portal, with Telegram notifications whenever a monitored competition is updated (new notice, new attachment, status change).

## How it works

Each competition of interest is identified by its `concorso_id` (the parameter found in the detail page URL). On every run, `monitor.py`:

1. downloads the detail page for each competition listed in `urls.json`;
2. extracts only the signals that indicate a real update to the notice ("Aggiornamento del..." blocks, competition status, list of attachments with publication date) — not the entire page, to avoid false alarms caused by page elements unrelated to the notice's actual content;
3. compares these signals against the last snapshot saved in `state.json`;
4. if something has changed, sends a Telegram message; if the competition is unreachable for several consecutive runs, sends an error notification (see below).

Execution is automated via GitHub Actions (`.github/workflows/monitor.yml`), twice a day, regardless of whether the local PC is on or off.

## Repository structure

```
monitor.py                     # main script
urls.json                      # list of competitions to monitor
state.json                     # last detected state for each competition (updated automatically)
heartbeat.json                # heartbeat history (updated automatically)
requirements.txt                # Python dependencies
.github/workflows/monitor.yml  # automatic scheduling
```

## Setup

### 1. Dependencies

```
pip install -r requirements.txt
```

### 2. Telegram secrets

The script reads `BOT_TOKEN` and `CHAT_ID` from environment variables.

**Locally (PowerShell)**, for manual testing only:

```powershell
$env:BOT_TOKEN="..."
$env:CHAT_ID="..."
python monitor.py
```

These locally set values are **not read by GitHub Actions**: for automatic execution they must be configured as repository *Secrets*:

1. Go to **Settings → Secrets and variables → Actions** in the GitHub repository;
2. create a secret named `BOT_TOKEN` with the Telegram bot token;
3. create a secret named `CHAT_ID` with the id of the destination chat/channel;
4. make sure the repository's **Actions** tab is enabled (GitHub sometimes disables it by default on new repositories).

Without these two secrets, the script prints "Telegram non configurato" and sends nothing — this is the expected behavior when running locally without wanting to set them.

### 3. Scheduling

The workflow runs at 10:00 and 22:00 Italian time (cron `0 8,20 * * *`, calculated for summer daylight saving time UTC+2; in winter the actual local time will be 9:00/21:00, since GitHub Actions always runs in UTC and doesn't automatically adjust for daylight saving changes). It can also be triggered manually from the Actions tab ("Run workflow").

## Adding or removing a competition

Edit `urls.json`, which is a list of objects with this structure:

```json
{
    "id": "6e0a452e4be74b249d597dfc580032ca",
    "label": "Ministero della Cultura - 1800 assistenti",
    "url": "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=6e0a452e4be74b249d597dfc580032ca"
}
```

- `id`: the `concorso_id` extracted from the page URL — this is the stable key used to track state over time; don't change it once the competition is already being monitored, or the history will be lost (it will simply be treated as a "first detection", with no alert, on the next run);
- `label`: human-readable name shown in Telegram messages;
- `url`: the full URL of the detail page.

**Warning**: when a `concorso_id` no longer exists (the notice has concluded and been removed from the portal), inPA doesn't respond with an error but silently redirects to some other page. The monitor detects this by comparing the final `concorso_id` with the expected one and reports it as an error (see below), instead of mistakenly tracking the wrong content.

## Error notifications

If a competition is unreachable (site down, network issue, expired/redirected concorso_id) for **two consecutive checks**, a separate Telegram notification is sent. The two-check threshold avoids alerting on a single transient issue (a momentary timeout), while still flagging a persistent problem within about ten hours. When the competition becomes reachable again, a recovery notification is sent.

The threshold can be configured by changing `ERROR_NOTIFY_THRESHOLD` in `monitor.py`.

## Heartbeat notifications

To confirm that the monitor is still running correctly even when no competitions change, the workflow sends a periodic Telegram heartbeat every 7 days (configurable through `HEARTBEAT_DAYS`).

The heartbeat includes:

- date and time of the latest successful check;
- number of monitored competitions;
- days since the previous heartbeat;
- date since the monitor has been running.

Heartbeat information is stored in `heartbeat.json`, which is automatically updated and committed together with `state.json`.

## Notes

- `state.json` and `heartbeat.json` are overwritten and automatically committed by the workflow on every run, to preserve monitoring history between runs.
- The dependencies in `requirements.txt` are not pinned to specific versions, to avoid conflicts with other projects in the local development environment.

## License

This project is licensed under the terms of the MIT license. You can find the full license in the `LICENSE` file.
