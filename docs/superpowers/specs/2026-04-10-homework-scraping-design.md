# Homework Scraping — Design Spec

**Date:** 2026-04-10
**Status:** Approved
**Scope:** `sgy` CLI (`schoology-scrape/`)

---

## Problem

The OpenClaw cron agent reads `sgy summary --json` every morning. Currently:

1. The JSON never includes actual homework content — only assignment titles and due dates.
2. When the scraper fails for a child, it fails silently. Parents discover missing data only after the fact.
3. `sgy pages` (which extracts Google Slides homework text) is a separate command never called by `summary`.

**Goals:**

1. Reliably fetch homework data — assignment listings AND the text of what to do.
2. Emit per-child `scrape_confidence` so the cron agent (and parents) know immediately when data is missing.
3. When failing for a specific child, surface a clear error rather than returning empty arrays silently.

---

## Children & Homework Locations

| Child | Homework slide location | Targeting |
|-------|------------------------|-----------|
| Penn  | Homeroom course        | `homeroom` (substring match) |
| Jack  | Spread across all courses | `all` |
| Ford  | Homeroom course        | `homeroom` (substring match) |

---

## JSON Output Shape

`sgy summary --json` gains per-child confidence fields:

```json
{
  "timestamp": "2026-04-10T06:00:00",
  "children": [...],
  "per_child": [
    {
      "child": { "name": "Penn", "uid": "123" },
      "scrape_confidence": "high",
      "scrape_stages": {
        "auth":         "ok",
        "child_switch": "ok",
        "courses":      "ok",
        "assignments":  "ok",
        "slides":       "partial"
      },
      "scrape_errors": ["slides: export failed for Homeroom"],
      "assignments": [...],
      "homework_slides": [
        {
          "course": "Homeroom",
          "content": "Read pages 45–60, answer questions 1–5",
          "fetched": true
        },
        {
          "course": "Math",
          "content": null,
          "fetched": false,
          "error": "export_failed"
        }
      ],
      "grades": [...],
      "announcements": [...],
      "warnings": [...]
    }
  ]
}
```

### Confidence Rules

| `scrape_confidence` | Meaning | Parent action |
|--------------------|---------|---------------|
| `"high"` | All 5 stages passed. Zero assignments = genuinely no homework. | Trust the data |
| `"partial"` | Auth/switch/courses OK, but assignments or slides had errors. | Data may be incomplete |
| `"failed"` | Auth, child_switch, or courses failed. | Check Schoology manually |

Critical stages (failure → `"failed"`): `auth`, `child_switch`, `courses`.
Non-critical stages (failure → `"partial"`): `assignments`, `slides`.

---

## Checkpoint Architecture

One `StageTracker` instance is created per child inside the `cmd_summary` per-child loop. It is never passed into scraper helpers — all stage marking happens at the call site.

```python
@dataclass
class StageTracker:
    stages: dict = field(default_factory=lambda: {
        "auth": "pending", "child_switch": "pending",
        "courses": "pending", "assignments": "pending", "slides": "pending",
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

**Per-child loop in `cmd_summary`:**

```python
tracker = StageTracker()

# Stage: auth
try:
    sgy.ensure_logged_in()
    tracker.ok("auth")
except Exception as e:
    tracker.fail("auth", str(e))
    child_results.append(build_failed_child(child, tracker))
    continue  # skip remaining stages for this child

# Stage: child_switch
try:
    sgy.switch_to_child(child)
    tracker.ok("child_switch")
except Exception as e:
    tracker.fail("child_switch", str(e))
    child_results.append(build_failed_child(child, tracker))
    continue

# Stage: courses
courses = get_courses_and_grades(sgy, child)
if courses:
    tracker.ok("courses")
else:
    tracker.fail("courses", "no courses found")
    child_results.append(build_failed_child(child, tracker))
    continue

# Stage: assignments
warn_before = len(sgy.warnings)
assignments = scrape_assignments(sgy, child)
warn_after = len(sgy.warnings)
new_warnings = warn_after - warn_before
if new_warnings:
    tracker.partial("assignments", f"{new_warnings} source error(s)")
else:
    tracker.ok("assignments")

# Stage: slides
slides = scrape_pages(sgy, child, course_filter=get_homework_target(child["name"]))
slide_errors = sum(1 for s in slides if not s.get("fetched"))
if slide_errors and not slides:
    tracker.partial("slides", "homeroom_not_found")
elif slide_errors:
    tracker.partial("slides", f"{slide_errors} slide fetch error(s)")
else:
    tracker.ok("slides")
```

---

## Slide Integration into Summary

### `scrape_pages` gains `course_filter` parameter

```python
def scrape_pages(
    sgy: SchoologySession,
    child: Optional[dict],
    course_filter: str = "all",   # "all" or name substring
    fetch_google_docs: bool = True,
) -> list:
```

When `course_filter != "all"`, only courses whose name contains `course_filter` (case-insensitive) are scraped.

### Preview warmup deduplication

`scrape_assignments` already warms up every course before `scrape_pages` runs. The warmup in `scrape_pages` is therefore a cheap redundant GET (~100ms per course) — no refactoring needed.

### `homework_slides` output per slide

```python
{
    "course": "Homeroom",
    "content": "Read pages 45–60, answer questions 1–5",  # None if fetch failed
    "fetched": True,
    "error": None   # or "export_failed" | "warmup_failed" | "no_embed_found"
}
```

Assignments and slides remain separate arrays. A slide is per-course, not per-assignment — one "Weekly Homework Slide" typically covers all assignments for the week.

---

## Per-Child Homework Targeting Config

```bash
# ~/.sgy/.env

# Per-child targeting: "ChildName:filter" pairs, comma-separated
# filter = "all" (scrape every course) or a course name substring (e.g. "homeroom")
SGY_HOMEWORK_COURSES="Penn:homeroom,Jack:all,Ford:homeroom"

# Optional single-child legacy (used if child not in SGY_HOMEWORK_COURSES)
# SGY_HOMEWORK_COURSE="all"
```

Resolution logic:

```python
def get_homework_target(child_name: str) -> str:
    """Returns 'all' or a course name substring for this child."""
    raw = os.environ.get("SGY_HOMEWORK_COURSES", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        name, target = pair.split(":", 1)
        if name.strip().lower() == child_name.strip().lower():
            return target.strip()
    # Fall back to single-child legacy env var, then default "all"
    return os.environ.get("SGY_HOMEWORK_COURSE", "all")
```

**Effect per child:**

| Child | `SGY_HOMEWORK_COURSES` value | Courses scraped for slides |
|-------|-----------------------------|-----------------------------|
| Penn  | `homeroom`                  | 1 course (fast)             |
| Jack  | `all`                       | All courses (comprehensive) |
| Ford  | `homeroom`                  | 1 course (fast)             |

---

## Failure Mode Reference

| Stage | Example cause | Confidence | `scrape_errors` entry |
|-------|--------------|------------|----------------------|
| `auth` | Bad credentials, network down | `"failed"` | `"auth: Login failed — check credentials"` |
| `child_switch` | Session expired mid-run | `"failed"` | `"child_switch: HTTP 302 on switch"` |
| `courses` | Portal layout change | `"failed"` | `"courses: no courses found"` |
| `assignments` | Sources errored | `"partial"` | `"assignments: 3 source error(s)"` |
| `slides` | Homeroom course not found | `"partial"` | `"slides: homeroom_not_found"` |
| `slides` | Google export 403 | `"partial"` | `"slides: 1 slide fetch error(s)"` |

**Failed child output** (what OpenClaw sees when it should alert parents):

```json
{
  "child": {"name": "Jack", "uid": "456"},
  "scrape_confidence": "failed",
  "scrape_stages": {"auth": "ok", "child_switch": "ok", "courses": "failed", "assignments": "pending", "slides": "pending"},
  "scrape_errors": ["courses: no courses found"],
  "assignments": [],
  "homework_slides": []
}
```

---

## What Changes

| File | Change |
|------|--------|
| `sgy_cli/cli.py` | Add `StageTracker` dataclass |
| `sgy_cli/cli.py` | Add `get_homework_target()` function |
| `sgy_cli/cli.py` | Add `build_failed_child()` helper |
| `sgy_cli/cli.py` | Add `course_filter` param to `scrape_pages()` |
| `sgy_cli/cli.py` | Rewrite `cmd_summary` per-child loop with stage tracking |
| `sgy_cli/cli.py` | Call `scrape_pages` from `cmd_summary` (currently never called) |
| `.env.example` | Add `SGY_HOMEWORK_COURSES` example |
| `README.md` | Document `scrape_confidence`, `homework_slides`, `SGY_HOMEWORK_COURSES` |

## What Does NOT Change

- `scrape_assignments()` internals — no changes
- `scrape_pages()` internals — only adds `course_filter` param
- `SchoologySession` — no changes
- All other commands (`children`, `grades`, `announcements`, `pages`) — no changes

---

## Non-Goals

- Per-assignment slide linkage (slides are per-course, not per-assignment)
- Threshold-based confidence (replaced by checkpoint-based)
- Webhook/push notification on failure (out of scope; OpenClaw handles alerting)
- Per-child session isolation (existing shared session is sufficient)
