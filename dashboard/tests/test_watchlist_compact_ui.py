from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_watchlist_defaults_to_compact_saved_watch_cards():
    template = (ROOT / "templates" / "watchlist.html").read_text()
    style = (ROOT / "static" / "style.css").read_text()
    assert "watch-compact-evidence" in template
    assert "watch-card-more" in template
    assert "watch-manage-menu" in template
    assert ">Preview History<" in template
    assert "MORE TOOLS" in template
    assert ".watch-list{display:grid" in style
