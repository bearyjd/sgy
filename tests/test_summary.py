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
