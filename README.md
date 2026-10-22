# Leash

**A budget your AI agent can't overspend, and a record it can't fake.** Each agent gets an HMAC-signed pass with a hard money limit instead of your API key. The key stays encrypted with AES-256-GCM where the agent can't reach it, and every call is sealed into an HMAC-chained record that catches any edit.

Hackathon track: *Safety by Design*. Hosted by SMU BIA AI Lodge at **https://ail-ay2627-s1-philbert-leash.vercel.app**: [watch a real agent hit its limit](https://ail-ay2627-s1-philbert-leash.vercel.app/try).

![Leash](docs/screenshots/00-cover.png)

## Why

A developer left an AI agent to finish a task with a $100 budget. When he came back, it had spent about $5,000, doing far more than he asked. When he checked its logs, they said nothing had gone wrong.

Three things failed, and Leash fixes each one:

| What failed | What Leash does |
|---|---|
| **The budget was a request, not a limit.** | The agent holds a pass signed with HMAC-SHA256 that carries its budget. Before every call Leash reserves the most that call could cost and refuses it if it could go over, then settles the real cost from the provider's usage numbers. Changing the pass breaks its signature. |
| **The agent held the real API key.** | The key is encrypted with AES-256-GCM (envelope encryption, master key outside the database) and decrypted only inside Leash for the moment of each approved call. |
| **The agent wrote its own logs.** | Leash writes the record, not the agent. Each entry is sealed with `HMAC-SHA256(key, previous seal ‖ entry)`, so changing or deleting any entry is caught at that exact entry. |

## Demo 1: an agent with a budget, running for real

On `/try` (the first tab), anyone can give a real AI agent (gpt-4o-mini) a task and a budget of a tenth of a cent, paid from the server's key:

1. The agent is told to keep improving its work, so it overdoes it, like the agent in the story. Every step is a real call through Leash, with its real cost.
2. Leash refuses the step that could take it over budget, before it is sent. It stops just under the limit, never over.
3. The agent writes its own status report. Leash shows it next to its own record of the same run: steps allowed and refused, real spend, and the seal check.
4. **Try to fake the record** really edits the database, changing one step's cost to $0.00, and Leash names the entry that was changed.

![A real agent stopped at its budget](docs/screenshots/17-agent-run.png)

The exact instructions given to the agent are shown on the page. Real AI in the public demo is kept small: a real pass carries at most 1 cent, each call at most 150 output tokens and 8,000 characters, and the shared AI Lodge rate limiter counts every real call per visitor. Past that limit the run stops and the AI Lodge invitation opens.

## Demo 2: Maya's weekend (attacks)

Maya runs a small online shop. On Friday she lets an AI assistant answer customer emails while she's away.

| Fri 11pm: a fake "IT Support" email tricks the assistant; Leash blocks it | Sat 3am: an auto-reply loop; Leash stops it at its limit | Mon: someone edits the record; Leash catches it |
|---|---|---|
| ![](docs/screenshots/03-story-attack.png) | ![](docs/screenshots/04-story-limit.png) | ![](docs/screenshots/05-story-coverup.png) |

The "admin key" in the email is a trap key Leash planted, the reply carrying it is blocked before it leaves, the loop stops at the limit, Maya gets a Slack message, and the record proves what happened.

## Leash as a service

> Part of the original hackathon build, not of the hosted AI Lodge demo (see [The pages](#the-pages)).

Teams sign up at `/signup` and get a private workspace at `/app`:

1. **Save your AI key.** Any OpenAI-compatible provider. Encrypted before it's saved (see below).
2. **Give each agent a pass.** A money limit, a call limit and an end date (1 hour to 30 days). The pass is shown once, with the two lines to change in your agent and a **Try it** button that sends a test call through Leash.
3. **Get alerts in Slack or Discord.** Trap key used, leak blocked, an agent hit its limit, or every agent stopped.
4. **Run a test agent** on your own saved key: the same run as Demo 1, so a team can watch its limit hold and check its record before connecting a real agent. In a workspace, **Try to fake the record** edits a copy of the record, so the real one is never touched.

Plus a list of every agent with its spend and a **Stop** button, a kill switch for all of them, trap keys to plant in your own files, and the activity log with **Check the log**.

| Workspace | Agents |
|---|---|
| ![](docs/screenshots/14-workspace.png) | ![](docs/screenshots/16-workspace-agents.png) |

How accounts are protected:

- **Passwords** are hashed with scrypt (memory-hard, random salt per password). Log-in attempts are rate-limited per address and per email, and take the same time whether or not the email has an account.
- **Sessions** are a signed cookie (HMAC-SHA256 with its own derived key), HttpOnly, SameSite=Lax, Secure over HTTPS, valid for 7 days.
- **Cross-site request forgery** is blocked by requiring an `X-Leash-App: 1` header on every account and workspace request.
- **Isolation:** each workspace is its own space (`acct_<id>`). Every key, pass, alert, trap key, setting and log entry belongs to exactly one space, and tests check one team can't see, stop or use another's.
- **Webhooks are secrets**, so Slack and Discord addresses are encrypted like API keys. Only `https://hooks.slack.com/...` and `https://discord.com/api/webhooks/...` are accepted, so the server can't be aimed at internal addresses. Alerts are sent in the background and rate-limited.
- Workspaces can't use the server's own key, have no demo buttons (tampering, reset), and are never touched by the clean-up that deletes old public demos.

## What Leash does

Leash is a proxy that sits between agents and the AI provider. The real key stays on the server. Agents get a **Leash token** instead.

```
Agent ──(Leash token)──▶ Leash ──(real OpenAI key)──▶ OpenAI
                           │
             1. canary key?            → block + alert
             2. raw provider key?      → reject
             3. signature + expiry     → HMAC-SHA256 check
             4. revoked / kill switch? → block
             5. secrets in the prompt? → block + alert
             6. call limit + budget    → block
             7. forward, settle real cost, seal into the audit chain
```

| Protection | How it works |
|---|---|
| **Scoped tokens** | `lsh_<claims>.<HMAC-SHA256 signature>`. Claims hold agent name, expiry, max calls and budget. Changing one character breaks the signature. Compared in constant time. |
| **Budgets that hold** | Every call is reserved before forwarding using a worst-case cost estimate, then settled using OpenAI's `usage` numbers. |
| **Kill switch** | Revoke one token, or stop every agent at once. Takes effect on the agent's next call. |
| **Tamper-evident log** | Each entry's seal is `HMAC-SHA256(key, previous_seal ‖ entry)`. Editing or deleting any entry breaks the chain at that exact point. Keyed hashing means someone with only database access can't recompute the chain. |
| **Canary keys** | Decoy keys that look exactly like OpenAI keys are planted where only an injected agent would find them. Any use fires an alert. |
| **Outbound secret scanning** | Prompts containing API keys, Leash tokens, private keys or canaries are blocked before they leave. |
| **Privacy in the log** | Raw keys are never logged, only their SHA-256 fingerprints. Prompts are stored as a SHA-256 hash plus a short preview. |
| **Key separation** | One master secret; separate HMAC keys are derived from it for tokens, the audit chain and canaries. |
| **Tokens never stored** | Leash keeps only each token's id and counters. The token itself is shown once. |
| **Bring your own key, encrypted** | Teams can store their own OpenAI-compatible provider key. See below. |

### Bring your own key, encrypted

Teams can connect their own provider (any OpenAI-compatible base URL, model and key) and issue Leash tokens against it. The key is protected with **envelope encryption**, the pattern cloud key-management services use:

1. A fresh random 256-bit **data key** encrypts the API key with **AES-256-GCM**.
2. The **master key** (`KEY_ENCRYPTION_KEY`, an environment variable, never in the database) encrypts the data key, also with AES-256-GCM.
3. The database stores only the two ciphertexts, plus a short hint (`…3456`) and an HMAC-SHA256 fingerprint to spot duplicates.

### If the website is attacked

Envelope encryption alone has a weak spot: someone who steals the database **and** the master key (for example from the host's settings) can decrypt every key. So keys saved in a customer workspace get two more locks, and Leash keeps neither secret:

1. **Locked to the team's password.** At sign-up and login, Leash derives an *account key* from the password with scrypt (salted per account with a server-side HMAC key, so it differs from the stored password hash). The data key is wrapped with `HMAC(master key, account key)`, which needs both. The account key is never stored; it lives only in the session cookie, encrypted with AES-256-GCM and bound to the account id.
2. **Each pass carries its own unlock code.** When a pass is created, Leash unwraps the data key with the account key, then stores a second copy wrapped with `HMAC(master key, pass secret)`. The 128-bit pass secret sits only inside the pass (`k` in its signed claims) and is never stored or shown in the readable claims. At call time the pass presents its secret, the copy is unwrapped, the key is decrypted for that one call and dropped.

| What the attacker has | Can they read a workspace key? |
| --- | --- |
| A copy of the database | No |
| The database and the master key | No: they also need a team's password or a live pass |
| The database, the master key and the signing secret | No: they can forge a pass, but not its unlock code (`PASS_CANT_UNLOCK`) |
| Control of the running server | Yes, for keys used while they're in control. Nothing can fully stop this, so use a key made just for Leash with a spending limit at the provider |

Stopping a pass deletes its copy of the lock, removing a key deletes all of them, and expired passes' copies are cleaned up. Keys saved before this upgrade are re-locked automatically the next time the team logs in (`KEYS_UPGRADED` in the log), and older sessions simply ask the team to log in again. The pages also send a strict Content-Security-Policy (only Leash's own scripts may run, and pages may only talk to Leash), HSTS, and `frame-ancestors 'none'`, so injected markup can't send a key anywhere. `tests/keyprotect_test.py` plays this attacker (49 checks, passing on SQLite and Postgres): `python tests/keyprotect_test.py`.

Details that matter:

- **Tamper-proof:** AES-GCM is authenticated. Flip one bit of the stored ciphertext and decryption fails, so the call is refused (`KEY_INTEGRITY_FAILED`) and an alert fires. Both layers are bound to their row as associated data, so ciphertext copied into another row won't decrypt either.
- **Decrypted just in time:** a key is decrypted only inside the request that forwards a call, then dropped. It's never returned by the API, shown in the dashboard or written to the log, and provider error messages are scrubbed of key fragments.
- **Crypto-shredding:** removing a key erases its ciphertext and wrapped data key, so it can never be recovered and its tokens stop working immediately.
- **Rotation-friendly:** each record notes which master key wrapped it (`kek_id`). Rotating the master key only means re-wrapping the small data keys, not touching the API keys.
- **Safe in the sandbox:** visitors can store keys to see exactly what the database keeps, but sandbox calls always go to the fake AI, so the page asks for a made-up key. Sandboxes are deleted after 3 hours.

![Bring your own key](docs/screenshots/11-key-tamper-caught.png)

It is **OpenAI-compatible**: any agent built on the OpenAI SDK works by changing `base_url` and `api_key`:

```python
from openai import OpenAI
client = OpenAI(base_url="https://<your-app>.onrender.com/v1", api_key="lsh_...")
```

## The pages

The pages are a static site in `frontend/`, deployed on Vercel. They call the API on Cloud Run.

| Page | What |
|---|---|
| `/` | Landing page in six tabs with Back and Next: why Leash exists, how it works, the seven checks, security, attacks (Maya's weekend), and who it's for. |
| `/try` | Public demo in four tabs with Back and Next: a real agent with a budget (real AI within the caps below), Maya's weekend, each protection on its own, and the log and tools. Each visitor gets an isolated copy, deleted after 3 hours. |

This deployment is the public demo only. The original hackathon build also had sign-up and team workspaces (`/signup`, `/app`), bring-your-own-key storage, Slack and Discord alerts, and a password-protected operator control room (`/control`). Those are not part of the AI Lodge deployment, because the hosting club shouldn't hold other people's provider keys or webhooks; the sections above describe the design.

## Demo script (about 3 minutes)

1. **The story (20 seconds).** Open the landing page and read the "Why we built Leash" section: $100 budget, $5,000 bill, logs that said all was fine.
2. **Demo 1 (90 seconds).** On `/try`, pick a task, keep the budget at $0.001, press **Start the agent**. Point at the money meter filling up and the agent's "Next:" plans. When it's refused: "It wanted to keep going. In the story it went to $5,000; here it can't go over a tenth of a cent." Read the agent's own report next to Leash's record, then press **Try to fake the record** and show the entry that was caught.
3. **Demo 2 (optional, 60 seconds).** Run Maya's weekend for prompt injection: the trap key and the blocked leak.

## Run it locally

Backend (FastAPI, SQLite by default):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # fill in SIGNING_SECRET; API_KEY is optional
uvicorn app.main:app --port 8787 --reload
curl http://localhost:8787/api/health   # {"status":"ok"}
```

Leave `DATABASE_URL` unset to use a local `leash.db` SQLite file. Without `API_KEY` the demo runs on the free fake AI only.

Frontend (static, no dependencies):

```bash
cd frontend
VITE_API_BASE=http://localhost:8787 npm run build   # writes dist/ and config.js
python -m http.server 3000 --directory dist         # open http://localhost:3000/try.html
```

The backend's default `CORS_ORIGINS` is `http://localhost:3000`.

To watch an external agent get cut off, issue a token under **Log and tools** on `/try` and run:

```bash
python examples/agent.py http://localhost:8787 lsh_...
```

## Deploy (AI Lodge: Cloud Run + Vercel + Neon)

- **Backend → Cloud Run.** `.github/workflows/deploy.yml` builds the `Dockerfile` at the repo root, pushes it to Docker Hub and deploys the service `ail-ay2627-s1-philbert-leash` on every push to `main`. Env vars come from the repo's `DOTENV` secret (`gh secret set DOTENV < .env`). The service URL is `https://ail-ay2627-s1-philbert-leash-464725632804.asia-southeast3.run.app`.
- **Single instance.** The workflow pins `--max-instances=1` (the club default is 2) and the container runs one uvicorn worker: budgets and audit chains are written under an in-process lock. State lives in Postgres, so scaling to zero is fine.
- **Database → Neon.** Set `DATABASE_URL` to the Neon connection string with `?sslmode=require`. Tables are created at startup (`CREATE TABLE IF NOT EXISTS`), so an empty database is all it needs.
- **Frontend → Vercel.** Root directory `frontend`, build command `npm run build`, output `dist` (both in `frontend/vercel.json`). Set `VITE_API_BASE` to the Cloud Run URL. `build.js` writes `config.js` and a Content-Security-Policy whose `connect-src` allows that origin only. The site is `https://ail-ay2627-s1-philbert-leash.vercel.app`, which goes in the backend's `CORS_ORIGINS`.
- **Rate limit.** Real AI in the public demo is limited only by the shared AI Lodge rate limiter, per IP: bucket `agent` counts each real-AI agent run (issuing a real-AI pass; `RATE_LIMIT_AGENT_MAX`, default 5) and bucket `agent_call` counts each real model call the proxy forwards (`RATE_LIMIT_AGENT_CALL_MAX`, default 40), both per `RATE_LIMIT_WINDOW_SECONDS` (default 5 days). A call Leash itself refuses (over its pass budget, for example) is not counted. Over the limit the page shows the AI Lodge modal and the real-AI run stops (it does not switch to the free fake AI); a paused demo (503) shows a notice and stops the run too. Each pass's own money limit is the product being demonstrated, not cost control.

**Before demoing with a real key:** set a credit limit on the OpenRouter key as well.

## Environment variables

Backend (see `.env.example`):

| Variable | Required | Purpose |
|---|---|---|
| `SIGNING_SECRET` | yes | Master secret. All signing keys derive from it. Generate once (`python -c "import secrets; print(secrets.token_hex(32))"`) and keep it: changing it invalidates every stored chain. Required whenever `DATABASE_URL` is set. |
| `DATABASE_URL` | yes in production | Neon Postgres, `postgresql://...neon.tech/db?sslmode=require`. Locally, SQLite is used if unset. |
| `CORS_ORIGINS` | yes in production | Comma-separated origins allowed to call the API, e.g. the Vercel URL. |
| `API_KEY` | no | Enables real AI through OpenRouter. Without it the demo is fake-AI only. |
| `MODEL_NAME` | no | The only model real-AI passes may use. Default `openai/gpt-4o-mini`. |
| `PRICE_INPUT_PER_1M`, `PRICE_OUTPUT_PER_1M` | no | The model's prices in USD per 1M tokens, used for the worst-case estimate before each call (the settled cost uses OpenRouter's reported `usage.cost`). Defaults 0.15 / 0.60 match `openai/gpt-4o-mini`. |
| `SANDBOX_REAL_MAX_PASS_USD` | no | Largest money limit a real-AI pass in the public demo can carry. Default 0.01. |
| `RATE_LIMIT_AGENT_MAX`, `RATE_LIMIT_AGENT_CALL_MAX` | no | Fallback per-IP limits for real-AI runs and real calls when the central control sheet has no row. Defaults 5 and 40. |
| `DEFAULT_MAX_COMPLETION_TOKENS` | no | Output cap per real call if the agent sets none. Default 300. |
| `SANDBOX_ENABLED` | no | Set to `false` to turn off the public sandbox. Default on. |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | yes in production | Shared AI Lodge rate limiter. Without them, run limits are off (fine locally). |
| `RATE_LIMIT_AGENT_MAX`, `RATE_LIMIT_WINDOW_SECONDS` | no | Fallback run limit per IP and window. Defaults 5 and 432000. |

Frontend (Vercel): `VITE_API_BASE`, the Cloud Run URL.

## API

| Endpoint | What |
|---|---|
| `GET /api/health` | `{"status": "ok"}` |
| `POST /v1/chat/completions` | OpenAI-compatible proxy. `Authorization: Bearer lsh_...` |
| `POST /unprotected/v1/chat/completions` | The "no Leash" half of the demo (fake AI only, no checks) |
| `POST /api/sb/session` | Open or reuse a sandbox; returns its id |
| `GET /api/sb/config`, `GET /api/sb/stats`, `GET /api/sb/canaries` | Sandbox settings, counters and trap keys |
| `POST /api/sb/tokens`, `GET /api/sb/tokens` | Issue or list passes (a real-AI pass counts against the `agent` rate limit) |
| `POST /api/sb/tokens/{id}/revoke`, `GET /api/sb/tokens/{id}/receipt` | Revoke a pass; Leash's own account of one pass with the seal check |
| `POST /api/sb/kill` | Stop or release every agent in the sandbox |
| `GET /api/sb/audit`, `GET /api/sb/audit/verify`, `GET /api/sb/alerts` | Read and verify the chain; alerts |
| `POST /api/sb/demo/tamper`, `POST /api/sb/demo/reset` | Edit a log entry the way a cover-up would; start a fresh chain |

Every `/api/sb/*` call except `session` needs the `X-Leash-Session` header with the sandbox id. A custom header can't be sent cross-site without a CORS preflight, which only `CORS_ORIGINS` pass, so this also blocks cross-site request forgery.

## Abuse protection on the public pages

- Real AI in the public demo is kept small: at most 1 cent per real pass, 150 output tokens and 8,000 characters per call, and the shared rate limiter's per-visitor counts of runs and calls. Past the limit the run stops with the AI Lodge invitation.
- Real agent runs per IP are limited by the shared AI Lodge rate limiter (bucket `agent`), which also has an operator kill switch.
- Sandbox limits are clamped: 10 minutes, 100 calls and $5 per pass, 60 passes and 3,000 log entries per sandbox, 300 sandboxes at once.
- In-memory per-IP limits on the free paths (proxy, sandbox API, new sandboxes, unprotected demo path) stop floods.
- Failed attempts that can't be tied to a sandbox are logged to a server-wide chain at a limited rate.

## Project layout

```
app/
  main.py               proxy and sandbox API
  tokens.py             HMAC-signed scoped tokens
  audit.py              HMAC hash chain and verification, one chain per space
  guards.py             canary keys and outbound secret scanning
  upstream.py           fake upstream, OpenRouter forwarding, cost accounting
  rate_limit.py         adapter for the shared AI Lodge rate limiter (bucket "agent")
  rate_limit_engine.py  shared engine, vendored unmodified
  ratelimit.py          in-memory abuse limits on the free paths
  config.py             settings from environment variables, key derivation
  db.py                 tables, created at startup (SQLite locally, Neon in production)
frontend/               static pages for Vercel (index.html, try.html, build.js)
examples/agent.py       a runaway agent to point at Leash
Dockerfile, .github/workflows/deploy.yml
```

## Limits and next steps

- **Single instance.** A process lock keeps budgets and the chain consistent, so the service runs one worker on one instance. Scaling out would need a database-level lock.
- **Deleting the newest entries** can't be detected from the chain alone. Fix: periodically publish the chain's head hash somewhere external (an email, a public repo, a timestamping service).
- **Streaming** responses aren't supported yet; `stream` is forced off.
- **Changing `SIGNING_SECRET`** invalidates every token and makes the old log fail verification from entry #1. That's by design. Use **Reset demo** to start a fresh chain.
- **Next:** per-tool scopes (not just per-model), human approval with signed sign-off for risky actions, Ed25519 signatures so third parties can verify the log without the secret.
