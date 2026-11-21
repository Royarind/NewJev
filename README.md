# NewJev — Computer Use Agent

> Browser and desktop automation powered by the **Jev TypeSafe decision model** — no vision LLM required
> (an optional screenshot fallback helps with apps that hide their controls).

## How It Works

```
User goal (natural language)
    ↓
Planner LLM — restates the goal, splits it into phases per app/site ([browser] / [desktop])
    ↓
Snapshot — DOM (all frames + open shadow roots) or Windows UI Automation tree. No screenshots.
    ↓
Jev (TypeSafe) — picks the operation + target, with probabilities
    ↓   below the confidence threshold → re-observe & ask again → still unsure → ask you
Text LLM — only supplies values (text to type, URL to open, app to launch)
    ↓
Executor — real input: Playwright mouse/keyboard, or Windows UIA + pyautogui
    ↓   risky clicks (Send, Delete, Pay …) → ask you · login / 2FA / CAPTCHA → hand over to you
Progress check — page fingerprint, loop detection
    ↓
DONE → independently verified by Jev Noul before it counts
```

### Operations Jev can choose

| | Browser | Desktop |
|---|---|---|
| CLICK / TYPE_TEXT / APPEND_TEXT / SELECT | ✓ (real mouse + keyboard events) | ✓ (UIA + mouse) |
| DOUBLE_CLICK / RIGHT_CLICK | | ✓ |
| PRESS_KEY | Enter, Esc, Tab, arrows, Ctrl+A/C/V … | Enter, Esc, F2, Ctrl+S/N/Shift+N … |
| SWITCH_TAB / GO_BACK / OPEN_URL | ✓ (new tabs are followed automatically) | |
| SWITCH_WINDOW / LAUNCH_APP | | ✓ |
| SCROLL_UP / SCROLL_DOWN / WAIT | ✓ | ✓ |
| VISUAL_CLICK / TYPE_FOCUSED (screenshot fallback) | ✓ | ✓ |
| SEND_EMAIL / READ_EMAIL / FILE_OP (shortcuts, when relevant) | ✓ | ✓ |
| NEED_USER / DONE / BLOCKED | ✓ | ✓ |

### Working with files (create / read / update / delete)

- **Find & open** an existing file: `OPEN_FILE` (searches Desktop, Documents, Downloads, Pictures, Music, Videos —
  OneDrive-aware — and opens it, optionally with a named app), `SEARCH_FILES` (types into Windows Search on the
  taskbar and picks a result), or File Explorer navigation (go to the folder, double-click / right-click → Open with).
- When a goal names a file that already exists, the agent starts in **File Explorer at that file's folder** instead
  of a blank app, and is told never to create a new document in its place.
- **Create / update**: type, append, Save / Save As (full paths are typed into the dialog).
- **Delete**: `DELETE_FILE` moves the item to the **Recycle Bin** (never a hard delete). Every deletion — including the
  Delete key in Explorer, Shift+Delete, or any "Delete" button — **always asks you first**, regardless of other
  settings; `--yes` and "Allow all" do not cover deletions, and unattended runs never delete
  (except benchmark files named `NewJev_Bench_*`). Drive roots, your home folder and Desktop/Documents/… themselves
  can never be deleted.

### Typing like a person

Desktop text is typed visibly, key by key (real key presses for your keyboard layout), in short chunks of words.
After each chunk the field is read back; if a busy app dropped or repeated letters, that chunk is erased with
Backspace and typed again more slowly (pasted only as a last resort), so text always arrives exactly once.
`TYPING_MODE=paste` pastes instead (fastest), `auto` types short text and pastes long text;
`TYPING_DELAY_MS` sets the speed.

### You take over → the agent pauses

If you click, scroll or type anywhere while a run is going (outside the NewJev window), the agent stops at once —
even in the middle of typing — and shows **⏸ Paused — you took over**. Do what you need, then press **Resume**
(or **Stop run**). It looks at the screen again before continuing. Its own clicks and keys are recognised
(Windows marks them as injected) and never pause it. Scheduled runs wait until you have left the PC alone for
`TAKEOVER_IDLE_RESUME_S` seconds. Settings: `TAKEOVER_PAUSE`, `TAKEOVER_INCLUDE_MOVES`.

### Fewer decisions per task

- **Plans from memory**: a fully verified run keeps its plan. The next request of the same kind (different file
  names / texts / numbers) reuses it with the new values swapped in — no planner call (`PLAN_RECIPES`).
- **One-step operations**: `TYPE_SUBMIT` types into a field and presses Enter; `SAVE_AS` opens Save As, types the
  full path, saves and **checks the file now exists** (never overwrites). Enter in a chat / message box counts as
  sending and is confirmed.

### Finishing touches

People do small "unsaid" steps without being told; so does the agent:

- **Before DONE** it checks for unfinished business — unsaved changes (`*file - Notepad`), a rename box still
  open, a menu still open, an "unsaved changes" dialog — and fixes it first (Enter, Ctrl+S, Escape, Save).
- **After the task** a short *Finishing up* phase saves and closes the windows/tabs the agent opened for the task
  (never windows you already had open). If the goal was to open/show something, it is left open.
- Jev's rules tell it to finish what it starts (confirm renames, save edits, submit requested forms), so
  situation-specific steps are handled even when no rule names them. Settings: `WRAP_UP`, `CLOSE_OPENED_WINDOWS`.

### Any app feature, without per-feature code

- **How-to procedures**: before acting, a model writes the step-by-step UI procedure for the task in that app
  (e.g. "Data tab → Sort → Sort by Sales → Largest to smallest → OK"). Jev gets it as a checklist, steps are
  ticked off as actions match, and the procedure is rewritten when the agent gets stuck. Shown live in the chat.
- **DRAG** (drag & drop, desktop + browser) and **SELECT_RANGE** (spreadsheet addresses via the Name Box:
  `A1:F200`, `C:C`).
- **Office shortcut (optional)**: `OFFICE_AUTOMATION=true` adds `OFFICE_ACTION`, which performs data-heavy steps
  through Excel/Word automation on the document already open (sort, filter, pivot, formulas, styles,
  find/replace). The generated code is checked against an allow-list first (no imports, files, saving, macros,
  other documents); anything that deletes or clears content asks you. Saving still goes through the normal
  finishing step.
- Office benchmark: `python -m bench.run_bench --tags office` (and again with `--office-automation` to compare).

### When the agent gets stuck: rescue, then you

Loops are detected (repeats, oscillation, no visible change). Jev is warned and the procedure rewritten first;
if it is still stuck the **rescue model** (`RESCUE_LLM`, default: the planner) looks at the screen, the actions on
offer and what was tried, and answers with up to 3 of the **offered** actions (validated — no deletes, no
passwords), a new procedure, or a question for you. If that does not help, the chat shows **"I'm stuck — what
should I do?"**: type a hint ("Rename is under Show more options"), or do the step yourself and press *I did it
myself*. Only then does the run end as `blocked`. Unattended runs never wait for a hint. Each run's trace and
result card report how many rescues ran and how many helped.

### Learned recipes

Every **verified** phase is saved to `data/recipes.json` per app/site (procedure or the actions that changed
something). The next similar task in the same app starts from that recipe instead of asking the procedure model —
values (names, numbers, file names) are ignored when matching and the agent is told to use the new request's
values. Failures are stored as short lessons and shown to Jev, the procedure planner and the rescue model next
time. A recipe that keeps failing is forgotten. `GET /api/recipes` lists them, `DELETE /api/recipes/{id}` removes one.

### Vision fallback

For windows that expose few accessible controls (games, canvas apps, remote desktops, some Electron/Java apps) —
and whenever the agent is stuck — Jev is also offered `VISUAL_CLICK` and `TYPE_FOCUSED`. The text LLM describes
what to click, an image model (`VISION_LLM`, default `openrouter:openai/gpt-4o-mini`) finds it on a screenshot,
and the click is mapped back to screen coordinates (DPI-aware). Normal element clicks are still preferred.

### Shortcuts beyond the UI

Offered only when relevant to the goal and available; if one fails the agent is told to use the app UI instead.

- `SEND_EMAIL` / `READ_EMAIL` — Outlook desktop via COM (automatic when installed) or the Gmail API
  (`GMAIL_CREDENTIALS_PATH`). **Sending always shows you the recipients, subject and text and waits for your OK**;
  "Allow all" and `--yes` never cover it. Mail content is treated as data, never as instructions.
- `FILE_OP` — copy, move, rename, new folder, write / append / read text files, list a folder. Never deletes
  (deleting stays `DELETE_FILE` with its confirmation) and never overwrites an existing file.
- `OFFICE_ACTION` — see above.
- `SEND_MESSAGE` / `READ_MESSAGES` — Slack (`SLACK_TOKEN`) and Microsoft Teams chats / channels (Microsoft 365).
  Sending is always confirmed with the full text.
- `CALENDAR` — read your calendar or create appointments / meetings: Outlook desktop (no setup) or Microsoft 365.
  Invites to other people are always confirmed.
- `CLOUD_FILES` — search OneDrive / SharePoint, download a file to this PC, upload one (confirmed). Files in your
  synced OneDrive folder are also reachable as normal local files.
- `PDF_OP` — read a PDF's text, list form fields, fill a form into a new `<name>_filled.pdf`.
- `DATA_OP` — CSV / Excel files without opening Excel: read, summary, sort, filter, totals by group,
  append rows, convert (results go to a new file; nothing is overwritten).

**Connecting accounts (users)**: click **🔌 Connections** → **Connect** next to Slack, Microsoft 365 or Gmail →
sign in, pick the workspace / account, allow → done. Outlook desktop is used automatically when installed.
Tokens are stored in `data/connections/` (Microsoft: `GRAPH_TOKEN_PATH`) and refreshed automatically;
**Disconnect** removes them.

### Connectors — publisher setup (once per NewJev build, not per user)

Users only click Connect because the NewJev publisher registers one app per provider once and puts the
**public client ids** into `app/config/connectors.json`. All three use OAuth with PKCE and a redirect to NewJev on
the user's own PC, so no client secret has to be protected (Google's desktop "secret" is not confidential).

1. **Slack** — api.slack.com/apps → *Create New App → From an app manifest* → paste `tools/slack_app_manifest.json`
   (user scopes, PKCE, token rotation, redirect `http://localhost:8766/oauth/slack/callback`). Then *Manage
   Distribution → Activate public distribution* so other workspaces can install it. Copy the **Client ID** into
   `slack_client_id`. (Enabling PKCE is permanent for that app.)
2. **Microsoft 365** — Azure portal → *Entra ID → App registrations → New registration*: "Accounts in any
   organizational directory", platform **Mobile and desktop applications** with redirect URI `http://localhost`
   (any port is accepted), *Allow public client flows = Yes*, delegated Graph permissions User.Read, Chat.ReadWrite,
   ChannelMessage.Send, ChannelMessage.Read.All, Team.ReadBasic.All, Channel.ReadBasic.All, Calendars.ReadWrite,
   Files.ReadWrite.All, Sites.Read.All. Copy the **Application (client) ID** into `microsoft_client_id`. Some
   companies require an admin to approve the app the first time someone connects.
3. **Gmail** — Google Cloud console → enable the Gmail API → OAuth consent screen → *Credentials → Create OAuth
   client → Desktop app*. Copy client id and secret into `google_client_id` / `google_client_secret`. While the app
   is in "Testing", only listed test users can connect and sign-ins expire after 7 days; for everyone, Google must
   verify the app (Gmail scopes are "restricted").

The port in the redirect URLs must match `PORT` (8766). Values in `.env` (`SLACK_CLIENT_ID`, `GRAPH_CLIENT_ID`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`) override `connectors.json`.

**Connecting Microsoft 365 manually** (alternative, device code) (Teams, calendar via Graph, OneDrive / SharePoint): in the Azure portal → *Entra ID →
App registrations → New registration* (any name, "Accounts in this organizational directory"), then
*Authentication → Allow public client flows = Yes*, and *API permissions → Microsoft Graph → Delegated*: User.Read,
Chat.ReadWrite, ChannelMessage.Send, ChannelMessage.Read.All, Team.ReadBasic.All, Channel.ReadBasic.All,
Calendars.ReadWrite, Files.ReadWrite.All, Sites.Read.All (your admin may need to grant consent). Put the
*Application (client) ID* in `GRAPH_CLIENT_ID` and run `.venv\Scripts\python tools\connect_microsoft.py`.

**Connecting Slack**: create a Slack app at api.slack.com/apps, add the scopes chat:write, channels:read,
groups:read, im:read, im:write, users:read, users:read.email, channels:history, groups:history, im:history,
install it to your workspace, and put the token in `SLACK_TOKEN`.

### Model fallback, rate limits, cost

`TEXT_LLM_FALLBACKS` / `PLANNER_LLM_FALLBACKS` list `provider:model` pairs tried in order on 429, timeouts, 5xx or a
bad key / unknown model. A short `Retry-After` is honoured; a long one puts the model on cool-down
(`LLM_COOLDOWN_S`) and the next model answers immediately. The SDKs' hidden retries are off, so nothing stalls
silently. Every call (planner, text, rescue, vision, Jev) is timed and costed — OpenRouter reports the real cost,
others use `LLM_PRICES` — and shown per model on the result card, in run history and in the trace.
`python tools\check_llm.py` also probes every fallback model.

### Steering a run

While a run is going, the input box stays active: type an instruction ("use the second sheet", "the file is in
Documents") and press Enter. It is applied at the next step — shown to Jev as an instruction that overrides
earlier ones, given to the text LLM, used to rewrite the checklist, and included in the final verification.

### Long jobs: "for every file…", "for each row…"

A request that repeats work over many items — *"For every PDF in Downloads, rename it to include today's date"*,
*"For each row in contacts.csv, email the person a welcome note"* — becomes a **job**: the items are listed
(files in a folder, rows of a CSV / Excel sheet, or a list), and each item runs as its own agent run. Progress is
saved after every item (`data/jobs`), so after a crash, restart or Stop the job continues where it left off:
**Tasks → Unfinished long jobs → Resume**, or `python run.py --jobs` / `--resume-job <id>`. Three failed items in a
row pause the job so it doesn't repeat the same mistake on every item.

### Pop-ups when the agent needs you

Every confirmation, hand-off, take-over pause and "I'm stuck" question also slides up as a small always-on-top
pop-up in the bottom-right corner (above the taskbar, with the Windows notification sound). Answer it right there —
Allow / Allow all / Reject, Send, Resume, a typed hint … — without switching back to the NewJev tab. It closes by
itself when the request is answered anywhere or expires, and is not shown while the NewJev window is in front.
`DESKTOP_POPUPS=false` turns it off.

### Voice and the Ctrl+Alt+J command box

- 🎤 next to **Run** (Edge / Chrome): speak a goal — or an instruction while a run is going.
- **Ctrl+Alt+J** anywhere in Windows opens a small NewJev box: type or speak a goal, Enter runs it in the NewJev
  window (or sends it as an instruction to the run in progress). Change it with `HOTKEY`.

### Run history and replay

**History** (header button) lists past runs from `logs/trace_*.json`: status, steps, time, cost, rescues. A run's
page shows the phases, your mid-run instructions, rescues, model time and cost, **screenshots taken at key
moments** (each phase end, when stuck), and every step with its value and result. **Run again** re-runs the
original request; **Save as task** turns it into a reusable task.

### Saved tasks, schedules and folder triggers

**Tasks** (header button): save a goal with `{placeholders}` ("Import {file} into the Sales workbook and email
{who}"), default values, a mode/URL, and optionally

- a **schedule** — `every 30m`, `daily 09:00`, `weekdays 08:30`, `mon,fri 10:00`, `once 2026-10-05 09:00`
  (local time; a run missed while the PC was off runs at the next check), and/or
- a **watch folder** + pattern — a new file (once its size is stable) runs the task with `{file}`, `{filename}`
  and `{folder}` filled in. Files already there when the server starts never trigger.

*Run now* runs it in the chat so you can watch and answer prompts; *Run in background* and schedules run
unattended: deleting is never allowed, Send/Submit/Pay-type clicks and sending mail only if the task has
"allow when nobody is watching", and logins end the run as `needs_user`. Background runs never start while you
have a run going. Each task keeps its last 30 results (with links to the run details).
CLI: `python run.py --list-tasks`, `python run.py --saved "Import CSV" --param file=C:\data\a.csv`.

### Run statuses

`done` (verified) · `unverified` (finished but the check failed or was unavailable — look at it) ·
`needs_user` (stopped at a login/2FA/CAPTCHA) · `blocked` · `max_steps` · `stopped` · `error`.
A multi-phase plan stops at the first phase that does not finish and reports every phase's status.

## Quick Start

### 1. Setup

```bash
# Windows: just double-click start.bat
# Or manually:
python -m venv .venv
.venv\Scripts\activate
pip install -e .
playwright install chromium
```

### 2. Configure

```bash
copy .env.example .env
# Edit .env and add your keys:
# - OPENROUTER_API_KEY (required — used for both Jev and LLMs)
```

### 3. Run

```bash
# Web UI with chat interface (default)
python run.py

# Interactive terminal
python run.py --cli

# Single task
python run.py --task "Search for flights from Delhi to Mumbai"
python run.py --task "Open Notepad and type Hello World" --mode desktop
python run.py --task "..." --yes     # auto-approve confirmations (exit code 0 only on verified DONE)
```

When the agent hits a sign-in, 2FA code or CAPTCHA it pauses and asks you to do that step in the
browser/app window, then continue (web UI button, or Enter in the terminal). Passwords are never typed by the agent.

Open **http://127.0.0.1:8766** for the chat UI.

## Configuration (.env)

All settings are in `.env`. Key options:

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | _(required)_ | Used for Jev + LLMs |
| `JEV_MODEL` | `jev-latest` | Jev model version |
| `TEXT_LLM_PROVIDER` | `openrouter` | Provider for text generation |
| `TEXT_LLM_MODEL` | `qwen/qwen3.8-27b:free` | Model for typing text, values, Office code |
| `TEXT_LLM_TEMPERATURE` | `0.2` | Generation temperature |
| `PLANNER_LLM_PROVIDER` | `openrouter` | Provider for goal planning |
| `PLANNER_LLM_MODEL` | `nvidia/nemotron-3-super-120b-a12b:free` | Planning & how-to procedures |
| `HEADLESS` | `false` | Show/hide browser window |
| `CAPTURE_MODE` | `browser` | `browser` / `desktop` / `auto` |
| `MAX_STEPS` | `60` | Max actions per run |
| `JEV_CONFIDENCE_THRESHOLD` | `0.6` | Below this: retry, then ask you |
| `VERIFY_DONE` | `true` | Verify DONE with Jev Noul |
| `CONFIRM_RISKY_ACTIONS` | `true` | Ask before Send/Delete/Pay … (`RISKY_KEYWORDS`) |
| `HANDOFF_TIMEOUT_S` | `600` | How long to wait for you during a login / 2FA hand-off |

See `.env.example` for the full list.

## LLM Providers

Supported providers (set in `.env`):

| Provider | Variable | Notes |
|---|---|---|
| OpenRouter | `TEXT_LLM_PROVIDER=openrouter` | 300+ models, recommended |
| OpenAI | `TEXT_LLM_PROVIDER=openai` | GPT-4o, GPT-4o-mini |
| Anthropic | `TEXT_LLM_PROVIDER=anthropic` | Claude 3.x |
| Groq | `TEXT_LLM_PROVIDER=groq` | Ultra-fast, free tier |
| HuggingFace | `TEXT_LLM_PROVIDER=huggingface` | Open models |
| Llama/Ollama | `TEXT_LLM_PROVIDER=llama` | Local models |
| Moonshot | `TEXT_LLM_PROVIDER=moonshot` | Kimi models |

## Architecture

```
app/
├── config/settings.py    # All .env settings via pydantic-settings
├── jev/client.py         # TypeSafe Jev API (Choice / Noul / Score)
├── llm/
│   ├── factory.py         # Provider factory
│   ├── openai_compat.py   # OpenRouter, OpenAI, Groq, HF, Llama
│   └── anthropic_provider.py
├── agent/
│   ├── planner.py         # Goal understanding → per-app phases
│   ├── loop.py            # Main Jev decision loop (single phase + multi-phase plans)
│   ├── guards.py          # Loop detection, risky-action and hand-off detection
│   ├── review.py          # Confirmations / hand-offs (web UI, terminal, unattended)
│   └── verifier.py        # Jev Noul verification
├── capture/
│   ├── browser.py         # Playwright snapshot (frames, shadow DOM) + real input
│   ├── snapshot.js        # In-page element/text extraction
│   └── desktop.py         # Windows UIA snapshot, window/app management
├── executor/__init__.py   # CaptureManager: one browser + one desktop session per run
└── ui/
    ├── server.py          # FastAPI + WebSocket
    └── static/            # Chat UI + Inspector
```

## Benchmarks & tests

```bash
python -m bench.run_bench --list                 # what can run on this machine
python -m bench.run_bench                        # all runnable tasks (web + desktop on Windows)
python -m bench.run_bench --tags web --repeat 3  # only web tasks, 3 runs each
python -m bench.run_bench --interactive --only gmail_send   # tasks that need you to log in
pip install -e .[dev] && pytest                  # unit + real-browser executor tests
```

Tasks live in `bench/tasks.json` (19 tasks: offline fixture, web forms/search, Notepad, Calculator,
File Explorer, Settings, Office, and cross-app copy/paste). Each task has post-condition checks
(file contents, folder exists, final URL/text), so a run only counts when the result is really there.
Results are saved to `bench/results/` with the success rate and the number of **false DONEs**.
Benchmarks only ever create or delete files named `NewJev_Bench_*`.

## Logs & Traces

All runs are saved to `logs/` as JSON traces with full step history, Jev probabilities, and timing.
