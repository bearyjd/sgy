# CLAUDE.md — schoology-scrape (sgy)

Guidance for Claude Code / AI agents working in this repo. This repo is one of three
independent scrapers under `../school/`; see `../school/CLAUDE.md` for the sibling
overview (note: that file currently understates this repo's maturity — see below).

## What this is

`sgy` — a single-package Python CLI (`sgy_cli`) that scrapes the Schoology **parent**
portal (assignments, grades, announcements, embedded homework slides) for an OpenClaw
cron agent to consume via `--json`. Pure `requests` + `beautifulsoup4` — no Playwright,
since Schoology login works via direct form POST.

Current version: see `pyproject.toml` (`version = "..."`) — **do not hand-bump this**,
see "Versioning / CI" below.

## Run / test commands (verified against this repo's actual files — not assumed)

```bash
pip install -e ".[dev]"     # installs requests, beautifulsoup4, pytest, ruff
ruff check .                 # lint — CI runs this before tests
pytest -q                    # full test suite (tests/test_summary.py, 41 tests — verified by running it)
pytest -q -k "warmup"        # example: run a subset by keyword
```

Both commands above are exactly what `.github/workflows/test.yml` and
`.github/workflows/auto-tag.yml` run in CI. There is no `pytest --cov` invocation
configured anywhere in this repo (no coverage tooling wired in `pyproject.toml` or CI) —
if you add coverage, add it to CI too, don't just run it locally.

**Correction to `../school/CLAUDE.md`**: that file says "sgy has no test suite yet."
That is stale. `tests/test_summary.py` exists and is substantial (41 tests, verified
passing via `pytest -q`, covering
`StageTracker`, `get_homework_target`, `build_failed_child`, `_pages_to_homework_slides`,
`_parse_date`, `_enrich_event_dates`, `SGY_MAX_COURSES` parsing, and `cmd_assignments`
child resolution). However, **all of it is pure-logic / mocked-session testing** — none
of it runs the real HTML/JSON parsers for the 6 assignment sources against realistic
Schoology markup, and there are no committed HTML fixtures anywhere in the repo. See
`.agent_native/agent_roadmap.md` item 1 before assuming "tests pass" means "scraping
still works" — it currently doesn't verify that.

No live scraping can be run in CI or by an agent without real parent-portal
credentials in `~/.sgy/.env` — there is no fixture-driven way today to reproduce a
scraping bug (e.g. "source 6 stopped returning data") without a human first
reproducing it against the live portal and, ideally, saving an HTML/JSON fixture
before handing off to an agent. Treat "reproduce first" as a hard prerequisite for any
6-source scraping bug report until fixtures exist (`.agent_native/agent_roadmap.md`
item 1).

## Architecture (single file, all logic in `sgy_cli/cli.py`, ~2,400 lines)

- `SchoologySession` — wraps `requests.Session`; handles login (`_do_login`), session
  cookie caching (`~/.sgy/session.json`, 90-min TTL via `SESSION_TTL`), child discovery
  (`get_children`), child switching (`switch_to_child`), and rate-limit/retry handling
  (`_request`, 429 backoff).
- `scrape_assignments` — runs **6 independent sources**, merges + dedups by richest
  record (never short-circuits on first success):
  1. `ajax_upcoming` — `/home/upcoming_submissions_ajax` (works)
  2. `home_widget` — `/home` page `.upcoming-event` widget — **structurally limited**:
     only extracts titles, no course/date/status (parent-account HTML doesn't match
     the primary selector; the `li` fallback path loses metadata)
  3. `calendar` — `/calendar/feed_ajax*` — **structurally broken for parent accounts**:
     always returns HTML instead of JSON, so it always throws `JSONDecodeError` and
     contributes zero items
  4. `folder_api` — `/v1/courses/{sid}/folder/0` per course — reliable, requires
     preview warmup first
  5. `materials_html` — `/course/{sid}/materials` scrape per course — reliable,
     requires preview warmup first
  6. `grades_xref` — `/course/{sid}/student_grades` cross-reference per course —
     catches graded items missed by other sources, requires preview warmup first

  **When a source returns nothing, first determine which case you're in** before
  "fixing" anything:
  - Sources 2 and 3 returning weak/no data is **expected and documented** — don't
    "fix" them into working, they are structurally incompatible with parent accounts.
  - Sources 1, 4, 5, 6 returning nothing is **a real regression** — likely Schoology
    changed HTML class names or JSON shape. Check `NOTES.md` for the currently-known
    selectors/shapes before assuming a fix is needed, and check `git blame` on the
    relevant `_parse_*`/`_get_assignments_from_*` function for the last-known-good
    selectors.

- **Preview warmup** (`GET /course/{sid}/preview/{child_uid}/parent`) — required
  before **any** `/course/{sid}/...` URL for parent accounts, or you get 403 on
  `/page/{id}` and empty results from grades/materials endpoints. One warmup per
  course per session is sufficient (server-side auth context persists). Documented in
  `NOTES.md` under "Preview Warmup Discovery (Key Algorithm)". **This is currently
  enforced only by convention** — 5 separate inline call sites in `cli.py`, no shared
  helper, no test asserting call order. If you add a 7th assignment source or any new
  `/course/{sid}/...` fetch, you must add the warmup call yourself; nothing will stop
  you from forgetting it, and the failure (silent 403) looks identical to a real
  Schoology HTML change. See `.agent_native/agent_roadmap.md` item 2 for the planned
  fix (a `warmup_course()` method with session-level tracking).

- `scrape_grades`, `scrape_announcements`, `scrape_pages` — simpler, single/few-source
  scrapers; `scrape_pages` also handles Google Slides/Docs export
  (`/export/txt`, `/export?format=txt`) for embedded homework content, with a 7-day
  disk cache at `~/.sgy/embed_cache.json`.

## `--json` output contract

`sgy summary --json` output includes a top-level `warnings` array (and, in
`per_child` mode, a per-child `warnings` array) for **non-fatal** errors: rate
limiting, warmup failures, truncated course lists (`SGY_MAX_COURSES`), per-source
scrape exceptions. This exists because **the OpenClaw cron agent reads only stdout**
— it never inspects stderr. Any new non-fatal error path you add MUST append to
`sgy.warnings` (the `SchoologySession.warnings` list), not just `_log(...)` to
stderr, or the consuming agent will silently miss it. `scrape_confidence`
(`"high"`/`"partial"`/`"failed"`, from `StageTracker`) is the higher-level signal;
`warnings` is the detail feed.

## Style rules for this repo (extends the user's global Python rules)

- Files currently exceed the 800-line house limit (`cli.py` is ~2,400 lines) — this
  is tracked as a known issue (`.agent_native/agent_roadmap.md` item 3), not a green
  light to keep growing it. New unrelated functionality should go in a new module if
  at all feasible, even before the full split lands.
- Never copy real student names, grades, assignment content, or credentials into
  commits, test fixtures, docs, or fixture files. Redact/synthesize before committing
  anything derived from a live scrape (e.g. HTML fixtures for
  `.agent_native/agent_roadmap.md` item 1).
- `--json` stdout must stay parse-clean: all human-facing/debug output goes to
  `_log(...)` (stderr) via the existing helper, never `print()` directly, when
  `args.json` is set.

## Versioning / CI (see `.github/workflows/`)

- `test.yml` runs `ruff check .` + `pytest -q` on every PR and on pushes to
  non-`main` branches.
- `auto-tag.yml` runs the same gate on pushes to `main`, then **auto-bumps the patch
  version** in `pyproject.toml`, commits with `[skip ci]`, tags `vX.Y.Z`, and creates
  a GitHub release — fully automated, one bump per push to `main`.
- **Do not manually edit the `version` field** in `pyproject.toml` in a PR; CI owns
  patch bumps. If you see two version tags appear after what felt like one merge,
  this is almost always two separate pushes to `main` in quick succession, not a
  workflow bug — verify with `git log --oneline --graph --all` and check tag parentage
  before concluding otherwise. Full playbook:
  `.claude/skills/git-graph-before-blaming-auto-tag-expertise.md` (also present at
  `.omc/skills/git-graph-before-blaming-auto-tag-expertise.md`).

## See also

- `NOTES.md` — detailed scraping reverse-engineering notes (auth quirks, materials
  page SPA behavior, Google Slides/Docs export endpoints, known embed IDs).
- `.agent_native/agent_roadmap.md` — prioritized list of what an agent should tackle
  next to close the fixture/testing/warmup-enforcement gaps identified in this audit.
