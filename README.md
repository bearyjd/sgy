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
sgy children                             # list all children
sgy assignments [--child NAME] [--days N]  # upcoming/overdue work
sgy grades      [--child NAME]           # grades per course
sgy announcements [--child NAME]         # recent feed updates
sgy summary     [--child NAME]           # everything in one shot
```

All commands accept `--json` for machine-readable output. `--child` takes a first-name substring match (e.g. `--child Alex`).

## Examples

```bash
# Human-readable summary for one kid
sgy summary --child Sam

# JSON dump for all kids (what the cron agent runs)
sgy summary --json

# Just assignments for the next 7 days
sgy assignments --child Taylor --days 7 --json
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
