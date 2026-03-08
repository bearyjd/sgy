"""
sgy — Schoology parent portal CLI scraper.

Logs in via session cookies, caches sessions, and scrapes
assignments, grades, and announcements for all children.

Usage:
    sgy init                          — set up credentials interactively
    sgy children                      — list children with IDs
    sgy assignments [--child NAME] [--days N] [--json]
    sgy grades      [--child NAME] [--json]
    sgy announcements [--child NAME] [--json]
    sgy summary     [--child NAME] [--json]
"""

import argparse
import json
import os
import random
import re
import sys
import time
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
            # Strip surrounding quotes
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
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


def save_session(cookies: dict):
    _ensure_dir()
    data = {"cookies": cookies, "ts": time.time()}
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
    """Try multiple date formats commonly used by Schoology."""
    if not date_str:
        return None
    date_str = date_str.strip()
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
    ):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
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
# Schoology session management
# ---------------------------------------------------------------------------

class SchoologySession:
    """Wraps requests.Session with Schoology login + child switching."""

    def __init__(self, verbose: bool = True):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": DEFAULT_UA})
        self.cfg = load_config()
        self.verbose = verbose
        self._logged_in = False
        self._children: Optional[list] = None
        self._current_child_uid: Optional[str] = None
        self._parent_home_soup: Optional[BeautifulSoup] = None
        self._last_request_time = 0.0

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
        return r

    # -- login / session cache --

    def ensure_logged_in(self):
        if self._logged_in:
            return

        cached = load_session()
        if cached:
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
        save_session(dict(self.s.cookies))
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
        except Exception:
            return None


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

def scrape_assignments(sgy: SchoologySession, child: Optional[dict], days: int = 14) -> list:
    """Scrape upcoming/overdue assignments for a child."""
    sgy.ensure_logged_in()
    if child:
        sgy.switch_to_child(child)

    assignments = []

    # Strategy 1: AJAX endpoint (from schoology-mcp — returns {"html": "..."})
    data = sgy.fetch_json("/home/upcoming_submissions_ajax")
    if data and isinstance(data, dict) and data.get("html"):
        soup = BeautifulSoup(data["html"], "html.parser")
        assignments.extend(_parse_upcoming_events(soup))

    # Strategy 2: The /home page right column (upcoming events widget)
    if not assignments:
        soup = sgy.fetch_page("/home")
        assignments.extend(_parse_upcoming_events(soup))

    # Strategy 3: Scrape individual course materials pages
    if not assignments:
        courses = get_courses_and_grades(sgy, child)
        for course in courses[:10]:
            sid = course.get("section_id", "")
            if not sid:
                continue
            try:
                csoup = sgy.fetch_page(f"/course/{sid}/materials")
                for item in csoup.select(".type-assignment, .material-row"):
                    a = _parse_material_item(item)
                    if a:
                        a["course"] = course.get("name", "")
                        assignments.append(a)
            except Exception:
                continue

    # Filter by date window
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
                filtered.append(a)
        assignments = filtered

    return assignments


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
                try:
                    detail_items = _scrape_course_grade_detail(sgy, sid)
                    grade_entry["items"] = detail_items
                except Exception:
                    pass

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

    return items[:30]


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
            except Exception:
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

    print("\n--- Grades ---")
    output_grades(grades, False)

    print("\n--- Announcements ---")
    output_announcements(announcements, False)


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
    password = input("Password: ").strip()

    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        sys.exit(1)

    with open(ENV_PATH, "w") as f:
        f.write(f'SGY_BASE_URL="{base_url}"\n')
        if school_nid:
            f.write(f'SGY_SCHOOL_NID="{school_nid}"\n')
        f.write(f'SGY_EMAIL="{email}"\n')
        f.write(f'SGY_PASSWORD="{password}"\n')
    os.chmod(ENV_PATH, 0o600)

    # Remove legacy config.json if it exists
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

    print(f"\nCredentials saved to {ENV_PATH}")
    print("Run `sgy children` to verify login works.")


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
        output_summary(child, children, assignments, grades, announcements, args.json)
    else:
        # Multi-child: skip per-assignment grade detail to stay fast
        if args.json:
            all_data = {
                "timestamp": datetime.now().isoformat(),
                "children": children,
                "per_child": [],
            }
            for child in children:
                sgy.switch_to_child(child)
                all_data["per_child"].append({
                    "child": child,
                    "assignments": scrape_assignments(sgy, child, days=14),
                    "grades": scrape_grades(sgy, child, detail=False),
                    "announcements": scrape_announcements(sgy, child, days=7),
                })
            print(json.dumps(all_data, indent=2))
        else:
            for child in children:
                assignments = scrape_assignments(sgy, child, days=14)
                grades = scrape_grades(sgy, child, detail=False)
                announcements = scrape_announcements(sgy, child, days=7)
                output_summary(child, children, assignments, grades, announcements, False)


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
