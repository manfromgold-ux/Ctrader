# Reef

Reef runs a fleet of paid web scrapers ("Actors") on [Apify Store](https://apify.com/store) with no day-to-day work from you.

People already go to Apify Store to find scrapers and pay per result. You get about 80% of the revenue. Apify handles billing, servers, proxies and refunds.

Scrapers break whenever a website changes its layout. Broken scrapers lose ranking and get removed, which wears down human sellers. Reef's main job is to notice those breaks and repair them automatically.

```
                 ┌──────────── every 24h ────────────┐
   SCOUT  ──►  candidate sites  ──►  SPAWN  ──►  live Actors on Apify Store
   (LLM + Store       │               (LLM writes extractor,       │
    gap check +       │                sandbox test, Apify          │
    fetch check)      │                canary, price, publish)      │
                      │                                             ▼
   PRUNE  ◄───────────┴─────── every 7 days ─────────  HEAL  (every 6h)
   (retire if 0 users after 60d,                       canary each Actor; on a break:
    clone winners to sibling sites)                    LLM patch → contract check →
                                                       Apify canary → ship, or roll back
   REPORT (weekly) ──► Telegram / e-mail: one screen, plus a stop switch
```

## What you do once (about 1–2 hours)

1. **Apify account**
   - Sign up at apify.com and pick a username; it appears on your Store listings.
   - Fill in your payout details in Apify Console so Store revenue can be paid out to you.
   - Copy your API token from *Settings → API & Integrations*.
2. **OpenRouter account**
   - Sign up at openrouter.ai and buy **$10** of credits. This one-time purchase raises free-model limits to 1,000 requests a day.
   - Create an API key. Setting a credit limit on the key is a good extra safety net.
3. **Notifications.** Set up a Telegram bot, an SMTP e-mail account, or both (see `.env.example`).
4. **Start it on your home PC (Docker Desktop):**
   1. Get the code. Download the branch as a ZIP from GitHub (*Code → Download ZIP*) and unzip it, or run `git clone -b claude/festive-pascal-s4yap3 https://github.com/manfromgold-ux/Ctrader.git`.
   2. Open the `reef` folder.
   3. Run the start script:
      - **Windows:** double-click `start-windows.bat`.
      - **Mac/Linux:** run `./start.sh` in a terminal.

   The script then does the rest:
   - creates `.env` and opens it in Notepad (or TextEdit / nano);
   - after you paste the keys and save, checks them and sends a test message;
   - starts Reef in Docker.

   To change a key later, edit `.env` and run `docker compose restart` in the `reef` folder.

   **Keeping it running on a home PC:**
   - In Docker Desktop, turn on *Settings → General → Start Docker Desktop when you sign in*.
   - In Windows power settings, set *Sleep* to *Never* while plugged in.
   - If the PC is off, **your scrapers keep selling.** Customers' runs happen on Apify's servers. Only health checks, repairs and new builds pause, and they catch up when the PC is back.

   **Or use a small Linux server** (~$5/month VPS) so nothing depends on your PC:

   ```bash
   git clone <this repo> && cd <repo>/reef
   cp .env.example .env && nano .env            # paste the keys
   docker compose run --rm reef python -m reef doctor --notify
   docker compose up -d
   ```

   `doctor` checks that every key works and sends you a test message. After `up -d`, Reef runs forever and restarts itself after crashes or reboots.

5. **Taxes.** Apify Store payouts are income, so declare them where you live. Reef can't do this part.

## What happens next

| When | What Reef does |
|---|---|
| Minute 1 | Sends the first report (empty fleet), scouts about 10 candidate sites, and tries to build the first Actor. |
| Daily | Tops up the candidate queue and builds at most one new Actor (5 per week by default). |
| Every 6 hours | Tests every Actor against the live site. Broken ones are repaired, re-tested and shipped, or rolled back. |
| Weekly | Retires Actors with no users after 60 days. Finds sibling sites for Actors with 10+ users. Sends the report. |

You only need to read the weekly report, which takes about two minutes. You get a separate message only when:

- an Actor can't be repaired automatically,
- a job keeps crashing, or
- Apify refused an API call that only you can do in its web console (see "Known limits" below).

**Stop switch:** run `docker compose exec reef python -m reef pause`, or create a file named `data/PAUSE`. `resume` (or deleting the file) starts it again.

## Money guards (all in `.env`)

| Guard | Default | Effect |
|---|---|---|
| `REEF_LLM_MONTHLY_BUDGET_USD` | $15 | Paid model calls stop when reached. Free models are always tried first. |
| `REEF_APIFY_MONTHLY_CAP_USD` | $25 | No new Actors once your Apify usage this month reaches this amount. |
| `REEF_MAX_NEW_ACTORS_PER_WEEK` | 5 | Keeps your account from looking like spam. |
| `REEF_MAX_LIVE_ACTORS` | 60 | Fleet size limit. |
| `REEF_PRICE_PER_1000_RESULTS` | $2.00 | What users pay. Under pay-per-event pricing, platform costs come out of this. |

Suggested split of a $500 start:

| Item | Amount |
|---|---|
| OpenRouter credits (the $10 unlock plus paid repairs) | $100 |
| VPS for about 6 months | ~$40 |
| Apify paid plan, only if the free tier's monthly credit runs out | $150 |
| Untouched reserve | ~$210 |

## Safety rules built into the code

- **What gets scraped:** only public pages with no login. Social networks and other risky domains are blocked (`config.py`). Output fields that look like personal data (emails, phone numbers, people's names) are refused at build time (`spec.py`). robots.txt is respected.
- **AI-written code is untrusted.** The model writes extractors after reading untrusted HTML. Before running, the code must pass an allowlist check: no network, files, `eval` or dunder access. It then runs in a separate process with **no environment variables (your API keys are not visible)**, CPU and memory limits, and a timeout (`sandbox.py`).
- **The field list is a contract.** A repair can change how values are found, but never the field names or types (`validate.py`, `HEAL_SYSTEM`).
- **Nothing untested ships.** A repair goes live only if it passes the local check *and* a test run on Apify. Otherwise the last working code is restored and the Actor is marked as broken.

## Honest expectations

- Most Apify Store Actors earn nothing. Reef plays the numbers: it builds many small Actors, keeps the few that get users, and copies those to sibling sites.
- **Months 1–2:** expect about $0 while the fleet grows to 20–40 Actors.
- **Months 3–6:** anything from $0 to a few hundred dollars a month is realistic. It isn't guaranteed.
- **Suggested stop rule:** if day 120 shows under 100 active users across the fleet, run `pause`. You'll have spent roughly $300.

## Known limits (read these)

- **Built and tested against fakes, not the live services.** Pricing, publishing and Store search follow the official `apify-client` types, but have not been run against a real Apify account. If Apify rejects the pricing or publishing call, Reef marks the Actor `pending` and messages you a direct link. You set the price in the Console once, and Reef notices and publishes it. Run `doctor` first, then watch the first report.
- **No JavaScript rendering.** Reef only builds for sites whose data is in the page source. Sites that need a browser are rejected while scouting.
- **Model quality varies.** Free models fail more often at writing working extractors. Failed builds cost nothing but time, and retries switch to the paid model (within your budget).
- **Revenue isn't tracked.** Reef counts users. Check payouts in Apify Console.
- **Apify Store terms apply.** You're responsible for using the data and the Store within Apify's terms and local law.

## Commands

```
python -m reef doctor [--notify]   check keys, models, notifications
python -m reef run                 run forever (Docker default)
python -m reef tick                run all due jobs once (for cron instead of Docker)
python -m reef status              fleet table + recent events
python -m reef candidates          candidate sites and why they were accepted/rejected
python -m reef scout | spawn | heal [--force] | prune | report
python -m reef pause | resume
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The tests use a fake website that can switch layouts, a scripted model, and an Apify stand-in that runs the generated Actors locally with the real Apify SDK. Together they cover the whole cycle: scout → build → canary → site redesign → automatic repair → failed repair with rollback → recovery → prune → report.

## Layout

```
reef/
  cli.py, scheduler.py        entry points and the forever loop
  jobs/scout.py               find under-served sites (Store gap check + fetch check)
  jobs/spawn.py               spec + extractor via LLM, sandbox test, deploy, canary, price, publish
  jobs/heal.py                canary every Actor, repair, roll back, maintenance, retire
  jobs/prune.py               retire unused Actors, clone winners
  jobs/report.py              weekly digest
  actor_template/             generic crawl engine every Actor shares (only extractor.py differs)
  actor_pkg.py                builds the Actor source: schemas, README with real sample output
  sandbox.py                  static checks + isolated process for AI-written code
  llm.py                      OpenRouter: free models first, paid fallback, budget cap
  apify_gw.py                 Apify API gateway
```
