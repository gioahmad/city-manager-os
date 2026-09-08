from executive_workflow_app import _capture_title, _parse_cycle_lines, _safe_return


def test_capture_title_uses_first_nonblank_line():
    assert _capture_title("\n  Call Boswell about Verizon poles\nmore detail") == "Call Boswell about Verizon poles"


def test_capture_title_is_bounded():
    title = _capture_title("x" * 300)
    assert len(title) == 180
    assert title.endswith("...")


def test_safe_return_rejects_external_and_auth_paths():
    assert _safe_return("https://example.com") == "/my-day"
    assert _safe_return("//example.com") == "/my-day"
    assert _safe_return("/login") == "/my-day"
    assert _safe_return("/inbox") == "/inbox"


def test_cycle_lines_keep_optional_detail():
    rows = _parse_cycle_lines("GROUP 1 | 24h\nGROUP 2\n\nGROUP 3 | relief")
    assert rows == [
        {"value": "GROUP 1", "detail": "24h"},
        {"value": "GROUP 2"},
        {"value": "GROUP 3", "detail": "relief"},
    ]
