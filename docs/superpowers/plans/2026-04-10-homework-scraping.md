# Homework Scraping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-child `scrape_confidence` tracking and `homework_slides` to `sgy summary --json`, so the OpenClaw cron agent knows immediately when data is missing and gets the actual text of what homework to do.

**Architecture:** Add `StageTracker` (tracks 5 checkpoints per child), `get_homework_target` (per-child slide course config), and `build_failed_child` as standalone helpers. Add `course_filter` to `scrape_pages` for targeted slide fetching. Rewrite the `cmd_summary` JSON path to use these. All changes are in `sgy_cli/cli.py` — no other files change structurally.

**Tech Stack:** Python 3.10+, `dataclasses` stdlib, `requests`, `beautifulsoup4`. Test runner: `pytest`.

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `tests/test_summary.py` | **Create** | Unit tests for `StageTracker`, `get_homework_target`, `build_failed_child`, `_pages_to_homework_slides` |
| `sgy_cli/cli.py` | **Modify** | Add `dataclasses` import; add `StageTracker`, `get_homework_target`, `build_failed_child`, `_pages_to_homework_slides`; add `course_filter` param to `scrape_pages`; rewrite `cmd_summary` JSON path |
| `.env.example` | **Modify** | Add `SGY_HOMEWORK_COURSES` example line |
| `README.md` | **Modify** | Document `scrape_confidence`, `homework_slides`, `SGY_HOMEWORK_COURSES` |

---

## Task 1: Test file scaffold + `StageTracker`

**Files:**
- Create: `tests/test_summary.py`
- Modify: `sgy_cli/cli.py` (imports section, ~line 16; after helpers section, before `SchoologySession`)

- [ ] **Step 1: Write failing tests for `StageTracker`**

Create `tests/test_summary.py`:

```python
"""Tests for StageTracker, get_homework_target, build_failed_child, _pages_to_homework_slides."""
import os
import pytest


# ---------------------------------------------------------------------------
# StageTracker
# ---------------------------------------------------------------------------

def test_stage_tracker_all_ok():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    for s in ["auth", "child_switch", "courses", "assignments", "slides"]:
        t.ok(s)
    assert t.confidence == "high"
    assert t.errors == []


def test_stage_tracker_critical_fail_gives_failed():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    t.ok("auth")
    t.fail("child_switch", "HTTP 302")
    assert t.confidence == "failed"
    assert "child_switch: HTTP 302" in t.errors


def test_stage_tracker_courses_fail_gives_failed():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    t.ok("auth")
    t.ok("child_switch")
    t.fail("courses", "no courses found")
    assert t.confidence == "failed"


def test_stage_tracker_noncritical_partial_gives_partial():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    for s in ["auth", "child_switch", "courses", "assignments"]:
        t.ok(s)
    t.partial("slides", "homeroom_not_found")
    assert t.confidence == "partial"
    assert "slides: homeroom_not_found" in t.errors


def test_stage_tracker_assignments_partial_gives_partial():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    for s in ["auth", "child_switch", "courses"]:
        t.ok(s)
    t.partial("assignments", "2 source error(s)")
    t.ok("slides")
    assert t.confidence == "partial"


def test_stage_tracker_errors_accumulate():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    t.partial("assignments", "err1")
    t.partial("slides", "err2")
    assert len(t.errors) == 2
    assert "assignments: err1" in t.errors
    assert "slides: err2" in t.errors
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /var/home/user/Documents/vibe-code/openclaw-programs/school/schoology-scrape
pytest tests/test_summary.py -v 2>&1 | head -30
```

Expected: `ImportError` — `StageTracker` not yet defined.

- [ ] **Step 3: Add `dataclasses` import to `sgy_cli/cli.py`**

Find the imports block (lines ~16–30). Add after `import argparse`:

```python
from dataclasses import dataclass, field
```

- [ ] **Step 4: Add `StageTracker` to `sgy_cli/cli.py`**

Add after the `_extract_letter` function (~line 261), before the `SchoologySession` class:

```python
# ---------------------------------------------------------------------------
# Stage tracking for per-child scrape confidence
# ---------------------------------------------------------------------------

@dataclass
class StageTracker:
    """Tracks checkpoint success/failure for one child's scrape run."""

    stages: dict = field(default_factory=lambda: {
        "auth": "pending",
        "child_switch": "pending",
        "courses": "pending",
        "assignments": "pending",
        "slides": "pending",
    })
    errors: list = field(default_factory=list)

    def ok(self, stage: str):
        self.stages[stage] = "ok"

    def fail(self, stage: str, reason: str):
        self.stages[stage] = "failed"
        self.errors.append(f"{stage}: {reason}")

    def partial(self, stage: str, reason: str):
        self.stages[stage] = "partial"
        self.errors.append(f"{stage}: {reason}")

    @property
    def confidence(self) -> str:
        if any(self.stages[s] == "failed" for s in ["auth", "child_switch", "courses"]):
            return "failed"
        if any(v in ("failed", "partial") for v in self.stages.values()):
            return "partial"
        return "high"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_summary.py::test_stage_tracker_all_ok \
       tests/test_summary.py::test_stage_tracker_critical_fail_gives_failed \
       tests/test_summary.py::test_stage_tracker_courses_fail_gives_failed \
       tests/test_summary.py::test_stage_tracker_noncritical_partial_gives_partial \
       tests/test_summary.py::test_stage_tracker_assignments_partial_gives_partial \
       tests/test_summary.py::test_stage_tracker_errors_accumulate \
       -v
```

Expected: all 6 PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_summary.py sgy_cli/cli.py
git commit -m "feat: add StageTracker dataclass with confidence property"
```

---

## Task 2: `get_homework_target` and `build_failed_child`

**Files:**
- Modify: `tests/test_summary.py` (append new tests)
- Modify: `sgy_cli/cli.py` (add two functions after `StageTracker`)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_summary.py`:

```python
# ---------------------------------------------------------------------------
# get_homework_target
# ---------------------------------------------------------------------------

def test_get_homework_target_known_children(monkeypatch):
    monkeypatch.setenv("SGY_HOMEWORK_COURSES", "Penn:homeroom,Jack:all,Ford:homeroom")
    monkeypatch.delenv("SGY_HOMEWORK_COURSE", raising=False)
    from sgy_cli.cli import get_homework_target
    assert get_homework_target("Penn") == "homeroom"
    assert get_homework_target("Jack") == "all"
    assert get_homework_target("Ford") == "homeroom"


def test_get_homework_target_case_insensitive(monkeypatch):
    monkeypatch.setenv("SGY_HOMEWORK_COURSES", "Penn:homeroom")
    monkeypatch.delenv("SGY_HOMEWORK_COURSE", raising=False)
    from sgy_cli.cli import get_homework_target
    assert get_homework_target("penn") == "homeroom"
    assert get_homework_target("PENN") == "homeroom"


def test_get_homework_target_unknown_child_defaults_all(monkeypatch):
    monkeypatch.setenv("SGY_HOMEWORK_COURSES", "Penn:homeroom")
    monkeypatch.delenv("SGY_HOMEWORK_COURSE", raising=False)
    from sgy_cli.cli import get_homework_target
    assert get_homework_target("NewKid") == "all"


def test_get_homework_target_legacy_fallback(monkeypatch):
    monkeypatch.delenv("SGY_HOMEWORK_COURSES", raising=False)
    monkeypatch.setenv("SGY_HOMEWORK_COURSE", "homeroom")
    from sgy_cli.cli import get_homework_target
    assert get_homework_target("Anyone") == "homeroom"


def test_get_homework_target_no_env_defaults_all(monkeypatch):
    monkeypatch.delenv("SGY_HOMEWORK_COURSES", raising=False)
    monkeypatch.delenv("SGY_HOMEWORK_COURSE", raising=False)
    from sgy_cli.cli import get_homework_target
    assert get_homework_target("Anyone") == "all"


# ---------------------------------------------------------------------------
# build_failed_child
# ---------------------------------------------------------------------------

def test_build_failed_child_shape():
    from sgy_cli.cli import StageTracker, build_failed_child
    child = {"name": "Jack", "uid": "456"}
    t = StageTracker()
    t.ok("auth")
    t.ok("child_switch")
    t.fail("courses", "no courses found")
    result = build_failed_child(child, t)
    assert result["child"] == child
    assert result["scrape_confidence"] == "failed"
    assert result["scrape_stages"]["auth"] == "ok"
    assert result["scrape_stages"]["courses"] == "failed"
    assert "courses: no courses found" in result["scrape_errors"]
    assert result["assignments"] == []
    assert result["homework_slides"] == []
    assert result["grades"] == []
    assert result["announcements"] == []
    assert result["warnings"] == []


def test_build_failed_child_all_empty_data():
    from sgy_cli.cli import StageTracker, build_failed_child
    child = {"name": "Penn", "uid": "123"}
    t = StageTracker()
    t.fail("auth", "Login failed")
    result = build_failed_child(child, t)
    assert result["scrape_confidence"] == "failed"
    assert result["assignments"] == []
    assert result["homework_slides"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_summary.py -k "homework_target or failed_child" -v 2>&1 | head -20
```

Expected: `ImportError` — functions not yet defined.

- [ ] **Step 3: Add `get_homework_target` and `build_failed_child` to `sgy_cli/cli.py`**

Add immediately after the `StageTracker` class:

```python
def get_homework_target(child_name: str) -> str:
    """Return 'all' or a course name substring for slide targeting.

    Reads SGY_HOMEWORK_COURSES="Penn:homeroom,Jack:all,Ford:homeroom".
    Falls back to SGY_HOMEWORK_COURSE (legacy single-child), then "all".
    """
    raw = os.environ.get("SGY_HOMEWORK_COURSES", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        name, _, target = pair.partition(":")
        if name.strip().lower() == child_name.strip().lower():
            return target.strip()
    return os.environ.get("SGY_HOMEWORK_COURSE", "all")


def build_failed_child(child: dict, tracker: StageTracker) -> dict:
    """Build a per_child entry for a child whose scrape failed a critical stage."""
    return {
        "child": child,
        "scrape_confidence": tracker.confidence,
        "scrape_stages": tracker.stages,
        "scrape_errors": tracker.errors,
        "assignments": [],
        "homework_slides": [],
        "grades": [],
        "announcements": [],
        "warnings": [],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_summary.py -k "homework_target or failed_child" -v
```

Expected: all 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_summary.py sgy_cli/cli.py
git commit -m "feat: add get_homework_target and build_failed_child helpers"
```

---

## Task 3: `course_filter` param on `scrape_pages`

**Files:**
- Modify: `tests/test_summary.py` (append new tests)
- Modify: `sgy_cli/cli.py` — `scrape_pages` function at line ~1446

- [ ] **Step 1: Write failing tests**

Append to `tests/test_summary.py`:

```python
# ---------------------------------------------------------------------------
# scrape_pages course_filter
# ---------------------------------------------------------------------------

def test_scrape_pages_accepts_course_filter_param():
    """Verify scrape_pages signature accepts course_filter without TypeError."""
    import inspect
    from sgy_cli.cli import scrape_pages
    sig = inspect.signature(scrape_pages)
    assert "course_filter" in sig.parameters
    assert sig.parameters["course_filter"].default == "all"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_summary.py::test_scrape_pages_accepts_course_filter_param -v
```

Expected: FAIL — `course_filter` not in signature.

- [ ] **Step 3: Add `course_filter` param to `scrape_pages` in `sgy_cli/cli.py`**

Find `scrape_pages` at line ~1446. Change the signature and add filtering logic:

```python
def scrape_pages(
    sgy: SchoologySession,
    child: Optional[dict],
    course_id: Optional[str] = None,
    course_filter: str = "all",
    fetch_google_docs: bool = True,
) -> list:
```

Then find the existing `course_id` filtering block (lines ~1461–1472):

```python
    if course_id:
        filtered = [
            c for c in courses
            if c.get("section_id") == course_id
            or course_id in c.get("href", "")
            or course_id in c.get("name", "").lower()
        ]
        if filtered:
            courses = filtered
        else:
            _log(f"Course '{course_id}' not found in enrolled courses.", sgy.verbose)
            courses = [{"name": f"Course {course_id}", "section_id": course_id}]
```

Replace it with:

```python
    # course_filter: name substring match (used by cmd_summary for per-child targeting)
    if course_filter != "all":
        courses = [c for c in courses if course_filter.lower() in c.get("name", "").lower()]
        if not courses:
            _log(f"No courses matching '{course_filter}' found.", sgy.verbose)
            return []
    # course_id: ID/href/name match (used by cmd_pages --course flag)
    elif course_id:
        filtered = [
            c for c in courses
            if c.get("section_id") == course_id
            or course_id in c.get("href", "")
            or course_id in c.get("name", "").lower()
        ]
        if filtered:
            courses = filtered
        else:
            _log(f"Course '{course_id}' not found in enrolled courses.", sgy.verbose)
            courses = [{"name": f"Course {course_id}", "section_id": course_id}]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_summary.py::test_scrape_pages_accepts_course_filter_param -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite to verify nothing regressed**

```bash
pytest tests/test_summary.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_summary.py sgy_cli/cli.py
git commit -m "feat: add course_filter param to scrape_pages for per-child slide targeting"
```

---

## Task 4: `_pages_to_homework_slides` helper

Converts `_filter_homework_pages` output to the flat `homework_slides` format the spec defines.

**Files:**
- Modify: `tests/test_summary.py` (append new tests)
- Modify: `sgy_cli/cli.py` — add after `_filter_homework_pages` (~line 1997)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_summary.py`:

```python
# ---------------------------------------------------------------------------
# _pages_to_homework_slides
# ---------------------------------------------------------------------------

def test_pages_to_homework_slides_with_embed_text():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [
        {
            "course": "Homeroom",
            "title": "Weekly Homework Slide",
            "body_text": "",
            "google_embeds": [{"url": "https://slides.google.com/...", "type": "slides", "text": "Read pages 1-10"}],
        }
    ]
    result = _pages_to_homework_slides(pages)
    assert len(result) == 1
    assert result[0]["course"] == "Homeroom"
    assert result[0]["content"] == "Read pages 1-10"
    assert result[0]["fetched"] is True
    assert result[0]["error"] is None


def test_pages_to_homework_slides_body_text_fallback():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [
        {
            "course": "Math",
            "title": "Homework",
            "body_text": "Complete worksheet 4B",
            "google_embeds": [],
        }
    ]
    result = _pages_to_homework_slides(pages)
    assert result[0]["content"] == "Complete worksheet 4B"
    assert result[0]["fetched"] is True


def test_pages_to_homework_slides_no_content():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [
        {
            "course": "Science",
            "title": "Homework",
            "body_text": "",
            "google_embeds": [{"url": "...", "type": "slides", "text": ""}],
        }
    ]
    result = _pages_to_homework_slides(pages)
    assert result[0]["content"] is None
    assert result[0]["fetched"] is False
    assert result[0]["error"] == "no_content_found"


def test_pages_to_homework_slides_multiple_courses():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [
        {"course": "Math", "title": "HW", "body_text": "p.23 #1-10", "google_embeds": []},
        {"course": "ELA", "title": "HW", "body_text": "", "google_embeds": [{"url": "", "type": "slides", "text": "Read ch 5"}]},
    ]
    result = _pages_to_homework_slides(pages)
    assert len(result) == 2
    assert result[0]["course"] == "Math"
    assert result[0]["content"] == "p.23 #1-10"
    assert result[1]["course"] == "ELA"
    assert result[1]["content"] == "Read ch 5"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_summary.py -k "homework_slides" -v 2>&1 | head -20
```

Expected: `ImportError` — function not yet defined.

- [ ] **Step 3: Add `_pages_to_homework_slides` to `sgy_cli/cli.py`**

Add immediately after `_filter_homework_pages` (~line 1997):

```python
def _pages_to_homework_slides(pages: list) -> list:
    """Convert _filter_homework_pages output to the homework_slides JSON format.

    Each item in the output has: course, title, content (str|None), fetched (bool), error (str|None).
    Content is the first non-empty Google embed text, falling back to body_text.
    """
    slides = []
    for p in pages:
        embed_texts = [e["text"] for e in p.get("google_embeds", []) if e.get("text")]
        body = p.get("body_text", "")
        content = embed_texts[0] if embed_texts else (body if body else None)
        fetched = content is not None
        slides.append({
            "course": p.get("course", ""),
            "title": p.get("title", ""),
            "content": content,
            "fetched": fetched,
            "error": None if fetched else "no_content_found",
        })
    return slides
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_summary.py -k "homework_slides" -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/test_summary.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_summary.py sgy_cli/cli.py
git commit -m "feat: add _pages_to_homework_slides helper"
```

---

## Task 5: Rewrite `cmd_summary` JSON path with stage tracking

This is the main integration task. The `--json` branch of `cmd_summary` gets per-child `StageTracker`, per-child `course_filter` targeting, and outputs `homework_slides` instead of `homework_pages`.

**Files:**
- Modify: `sgy_cli/cli.py` — `cmd_summary` function at line ~2000

- [ ] **Step 1: Replace the `--json` branch of `cmd_summary`**

Find the `else:` block inside `cmd_summary` at ~line 2016 (the `if args.json:` branch). Replace from `if args.json:` through `print(json.dumps(all_data, indent=2))`:

```python
        if args.json:
            all_data = {
                "timestamp": datetime.now().isoformat(),
                "children": children,
                "per_child": [],
            }
            for child in children:
                tracker = StageTracker()

                # Stage: auth
                try:
                    sgy.ensure_logged_in()
                    tracker.ok("auth")
                except Exception as e:
                    tracker.fail("auth", str(e))
                    all_data["per_child"].append(build_failed_child(child, tracker))
                    continue

                # Stage: child_switch
                try:
                    sgy.switch_to_child(child)
                    tracker.ok("child_switch")
                except Exception as e:
                    tracker.fail("child_switch", str(e))
                    all_data["per_child"].append(build_failed_child(child, tracker))
                    continue

                # Stage: courses
                courses = get_courses_and_grades(sgy, child)
                if courses:
                    tracker.ok("courses")
                else:
                    tracker.fail("courses", "no courses found")
                    all_data["per_child"].append(build_failed_child(child, tracker))
                    continue

                # Stage: assignments
                warn_before = len(sgy.warnings)
                assignments = scrape_assignments(sgy, child, days=14)
                new_warnings = len(sgy.warnings) - warn_before
                if new_warnings:
                    tracker.partial("assignments", f"{new_warnings} source error(s)")
                else:
                    tracker.ok("assignments")

                # Stage: slides (per-child course targeting via SGY_HOMEWORK_COURSES)
                course_filter = get_homework_target(child["name"])
                raw_pages = scrape_pages(
                    sgy, child,
                    course_filter=course_filter,
                    fetch_google_docs=True,
                )
                filtered_pages = _filter_homework_pages(raw_pages)
                homework_slides = _pages_to_homework_slides(filtered_pages)
                if not raw_pages and course_filter != "all":
                    tracker.partial("slides", "homeroom_not_found")
                elif any(not s["fetched"] for s in homework_slides):
                    unfetched = sum(1 for s in homework_slides if not s["fetched"])
                    tracker.partial("slides", f"{unfetched} slide fetch error(s)")
                else:
                    tracker.ok("slides")

                # Grades and announcements (non-staged — failures go into warnings)
                grades = scrape_grades(sgy, child, detail=False)
                announcements = scrape_announcements(sgy, child, days=7)

                all_data["per_child"].append({
                    "child": child,
                    "scrape_confidence": tracker.confidence,
                    "scrape_stages": tracker.stages,
                    "scrape_errors": tracker.errors,
                    "assignments": assignments,
                    "homework_slides": homework_slides,
                    "grades": grades,
                    "announcements": announcements,
                    "warnings": sgy.warnings[warn_before:],
                })

            print(json.dumps(all_data, indent=2))
```

- [ ] **Step 2: Verify the command runs without error**

Confirm the JSON structure is valid (requires credentials in `~/.sgy/.env`):

```bash
cd /var/home/user/Documents/vibe-code/openclaw-programs/school/schoology-scrape
sgy summary --json | python -c "
import json, sys
data = json.load(sys.stdin)
for child in data.get('per_child', []):
    name = child['child']['name']
    conf = child.get('scrape_confidence', 'MISSING')
    slides = child.get('homework_slides', 'MISSING')
    stages = child.get('scrape_stages', 'MISSING')
    print(f'{name}: confidence={conf}, slides={len(slides) if isinstance(slides, list) else slides}, stages={stages}')
"
```

Expected output (fields present, no KeyError):
```
Penn: confidence=high, slides=1, stages={'auth': 'ok', ...}
Jack: confidence=high, slides=3, stages={'auth': 'ok', ...}
Ford: confidence=high, slides=1, stages={'auth': 'ok', ...}
```

- [ ] **Step 3: Verify `homework_pages` key is gone from output**

```bash
sgy summary --json | python -c "
import json, sys
data = json.load(sys.stdin)
for child in data['per_child']:
    assert 'homework_pages' not in child, f'homework_pages still present for {child[\"child\"][\"name\"]}'
    assert 'homework_slides' in child, f'homework_slides missing for {child[\"child\"][\"name\"]}'
    assert 'scrape_confidence' in child
    assert 'scrape_stages' in child
print('All fields correct.')
"
```

Expected: `All fields correct.`

- [ ] **Step 4: Commit**

```bash
git add sgy_cli/cli.py
git commit -m "feat: rewrite cmd_summary JSON path with StageTracker and homework_slides"
```

---

## Task 6: Update `.env.example` and `README.md`

**Files:**
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Update `.env.example`**

Open `.env.example`. It currently contains:

```
SGY_BASE_URL="https://app.schoology.com"
# SGY_SCHOOL_NID=""
SGY_EMAIL="your-email@example.com"
SGY_PASSWORD="your-password"
```

Add after `SGY_PASSWORD`:

```
# Per-child homework slide targeting. Format: "Name:filter" pairs, comma-separated.
# filter = "all" (scrape every course) or a course name substring (e.g. "homeroom")
# SGY_HOMEWORK_COURSES="Child1:homeroom,Child2:all"
# SGY_HOMEWORK_COURSE="all"   # legacy single-child fallback
```

- [ ] **Step 2: Add `scrape_confidence` and `homework_slides` to README**

In `README.md`, find the `## AI Agent Integration` section. Before the JSON Structure Guide, add a new subsection:

```markdown
## JSON Output: Confidence & Homework Slides

`sgy summary --json` includes two new fields per child:

**`scrape_confidence`** — `"high"` | `"partial"` | `"failed"`

| Value | Meaning | Action |
|-------|---------|--------|
| `"high"` | All stages passed. Zero assignments = genuinely no homework. | Trust the data |
| `"partial"` | Auth/switch/courses OK, but some data may be missing. | Data may be incomplete |
| `"failed"` | Login, child switch, or course discovery failed. | Check Schoology manually |

Check `scrape_errors[]` for specific reasons.

**`homework_slides`** — array of homework page content extracted from Google Slides/Docs embedded in courses:

```json
{
  "course": "Homeroom",
  "title": "Weekly Homework Slide",
  "content": "Read pages 45–60, answer questions 1–5",
  "fetched": true,
  "error": null
}
```

**Per-child slide targeting** — configure which courses to scrape for slides in `~/.sgy/.env`:

```bash
# Penn and Ford: homeroom only (fast); Jack: all courses (comprehensive)
SGY_HOMEWORK_COURSES="Penn:homeroom,Jack:all,Ford:homeroom"
```
```

- [ ] **Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs: document SGY_HOMEWORK_COURSES, scrape_confidence, and homework_slides"
```

---

## Self-Review Checklist (agent: run before declaring done)

- [ ] `StageTracker`, `get_homework_target`, `build_failed_child`, `_pages_to_homework_slides` — all in `cli.py`, all tested
- [ ] `scrape_pages` has `course_filter` param with default `"all"`
- [ ] `cmd_summary --json` outputs `scrape_confidence`, `scrape_stages`, `scrape_errors`, `homework_slides`
- [ ] `homework_pages` key no longer appears in JSON output
- [ ] `SGY_HOMEWORK_COURSES` documented in `.env.example` and `README.md`
- [ ] All `pytest tests/test_summary.py` tests pass
- [ ] `sgy summary --json` runs end-to-end without error
