import os
import sys
from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
os.environ.setdefault("DB_PASSWORD", "test")

import operations_app


def test_us_phone_numbers_gain_country_code_without_changing_international_numbers():
    assert operations_app._phone_numbers("201-555-1234, 1 (212) 555-9876, +442071838750") == [
        "+12015551234",
        "+12125559876",
        "+442071838750",
    ]


def test_contact_directory_is_shared_with_alert_share_and_existing_recipients():
    migration = (REPO_ROOT / "deploy/postgis/init/035_contact_directory.sql").read_text()
    source = (DASHBOARD_ROOT / "operations_app.py").read_text()
    nav = (DASHBOARD_ROOT / "templates/nav.html").read_text()
    dialog = (DASHBOARD_ROOT / "templates/share_dialog.html").read_text()
    contacts = (DASHBOARD_ROOT / "templates/contacts.html").read_text()

    assert "CREATE TABLE IF NOT EXISTS contacts" in migration
    assert "CREATE TABLE IF NOT EXISTS issue_contacts" in migration
    assert "CREATE TABLE IF NOT EXISTS contact_activity" in migration
    assert "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS contact_id" in migration
    assert "INSERT INTO contacts" in migration and "FROM subscribers" in migration
    assert '@app.get("/contacts"' in source
    assert '@app.get("/api/share/context")' in source
    assert "contact_ids: list[uuid.UUID]" in source
    assert nav.count('href="/contacts"') == 2
    assert 'a[href^="/share?"]' in dialog
    assert "dialog.showModal()" in dialog
    assert "All authenticated staff" in contacts
    assert "Executive only" in contacts
    assert "Private to me" in contacts


def test_contact_form_normalizes_multiple_delivery_methods():
    values = operations_app._contact_form_values(
        " Jane Doe ",
        "resident",
        " Township ",
        " Resident ",
        "201-555-1234\n+442071838750",
        "JANE@EXAMPLE.COM; jane.alt@example.com",
        "1 Main Street",
        "Resident, Emergency, Resident",
        " Call first ",
        "private",
    )
    assert values[0:4] == ("Jane Doe", "RESIDENT", "Township", "Resident")
    assert values[4] == ["+12015551234", "+442071838750"]
    assert values[5] == ["jane@example.com", "jane.alt@example.com"]
    assert values[7] == ["Resident", "Emergency"]
    assert values[9] == "PRIVATE"
