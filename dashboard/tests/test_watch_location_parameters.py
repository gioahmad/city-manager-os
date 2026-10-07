import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LocationError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def helpers(**overrides):
    names = {"LOCATION_KINDS", "LOCATION_SCOPES", "_location_kind", "_location_setup_mode",
             "_selected_location", "watch_location_search"}
    nodes = []
    for node in ast.parse((ROOT / "spatial_watch_app.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    counties = next(node for node in ast.parse((ROOT / "watch_preview_app.py").read_text()).body
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "COUNTIES" for t in node.targets))
    namespace = {"re": re, "HTTPException": LocationError,
                 "JSONResponse": lambda data: data, "_json_safe": lambda data: data}
    namespace.update(overrides)
    exec(compile(ast.Module(body=[counties, *nodes], type_ignores=[]), "location_helpers", "exec"), namespace)
    return namespace


class Cursor:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params):
        self.queries.append((sql, params))

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.cur = Cursor(row)

    def cursor(self):
        return self.cur


class WatchLocationParameters(unittest.TestCase):
    def test_explicit_county_and_town_never_use_address_resolver(self):
        def wrong_resolver(*args):
            self.fail("A county or town was sent to the address resolver")
        select = helpers(_resolve_target=wrong_resolver)["_selected_location"]
        for scope, query, row in (("COUNTY", "Hudson County", {"label": "Hudson"}),
                                  ("MUNICIPALITY", "Weehawken", {"label": "Weehawken", "county": "Hudson"})):
            conn = Connection(row)
            result = select(conn, scope=scope, kind="TYPED_ADDRESS", source_id="",
                            location_query=query, latitude="", longitude="", municipality="")
            self.assertEqual(result["kind"], scope)
            self.assertFalse(result["spatial"])
            self.assertEqual(len(conn.cur.queries), 0 if scope == "COUNTY" else 1)
        with self.assertRaises(LocationError):
            select(Connection(None), scope="COUNTY", kind="TYPED_ADDRESS", source_id="",
                   location_query="Not a county", latitude="", longitude="", municipality="")

    def test_scope_controls_mode_and_validates_saved_places_and_points(self):
        namespace = helpers()
        mode = namespace["_location_setup_mode"]
        self.assertEqual(mode("LOCATION", "ANYWHERE", "fire"), "TOPIC")
        self.assertEqual(mode("TOPIC", "COUNTY", ""), "LOCATION")
        self.assertEqual(mode("TOPIC", "MUNICIPALITY", "fire"), "LOCATION_TOPIC")
        self.assertEqual(mode("LOCATION", "", ""), "LOCATION")
        with self.assertRaises(LocationError):
            mode("TOPIC", "unknown", "")
        for scope in ("SAVED", "MAP_POINT"):
            with self.assertRaises(LocationError):
                namespace["_selected_location"](None, scope=scope, kind="TYPED_ADDRESS", source_id="",
                                                 location_query="Park Avenue", latitude="", longitude="", municipality="")

    def test_scoped_search_avoids_unrelated_catalogs(self):
        queries = []
        def query_all(sql, params):
            queries.append((sql, params))
            return []
        search = helpers(query_all=query_all)["watch_location_search"]
        result = search("Hudson County", kind="COUNTY")
        self.assertEqual([item["label"] for item in result["items"]], ["Hudson"])
        self.assertEqual(queries, [])
        search("Weehawken", kind="MUNICIPALITY")
        self.assertEqual(len(queries), 1)
        self.assertIn("'MUNICIPALITY' AS kind", queries[0][0])
        queries.clear()
        search("400 Park", kind="TYPED_ADDRESS", municipality="Weehawken")
        self.assertEqual(len(queries), 1)
        self.assertIn("'ADDRESS' AS kind", queries[0][0])
        self.assertEqual(queries[0][1], ("%400 Park%", "Weehawken", "%Weehawken%"))


if __name__ == "__main__":
    unittest.main()
