# upwork-scout — local Upwork job triage

upwork-scout is a local script for a single Windows user. One manual run (`.\run.bat`) opens
the installed Google Chrome with a separate profile (`browser-profile/`); the first time you
log in to Upwork manually, and the session is kept afterwards. The script dynamically
discovers **all** of your saved searches on `https://www.upwork.com/nx/find-work/` — via links
shaped like `/nx/find-work/{id}`, with no hardcoded ID or name — and opens them one by one in a
single tab. For each search it collects job cards (optionally clicking "Load More Jobs" and
opening job pages), merges duplicates by job ID in SQLite, applies hard rules and a
transparent 0–100 score, optionally asks Jev for its opinion through OpenRouter if a key is
configured, and writes `outputs/upwork-report-YYYY-MM-DD-HH-mm.html`. The script is read-only:
it never submits proposals, sends messages, saves jobs or reacts to anything on Upwork.

## Stack

- Python 3.11+ (tested on 3.14)
- Playwright, `channel="chrome"` — drives the installed Google Chrome, not a separate Chromium
- BeautifulSoup — HTML parsing
- SQLite — storage for jobs, searches and evaluations
- Jinja2 — HTML report
- PyYAML — `config.yaml`
- pydantic — settings validation
- httpx — requests to Jev via OpenRouter
- python-dotenv — reads `.env`
- pytest — tests

## Project structure

```
upwork_scout/
  app.py            — CLI entry point: argument parsing, logging, exit codes
  browser.py        — Playwright driver: installed Chrome, one tab, read-only
  scraper.py         — one pass over all saved searches (login, discovery, collection, details)
  discovery.py         — dynamic discovery of saved searches on the Find Work page
  parsing.py             — parses job cards and job pages into Job objects
  guards.py               — detects CAPTCHA / security check / login pages
  ids.py                    — normalisation of job IDs and saved-search IDs
  htmltext.py                — readable text extraction from HTML (BeautifulSoup)
  textmatch.py                 — case-insensitive, word-bounded phrase matching with `*`
  models.py                      — Job and SavedSearch dataclasses, merging, content hash
  scoring.py                       — hard exclusion rules + explainable 0–100 score
  ai.py                              — optional AI evaluation stage via Jev (OpenRouter)
  storage.py                          — SQLite storage (runs, searches, jobs, evaluations)
  pipeline.py                          — evaluates a run's jobs and builds the report context
  report.py                             — HTML report rendering (Jinja2)
  fmt.py                                  — shared formatting (money, age, budget…)
  config.py                                — loads and validates config.yaml and .env
  templates/report.html.j2                  — report HTML template
config.yaml            — non-secret settings (see "Configuring config.yaml")
.env / .env.example    — secrets (OpenRouter key) and their template
run.bat                — runs the script inside .venv
tests/ (+ fixtures)    — tests on local HTML fixtures, no network or browser
data/                  — SQLite database (data/upwork.sqlite3), created automatically
outputs/                — HTML reports from each run
logs/                    — log file (logs/upwork-scout.log)
browser-profile/          — separate persistent Chrome profile used by the script
debug/                      — sanitised HTML of empty search pages (only if collection.debug_dump_html: true)
```

## Installation

```
python -m venv .venv
source .venv/Scripts/activate        # Git Bash
.venv\Scripts\activate               # cmd / PowerShell
pip install -r requirements.txt
```

Google Chrome must be installed on the system. `playwright install` is **not** needed — the
script drives the already-installed Chrome via `channel="chrome"`, not a separately downloaded
Chromium.

## First run and login

Run:

```
.\run.bat
```

(or `.venv\Scripts\python -m upwork_scout`). A Chrome window opens with the profile folder
`browser-profile/` — separate from your everyday Chrome profile; the script refuses to run if
`browser.profile_dir` points at `%LOCALAPPDATA%\Google\Chrome\User Data`. Log in to Upwork
manually in the opened window; the script waits up to `browser.login_timeout_minutes` (10
minutes by default) and continues on its own once it sees you are logged in. The password and
cookies never reach the code, logs or the report — the session lives only in
`browser-profile/`.

To only log in without collecting anything:

```
.\run.bat --login-only
```

During the login phase the script only watches and never clicks anything: if Upwork shows a
check, pass it yourself manually. Once the pass has started (login confirmed), any
CAPTCHA/security check or login page stops the whole pass — but data already collected up to
that point are kept, and the report is still created.

## Regular run

```
.\run.bat
```

| Flag | What it does |
|---|---|
| `--show-all` | show all found jobs in the report, not just new ones |
| `--no-ai` | do not use AI evaluation (Jev) in this run |
| `--no-details` | do not open job pages (faster) |
| `--load-more N` | how many times to click "Load More Jobs" in each search (0–50) |
| `--rescore` | no browser: recalculate the last run's scores against the current `config.yaml` and write a new report |
| `--login-only` | only open Upwork in the script's profile and wait for login (no collection) |
| `--config PATH` | path to `config.yaml` (defaults to the project folder) |
| `--version` | print the version and exit |

Exit codes: `0` success, `1` failure, `2` configuration error, `3` pass stopped (security
check, lost session, closed window, etc.; the report from data already collected is still
created).

## Configuring config.yaml

All non-secret settings live in `config.yaml`, with paths resolved relative to the project
folder.

- **browser** — Chrome profile and timing: `profile_dir` (separate profile), `channel`
  (`chrome`), `login_timeout_minutes` (10 — how long to wait for manual login),
  `navigation_timeout_seconds` (45), `discovery_wait_seconds` (25 — wait until the list of
  saved-search tabs stops changing), `cards_wait_seconds` (20 — wait for cards on a search
  page), `delay_between_pages_seconds` (`[3, 6]` — random pause between page transitions, a
  calm pace in a single tab).
- **upwork** — `find_work_url`, the page listing saved searches.
- **collection** — card collection: `load_more_clicks` (how many times to click "Load More
  Jobs"), `load_more_wait_seconds`; `details.mode`: `off` — never open job pages (fastest),
  `missing` — open a page only when the card is missing key fields, `always` — open the page
  of every matching job (full description); `details.only_new` — only open pages for new jobs;
  `details.max_per_run` (40) — no more than N job pages per run; `debug_dump_html` — save the
  HTML of search pages with zero cards to `debug/` (for diagnosing Upwork interface changes). The
  file is sanitised: only `<main>` is kept, without scripts, forms, hidden inputs, header/account
  menus or non-content attributes; only links to jobs and saved searches keep their href. Still
  review it before sharing — the page text may contain your name.
- **storage** — `db_path` (`data/upwork.sqlite3`).
- **report** — `show`: `new` — only jobs that were not there before the run, `all` — every job
  found; `show_below_threshold` and `show_excluded` — whether to show the collapsed "below
  threshold" and "excluded by hard rules" blocks in the report.
- **logging** — `file`, `level`.
- **filters** (hard rules, applied before scoring) — `min_fixed_budget` (100),
  `min_hourly_rate` (15); `exclude_in` (`[title, skills]`) — where a stop-phrase match means
  the job is excluded entirely; elsewhere (usually the description) a match only produces a
  category penalty; `soften_if_stack_in_title_or_skills` — if the title or skills contain the
  priority stack (`scoring.stack`), a stop phrase does not exclude the job, only penalises it;
  `description_penalty_share` (0.5) — the share of the category penalty applied when a stop
  phrase is found only in the description; `stop_categories` — categories (`marketing`,
  `crypto_trading`, `data_entry`, `design`), each with a `label`, `penalty` and a list of
  `phrases`.
- **scoring** (0–100 score) — `min_score` (40, final score ≥ this → "passed the filter"),
  `recommend_apply_from` (70, ≥ — "apply"), `recommend_review_from` (45, ≥ — "review manually",
  below — "skip"), `unknown_share` (0.5 — if a field is not shown/not loaded, the component
  gets this share of its maximum: missing data ≠ a bad value). Components and their maximum:
  `stack` up to 40 (match against the priority stack: title ×1.5, skills ×1.25, description
  ×1.0), `freshness` up to 10 (how recently posted), `competition` up to 10 (number of
  proposals — fewer is better), `client` up to 20 (payment verified + client spend + rating),
  `budget` up to 10 (budget/rate), `clarity` up to 10 (a concrete, bounded scope: description
  length, outcome-related phrases, bulleted lists). `penalties` — deductions for unverified
  payment, low client rating, too-short a description, vague wording, "huge scope at a token
  budget", and matches against stop categories.
- **ai** — the Jev stage settings (see the section below).

Phrases (in `filters.stop_categories.*.phrases`, `scoring.stack.*.phrases`,
`scoring.clarity.scope_phrases`, `scoring.penalties.vague_phrases`, etc.) are matched
case-insensitively, on word boundaries; a trailing `*` means a match on the word's prefix
(`"cold email*"` matches "cold emails").

Tip: after editing the weights, run `.\run.bat --rescore` to see the effect without opening
Upwork.

## Connecting third-party services and APIs

The AI stage uses **Jev by TypeSafe** through **OpenRouter**.

1. Create an account at https://openrouter.ai and add credits.
2. Get a key at https://openrouter.ai/settings/keys .
3. Put it into `.env`:

   ```
   OPENROUTER_API_KEY=your_key
   ```

   If you leave it empty, the script falls back to the Windows environment variable
   `OPENROUTER_API_KEY` if it is set; otherwise the AI stage is skipped.

`.env` variables (see `.env.example`):

| Variable | Purpose | Default |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key | — (empty → falls back to the environment) |
| `JEV_MODEL` | Jev model on OpenRouter | `~typesafe/jev-latest` |
| `JEV_API_URL` | System One API endpoint | `https://openrouter.ai/api/v1/systemone` |

Docs: https://openrouter.ai/docs/guides/community/jev ,
https://openrouter.ai/docs/guides/community/typesafe-sdk , https://openrouter.ai/typesafe .

**What is sent:** only public job fields — title, description (truncated to
`ai.description_max_chars`), skills, budget, experience level, duration, number of proposals,
and the client's payment-verified status / rating / total spent / country. The posted date is
not sent: freshness is scored by the deterministic rules.
**What is never sent:** cookies, the browser profile, or any Upwork account data.

Jev returns typed decisions rather than free text: a relevance score on a 0–4 scale (mapped to
0–100), a choice of `apply`/`review`/`skip`, and a probability (noul) for each configured risk
question. The explanation text in the report is composed from these typed answers. The
approximate cost is about $0.00002 per request.

Without a key the project runs on deterministic rules only, and the report states that the AI
stage was not run. Timeouts, HTTP 429 and 5xx are retried
(`ai.max_retries`); invalid JSON or a schema mismatch are not. If no assessment is obtained, the
job's entry says "AI evaluation not performed" (`AI-оценка не выполнена`) and the rule-based score
stays. A 401/402 error, an exhausted 429 rate limit, or `ai.max_consecutive_failures` errors in a
row stop the AI stage for the rest of the run. Results are cached by a fingerprint of all inputs:
the job fields sent, the model, the questions, the risk threshold and the interpretation version.
Changing `ai.freelancer_focus`, the questions, the threshold or the model makes Jev be asked again. AI never overturns a hard exclusion unless
`ai.override_exclusions: true` is explicitly set.

`.env.example` must stay structurally identical to `.env` (same variables, same order, only
the values differ). Check for drift (output must be empty):

```bash
diff <(sed -E 's/=.*/=<V>/' .env) <(sed -E 's/=.*/=<V>/' .env.example)
```

## Report

The header of the report shows: how many searches were processed (out of how many found), how
many unique jobs, how many new, how many passed the filter, how many searches ended in an
error, and a summary of the AI stage (model, number of successful/failed requests, cost).

Each job card shows: the final score `/100` broken down into the rule-based score and Jev's
contribution, a 6-segment bar (stack / freshness / competition / client / budget / clarity),
the budget, client data, number of proposals, posted date, a list of reasons ("why it fits"),
a list of risks with the points deducted for each, the recommendation (apply / review manually
/ skip), the list of searches the job was found in, the skills and the description.

The collapsed "Below threshold" and "Excluded by hard rules" blocks show jobs that did not
pass the score filter or were excluded by the hard rules (controlled by
`report.show_below_threshold` and `report.show_excluded`).

`"не указано"` ("not shown") means the field was not present on the card/page from Upwork —
this is missing data, not a "bad" value. All text coming from Upwork (titles, descriptions,
search names) is HTML-escaped (Jinja2 autoescaping), so nothing from an Upwork page can execute
as code in the report.

## Data storage

Data is stored in SQLite (`data/upwork.sqlite3`) in these tables:

- `runs` — each script run (status, timing, report path);
- `searches` — saved searches (ID, name, URL);
- `search_runs` — the outcome of each search in each run (ok/error/aborted/skipped, card
  count);
- `jobs` — one row per job (by its ID), the latest known values of its fields;
- `job_searches` — which searches a job was found in;
- `sightings` — in which run a job was seen in which search;
- `evaluations` — a job's evaluation for a specific run (rules + AI).

A job is considered **new** in a run if its ID was not in the database before that run started
(the run's `first_seen_run_id` equals the current run). Data are committed after every search
is processed, so an interrupted pass does not lose what was already collected.

When the project is updated, the database is migrated to the new schema automatically
(`PRAGMA user_version`; missing columns are added with `ALTER TABLE`) and existing data are kept.
Each job also stores the number of attempts to open its page (`details_attempts`) and the last
error (`details_last_error`), plus a deferred-page flag (`details_pending`).

## Tests

```
.venv\Scripts\python -m pytest -q
```

Tests use local HTML fixtures from `tests/fixtures/` and never touch the network or a browser.

## Known limitations

- Upwork regularly changes its page markup. Parsing is layered: first `data-test`-style
  attributes, then text patterns — but after a redesign some fields may come out as "not
  shown", or a search page may return zero cards. If this happens, enable
  `collection.debug_dump_html: true` in `config.yaml` and inspect the saved files in `debug/`.
- Saved searches must be visible as `/nx/find-work/{id}` links on the Find Work page. If
  Upwork hides some searches behind an extra menu, the script will not find them — there is no
  hardcoded fallback list of searches.
- Relative dates ("2 hours ago") are converted into an approximate timestamp, not an exact
  posting time.
- The description on a card may be truncated by Upwork; to get the full text, use
  `collection.details.mode: always` (opens the page of every matching job).
- A client rating of `0` (no reviews) is stored and shown in the report as "not shown".
- The script does not bypass CAPTCHA/security checks — when one appears, the pass stops.
  Observed during development: without a login, Upwork immediately shows a Playwright-driven
  browser a Cloudflare check (the title is localised, e.g. "Один момент…") — the script
  recognises it. If, after you log in, Upwork shows a check on every page, the pass will stop at
  the first search: that is the intended behaviour, not a bug.
- A job page (deep collection) is stored only if it really is the requested job's page: it
  loaded, the job ID in the URL matches (a matching title is not accepted as proof), the job is
  not removed, and minimal job data are present. Otherwise nothing is written and the page is
  retried in later runs — up to `collection.details.max_attempts` times (default 3), even with
  `only_new: true`. Jobs that did not fit into `collection.details.max_per_run` are marked as
  deferred and are opened first in the following runs — if they show up in the search feed
  again. Pages of jobs that have left the feed are not opened: they will not appear in any
  report, and extra requests to Upwork are not needed.
- The list of saved searches is considered loaded when the set of IDs has not changed for 3 s
  (and at least 4 s have passed since the first one appeared). If it was still changing when
  `browser.discovery_wait_seconds` ran out, the report shows a warning.
- The live Upwork interface was not verified by the developer. On the first run, cross-check
  the number of searches, card counts and field values in the report against what you see on
  the site.
- Jev does not return free-form text, so the explanations in the report are composed from
  probabilities and typed answers, not a verbatim model comment.

## Updating the project

No server is used — everything runs locally on your machine.

1. Copy the new files over the old ones (keeping `.env`, `data/`, `outputs/`, `logs/`,
   `browser-profile/`).
2. Update dependencies:

   ```
   source .venv/Scripts/activate        # Git Bash
   .venv\Scripts\activate               # cmd / PowerShell
   pip install -r requirements.txt
   ```
3. Run as usual:

   ```
   .\run.bat
   ```
