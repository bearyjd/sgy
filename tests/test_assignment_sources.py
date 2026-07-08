"""Parser-fixture scaffolding (roadmap item 1, started).

Feeds hand-built SYNTHETIC fixtures (fake names/ids/dates — no real student
data, per CLAUDE.md's hard privacy requirement) through the REAL parser
functions for two of the six `scrape_assignments` sources:

  - folder_api      -> _get_assignments_from_folder_api  (JSON fixture)
  - materials_html  -> _parse_material_item               (HTML fixture)

Only the network-fetch boundary is mocked (`sgy.get_folder` /
`BeautifulSoup(fixture_html)`); the actual parsing/selector logic under test
runs unmodified. This is a starting point, not the full 6-source suite the
roadmap describes — ajax_upcoming, home_widget, calendar, and grades_xref
fixtures are still TODO.
"""

from pathlib import Path
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_folder_api_fixture_parses_expected_assignment_shape():
    """folder_api source: /v1/courses/{sid}/folder/0 JSON -> assignment dicts.

    Feeds the real parser (_get_assignments_from_folder_api), only stubbing
    sgy.get_folder (the network boundary) to return the fixture JSON.
    """
    import json

    from sgy_cli.cli import _get_assignments_from_folder_api

    fixture_data = json.loads((FIXTURES_DIR / "folder_api_response.json").read_text())

    sgy = MagicMock()
    sgy.get_folder = MagicMock(return_value=fixture_data)

    results = _get_assignments_from_folder_api(sgy, sid="9999999999")

    # Non-assignment types (page, folder) and the blank-title item are dropped;
    # assignment/discussion/quiz with a title survive.
    assert len(results) == 3
    titles = {r["title"] for r in results}
    assert titles == {"Fake Worksheet 1", "Fake Discussion Topic", "Fake Chapter Quiz"}

    for r in results:
        assert set(r.keys()) == {"title", "course", "due_date", "status", "link"}
        assert r["status"] == "unknown"
        assert r["course"] == ""  # filled in by the caller, not this function

    worksheet = next(r for r in results if r["title"] == "Fake Worksheet 1")
    assert worksheet["due_date"] == "2026-05-01 23:59:00"
    assert worksheet["link"] == "/course/9999999999/assignment/1001"

    sgy.get_folder.assert_called_once_with("9999999999")


def test_folder_api_fixture_no_assignments_returns_empty_list():
    from sgy_cli.cli import _get_assignments_from_folder_api

    sgy = MagicMock()
    sgy.get_folder = MagicMock(return_value={"folder-item": [{"type": "folder", "id": "1", "title": "X"}]})

    assert _get_assignments_from_folder_api(sgy, sid="1") == []


def test_materials_html_fixture_parses_expected_assignment_shape():
    """materials_html source: /course/{sid}/materials scrape -> assignment dicts.

    Feeds the fixture HTML through the real selector query
    (scrape_assignments' row selector) and the real _parse_material_item
    parser for each row — no mocking of the parser itself.
    """
    from sgy_cli.cli import _parse_material_item

    html = (FIXTURES_DIR / "materials_page.html").read_text()
    soup = BeautifulSoup(html, "html.parser")

    rows = soup.select(".type-assignment, .type-discussion, .type-assessment, .material-row")
    # The type-folder row must NOT match any of these selectors.
    assert len(rows) == 3

    results = [_parse_material_item(row) for row in rows]
    results = [r for r in results if r]
    assert len(results) == 3

    for r in results:
        assert set(r.keys()) == {"title", "course", "due_date", "status", "link"}
        assert r["status"] == "unknown"
        assert r["course"] == ""

    worksheet = next(r for r in results if r["title"] == "Fake Worksheet A")
    assert worksheet["due_date"] == "2026-05-01"
    assert worksheet["link"] == "/course/9999999999/assignment/1001"

    discussion = next(r for r in results if r["title"] == "Fake Discussion B")
    # due_el.get("datetime") is preferred over get_text() when both exist.
    assert discussion["due_date"] == "2026-05-03"

    quiz = next(r for r in results if r["title"] == "Fake Quiz C")
    assert quiz["due_date"] == ""  # no due element in this row


def test_materials_html_fixture_row_without_link_returns_none():
    from sgy_cli.cli import _parse_material_item

    soup = BeautifulSoup('<tr class="type-assignment"><td>No link here</td></tr>', "html.parser")
    row = soup.select_one(".type-assignment")

    assert _parse_material_item(row) is None
