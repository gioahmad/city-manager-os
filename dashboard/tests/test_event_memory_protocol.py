from urllib.parse import parse_qs, urlsplit

from event_memory_protocol import obsidian_link


def test_obsidian_names_and_paths_use_percent_encoded_spaces():
    vault = 'Private Event Vault + Personal'
    note = 'Event Memory/example/Material.md'
    url = obsidian_link(vault, note)
    assert '%20' in url and '%2B' in url and '%2F' in url
    assert '+' not in url
    assert parse_qs(urlsplit(url).query) == {'vault': [vault], 'file': [note]}
