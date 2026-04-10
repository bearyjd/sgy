"""
sgy — Schoology parent portal CLI scraper.

Logs in via session cookies, caches sessions, and scrapes
assignments, grades, and announcements for all children.

Usage:
    sgy init                                          — set up credentials interactively
    sgy children                                      — list children with IDs
    sgy assignments [--child NAME] [--days N] [--json]
    sgy grades      [--child NAME] [--detail] [--json]
    sgy announcements [--child NAME] [--json]
    sgy summary     [--child NAME] [--json]
    sgy pages       [--child NAME] [--course ID|NAME] [--no-docs] [--json]
"""

import argparse
import getpass
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import requests
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------

SGY_DIR = Path.home() / ".sgy"
CONFIG_PATH = SGY_DIR / "config.json"
ENV_PATH = SGY_DIR / ".env"
SESSION_PATH = SGY_DIR / "session.json"
EMBED_CACHE_PATH = SGY_DIR / "embed_cache.json"

DEFAULT_BASE_URL = "https://app.schoology.com"
DEFAULT_SCHOOL_NID = ""

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Schoology sessions typically last a few hours; re-auth after 90 min.
SESSION_TTL = 90 * 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dir():
    SGY_DIR.mkdir(mode=0o700, exist_ok=True)


def _log(msg: str, verbose: bool = True):
    """Print to stderr so it doesn't pollute --json stdout."""
    if verbose:
        print(msg, file=sys.stderr)


def _load_env_file(path: Path) -> dict:
    """Parse a .env file into a dict. Handles KEY=VALUE and KEY="VALUE"."""
    env = {}
    if not path.exists():
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Strip surrounding quotes and unescape
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
                val = val.replace('\\"', '"').replace("\\\\", "\\")
            env[key] = val
    return env


def load_config() -> dict:
    """Load credentials + site config. Priority: env vars > ~/.sgy/.env > ~/.sgy/config.json."""
    # Merge all sources (env vars override .env file override config.json)
    cfg = {}

    # 3. Legacy config.json (lowest priority)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))

    # 2. .env file
    env = _load_env_file(ENV_PATH)
    if env.get("SGY_EMAIL"):
        cfg["email"] = env["SGY_EMAIL"]
    if env.get("SGY_PASSWORD"):
        cfg["password"] = env["SGY_PASSWORD"]
    if env.get("SGY_BASE_URL"):
        cfg["base_url"] = env["SGY_BASE_URL"]
    if env.get("SGY_SCHOOL_NID"):
        cfg["school_nid"] = env["SGY_SCHOOL_NID"]

    # 1. Environment variables (highest priority)
    if os.environ.get("SGY_EMAIL"):
        cfg["email"] = os.environ["SGY_EMAIL"]
    if os.environ.get("SGY_PASSWORD"):
        cfg["password"] = os.environ["SGY_PASSWORD"]
    if os.environ.get("SGY_BASE_URL"):
        cfg["base_url"] = os.environ["SGY_BASE_URL"]
    if os.environ.get("SGY_SCHOOL_NID"):
        cfg["school_nid"] = os.environ["SGY_SCHOOL_NID"]

    if not cfg.get("email") or not cfg.get("password"):
        print(
            "No credentials found. Set SGY_EMAIL/SGY_PASSWORD env vars,\n"
            "create ~/.sgy/.env, or run `sgy init`.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Apply defaults for optional fields
    cfg.setdefault("base_url", DEFAULT_BASE_URL)
    cfg.setdefault("school_nid", DEFAULT_SCHOOL_NID)

    return cfg


def save_config(cfg: dict):
    _ensure_dir()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def save_session(cookies_jar):
    """Save session cookies with full metadata (domain, path)."""
    _ensure_dir()
    cookie_list = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in cookies_jar
    ]
    data = {"cookies": cookie_list, "ts": time.time()}
    with open(SESSION_PATH, "w") as f:
        json.dump(data, f)
    os.chmod(SESSION_PATH, 0o600)


def load_session() -> Optional[dict]:
    """Return saved cookies if still fresh, else None."""
    if not SESSION_PATH.exists():
        return None
    try:
        with open(SESSION_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("ts", 0) > SESSION_TTL:
        return None
    return data.get("cookies")


def _parse_date(date_str: str) -> Optional[datetime]:
    """Try multiple date formats commonly used by Schoology.

    Handles ISO dates, US formats, named months, and relative phrases
    like 'Due tomorrow', 'Overdue by 2 days', 'Due in 3 days'.
    """
    if not date_str:
        return None
    date_str = date_str.strip()

    # --- Relative date phrases (Schoology uses these in upcoming widgets) ---
    lower = date_str.lower()
    now = datetime.now()

    if "today" in lower:
        return now.replace(hour=23, minute=59, second=0, microsecond=0)
    if "tomorrow" in lower:
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
    if "yesterday" in lower:
        return (now - timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)

    # "Due in 3 days" / "in 2 days"
    m_in = re.search(r"in\s+(\d+)\s+day", lower)
    if m_in:
        return (now + timedelta(days=int(m_in.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    # "Overdue by 2 days"
    m_overdue = re.search(r"overdue\s+by\s+(\d+)\s+day", lower)
    if m_overdue:
        return (now - timedelta(days=int(m_overdue.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    # "2 days ago"
    m_ago = re.search(r"(\d+)\s+days?\s+ago", lower)
    if m_ago:
        return (now - timedelta(days=int(m_ago.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    # "Due Mon", "Due Tuesday" etc. — resolve to next occurrence of that weekday
    day_names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    m_day = re.search(r"(?:due\s+)?(mon|tue|wed|thu|fri|sat|sun)\w*", lower)
    if m_day and not re.search(r"\d", date_str):  # only if no numeric date present
        target = day_names[m_day.group(1)[:3]]
        current = now.weekday()
        delta = (target - current) % 7
        # delta == 0 means today (same weekday) — don't push to next week
        return (now + timedelta(days=delta)).replace(hour=23, minute=59, second=0, microsecond=0)

    # --- Absolute date formats ---
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%b %d, %Y at %I:%M %p",
        "%B %d, %Y at %I:%M %p",
        "%b %d",          # "Mar 21" — assume current year
        "%B %d",          # "March 21"
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            # Strip timezone info to keep all datetimes naive (compared with datetime.now())
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            # If format had no year, fill in current year
            if fmt in ("%b %d", "%B %d"):
                dt = dt.replace(year=now.year)
                # If that date is >6 months ago, assume next year
                if (now - dt).days > 180:
                    dt = dt.replace(year=now.year + 1)
            return dt
        except ValueError:
            continue

    # Last resort: extract any YYYY-MM-DD substring
    m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _extract_letter(grade_text: str) -> str:
    """Extract letter grade from a grade string like '95% A' or 'A+'."""
    m = re.search(r"\b([A-F][+-]?)\b", grade_text)
    return m.group(1) if m else ""


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


def build_failed_child(child: dict, tracker: "StageTracker") -> dict:
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


# ---------------------------------------------------------------------------
# Schoology session management
# ---------------------------------------------------------------------------

class SchoologySession:
    """Wraps requests.Session with Schoology login + child switching."""

    def __init__(self, verbose: bool = True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": DEFAULT_UA})
        self.cfg = load_config()
        self.verbose = verbose
        self.warnings: list = []
        self._logged_in = False
        self._children: Optional[list] = None
        self._current_child_uid: Optional[str] = None
        self._parent_home_soup: Optional[BeautifulSoup] = None
        self._last_request_time = 0.0
        self._folder_cache: dict = {}
        # Separate session for Google fetches (no Schoology auth cookies)
        self._google_session = requests.Session()
        self._google_session.headers.update({"User-Agent": DEFAULT_UA})

        # Derive URLs from config
        self.base_url = self.cfg["base_url"].rstrip("/")
        self.school_nid = self.cfg["school_nid"]

    def _sleep_if_needed(self):
        """Add jitter/sleep to avoid 429 Too Many Requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        # Want at least ~0.5s to 1.0s between requests
        target_wait = random.uniform(0.5, 1.2)
        if elapsed < target_wait:
            time.sleep(target_wait - elapsed)
        self._last_request_time = time.time()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Wrapper around requests to add rate limiting and retries."""
        max_retries = 3
        base_delay = 2.0
        r = None

        for attempt in range(max_retries):
            self._sleep_if_needed()
            r = self.s.request(method, url, **kwargs)
            
            if r.status_code == 429:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                _log(f"Rate limited (429). Retrying in {delay:.1f}s...", self.verbose)
                time.sleep(delay)
                continue
                
            return r
            
        if r is None:
            raise RuntimeError("Request failed completely")
        # All retries exhausted on 429 — raise instead of returning error response
        if r.status_code == 429:
            raise requests.exceptions.HTTPError(
                f"Rate limited (429) after {max_retries} retries: {r.url}",
                response=r,
            )
        return r

    # -- login / session cache --

    def ensure_logged_in(self):
        if self._logged_in:
            return

        cached = load_session()
        if cached:
            if isinstance(cached, list):
                for c in cached:
                    self.s.cookies.set(c["name"], c["value"],
                                       domain=c.get("domain", ""), path=c.get("path", "/"))
            elif isinstance(cached, dict):
                self.s.cookies.update(cached)
            try:
                r = self._request("GET", f"{self.base_url}/home", allow_redirects=False, timeout=15)
                loc = r.headers.get("Location", "")
                if r.status_code == 200 or (r.status_code == 302 and "/login" not in loc):
                    self._logged_in = True
                    _log("Using cached session.", self.verbose)
                    return
            except requests.RequestException:
                pass

        try:
            self._do_login()
        except (RuntimeError, requests.RequestException):
            _log("Login failed, retrying in 3s...", self.verbose)
            time.sleep(3)
            self._do_login()

    def _do_login(self):
        _log("Logging in to Schoology...", self.verbose)
        login_url = f"{self.base_url}/login"
        login_dest = f"{login_url}?destination=parent/home"
        if self.school_nid:
            login_dest += f"&school={self.school_nid}"

        r = self._request("GET", login_dest, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        fbi = soup.find("input", {"name": "form_build_id"})
        if not fbi:
            raise RuntimeError("Cannot find form_build_id on login page — site may have changed.")

        payload = {
            "mail": self.cfg["email"],
            "pass": self.cfg["password"],
            "school_nid": self.school_nid,
            "form_build_id": fbi["value"],
            "form_id": "s_user_login_form",
            "op": "Log in",
        }
        r = self._request("POST", login_dest, data=payload, allow_redirects=True, timeout=15)

        if "/login" in r.url or "Access denied" in r.text:
            raise RuntimeError("Login failed — check credentials in ~/.sgy/config.json")

        self._logged_in = True
        save_session(self.s.cookies)
        _log("Login successful.", self.verbose)

    # -- child discovery --

    def get_children(self) -> list:
        """Return list of dicts: {name, uid, building}.

        Children are extracted from the siteNavigationUiProps JS variable
        embedded in the parent home page. The key is 'childrenAccounts'.
        """
        if self._children is not None:
            return self._children

        self.ensure_logged_in()

        r = self._request("GET", f"{self.base_url}/parent/home", timeout=15)
        self._parent_home_soup = BeautifulSoup(r.text, "html.parser")
        html = r.text
        children = []

        # Primary: Parse childrenAccounts from siteNavigationUiProps JS
        # This is the proven method — the data lives in:
        #   window.siteNavigationUiProps = {..., childrenAccounts: [{name, id, buildingName}, ...]}
        m = re.search(
            r'"childrenAccounts"\s*:\s*(\[.*?\])',
            html,
        )
        if m:
            try:
                accounts = json.loads(m.group(1))
                for acct in accounts:
                    children.append({
                        "name": acct.get("name", "Unknown"),
                        "uid": str(acct.get("id", "")),
                        "building": acct.get("buildingName", ""),
                    })
            except json.JSONDecodeError:
                pass

        # Fallback: Parse from any JSON blob with {name, id, buildingName} pattern
        if not children:
            for match in re.finditer(
                r'\{"name"\s*:\s*"([^"]+)"\s*,\s*"profilePictureUrl"\s*:\s*"[^"]*"\s*,'
                r'\s*"buildingName"\s*:\s*"([^"]*)"\s*,\s*"id"\s*:\s*(\d+)\}',
                html,
            ):
                children.append({
                    "name": match.group(1),
                    "uid": match.group(3),
                    "building": match.group(2),
                })

        # Deduplicate by uid
        seen = set()
        unique = []
        for c in children:
            if c["uid"] and c["uid"] not in seen:
                seen.add(c["uid"])
                unique.append(c)

        self._children = unique
        return self._children

    def resolve_child(self, name_hint: Optional[str]) -> Optional[dict]:
        """Resolve a child by name (case-insensitive substring match)."""
        children = self.get_children()
        if not children:
            return None
        if name_hint is None:
            return children[0]

        hint = name_hint.lower()
        # Exact first-name match
        for c in children:
            first = c["name"].split()[0].lower() if c["name"] else ""
            if hint == first:
                return c
        # Substring match
        for c in children:
            if hint in c["name"].lower():
                return c
        return None

    # -- child switching --

    def switch_to_child(self, child: dict):
        """Switch the server-side session to view as this child.

        Uses the proven mechanism: GET /parent/home?format=json&child_uid=UID
        with XHR header. This is what the Schoology JS uses internally.
        """
        uid = child["uid"]
        if self._current_child_uid == uid:
            return
        self.ensure_logged_in()
        self._request(
            "GET",
            f"{self.base_url}/parent/home",
            params={"format": "json", "child_uid": uid},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        self._current_child_uid = uid
        self._parent_home_soup = None  # invalidate cached soup

    # -- page fetching --

    def get_parent_home_soup(self) -> BeautifulSoup:
        """Get the parent home page (cached per child switch)."""
        if self._parent_home_soup is None:
            self.ensure_logged_in()
            r = self._request("GET", f"{self.base_url}/parent/home", timeout=15)
            self._parent_home_soup = BeautifulSoup(r.text, "html.parser")
        return self._parent_home_soup

    def fetch_page(self, path: str, params: Optional[dict] = None) -> BeautifulSoup:
        """Fetch a page and return parsed soup."""
        self.ensure_logged_in()
        r = self._request("GET", f"{self.base_url}{path}", params=params, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    def fetch_json(self, path: str, params: Optional[dict] = None) -> Union[dict, list, None]:
        """Fetch JSON from an internal API path."""
        self.ensure_logged_in()
        try:
            r = self._request(
                "GET",
                f"{self.base_url}{path}",
                params=params,
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=15,
            )
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as exc:
            _log(f"  [warn] fetch_json({path}) failed: {exc}", self.verbose)
            return None

    def get_folder(self, sid: str) -> Optional[dict]:
        """Fetch folder API with per-session caching."""
        if sid in self._folder_cache:
            return self._folder_cache[sid]
        data = self.fetch_json(f"/v1/courses/{sid}/folder/0")
        if data is not None:
            self._folder_cache[sid] = data
        return data


# ---------------------------------------------------------------------------
# Course discovery (from parent home s-advisor-course-table)
# ---------------------------------------------------------------------------

def get_courses_and_grades(sgy: SchoologySession, child: Optional[dict] = None) -> list:
    """Get list of courses + overview grades from the parent home enrollments tab.

    Uses the AJAX endpoint: GET /parent/home/enrollments?format=json
    which returns JSON with content containing an .s-advisor-course-table:
        <tr>
            <td class="column-one">
                <span class="course-name">
                    <a href="/course/{sid}/preview/{child_uid}/parent">Course Name</a>
                </span>
            </td>
            <td class="column-two">Grade or -</td>
        </tr>
    """
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    courses = []
    soup = None

    # Primary: fetch the enrollments tab via AJAX (works after child switching)
    data = sgy.fetch_json("/parent/home/enrollments", params={"format": "json"})
    if data and isinstance(data, dict) and "content" in data:
        content = data["content"]
        html = ""
        if isinstance(content, str):
            html = content
        elif isinstance(content, dict):
            html = content.get("main", "")
        if html:
            soup = BeautifulSoup(html, "html.parser")

    # Fallback: try the parent home page directly (works on initial load)
    if soup is None:
        soup = sgy.get_parent_home_soup()

    table = soup.select_one(".s-advisor-course-table")
    if not table:
        return courses

    for tr in table.select("tr"):
        course_el = tr.select_one(".course-name a")
        grade_el = tr.select_one(".column-two")

        if not course_el:
            continue

        href = course_el.get("href", "")
        name = course_el.get_text(strip=True)
        grade_text = grade_el.get_text(strip=True) if grade_el else ""

        # Extract section_id from href: /course/{sid}/preview/{uid}/parent
        sid_match = re.search(r"/course/(\d+)/", href)
        section_id = sid_match.group(1) if sid_match else ""

        # Clean up grade: "-" means no grade
        if grade_text == "-":
            grade_text = ""

        courses.append({
            "name": name,
            "section_id": section_id,
            "grade": grade_text,
            "letter": _extract_letter(grade_text),
            "href": href,
        })

    return courses


# ---------------------------------------------------------------------------
# Assignments scraper
# ---------------------------------------------------------------------------

def _dedup_assignments(items: list) -> list:
    """Deduplicate assignments by normalized title+course or by link.

    When the same assignment appears from multiple sources (AJAX, calendar,
    folder API, grades page) we keep the richest version — the one with the
    most non-empty fields.
    """
    def _key(a: dict) -> str:
        link = a.get("link", "").strip().rstrip("/")
        if link:
            # Normalize to just the path portion
            link = re.sub(r"^https?://[^/]+", "", link)
            return f"link:{link}"
        title = re.sub(r"\s+", " ", a.get("title", "").strip().lower())
        course = re.sub(r"\s+", " ", a.get("course", "").strip().lower())
        return f"tc:{title}|{course}"

    def _richness(a: dict) -> int:
        """Score how many useful fields this record has."""
        score = 0
        for k in ("title", "course", "due_date", "status", "link", "grade"):
            v = a.get(k, "")
            if v and v != "unknown":
                score += 1
        return score

    best: dict = {}  # key -> assignment
    for a in items:
        k = _key(a)
        if k not in best or _richness(a) > _richness(best[k]):
            best[k] = a
    return list(best.values())


def scrape_assignments(sgy: SchoologySession, child: Optional[dict], days: int = 14) -> list:
    """Scrape upcoming/overdue assignments for a child.

    Runs ALL sources and merges results (no short-circuiting):
      1. AJAX upcoming submissions endpoint
      2. Home page upcoming events widget
      3. Calendar feed (AJAX + iCal)
      4. Folder API per-course (/v1/courses/{sid}/folder/0)
      5. Course materials HTML scrape
      6. Grades page cross-reference (catches graded items not listed elsewhere)

    Results are deduplicated by title+course or link, keeping the richest record.
    """
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    all_items: list = []
    source_counts: dict = {}

    # --- Source 1: AJAX upcoming submissions endpoint ---
    try:
        data = sgy.fetch_json("/home/upcoming_submissions_ajax")
        if data and isinstance(data, dict) and data.get("html"):
            soup = BeautifulSoup(data["html"], "html.parser")
            found = _parse_upcoming_events(soup)
            all_items.extend(found)
            source_counts["ajax_upcoming"] = len(found)
    except Exception as exc:
        _log(f"  [warn] ajax_upcoming failed: {exc}", sgy.verbose)
        sgy.warnings.append(f"ajax_upcoming: {exc}")
        source_counts["ajax_upcoming"] = 0

    # --- Source 2: Home page upcoming events widget ---
    try:
        soup = sgy.fetch_page("/home")
        found = _parse_upcoming_events(soup)
        all_items.extend(found)
        source_counts["home_widget"] = len(found)
    except Exception as exc:
        _log(f"  [warn] home_widget failed: {exc}", sgy.verbose)
        sgy.warnings.append(f"home_widget: {exc}")
        source_counts["home_widget"] = 0

    # --- Source 3: Calendar feed ---
    try:
        found = _scrape_calendar_assignments(sgy)
        all_items.extend(found)
        source_counts["calendar"] = len(found)
    except Exception as exc:
        _log(f"  [warn] calendar failed: {exc}", sgy.verbose)
        sgy.warnings.append(f"calendar: {exc}")
        source_counts["calendar"] = 0

    # --- Sources 4 & 5: Per-course folder API + materials HTML ---
    courses = get_courses_and_grades(sgy, child)
    child_uid = child.get("uid", "") if child else ""
    max_courses = 15
    if len(courses) > max_courses:
        _log(f"  [warn] {len(courses)} courses found, limiting to {max_courses}", sgy.verbose)
        sgy.warnings.append(f"courses_truncated: {len(courses)} courses, showing {max_courses}")
    folder_count = 0
    materials_count = 0
    for course in courses[:max_courses]:
        sid = course.get("section_id", "")
        if not sid:
            continue
        cname = course.get("name", "")

        # Preview warmup — needed for parent accounts to access course-level URLs
        if child_uid:
            try:
                sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)
            except Exception as exc:
                _log(f"  [warn] preview warmup({cname}) failed: {exc}", sgy.verbose)

        # Source 4: Folder API — structured list of ALL material types
        try:
            found = _get_assignments_from_folder_api(sgy, sid)
            for a in found:
                a["course"] = cname
            all_items.extend(found)
            folder_count += len(found)
        except Exception as exc:
            _log(f"  [warn] folder_api({cname}) failed: {exc}", sgy.verbose)
            sgy.warnings.append(f"folder_api({cname}): {exc}")

        # Source 5: Materials HTML scrape (catches UI-rendered items)
        try:
            csoup = sgy.fetch_page(f"/course/{sid}/materials")
            for item in csoup.select(".type-assignment, .type-discussion, .type-assessment, .material-row"):
                a = _parse_material_item(item)
                if a:
                    a["course"] = cname
                    all_items.append(a)
                    materials_count += 1
        except Exception as exc:
            _log(f"  [warn] materials_html({cname}) failed: {exc}", sgy.verbose)
            sgy.warnings.append(f"materials_html({cname}): {exc}")
            continue

    source_counts["folder_api"] = folder_count
    source_counts["materials_html"] = materials_count

    # --- Source 6: Grades page cross-reference ---
    # Note: preview warmup was already done in the sources 4&5 loop above,
    # and the server-side auth context persists per-course, so we don't need
    # to repeat warmup here if courses are the same. But if the loop above
    # was truncated or skipped a course, we warm up again to be safe.
    grades_count = 0
    for course in courses[:max_courses]:
        sid = course.get("section_id", "")
        if not sid:
            continue
        cname = course.get("name", "")
        # Preview warmup for parent accounts
        if child_uid:
            try:
                sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)
            except Exception:
                pass  # warmup failure is non-fatal; _get_assignments_from_grades handles 403
        try:
            found = _get_assignments_from_grades(sgy, sid)
            for a in found:
                a["course"] = cname
            all_items.extend(found)
            grades_count += len(found)
        except Exception as exc:
            _log(f"  [warn] grades_xref({cname}) failed: {exc}", sgy.verbose)
            sgy.warnings.append(f"grades_xref({cname}): {exc}")
            continue
    source_counts["grades_xref"] = grades_count

    # --- Deduplicate ---
    before_dedup = len(all_items)
    assignments = _dedup_assignments(all_items)

    # --- Filter by date window ---
    if days and assignments:
        now = datetime.now()
        cutoff_future = now + timedelta(days=days)
        cutoff_past = now - timedelta(days=7)
        filtered = []
        for a in assignments:
            dt = _parse_date(a.get("due_date", ""))
            if dt:
                if cutoff_past <= dt <= cutoff_future:
                    filtered.append(a)
            else:
                # Keep items with no parseable date (better to show than to hide)
                filtered.append(a)
        assignments = filtered

    # --- Debug summary ---
    _log(
        f"Assignment sources: {source_counts} | "
        f"raw={before_dedup} deduped={len(_dedup_assignments(all_items))} "
        f"after_date_filter={len(assignments)}",
        sgy.verbose,
    )

    return assignments


def _scrape_calendar_assignments(sgy: SchoologySession) -> list:
    """Scrape assignments from Schoology's calendar feed.

    Tries multiple calendar endpoints that aggregate due dates across all
    assignment types (including those without online submission).
    """
    results = []

    # Strategy A: Calendar upcoming AJAX (mirrors the calendar widget)
    try:
        now = datetime.now()
        start = now - timedelta(days=7)
        end = now + timedelta(days=30)
        data = sgy.fetch_json(
            "/calendar/feed_ajax/upcoming",
            params={
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
            },
        )
        if data and isinstance(data, list):
            for event in data:
                title = event.get("title", "").strip()
                if not title:
                    continue
                results.append({
                    "title": title,
                    "course": event.get("course_title", "") or event.get("realm_title", ""),
                    "due_date": event.get("start", "") or event.get("end", ""),
                    "status": "unknown",
                    "link": event.get("url", "") or event.get("link", ""),
                })
    except Exception as exc:
        _log(f"  [warn] calendar_upcoming failed: {exc}", sgy.verbose)
        sgy.warnings.append(f"calendar_upcoming: {exc}")

    # Strategy B: Calendar feed AJAX (full events range)
    if not results:
        try:
            now = datetime.now()
            start = now - timedelta(days=7)
            end = now + timedelta(days=30)
            data = sgy.fetch_json(
                "/calendar/feed_ajax",
                params={
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                },
            )
            if data and isinstance(data, list):
                for event in data:
                    title = event.get("title", "").strip()
                    if not title:
                        continue
                    # Calendar events include meetings, so filter to assignment-like items
                    etype = event.get("type", "").lower()
                    if etype and etype not in ("assignment", "event", "assessment", "discussion", ""):
                        continue
                    results.append({
                        "title": title,
                        "course": event.get("course_title", "") or event.get("realm_title", ""),
                        "due_date": event.get("start", "") or event.get("end", ""),
                        "status": "unknown",
                        "link": event.get("url", "") or event.get("link", ""),
                    })
        except Exception as exc:
            _log(f"  [warn] calendar_feed failed: {exc}", sgy.verbose)
            sgy.warnings.append(f"calendar_feed: {exc}")

    return results


def _get_assignments_from_folder_api(sgy: SchoologySession, sid: str) -> list:
    """Use /v1/courses/{sid}/folder/0 to find assignments, discussions, and assessments.

    The folder API returns structured items with a 'type' field. Unlike the pages
    scraper which only collects 'page' and 'document', this collects assignment-like types.
    """
    data = sgy.get_folder(sid)
    if not data or not isinstance(data, dict):
        return []

    items = data.get("folder-item", [])
    if isinstance(items, dict):
        items = [items]

    results = []
    assignment_types = {"assignment", "discussion", "assessment", "quiz", "test"}

    for item in items:
        item_type = item.get("type", "").lower()
        if item_type not in assignment_types:
            continue
        title = item.get("title", "").strip()
        if not title:
            continue

        due = item.get("due", "") or item.get("due_date", "")
        link = ""
        item_id = item.get("id", "")
        if item_id:
            link = f"/course/{sid}/{item_type}/{item_id}"

        results.append({
            "title": title,
            "course": "",
            "due_date": due,
            "status": "unknown",
            "link": link,
        })

    return results


def _get_assignments_from_grades(sgy: SchoologySession, sid: str) -> list:
    """Extract assignment names from the grades page as a cross-reference.

    Any item that has a grade entry is an assignment the student should know about.
    Items that appear here but not in other sources are homework we'd otherwise miss.
    """
    try:
        soup = sgy.fetch_page(f"/course/{sid}/student_grades")
    except Exception as exc:
        _log(f"  [warn] grades page for section {sid} failed: {exc}", sgy.verbose)
        sgy.warnings.append(f"grades_page({sid}): {exc}")
        return []

    grade_table = soup.find("table", {"role": "presentation"})
    if not grade_table:
        return []

    results = []
    for tr in grade_table.select("tr.report-row"):
        classes = tr.get("class", [])
        if "course-row" in classes or "period-row" in classes or "category-row" in classes:
            continue

        title_td = tr.select_one("td.title-column")
        if not title_td:
            continue

        title_el = title_td.select_one("a.sExtlink-processed, a, .title")
        name = title_el.get_text(strip=True) if title_el else title_td.get_text(strip=True)
        if not name or name == "Category":
            continue

        link = ""
        if title_el and title_el.name == "a":
            link = title_el.get("href", "")

        due_el = title_td.select_one(".due-date, .due")
        due = due_el.get_text(strip=True) if due_el else ""

        grade_td = tr.select_one("td.grade-column")
        grade_text = ""
        if grade_td:
            rounded = grade_td.select_one(".rounded-grade")
            if rounded:
                grade_text = rounded.get_text(strip=True)
            else:
                grade_text = grade_td.get_text(strip=True)
            if grade_text in ("—", "-"):
                grade_text = ""

        # Determine status from grade
        status = "unknown"
        if grade_text:
            status = "graded"
        else:
            # No grade could mean missing, upcoming, or excused
            exception_el = tr.select_one(".exception-text, .exception")
            if exception_el:
                status = exception_el.get_text(strip=True).lower()

        results.append({
            "title": name,
            "course": "",
            "due_date": due,
            "status": status,
            "link": link,
            "grade": grade_text,
        })

    return results


def _parse_upcoming_events(soup: BeautifulSoup) -> list:
    """Parse upcoming assignments from Schoology's upcoming widget.

    Proven selectors (from schoology-mcp):
      .upcoming-event
      .event-title a           -> assignment title + link
      .readonly-title.event-subtitle  -> [0]=due text, [-1]=course name
    """
    results = []

    for event in soup.select(".upcoming-event"):
        title_tag = event.select_one(".event-title a") or event.select_one(".event-title")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        link = title_tag.get("href", "") if title_tag.name == "a" else ""

        subtitles = event.select(".readonly-title.event-subtitle")
        due = subtitles[0].get_text(strip=True) if subtitles else ""
        course = subtitles[-1].get_text(strip=True) if len(subtitles) > 1 else ""

        time_el = event.select_one("time, .event-date")
        if time_el:
            due = time_el.get("datetime", "") or time_el.get_text(strip=True) or due

        status = "unknown"
        status_el = event.select_one('.submission-status, [class*="status"]')
        if status_el:
            status = status_el.get_text(strip=True)

        results.append({
            "title": title,
            "course": course,
            "due_date": due,
            "status": status,
            "link": link,
        })

    # Fallback: broader selectors for the upcoming widget
    if not results:
        upcoming = soup.select_one(".upcoming-events, .upcoming-events-wrapper")
        if upcoming:
            for li in upcoming.select("li"):
                a_tag = li.select_one("a")
                if a_tag:
                    results.append({
                        "title": a_tag.get_text(strip=True),
                        "course": "",
                        "due_date": "",
                        "status": "unknown",
                        "link": a_tag.get("href", ""),
                    })

    return results


def _parse_material_item(item: Tag) -> Optional[dict]:
    """Parse an assignment from a course materials page."""
    title_el = item.select_one("a.sExtlink-processed, a[href*='/assignment/'], .title a, a")
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    if not title:
        return None

    link = title_el.get("href", "")
    due_el = item.select_one(".due-date, time, .date")
    due = ""
    if due_el:
        due = due_el.get("datetime", "") or due_el.get_text(strip=True)

    return {
        "title": title,
        "course": "",
        "due_date": due,
        "status": "unknown",
        "link": link,
    }


# ---------------------------------------------------------------------------
# Grades scraper
# ---------------------------------------------------------------------------

def scrape_grades(sgy: SchoologySession, child: Optional[dict], detail: bool = True) -> list:
    """Scrape grades per course for a child.

    Primary source: .s-advisor-course-table on /parent/home gives course + overall grade.
    Detail (if detail=True): /course/{sid}/student_grades has table[role="presentation"]
    with per-assignment grades. This is slow (1 request per course) so can be skipped.
    """
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    # Get overview from parent home (always available, single request)
    courses = get_courses_and_grades(sgy, child)
    child_uid = child.get("uid", "") if child else ""

    grades = []
    for course in courses:
        grade_entry = {
            "course": course["name"],
            "grade": course["grade"],
            "letter": course["letter"],
            "items": [],
        }

        # Optionally fetch detailed per-assignment grades (slow — 1 req per course)
        if detail:
            sid = course.get("section_id", "")
            if sid:
                # Preview warmup — needed for parent accounts to access student_grades
                if child_uid:
                    try:
                        sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)
                    except Exception as exc:
                        _log(f"  [warn] preview warmup({course['name']}) failed: {exc}", sgy.verbose)
                try:
                    detail_items = _scrape_course_grade_detail(sgy, sid)
                    grade_entry["items"] = detail_items
                except Exception as exc:
                    _log(f"  [warn] grade detail for {course['name']} failed: {exc}", sgy.verbose)
                    sgy.warnings.append(f"grade_detail({course['name']}): {exc}")

        grades.append(grade_entry)

    return grades


def _scrape_course_grade_detail(sgy: SchoologySession, section_id: str) -> list:
    """Scrape per-assignment grades from /course/{sid}/student_grades.

    Proven structure (from SchoologyGradeChecker):
        table[role="presentation"]
        tr.report-row with classes: course-row, period-row, category-row, item-row
        td.title-column  -> assignment name (contains a.sExtlink-processed)
        td.grade-column  -> grade (span.rounded-grade / span.max-grade)
        td.comment-column -> teacher comment
    """
    soup = sgy.fetch_page(f"/course/{section_id}/student_grades")
    items = []

    grade_table = soup.find("table", {"role": "presentation"})
    if not grade_table:
        return items

    for tr in grade_table.select("tr.report-row"):
        classes = tr.get("class", [])

        # Skip course-row and period-row headers
        if "course-row" in classes or "period-row" in classes:
            continue

        title_td = tr.select_one("td.title-column")
        grade_td = tr.select_one("td.grade-column")
        comment_td = tr.select_one("td.comment-column")

        if not title_td:
            continue

        # Extract item name
        title_el = title_td.select_one("a.sExtlink-processed, a, .title")
        name = ""
        if title_el:
            name = title_el.get_text(strip=True)
        else:
            name = title_td.get_text(strip=True)

        if not name or name == "Category":
            continue

        # Extract grade
        grade_text = ""
        if grade_td:
            rounded = grade_td.select_one(".rounded-grade")
            max_grade = grade_td.select_one(".max-grade")
            if rounded:
                grade_text = rounded.get_text(strip=True)
                if max_grade:
                    grade_text += f" / {max_grade.get_text(strip=True)}"
            else:
                grade_text = grade_td.get_text(strip=True)
            if grade_text in ("—", "-"):
                grade_text = ""

        # Extract due date
        due_el = title_td.select_one(".due-date, .due")
        due = due_el.get_text(strip=True) if due_el else ""

        # Comment
        comment = ""
        if comment_td:
            ct = comment_td.get_text(strip=True)
            if ct and ct != "No comment":
                comment = ct

        is_category = "category-row" in classes

        items.append({
            "name": name,
            "grade": grade_text,
            "due_date": due,
            "comment": comment,
            "is_category": is_category,
        })

    return items


# ---------------------------------------------------------------------------
# Pages scraper (course materials → pages with embedded Google Slides/Docs)
# ---------------------------------------------------------------------------

def _load_embed_cache() -> dict:
    if EMBED_CACHE_PATH.exists():
        try:
            raw = json.loads(EMBED_CACHE_PATH.read_text())
            now = time.time()
            ttl = 7 * 86400  # 7 days
            return {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and now - v.get("ts", 0) < ttl
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_embed_cache(cache: dict):
    _ensure_dir()
    EMBED_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _discover_page_embeds(sgy: SchoologySession, page_id: str, sid: str, child_uid: str = "") -> list:
    """Warm up session via course preview, then fetch /page/{id} for embed URLs.

    Schoology returns 403 on /page/{id} unless the session has first visited
    /course/{sid}/preview/{uid}/parent, which sets a server-side auth context.
    """
    if not child_uid:
        return []

    _log(f"    Discovering embeds via preview warmup...", sgy.verbose)
    sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)

    r = sgy._request("GET", f"{sgy.base_url}/page/{page_id}", timeout=15)
    if r.status_code != 200:
        return []

    return _extract_google_embed_urls(r.text)


def _extract_google_embed_urls(html: str) -> list:
    if not html:
        return []
    urls = []
    for pattern in [
        r'docs\.google\.com/presentation/d/[a-zA-Z0-9_-]+[^"\'<>\s]*',
        r'docs\.google\.com/document/d/[a-zA-Z0-9_-]+[^"\'<>\s]*',
    ]:
        for match in re.finditer(pattern, html):
            url = "https://" + match.group(0)
            if url not in urls:
                urls.append(url)
    return urls


def _extract_google_id_and_type(url: str) -> tuple:
    """Returns (doc_id, type) where type is 'slides' or 'docs'."""
    m = re.search(r"docs\.google\.com/(presentation|document)/d/([a-zA-Z0-9_-]+)", url)
    if m:
        kind = "slides" if m.group(1) == "presentation" else "docs"
        return m.group(2), kind
    return None, None


def _fetch_google_content_text(url: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """Fetch text from a Google Slides or Docs URL via export."""
    doc_id, kind = _extract_google_id_and_type(url)
    if not doc_id:
        return None

    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = DEFAULT_UA

    if kind == "slides":
        export_url = f"https://docs.google.com/presentation/d/{doc_id}/export/txt"
    else:
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"

    try:
        time.sleep(random.uniform(0.5, 1.0))
        r = session.get(export_url, timeout=15, allow_redirects=True)
        if r.status_code == 200 and len(r.text.strip()) > 0:
            ct = r.headers.get("Content-Type", "")
            if "text/plain" in ct or not ct.startswith("text/html"):
                return r.text.strip()
    except requests.RequestException:
        pass

    for suffix in ["/pub", "/preview"]:
        if kind == "slides":
            fallback_url = f"https://docs.google.com/presentation/d/{doc_id}{suffix}"
        else:
            fallback_url = f"https://docs.google.com/document/d/{doc_id}{suffix}"
        try:
            time.sleep(random.uniform(0.5, 1.0))
            r = session.get(fallback_url, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                body = soup.find("body")
                if body:
                    text = body.get_text(separator="\n", strip=True)
                    if len(text) > 50:
                        return text
        except requests.RequestException:
            pass

    return None


def _get_page_ids_from_folder_api(sgy: SchoologySession, sid: str) -> list:
    """Use /v1/courses/{sid}/folder/0 to list materials (works with session cookies)."""
    data = sgy.get_folder(sid)
    if not data or not isinstance(data, dict):
        return []
    items = data.get("folder-item", [])
    if isinstance(items, dict):
        items = [items]
    results = []
    for item in items:
        item_type = item.get("type", "")
        if item_type in ("page", "document"):
            results.append({
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
                "material_type": item_type,
                "body": item.get("body", ""),
            })
    return results


def _get_page_ids_from_html(sgy: SchoologySession, sid: str, child_uid: Optional[str] = None) -> list:
    """Fallback: parse materials HTML for page links.

    For parent accounts, the materials page may require a preview warmup
    (visiting /course/{sid}/preview/{uid}/parent first) to set the server-side
    auth context. Without this, the page may return empty or 403.
    """
    # Preview warmup — needed for parent accounts to access course materials
    if child_uid:
        try:
            sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)
        except Exception as exc:
            _log(f"  [warn] preview warmup failed: {exc}", sgy.verbose)

    params = {}
    if child_uid:
        params["child_uid"] = child_uid
    try:
        soup = sgy.fetch_page(f"/course/{sid}/materials", params=params or None)
    except Exception as exc:
        _log(f"  [warn] materials HTML fetch failed: {exc}", sgy.verbose)
        return []
    results = []
    seen = set()
    for link in soup.select("a[href*='/page/']"):
        href = str(link.get("href", ""))
        m = re.search(r"/page/(\d+)", href)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            results.append({"id": m.group(1), "title": link.get_text(strip=True), "material_type": "page"})
    for link in soup.select("a[href*='/materials/link/view/']"):
        href = str(link.get("href", ""))
        m = re.search(r"/materials/link/view/(\d+)", href)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            results.append({"id": m.group(1), "title": link.get_text(strip=True), "material_type": "document"})
    return results


def _fetch_page_content(sgy: SchoologySession, item_id: str, material_type: str = "page",
                        sid: str = "", folder_body: str = "",
                        child_uid: str = "") -> dict:
    """Fetch a Schoology page or document link and extract embedded Google content."""
    # Preview warmup — parent accounts need this before accessing course content
    if child_uid and sid:
        try:
            sgy._request("GET", f"{sgy.base_url}/course/{sid}/preview/{child_uid}/parent", timeout=15)
        except Exception as exc:
            _log(f"  [warn] content preview warmup failed: {exc}", sgy.verbose)

    if material_type == "document" and sid:
        url = f"{sgy.base_url}/course/{sid}/materials/link/view/{item_id}"
    elif sid:
        url = f"{sgy.base_url}/course/{sid}/page/{item_id}"
    else:
        url = f"{sgy.base_url}/page/{item_id}"

    r = sgy._request("GET", url, timeout=15)

    html = r.text if r.status_code == 200 else ""

    soup = BeautifulSoup(html, "html.parser") if html else None

    body_text = ""
    body_el = None
    if soup:
        body_el = soup.select_one("#center-top .content, .s-page-body, .page-body")
        if body_el:
            body_text = body_el.get_text(separator="\n", strip=True)

    embed_urls = _extract_google_embed_urls(html)

    if not embed_urls and folder_body:
        embed_urls = _extract_google_embed_urls(folder_body)

    if not embed_urls and material_type == "page" and sid:
        cache = _load_embed_cache()
        cache_key = f"page:{item_id}"
        cached = cache.get(cache_key)
        if cached:
            _log(f"    Using cached embed URLs for page {item_id}", sgy.verbose)
            embed_urls = cached.get("urls", cached) if isinstance(cached, dict) else cached
        else:
            embed_urls = _discover_page_embeds(sgy, item_id, sid, child_uid)
            if embed_urls:
                cache[cache_key] = {"urls": embed_urls, "ts": time.time()}
                _save_embed_cache(cache)

    return {
        "body_html": str(body_el) if body_el else "",
        "body_text": body_text,
        "embeds": embed_urls,
    }


def scrape_pages(
    sgy: SchoologySession,
    child: Optional[dict],
    course_id: Optional[str] = None,
    course_filter: str = "all",
    fetch_google_docs: bool = True,
) -> list:
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    courses = get_courses_and_grades(sgy, child)
    if not courses:
        _log("No courses found.", sgy.verbose)
        return []

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

    child_uid = child.get("uid") if child else None
    all_pages = []

    for course in courses:
        sid = course.get("section_id", "")
        if not sid:
            continue

        course_name = course.get("name", "")
        _log(f"Fetching pages for: {course_name} (section {sid})...", sgy.verbose)

        page_refs = _get_page_ids_from_folder_api(sgy, sid)
        if not page_refs:
            _log("  Folder API returned no items, trying HTML scrape...", sgy.verbose)
            page_refs = _get_page_ids_from_html(sgy, sid, child_uid)

        if not page_refs:
            continue

        for ref in page_refs:
            item_id = ref["id"]
            title = ref["title"]
            material_type = ref.get("material_type", "page")
            _log(f"  {material_type.title()}: {title} (id {item_id})", sgy.verbose)

            page_data = _fetch_page_content(
                sgy, item_id, material_type=material_type, sid=sid,
                folder_body=ref.get("body", ""),
                child_uid=child_uid or "",
            )

            google_embeds = []
            for embed_url in page_data["embeds"]:
                doc_id, kind = _extract_google_id_and_type(embed_url)
                entry = {
                    "url": embed_url,
                    "doc_id": doc_id or "",
                    "type": kind or "unknown",
                    "text": None,
                }
                if fetch_google_docs and doc_id:
                    _log(f"    Fetching {kind}: {doc_id[:25]}...", sgy.verbose)
                    entry["text"] = _fetch_google_content_text(embed_url, session=sgy._google_session)
                    if entry["text"] is None and material_type == "page":
                        _log("    Export failed — clearing stale cache entry", sgy.verbose)
                        cache = _load_embed_cache()
                        cache.pop(f"page:{item_id}", None)
                        _save_embed_cache(cache)
                google_embeds.append(entry)

            all_pages.append({
                "title": title,
                "body_text": page_data["body_text"],
                "course": course_name,
                "section_id": sid,
                "page_id": item_id,
                "google_embeds": google_embeds,
            })

    return all_pages


# ---------------------------------------------------------------------------
# Announcements / activity feed scraper
# ---------------------------------------------------------------------------

def scrape_announcements(sgy: SchoologySession, child: Optional[dict], days: int = 7) -> list:
    """Scrape recent announcements / activity feed updates."""
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    announcements = []

    # Strategy 1: Parent home activity feed (AJAX)
    data = sgy.fetch_json("/parent/home/activity", params={"format": "json"})
    if data and isinstance(data, dict) and "content" in data:
        content = data["content"]
        html = ""
        if isinstance(content, str):
            html = content
        elif isinstance(content, dict) and "main" in content:
            html = content["main"]
        if html:
            soup = BeautifulSoup(html, "html.parser")
            announcements.extend(_parse_feed(soup))

    # Strategy 2: Recent activity page
    if not announcements:
        soup = sgy.fetch_page("/home/recent-activity")
        announcements.extend(_parse_feed(soup))

    # Strategy 3: Home page feed
    if not announcements:
        soup = sgy.fetch_page("/home")
        announcements.extend(_parse_feed(soup))

    # Strategy 4: Course-level updates
    if not announcements:
        courses = get_courses_and_grades(sgy, child)
        for course in courses[:8]:
            sid = course.get("section_id", "")
            if not sid:
                continue
            try:
                usoup = sgy.fetch_page(f"/course/{sid}/updates")
                for ann in _parse_feed(usoup):
                    ann["course"] = ann.get("course") or course["name"]
                    announcements.append(ann)
            except Exception as exc:
                _log(f"  [warn] course announcements({course['name']}) failed: {exc}", sgy.verbose)
                sgy.warnings.append(f"announcements({course['name']}): {exc}")
                continue

    # Filter by recency
    if days:
        cutoff = datetime.now() - timedelta(days=days)
        filtered = []
        for a in announcements:
            dt = _parse_date(a.get("date", ""))
            if dt and dt < cutoff:
                continue
            filtered.append(a)
        announcements = filtered

    return announcements


def _parse_feed(soup: BeautifulSoup) -> list:
    """Parse announcement/update items from a feed page."""
    results = []

    for item in soup.select(".s-edge-feed .edge-item, .feed-item"):
        ann = _parse_single_feed_item(item)
        if ann:
            results.append(ann)

    if not results:
        for item in soup.select(".update-body, .post-body, .announcement"):
            ann = _parse_single_feed_item(item)
            if ann:
                results.append(ann)

    return results


def _parse_single_feed_item(item: Tag) -> Optional[dict]:
    """Parse a single announcement/update from the feed."""
    title_el = item.select_one(
        ".update-title a, .post-title, h3 a, h4 a, h3, h4"
    )
    title = title_el.get_text(strip=True) if title_el else ""

    body_el = item.select_one(
        ".update-body-inner, .post-body-inner, .post-body, .body, p"
    )
    body = ""
    if body_el:
        body = body_el.get_text(strip=True)[:500]

    if not title and not body:
        text = item.get_text(strip=True)
        if text and len(text) > 10:
            title = text[:100]
            body = text[:500]
        else:
            return None

    author_el = item.select_one(
        ".iden-name a, .update-sentence-author a, .author a, .posted-by"
    )
    author = author_el.get_text(strip=True) if author_el else ""

    date_el = item.select_one("time, .update-sentence-date, .date, .timestamp")
    date_str = ""
    if date_el:
        date_str = date_el.get("datetime", "") or date_el.get_text(strip=True)

    course_el = item.select_one(
        ".update-sentence-realm a, .realm-name, .course-name"
    )
    course = course_el.get_text(strip=True) if course_el else ""

    return {
        "title": title,
        "body": body,
        "author": author,
        "date": date_str,
        "course": course,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def output_children(children: list, as_json: bool):
    if as_json:
        print(json.dumps(children, indent=2))
        return
    if not children:
        print("No children found.")
        return
    print(f"\n{'Name':<25} {'UID':<15} {'School'}")
    print("-" * 55)
    for c in children:
        print(f"{c['name']:<25} {c['uid']:<15} {c.get('building', '')}")
    print()


def output_assignments(assignments: list, as_json: bool):
    if as_json:
        print(json.dumps(assignments, indent=2))
        return
    if not assignments:
        print("No upcoming assignments found.")
        return
    print(f"\n{'Due Date':<22} {'Status':<12} {'Course':<25} {'Title'}")
    print("-" * 85)
    for a in assignments:
        due = a.get("due_date", "")[:21]
        status = a.get("status", "")[:11]
        course = a.get("course", "")[:24]
        title = a.get("title", "")
        print(f"{due:<22} {status:<12} {course:<25} {title}")
    print()


def output_grades(grades: list, as_json: bool):
    if as_json:
        print(json.dumps(grades, indent=2))
        return
    if not grades:
        print("No grades found.")
        return
    print(f"\n{'Course':<35} {'Grade':<15} {'Letter'}")
    print("-" * 55)
    for g in grades:
        course = g.get("course", "")[:34]
        grade = g.get("grade", "") or "—"
        letter = g.get("letter", "")
        grade_display = grade[:14]
        print(f"{course:<35} {grade_display:<15} {letter}")
        for item in g.get("items", [])[:5]:
            if item.get("is_category"):
                continue
            iname = item.get("name", "")[:30]
            igrade = item.get("grade", "")
            print(f"  - {iname:<30} {igrade}")
    print()


def output_announcements(announcements: list, as_json: bool):
    if as_json:
        print(json.dumps(announcements, indent=2))
        return
    if not announcements:
        print("No recent announcements found.")
        return
    for a in announcements:
        date = a.get("date", "unknown date")
        author = a.get("author", "")
        course = a.get("course", "")
        title = a.get("title", "")
        body = a.get("body", "")

        header = f"[{date}]"
        if course:
            header += f" {course}"
        if author:
            header += f" — {author}"

        print(f"\n{header}")
        if title:
            print(f"  {title}")
        if body and body != title:
            print(f"  {body[:200]}")
    print()


def output_summary(
    child: Optional[dict],
    children: list,
    assignments: list,
    grades: list,
    announcements: list,
    as_json: bool,
    homework_pages: Optional[list] = None,
    warnings: Optional[list] = None,
):
    if as_json:
        data = {
            "child": child,
            "timestamp": datetime.now().isoformat(),
            "children": children,
            "assignments": assignments,
            "grades": grades,
            "announcements": announcements,
        }
        if homework_pages:
            data["homework_pages"] = homework_pages
        if warnings:
            data["warnings"] = warnings
        print(json.dumps(data, indent=2))
        return

    name = child["name"] if child else "All"
    print(f"\n{'=' * 60}")
    print(f"  Schoology Summary for: {name}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 60}")

    print("\n--- Children ---")
    output_children(children, False)

    print("\n--- Assignments ---")
    output_assignments(assignments, False)

    # Homework pages (Google Slides/Docs embedded in course materials)
    if homework_pages:
        print("\n--- Homework Pages (Embedded Slides/Docs) ---")
        _output_homework_pages(homework_pages)

    print("\n--- Grades ---")
    output_grades(grades, False)

    print("\n--- Announcements ---")
    output_announcements(announcements, False)


def _output_homework_pages(pages: list):
    """Display homework pages with their embedded Google content."""
    if not pages:
        print("No homework pages found.")
        return
    for p in pages:
        course = p.get("course", "")
        title = p.get("title", "")
        print(f"\n  [{course}] {title}")

        # Show embedded Google content (the actual homework)
        for embed in p.get("google_embeds", []):
            text = embed.get("text", "")
            if text:
                for line in text.split("\n"):
                    line = line.strip()
                    if line:
                        print(f"    {line}")
            else:
                url = embed.get("url", "")
                print(f"    (embed: {url})")

        # If no embeds, show body text
        if not p.get("google_embeds") and p.get("body_text"):
            for line in p["body_text"].split("\n"):
                line = line.strip()
                if line:
                    print(f"    {line}")
    print()


def output_pages(pages: list, as_json: bool):
    if as_json:
        output = []
        for p in pages:
            entry = {
                "title": p.get("title", ""),
                "course": p.get("course", ""),
                "section_id": p.get("section_id", ""),
                "page_id": p.get("page_id", ""),
                "body_text": p.get("body_text", ""),
                "google_embeds": p.get("google_embeds", []),
            }
            output.append(entry)
        print(json.dumps(output, indent=2))
        return
    if not pages:
        print("No pages found.")
        return
    for p in pages:
        course = p.get("course", "")
        title = p.get("title", "Untitled")
        body_text = p.get("body_text", "")
        google_embeds = p.get("google_embeds", [])

        print(f"\n{'=' * 60}")
        if course:
            print(f"  [{course}]")
        print(f"  {title}")
        print(f"{'=' * 60}")

        if body_text:
            print(f"\n{body_text[:1000]}")

        for embed in google_embeds:
            doc_id = embed.get("doc_id", "unknown")
            kind = embed.get("type", "unknown")
            doc_text = embed.get("text")
            print(f"\n--- Embedded Google {kind.title()} ({doc_id[:30]}) ---")
            if doc_text:
                print(doc_text[:2000])
            else:
                print(f"  (Could not fetch content. URL: {embed.get('url', '')})")
    print()


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Interactive setup of credentials."""
    _ensure_dir()
    print("Schoology CLI Setup")
    print("=" * 40)
    base_url = input(f"Base URL [{DEFAULT_BASE_URL}]: ").strip() or DEFAULT_BASE_URL
    school_nid = input("School NID (optional, press Enter to skip): ").strip()
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ").strip()

    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)

    with open(ENV_PATH, "w") as f:
        f.write(f'SGY_BASE_URL="{base_url}"\n')
        if school_nid:
            f.write(f'SGY_SCHOOL_NID="{school_nid}"\n')
        f.write(f'SGY_EMAIL="{email}"\n')
        escaped_password = password.replace("\\", "\\\\").replace('"', '\\"')
        f.write(f'SGY_PASSWORD="{escaped_password}"\n')
    os.chmod(ENV_PATH, 0o600)

    # Remove legacy config.json if it exists
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

    print(f"\nCredentials saved to {ENV_PATH}")
    print("Testing login...")
    try:
        test_sgy = SchoologySession(verbose=True)
        test_sgy.ensure_logged_in()
        children = test_sgy.get_children()
        print(f"Login successful! Found {len(children)} child(ren).")
    except Exception as exc:
        print(f"Warning: Login test failed: {exc}", file=sys.stderr)
        print("Credentials saved but may be incorrect. Run `sgy children` to retry.", file=sys.stderr)


def cmd_children(args):
    sgy = SchoologySession(verbose=not args.json)
    children = sgy.get_children()
    output_children(children, args.json)


def cmd_assignments(args):
    sgy = SchoologySession(verbose=not args.json)
    child = sgy.resolve_child(args.child) if args.child else None
    if args.child and not child:
        print(f"Child '{args.child}' not found.", file=sys.stderr)
        sys.exit(1)
    assignments = scrape_assignments(sgy, child, days=args.days)
    output_assignments(assignments, args.json)


def cmd_grades(args):
    sgy = SchoologySession(verbose=not args.json)
    child = sgy.resolve_child(args.child) if args.child else None
    if args.child and not child:
        print(f"Child '{args.child}' not found.", file=sys.stderr)
        sys.exit(1)
    grades = scrape_grades(sgy, child, detail=args.detail)
    output_grades(grades, args.json)


def cmd_announcements(args):
    sgy = SchoologySession(verbose=not args.json)
    child = sgy.resolve_child(args.child) if args.child else None
    if args.child and not child:
        print(f"Child '{args.child}' not found.", file=sys.stderr)
        sys.exit(1)
    announcements = scrape_announcements(sgy, child, days=7)
    output_announcements(announcements, args.json)


def _filter_homework_pages(pages: list) -> list:
    """Filter scraped pages to those containing homework content.

    Includes any page that has Google embeds with fetchable text content,
    or whose title suggests homework. This is the primary way homework
    shows up for teachers who use embedded Google Slides instead of
    Schoology assignment objects.
    """
    homework_keywords = {"homework", "hw", "assignment", "weekly", "daily", "practice",
                         "worksheet", "study guide", "review", "packet", "slide"}

    results = []
    for p in pages:
        title_lower = p.get("title", "").lower()
        has_google = bool(p.get("google_embeds"))
        has_text = any(e.get("text") for e in p.get("google_embeds", []))
        is_homework = any(kw in title_lower for kw in homework_keywords)

        # Include if:
        #  - It has Google content we could actually fetch (text available), OR
        #  - It has a Google embed and the title looks homework-related, OR
        #  - The title looks homework-related even without embeds (body text may help)
        if has_text or (has_google and is_homework) or (is_homework and p.get("body_text")):
            results.append({
                "title": p.get("title", ""),
                "course": p.get("course", ""),
                "page_id": p.get("page_id", ""),
                "body_text": p.get("body_text", ""),
                "google_embeds": [
                    {
                        "url": e.get("url", ""),
                        "type": e.get("type", ""),
                        "text": e.get("text", ""),
                    }
                    for e in p.get("google_embeds", [])
                ],
            })

    return results


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


def cmd_summary(args):
    sgy = SchoologySession(verbose=not args.json)
    children = sgy.get_children()

    if args.child:
        child = sgy.resolve_child(args.child)
        if not child:
            print(f"Child '{args.child}' not found.", file=sys.stderr)
            sys.exit(1)
        assignments = scrape_assignments(sgy, child, days=14)
        grades = scrape_grades(sgy, child, detail=True)
        announcements = scrape_announcements(sgy, child, days=7)
        pages = scrape_pages(sgy, child, fetch_google_docs=True)
        homework_pages = _filter_homework_pages(pages)
        output_summary(child, children, assignments, grades, announcements, args.json,
                        homework_pages=homework_pages, warnings=sgy.warnings)
    else:
        if args.json:
            all_data = {
                "timestamp": datetime.now().isoformat(),
                "children": children,
                "per_child": [],
            }
            for child in children:
                sgy.switch_to_child(child)
                pages = scrape_pages(sgy, child, fetch_google_docs=True)
                all_data["per_child"].append({
                    "child": child,
                    "assignments": scrape_assignments(sgy, child, days=14),
                    "grades": scrape_grades(sgy, child, detail=False),
                    "announcements": scrape_announcements(sgy, child, days=7),
                    "homework_pages": _filter_homework_pages(pages),
                })
            if sgy.warnings:
                all_data["warnings"] = sgy.warnings
            print(json.dumps(all_data, indent=2))
        else:
            for child in children:
                assignments = scrape_assignments(sgy, child, days=14)
                grades = scrape_grades(sgy, child, detail=False)
                announcements = scrape_announcements(sgy, child, days=7)
                pages = scrape_pages(sgy, child, fetch_google_docs=True)
                homework_pages = _filter_homework_pages(pages)
                output_summary(child, children, assignments, grades, announcements, False,
                                homework_pages=homework_pages)


def cmd_pages(args):
    sgy = SchoologySession(verbose=not args.json)
    child = sgy.resolve_child(args.child) if args.child else None
    if args.child and not child:
        print(f"Child '{args.child}' not found.", file=sys.stderr)
        sys.exit(1)
    pages = scrape_pages(
        sgy,
        child,
        course_id=args.course,
        fetch_google_docs=not args.no_docs,
    )
    output_pages(pages, args.json)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="sgy",
        description="Schoology parent portal CLI scraper",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    sp_init = subparsers.add_parser("init", help="Set up credentials")
    sp_init.set_defaults(func=cmd_init)

    sp_children = subparsers.add_parser("children", help="List all children")
    sp_children.add_argument("--json", action="store_true", help="JSON output")
    sp_children.set_defaults(func=cmd_children)

    sp_assign = subparsers.add_parser("assignments", help="Upcoming/overdue assignments")
    sp_assign.add_argument("--child", type=str, help="Child name filter")
    sp_assign.add_argument("--days", type=int, default=14, help="Days ahead (default 14)")
    sp_assign.add_argument("--json", action="store_true", help="JSON output")
    sp_assign.set_defaults(func=cmd_assignments)

    sp_grades = subparsers.add_parser("grades", help="Grades per course")
    sp_grades.add_argument("--child", type=str, help="Child name filter")
    sp_grades.add_argument("--detail", action="store_true", help="Fetch per-assignment grades (slow)")
    sp_grades.add_argument("--json", action="store_true", help="JSON output")
    sp_grades.set_defaults(func=cmd_grades)

    sp_ann = subparsers.add_parser("announcements", help="Recent announcements")
    sp_ann.add_argument("--child", type=str, help="Child name filter")
    sp_ann.add_argument("--json", action="store_true", help="JSON output")
    sp_ann.set_defaults(func=cmd_announcements)

    sp_sum = subparsers.add_parser("summary", help="Full summary (all data)")
    sp_sum.add_argument("--child", type=str, help="Child name filter")
    sp_sum.add_argument("--json", action="store_true", help="JSON output")
    sp_sum.set_defaults(func=cmd_summary)

    sp_pages = subparsers.add_parser("pages", help="Course pages (with embedded Google Docs)")
    sp_pages.add_argument("--child", type=str, help="Child name filter")
    sp_pages.add_argument("--course", type=str, help="Course/section ID or name substring")
    sp_pages.add_argument("--no-docs", action="store_true", help="Skip fetching Google Doc content")
    sp_pages.add_argument("--json", action="store_true", help="JSON output")
    sp_pages.set_defaults(func=cmd_pages)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        args.func(args)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
