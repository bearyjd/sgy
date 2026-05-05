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


def test_stage_tracker_default_state():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    assert all(v == "pending" for v in t.stages.values())
    assert t.errors == []


def test_stage_tracker_auth_fail_gives_failed():
    from sgy_cli.cli import StageTracker
    t = StageTracker()
    t.fail("auth", "Login failed")
    assert t.confidence == "failed"
    assert "auth: Login failed" in t.errors


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


# ---------------------------------------------------------------------------
# _parse_date weekday formats
# ---------------------------------------------------------------------------

def test_parse_date_full_weekday():
    from sgy_cli.cli import _parse_date
    dt = _parse_date("Friday, May 8, 2026")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 8)


def test_parse_date_abbreviated_weekday():
    from sgy_cli.cli import _parse_date
    dt = _parse_date("Mon, May 11, 2026")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 5, 11)


def test_parse_date_full_weekday_with_time():
    from sgy_cli.cli import _parse_date
    dt = _parse_date("Friday, May 8, 2026 at 11:59 PM")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 5, 8, 23, 59)


def test_parse_date_returns_none_on_garbage():
    from sgy_cli.cli import _parse_date
    assert _parse_date("not a date") is None


# ---------------------------------------------------------------------------
# _parse_upcoming_events normalizes "Due {weekday}, ..." to ISO
# ---------------------------------------------------------------------------

def test_upcoming_events_normalizes_due_prefix_to_iso():
    """Home-widget items with 'Due Friday, May 8, 2026' must store '2026-05-08'."""
    from bs4 import BeautifulSoup
    from sgy_cli.cli import _parse_upcoming_events

    html = """
    <div class="upcoming-event">
      <div class="event-title"><a href="/event/100">Reading Project</a></div>
      <div class="readonly-title event-subtitle">Due Friday, May 8, 2026</div>
      <div class="readonly-title event-subtitle">Reading</div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    items = _parse_upcoming_events(soup)
    assert len(items) == 1
    assert items[0]["due_date"] == "2026-05-08"
    assert items[0]["course"] == "Reading"


def test_upcoming_events_keeps_raw_text_when_unparseable():
    """If the due text doesn't parse, leave it as-is (don't blank it)."""
    from bs4 import BeautifulSoup
    from sgy_cli.cli import _parse_upcoming_events

    html = """
    <div class="upcoming-event">
      <div class="event-title"><a href="/event/200">X</a></div>
      <div class="readonly-title event-subtitle">Due whenever</div>
      <div class="readonly-title event-subtitle">Math</div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    items = _parse_upcoming_events(soup)
    assert items[0]["due_date"] == "Due whenever"


# ---------------------------------------------------------------------------
# SGY_MAX_COURSES env override
# ---------------------------------------------------------------------------

def test_max_courses_env_override(monkeypatch):
    """Setting SGY_MAX_COURSES changes the cap used by scrape_assignments."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import scrape_assignments

    monkeypatch.setenv("SGY_MAX_COURSES", "3")
    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False
    sgy.warnings = []
    sgy.ensure_logged_in = MagicMock()
    sgy.switch_to_child = MagicMock()
    sgy.fetch_json = MagicMock(return_value=None)
    sgy.fetch_page = MagicMock(side_effect=Exception("skip"))
    sgy._request = MagicMock(side_effect=Exception("skip"))

    five_courses = [{"section_id": str(i), "name": f"C{i}"} for i in range(5)]
    with patch("sgy_cli.cli.get_courses_and_grades", return_value=five_courses), \
         patch("sgy_cli.cli._scrape_calendar_assignments", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_folder_api", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_grades", return_value=[]):
        scrape_assignments(sgy, child={"uid": "1"}, days=14)

    truncations = [w for w in sgy.warnings if "courses_truncated" in w]
    assert truncations
    assert "showing 3" in truncations[0]


def test_max_courses_invalid_env_falls_back(monkeypatch):
    """A non-integer SGY_MAX_COURSES must not crash; cap defaults to 25."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import scrape_assignments

    monkeypatch.setenv("SGY_MAX_COURSES", "not-a-number")
    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False
    sgy.warnings = []
    sgy.ensure_logged_in = MagicMock()
    sgy.switch_to_child = MagicMock()
    sgy.fetch_json = MagicMock(return_value=None)
    sgy.fetch_page = MagicMock(side_effect=Exception("skip"))
    sgy._request = MagicMock(side_effect=Exception("skip"))

    # 30 courses → with cap=25 default, expect truncation showing 25
    courses = [{"section_id": str(i), "name": f"C{i}"} for i in range(30)]
    with patch("sgy_cli.cli.get_courses_and_grades", return_value=courses), \
         patch("sgy_cli.cli._scrape_calendar_assignments", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_folder_api", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_grades", return_value=[]):
        scrape_assignments(sgy, child={"uid": "1"}, days=14)

    truncations = [w for w in sgy.warnings if "courses_truncated" in w]
    assert truncations
    assert "showing 25" in truncations[0]


def test_max_courses_no_truncation_when_under_cap(monkeypatch):
    """16 courses under default cap of 25: no truncation warning."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import scrape_assignments

    monkeypatch.delenv("SGY_MAX_COURSES", raising=False)
    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False
    sgy.warnings = []
    sgy.ensure_logged_in = MagicMock()
    sgy.switch_to_child = MagicMock()
    sgy.fetch_json = MagicMock(return_value=None)
    sgy.fetch_page = MagicMock(side_effect=Exception("skip"))
    sgy._request = MagicMock(side_effect=Exception("skip"))

    courses = [{"section_id": str(i), "name": f"C{i}"} for i in range(16)]
    with patch("sgy_cli.cli.get_courses_and_grades", return_value=courses), \
         patch("sgy_cli.cli._scrape_calendar_assignments", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_folder_api", return_value=[]), \
         patch("sgy_cli.cli._get_assignments_from_grades", return_value=[]):
        scrape_assignments(sgy, child={"uid": "1"}, days=14)

    assert not any("courses_truncated" in w for w in sgy.warnings)


# ---------------------------------------------------------------------------
# cmd_assignments default child
# ---------------------------------------------------------------------------

def test_cmd_assignments_defaults_to_first_child():
    """Without --child, cmd_assignments must select children[0] for warmup."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import cmd_assignments

    args = MagicMock()
    args.child = None
    args.days = 7
    args.json = False

    first_child = {"uid": "111", "name": "Ford"}
    mock_session = MagicMock()
    mock_session.get_children = MagicMock(return_value=[first_child, {"uid": "222", "name": "John"}])

    with patch("sgy_cli.cli.SchoologySession", return_value=mock_session), \
         patch("sgy_cli.cli.scrape_assignments", return_value=[]) as scrape, \
         patch("sgy_cli.cli.output_assignments"):
        cmd_assignments(args)

    scrape.assert_called_once()
    called_child = scrape.call_args.args[1]
    assert called_child == first_child


def test_cmd_assignments_with_explicit_child_uses_resolve():
    """With --child, cmd_assignments must call resolve_child, not get_children."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import cmd_assignments

    args = MagicMock()
    args.child = "Ford"
    args.days = 7
    args.json = False

    ford = {"uid": "111", "name": "Ford"}
    mock_session = MagicMock()
    mock_session.resolve_child = MagicMock(return_value=ford)
    mock_session.get_children = MagicMock()  # should NOT be called

    with patch("sgy_cli.cli.SchoologySession", return_value=mock_session), \
         patch("sgy_cli.cli.scrape_assignments", return_value=[]) as scrape, \
         patch("sgy_cli.cli.output_assignments"):
        cmd_assignments(args)

    mock_session.resolve_child.assert_called_once_with("Ford")
    mock_session.get_children.assert_not_called()
    scrape.assert_called_once()
    assert scrape.call_args.args[1] == ford


def test_cmd_assignments_no_children_passes_none():
    """If get_children returns empty, child stays None and scrape proceeds."""
    from unittest.mock import MagicMock, patch
    from sgy_cli.cli import cmd_assignments

    args = MagicMock()
    args.child = None
    args.days = 7
    args.json = False

    mock_session = MagicMock()
    mock_session.get_children = MagicMock(return_value=[])

    with patch("sgy_cli.cli.SchoologySession", return_value=mock_session), \
         patch("sgy_cli.cli.scrape_assignments", return_value=[]) as scrape, \
         patch("sgy_cli.cli.output_assignments"):
        cmd_assignments(args)

    assert scrape.call_args.args[1] is None


# ---------------------------------------------------------------------------
# _pages_to_homework_slides new fields
# ---------------------------------------------------------------------------

def test_pages_to_homework_slides_includes_page_url_and_embed_urls():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [{
        "course": "Science: Section 5Gold",
        "title": "Skeletal System Slides",
        "page_id": "7891690888",
        "body_text": "",
        "google_embeds": [
            {"url": "https://docs.google.com/presentation/d/abc/edit", "type": "slides", "text": ""},
            {"url": "https://docs.google.com/presentation/d/def/edit", "type": "slides", "text": ""},
        ],
    }]
    result = _pages_to_homework_slides(pages)
    assert result[0]["page_url"] == "/page/7891690888"
    assert result[0]["embed_urls"] == [
        "https://docs.google.com/presentation/d/abc/edit",
        "https://docs.google.com/presentation/d/def/edit",
    ]
    assert result[0]["fetched"] is False
    assert result[0]["error"] == "no_content_found"


def test_pages_to_homework_slides_missing_page_id_safe():
    from sgy_cli.cli import _pages_to_homework_slides
    pages = [{"course": "X", "title": "Y", "body_text": "z", "google_embeds": []}]
    result = _pages_to_homework_slides(pages)
    assert result[0]["page_url"] == ""
    assert result[0]["embed_urls"] == []


# ---------------------------------------------------------------------------
# _enrich_event_dates
# ---------------------------------------------------------------------------

def _make_response(ok, text="", json_data=None, status_code=200):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.ok = ok
    r.status_code = status_code
    r.text = text
    r.json = MagicMock(return_value=json_data or {})
    return r


def test_enrich_event_dates_uses_api_when_ok():
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False

    # 2026-05-08 11:59:00 UTC = 1746748740
    sgy._request = MagicMock(return_value=_make_response(
        ok=True, json_data={"start": 1746748740, "realm_title": "Reading 5Gold"}
    ))

    items = [{"title": "X", "link": "/event/123", "due_date": "", "course": ""}]
    n = _enrich_event_dates(sgy, items)

    assert n == 1
    assert items[0]["due_date"]  # set; exact value timezone-dependent so don't pin
    assert items[0]["course"] == "Reading 5Gold"
    assert sgy._request.call_count == 1


def test_enrich_event_dates_falls_back_to_profile_on_api_500():
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False

    profile_html = """
    <html><body>
      <table class="info-tab">
        <tr><th>Type</th><td>Assignment</td></tr>
        <tr><th>Time</th><td>Friday, May 8, 2026 at 11:59 PM</td></tr>
        <tr><th>Where</th><td>Schoology</td></tr>
      </table>
      <h2 class="course-title">Reading: Section 5Gold</h2>
    </body></html>
    """

    def router(method, url, **kwargs):
        if "/v1/events/" in url:
            return _make_response(ok=False, status_code=500)
        if url.endswith("/event/123"):
            return _make_response(ok=True, text=profile_html)
        return _make_response(ok=False, status_code=404)
    sgy._request = MagicMock(side_effect=router)

    items = [{"title": "Reading Project", "link": "/event/123", "due_date": "", "course": ""}]
    n = _enrich_event_dates(sgy, items)

    assert n == 1
    assert items[0]["due_date"] == "2026-05-08"
    assert items[0]["course"] == "Reading: Section 5Gold"
    # Once for API, once for profile
    assert sgy._request.call_count == 2


def test_enrich_event_dates_skips_when_no_event_id():
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False
    sgy._request = MagicMock()

    items = [{"title": "X", "link": "/assignment/999", "due_date": "", "course": "Math"}]
    n = _enrich_event_dates(sgy, items)

    assert n == 0
    assert items[0]["due_date"] == ""
    sgy._request.assert_not_called()


def test_enrich_event_dates_skips_when_already_has_date():
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False
    sgy._request = MagicMock()

    items = [{"title": "X", "link": "/event/123", "due_date": "2026-05-01", "course": ""}]
    n = _enrich_event_dates(sgy, items)

    assert n == 0
    sgy._request.assert_not_called()


def test_enrich_event_dates_handles_profile_without_time_row():
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False

    profile_html = """
    <html><body>
      <table class="info-tab">
        <tr><th>Type</th><td>Assignment</td></tr>
        <tr><th>Where</th><td>Schoology</td></tr>
      </table>
    </body></html>
    """

    def router(method, url, **kwargs):
        if "/v1/events/" in url:
            return _make_response(ok=False, status_code=500)
        return _make_response(ok=True, text=profile_html)
    sgy._request = MagicMock(side_effect=router)

    items = [{"title": "X", "link": "/event/123", "due_date": "", "course": ""}]
    n = _enrich_event_dates(sgy, items)

    assert n == 0
    assert items[0]["due_date"] == ""


def test_enrich_event_dates_continues_after_exception():
    """One item raising must not stop enrichment of subsequent items."""
    from unittest.mock import MagicMock
    from sgy_cli.cli import _enrich_event_dates

    sgy = MagicMock()
    sgy.base_url = "https://test.schoology.com"
    sgy.verbose = False

    profile_html = """
    <table class="info-tab">
      <tr><th>Time</th><td>Mon, May 11, 2026</td></tr>
    </table>
    """

    call_count = {"n": 0}

    def router(method, url, **kwargs):
        call_count["n"] += 1
        # First item's API call raises
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        # First item's profile call also raises
        if call_count["n"] == 2:
            raise RuntimeError("boom2")
        # Second item's API returns 500
        if "/v1/events/" in url:
            return _make_response(ok=False, status_code=500)
        # Second item's profile succeeds
        return _make_response(ok=True, text=profile_html)
    sgy._request = MagicMock(side_effect=router)

    items = [
        {"title": "First", "link": "/event/100", "due_date": "", "course": ""},
        {"title": "Second", "link": "/event/200", "due_date": "", "course": ""},
    ]
    n = _enrich_event_dates(sgy, items)

    assert n == 1
    assert items[0]["due_date"] == ""        # first item unchanged
    assert items[1]["due_date"] == "2026-05-11"  # second item enriched
