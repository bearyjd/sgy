# Agent-Native Roadmap — schoology-scrape (sgy)

Audit date: 2026-07-07. Static/code review only — no live scraping was run.

## Correction to parent `school/CLAUDE.md`

The parent doc says "sgy has no test suite yet" and lists `sgy` as v0.1.0. Both are
stale: `pyproject.toml` is at **v0.4.7**, `tests/test_summary.py` has **41 tests**
(verified by running `pytest -q` — all pass), and CI (`.github/workflows/test.yml`,
`auto-tag.yml`) runs `ruff check .` + `pytest -q`
on every PR/push. This repo is more mature than the parent doc implies — but the test
suite, while real, tests almost none of the actual scraping logic (see Priority 1).

## Priority ranking method

Ranked by **Human-Attention-Saved per Unit of Effort**: how much a human currently has
to do (reproduce against live portal, distinguish site-change vs known-limitation,
manually mock 6 tightly-coupled sources) divided by how much work the fix is.

---

### 1. Capture recorded/saved HTML fixtures + build a fixture-driven test harness for the 6 assignment sources and warmup logic
**Effort: Medium (~1 session) | Attention saved: Very high**
**STATUS: Started (2026-07-07).** `tests/fixtures/` now exists with two hand-built
SYNTHETIC fixtures (fake names/ids/dates, no real student data):
`folder_api_response.json` (JSON shape for `/v1/courses/{sid}/folder/0`) and
`materials_page.html` (HTML row shape for `/course/{sid}/materials`). New file
`tests/test_assignment_sources.py` (4 tests) feeds each fixture through the real
parser (`_get_assignments_from_folder_api`, `_parse_material_item`) — only the
network boundary (`sgy.get_folder`) is mocked, not the parser itself — and asserts
the returned `{title, course, due_date, status, link}` shape.
**Remaining (not done this pass):** fixtures/tests for the other 4 sources
(`ajax_upcoming`, `home_widget`, `calendar`, `grades_xref`), plus the
`student_grades` HTML and `home_widget`/`activity feed` fixtures this item
originally called for. A human still needs to supply real (then-redacted)
captures if higher-fidelity fixtures are wanted — the ones added here are
fabricated from the documented shapes in this file and `NOTES.md`, not captured
from a live account.

This is the single biggest gap. `tests/test_summary.py` (691 lines, 41 tests, all
passing per a verified `pytest -q` run) covers
`StageTracker`, `get_homework_target`, `build_failed_child`, `_pages_to_homework_slides`,
`_parse_date`, `_enrich_event_dates`, `SGY_MAX_COURSES` env parsing, and `cmd_assignments`
child-resolution — all pure logic, all with hand-typed micro-HTML or `MagicMock` sessions.
**Zero tests exercise the real HTML-parsing bodies of the 6 assignment sources**:
`_get_assignments_from_folder_api`, `_get_assignments_from_grades`, `_parse_material_item`,
`_scrape_calendar_assignments`, `_parse_feed`/`_parse_single_feed_item`. There are no saved
HTML fixtures anywhere in the repo (`tests/fixtures/` does not exist). This means:
regressions in Schoology's HTML/JSON shape for these 6 sources would only be caught by a
human running the CLI against the real portal.

**Concrete steps for the next agent:**
- Add `tests/fixtures/` with **redacted** captures of: one `student_grades` HTML page,
  one `/v1/courses/{sid}/folder/0` JSON response, one `/course/{sid}/materials` HTML page,
  one `home_widget` `.upcoming-event` HTML block, one `/parent/home/activity` feed HTML
  block. A human must produce these once from a real (test/dummy) account — an agent
  cannot fabricate realistic-enough markup alone, but afterwards all further test writing
  is agent-doable. Strip/replace all real student names, grades, and course names with
  synthetic placeholders (e.g. "Student A", "Course 1") before committing — this is a
  hard privacy requirement, not just style.
- Write `tests/test_assignment_sources.py` parameterized over the 6 source functions,
  feeding each fixture through the parser and asserting the returned dict shape
  (`title`, `course`, `due_date`, `status`, `link`).
- Add one "warmup ordering" test using `unittest.mock.call` ordering to assert that for
  each `/course/{sid}/...` fetch, `sgy._request` is called with the
  `/course/{sid}/preview/{child_uid}/parent` warmup URL **before** the course URL. This
  currently has zero test coverage — see Priority 2.

**Acceptance criteria:** `pytest -q` covers all 6 source-parsing functions against
committed fixtures; a `git log -p` diff review confirms no real student PII in any
fixture file; CI passes.

---

### 2. Assert (not just comment) the preview-warmup-before-course-URL invariant
**Effort: Small (~1-2 hours) | Attention saved: High**
**STATUS: Completed (2026-07-07).** Added `SchoologySession._warmed_courses: set`
and `SchoologySession.warmup_course(sid, child_uid, context="")`
(`sgy_cli/cli.py`, right after `_request`), which no-ops if `sid` is already
warmed this session (or if `sid`/`child_uid` is falsy), else issues the GET and
marks it warmed regardless of success/failure (so a failing course doesn't get
hammered on every subsequent fetch). Replaced all inline call sites — the audit
above counted 5, but the actual code had a **6th** in `_discover_page_embeds`
(used by the pages/homework-slide scraper) — with `sgy.warmup_course(...)`:
`scrape_assignments` (folder_api/materials loop and grades_xref loop),
`scrape_grades`, `_discover_page_embeds`, `_get_page_ids_from_html`, and
`_fetch_page_content`. `grep -n "preview.*parent\|/preview/" sgy_cli/cli.py`
outside the `warmup_course` method body now returns only docstrings/comments and
one unrelated `href` example in `get_courses_and_grades` — zero remaining inline
request calls. Added 6 tests to `tests/test_summary.py`: idempotency per `sid`
(against a real `SchoologySession`, not a mock, so `_warmed_courses` dedup is
exercised for real), a different-`sid` case issuing a new GET, no-op without
`sid`/`child_uid`, exception-swallowing + still-marks-warmed, and a call-order
test (`test_scrape_grades_warmup_happens_before_detail_fetch`) proving
`warmup_course` runs before the course-URL fetch in `scrape_grades`. All 41
pre-existing tests pass unmodified.

Warmup (`GET /course/{sid}/preview/{child_uid}/parent`) is currently pure tribal
knowledge: documented in `NOTES.md` and repeated as inline comments at 5 call sites in
`sgy_cli/cli.py` (`scrape_assignments` x2, `scrape_grades`, `_get_page_ids_from_html`,
`_fetch_page_content`), but there is no shared helper, no flag on `SchoologySession`
tracking "warmed up for course X in this session," and no code path that fails loudly
if a new course-scoped fetch skips it. An agent adding a 7th assignment source (or
fixing a broken one) can easily copy an existing per-course loop, forget the warmup
call, and the failure mode is a silent 403 that looks identical to "Schoology changed
their HTML" — exactly the ambiguity called out in the task background.

**Concrete steps:**
- Add a `SchoologySession._warmed_courses: set[str]` and a
  `SchoologySession.warmup_course(sid, child_uid)` method that no-ops if already
  warmed this session, else does the GET and records it. Replace all 5 inline
  `sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", ...)`
  call sites with this single method (also removes ~15 lines of duplicated try/except).
- Add a docstring/comment at the top of `SchoologySession` pointing to
  `NOTES.md#preview-warmup-discovery-key-algorithm` and to this file, explicitly telling
  future agents: "any new function that fetches `/course/{sid}/...` MUST call
  `self.warmup_course(sid, child_uid)` first."
- Add a unit test (mocked `_request`) asserting `warmup_course` is idempotent per
  `(sid)` within a session and that a plain `_request` call to a `/course/{sid}/`
  path elsewhere does NOT bypass it (i.e., structurally impossible to forget, not just
  advisory).

**Acceptance criteria:** grep for the literal string
`/course/{sid}/preview/` in `cli.py` outside `warmup_course` returns zero matches;
new test passes; existing 55 tests still pass.

---

### 3. Split `sgy_cli/cli.py` (2,399 lines) into a package
**Effort: Medium (~half day) | Attention saved: High**

The user's own house rule caps files at 800 lines (aside: 2399 lines is triple that).
More importantly for agent autonomy: session/warmup logic, all 6 assignment sources,
grades, announcements, pages/Google-embed scraping, output formatting, and argparse
wiring are one file, so an agent fixing "source 5 (materials_html) is broken" must read
and reason about all of session management, folder-API caching, and CLI parsing to be
confident it isn't touching shared state. Suggested split (mirrors `ixl-scrape`'s
already-modular layout, which the parent doc documents as the more mature pattern):

```
sgy_cli/
  session.py       # SchoologySession, load/save_session, load_config, warmup_course
  dates.py         # _parse_date, _extract_letter
  assignments.py   # scrape_assignments + all 6 source functions + _dedup_assignments
  grades.py        # scrape_grades, _scrape_course_grade_detail, get_courses_and_grades
  announcements.py # scrape_announcements, _parse_feed, _parse_single_feed_item
  pages.py         # scrape_pages, Google-embed extraction/fetch, embed cache
  cli.py           # argparse + cmd_* + output_* (thin)
```

**Concrete steps:** a mechanical extract-module refactor with no behavior change;
verify via `pytest -q` before/after (byte-identical `--json` output on a recorded
fixture run is the acceptance bar, not just "tests still pass," since the current
tests mock at the function level and wouldn't catch cross-module import breakage).

**Acceptance criteria:** no file >800 lines; `pytest -q` and `ruff check .` pass
unchanged; `sgy --help` and one `--json` fixture-driven summary run produce identical
output to pre-refactor.

---

### 4. Write `CLAUDE.md` for this repo (done as part of this audit — see below)
**Effort: Small | Attention saved: High**
Already delivered in this audit; see `/home/user/Documents/vibe-code/school/schoology-scrape/CLAUDE.md`.
Ranked here only to note it as a completed, high-value item so a future agent doesn't
re-derive the same context from scratch.

---

### 5. Reconcile `.claude/agent-memory/` project notes against current code
**Effort: Small (~1 hour) | Attention saved: Medium**

`.claude/agent-memory/claude-memory-signal-discoverer/project_assignment_sources.md`
describes the 6 sources with different names/order ("upcoming events API",
"due-soon events API") than what's actually in `scrape_assignments` today
(`ajax_upcoming`, `home_widget`, `calendar`, `folder_api`, `materials_html`,
`grades_xref`). This is exactly the kind of drift that causes an agent to misdiagnose
"structurally broken" vs "recently changed" — the task's own audit background warns
about this distinction. A quick pass reconciling or deleting stale memory notes (and
updating `project_preview_warmup_scope.md`'s claim list against the actual 5 call
sites found in this audit) prevents a future agent from trusting outdated notes over
the code.

**Acceptance criteria:** each `.claude/agent-memory/.../project_*.md` file's factual
claims are checked against current `sgy_cli/cli.py` line-by-line; stale claims are
either corrected or the file is deleted with a note in its place.

---

## Findings not requiring action, noted for completeness

- No hardcoded credentials/tokens found outside `.env.example` placeholders (checked
  `.py`, `.md`, `.json` for password/api-key/secret literal patterns — none found).
- `.github/workflows/auto-tag.yml` auto-bumps `pyproject.toml` patch version and tags
  on every push to `main` (gated behind the `test` job and `[skip ci]`/bot-actor
  guards). See `.claude/skills/git-graph-before-blaming-auto-tag-expertise.md` (also
  mirrored at `.omc/skills/git-graph-before-blaming-auto-tag-expertise.md`): if you
  see two version tags after what feels like one merge, check `git log --graph`
  parentage before assuming the workflow double-fires — it is almost always two
  separate pushes, not a CI bug.
