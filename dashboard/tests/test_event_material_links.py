from pathlib import Path

import pytest

import event_materials


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def test_google_drive_links_are_provider_scoped_and_ids_are_extracted():
    file_url = 'https://drive.google.com/file/d/1AbC_defGHIjkLMnopQR/view?usp=sharing'
    docs_url = 'https://docs.google.com/document/d/1ZyxWVutsRQponMLKjih/edit'
    assert event_materials._drive_url(file_url) == file_url
    assert event_materials._drive_url(docs_url) == docs_url
    assert event_materials._drive_id(file_url) == '1AbC_defGHIjkLMnopQR'
    assert event_materials._drive_id(docs_url) == '1ZyxWVutsRQponMLKjih'
    with pytest.raises(Exception):
        event_materials._drive_url('http://drive.google.com/file/d/1AbC_defGHIjkLMnopQR/view')
    with pytest.raises(Exception):
        event_materials._drive_url('https://example.com/file/d/1AbC_defGHIjkLMnopQR/view')


def test_event_memory_stores_references_not_file_bytes():
    migration = (REPO / 'deploy/postgis/init/043_event_material_links.sql').read_text()
    module = (ROOT / 'event_materials.py').read_text()
    template = (ROOT / 'templates/context.html').read_text()
    installer = (REPO / 'deploy/workspace/install_workspace.sh').read_text()
    assert 'workspace_event_materials' in migration
    assert 'bytea' not in migration.casefold()
    assert 'UploadFile' not in module and 'File(' not in module
    assert "GOOGLE_DRIVE" in migration and "drive.google.com" in module
    assert 'no PDF or photo is copied to the VPS' in template
    assert '043_event_material_links.sql' in installer
    assert "'workspace_event_materials'" in installer


def test_event_materials_are_private_and_detach_does_not_delete_drive_content():
    module = (ROOT / 'event_materials.py').read_text()
    assert 'owner_username=%s' in module
    assert "DELETE FROM workspace_event_materials" in module
    assert 'drive.google.com' in module
    assert 'requests.' not in module and 'httpx.' not in module
    assert 'Google Drive material linked' in module
