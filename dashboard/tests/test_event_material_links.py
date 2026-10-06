from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_event_materials_use_existing_links_without_file_upload():
    template = (ROOT / "templates" / "schedule.html").read_text()
    source = (ROOT / "schedule_app.py").read_text()
    assert "Event Materials / Google Drive" in template
    assert "City Manager OS does not copy the file to the VPS." in template
    assert "Open material {{ loop.index }} ↗" in template
    assert 'name="reference_links"' in template
    assert "reference_links.strip() or None" in source
    assert 'type="file"' not in template
