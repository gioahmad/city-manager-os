"""Recipient names are visible on saved Watch cards without opening Details."""
from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, select_autoescape

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "watchlist.html"


class RecipientLine(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.lines = []
        self.in_recipient_line = False
        self.tags_in_line = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "data-watch-recipients" in attributes:
            assert not any(parent == "details" for parent in self.stack)
            assert "hidden" not in attributes
            self.lines.append("")
            self.in_recipient_line = True
        if self.in_recipient_line:
            self.tags_in_line.append(tag)
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "div" and self.in_recipient_line:
            self.in_recipient_line = False
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.in_recipient_line:
            self.lines[-1] += data


def render_card(names, lifecycle):
    source = TEMPLATE.read_text()
    start = source.index("{% set source_rule =")
    end = source.index("{% if focus_id and w.id == focus_id %}", start)
    environment = Environment(autoescape=select_autoescape(default_for_string=True))
    card = environment.from_string(source[start:end] + "</article>").render(w={
        "id": "fixture-watch", "display_name": "Test Watch", "watch_type": "PHRASE",
        "setup_mode": "TOPIC", "match_mode": "CONTAINS", "search_term": "fire",
        "aliases": [], "source_filter": [], "alert_category_filter": [],
        "nearby_enabled": False, "min_priority": 1, "lifecycle_label": lifecycle,
        "lifecycle_class": "active", "lifecycle_reason": "Test state",
        "delivery_label": "No matches yet", "delivery_class": "inactive-status",
        "delivery_reason": "No delivery", "setup_issues": [], "matches_7d": 0,
        "sent_7d": 0, "active_recipient_count": len(names or []),
        "recipient_names": names,
    })
    parsed = RecipientLine()
    parsed.feed(card)
    return card, parsed


@pytest.mark.parametrize("lifecycle", ["On", "Paused", "Scheduled", "Expired"])
def test_every_lifecycle_shows_all_recipient_names(lifecycle):
    names = ["Recipient A", "Recipient B", "Recipient C"]
    _, parsed = render_card(names, lifecycle)
    assert len(parsed.lines) == 1
    assert " ".join(parsed.lines[0].split()) == "Recipients: Recipient A, Recipient B, Recipient C"


@pytest.mark.parametrize("names", [[], None])
def test_missing_recipients_are_explicit(names):
    _, parsed = render_card(names, "On")
    assert len(parsed.lines) == 1
    assert " ".join(parsed.lines[0].split()) == "Recipients: No active recipients"


def test_full_list_and_special_characters_are_preserved_safely():
    names = ["A & B", '<script>alert("test")</script>', *[f"Recipient {i:02d}" for i in range(25)]]
    card, parsed = render_card(names, "On")
    assert len(parsed.lines) == 1
    assert " ".join(parsed.lines[0].split()) == "Recipients: " + ", ".join(names)
    assert "script" not in parsed.tags_in_line
    assert "&lt;script&gt;" in card
    assert card.count("<strong>Recipients:</strong>") == 1
    assert 'class="meta watch-rule-summary" data-watch-recipients' in card
