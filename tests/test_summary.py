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
