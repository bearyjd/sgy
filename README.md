# sgy — Schoology Parent Portal CLI

CLI scraper for the Schoology parent portal. Pulls assignments, grades, and announcements for all your children. Designed to be called by an OpenClaw cron agent via `--json`.

## Setup

```bash
pip install git+https://github.com/bearyjd/sgy
```

Or with `--break-system-packages` on system Python:

```bash
pip install git+https://github.com/bearyjd/sgy --break-system-packages
```

Configure credentials (pick one):

```bash
# Option A: interactive
sgy init

# Option B: .env file
mkdir -p ~/.sgy && chmod 700 ~/.sgy
cat > ~/.sgy/.env << 'EOF'
SGY_BASE_URL="https://yourschool.schoology.com"
SGY_SCHOOL_NID="1234567890"
SGY_EMAIL="you@example.com"
SGY_PASSWORD="your-password"
EOF
chmod 600 ~/.sgy/.env

# Option C: environment variables
export SGY_BASE_URL="https://yourschool.schoology.com"
export SGY_EMAIL="you@example.com"
export SGY_PASSWORD="your-password"
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `SGY_EMAIL` | yes | — | Schoology login email |
| `SGY_PASSWORD` | yes | — | Schoology login password |
| `SGY_BASE_URL` | no | `https://app.schoology.com` | Your school's Schoology URL |
| `SGY_SCHOOL_NID` | no | — | School node ID (in your login URL's `school=` param) |

Config priority: env vars > `~/.sgy/.env` > `~/.sgy/config.json`

## Usage

```
sgy children                                      # list all children
sgy assignments [--child NAME] [--days N]         # upcoming/overdue work
sgy grades      [--child NAME] [--detail]         # grades per course
sgy announcements [--child NAME]                  # recent feed updates
sgy summary     [--child NAME]                    # everything in one shot
sgy pages       [--child NAME] [--course ID|NAME] [--no-docs]  # course pages + embedded Google Docs
```

All commands accept `--json` for machine-readable output. `--child` takes a first-name substring match (e.g. `--child Alex`).

| Flag | Command | Description |
|---|---|---|
| `--days N` | `assignments` | Look-ahead window in days (default: 14) |
| `--detail` | `grades` | Fetch per-assignment grades (slow, ~3–5s per course) |
| `--course` | `pages` | Filter by course/section ID or name substring |
| `--no-docs` | `pages` | Skip fetching embedded Google Doc/Slides content |

## Examples

```bash
# Human-readable summary for one kid
sgy summary --child Alex

# JSON dump for all kids (what the cron agent runs)
sgy summary --json

# Just assignments for the next 7 days
sgy assignments --child Taylor --days 7 --json

# Per-assignment grade detail for one kid (slower)
sgy grades --child Alex --detail --json

# Course pages with embedded Google Slides content (e.g. homework slides)
sgy pages --child Alex --json

# Pages for a specific course only, skipping Google Docs fetch
sgy pages --child Alex --course "Math" --no-docs --json
```

## AI Agent Integration (OpenClaw / ChatGPT / etc)

To make an AI agent highly effective at parsing your Schoology data, add this to its System Prompt or Tool Description:

```text
# Schoology Data Access (via `sgy` CLI)

You have access to the `sgy` command-line tool to fetch the user's children's school data. ALWAYS use the `--json` flag so you can parse the output programmatically.

## Available Commands:

1. **The Daily Briefing (Use this 90% of the time)**
   `sgy summary --json`
   - Returns a complete overview for ALL children in ~15 seconds.
   - Includes: basic child info, upcoming assignments (14 days), high-level course grades, and recent announcements.

2. **Targeted Assignments Query**
   `sgy assignments --child <FirstName> --days <N> --json`
   - Use when the user asks specifically about homework, tests, or due dates.
   - Example: `sgy assignments --child Alex --days 7 --json`

3. **Deep-Dive Grades Query (IMPORTANT)**
   `sgy grades --child <FirstName> --detail --json`
   - Use the `--detail` flag ONLY when the user asks for specific assignment grades, test scores, or "why is their grade low?".
   - Note: The `--detail` flag takes ~3-5 seconds per course, so only use it for a single child when specifically requested.

4. **Announcements Query**
   `sgy announcements --child <FirstName> --json`
   - Use to check teacher/school updates from the activity feed.

## JSON Structure Guide for `sgy summary --json`:
- `timestamp`: When the data was fetched
- `children`: Array of [{name, uid, building}]
- `per_child`: Array containing the actual data per student
  - `child`: {name, uid, building}
  - `scrape_confidence`: `"high"` | `"partial"` | `"failed"` — data reliability indicator
  - `scrape_stages`: checkpoint results for auth, child_switch, courses, assignments, slides
  - `scrape_errors`: list of error strings (e.g. `"slides: homeroom_not_found"`)
  - `assignments`: [{title, course, due_date, status, link}]
  - `homework_slides`: [{course, title, content, fetched, error}] — Google Slides homework text
  - `grades`: [{course, grade, letter, items: []}]
  - `announcements`: [{title, body, author, date, course}]
  - `warnings`: per-child non-fatal warnings from scraping

When summarizing for the user:
- Ignore courses where the `grade` is empty (`""` or `"—"`).
- Highlight assignments due within the next 48 hours.
- Check `scrape_confidence` first: if `"failed"`, alert the user that data may be missing.
- Use `homework_slides[].content` for the actual text of what homework to do.
- Do not output raw JSON to the user; format it into a friendly, readable daily briefing.
```

## How it works

- Logs in with username/password via the standard Schoology login form
- Caches session cookies to `~/.sgy/session.json` (90-min TTL, auto-refreshes)
- Discovers children from the `childrenAccounts` array in the page's embedded JS
- Switches between children using `GET /parent/home?format=json&child_uid=UID` (reverse-engineered from Schoology's own `s_parent.js`)
- Pulls courses/grades overview from the enrollments tab AJAX endpoint
- Pulls upcoming assignments from `/home/upcoming_submissions_ajax`
- Pulls per-assignment grade detail from each course's `student_grades` page (`table[role="presentation"]`)
- All status/debug output goes to stderr; `--json` output is clean on stdout

## File layout

```
~/.sgy/
  .env              # SGY_BASE_URL, SGY_EMAIL, SGY_PASSWORD, etc. (0600)
  session.json      # cached session cookies (0600, auto-managed)
```

## Cron

```bash
# Daily at 6am — dump full summary for all kids
0 6 * * * /usr/local/bin/sgy summary --json > /tmp/schoology-daily.json 2>/dev/null
```
