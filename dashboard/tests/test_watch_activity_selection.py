import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WatchError(Exception):
    pass


class ActivitySelection(unittest.TestCase):
    def setUp(self):
        names = {'_matching_inputs', '_watch_prefill_from_record', '_watch_prefill_values'}
        nodes = [n for n in ast.parse((ROOT/'spatial_watch_app.py').read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name in names]
        import uuid
        self.queries = []
        self.row = None
        def query(sql, args):
            self.queries.append((sql, args))
            return self.row
        self.ns = {'HTTPException': WatchError, 'uuid': uuid, 'query_one': query,
                   'alert_keyword_choices': lambda row: [row['title']]}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'watch_inputs', 'exec'), self.ns)

    def test_all_activity_clears_every_filter_even_with_stale_form_values(self):
        fn = self.ns['_matching_inputs']
        self.assertEqual(fn('ANY', 'EXISTING', 'fire', 'alarm', 'BNN', 'PUBLIC_SAFETY', 5), ('', '', '', '', 1))
        values = ('fire', 'alarm', 'BNN', 'PUBLIC_SAFETY', 5)
        self.assertEqual(fn('FILTERED', 'MAP_POINT', *values), values)
        self.assertEqual(fn('', 'MAP_POINT', *values), values)
        for selection, scope in [('ANY', 'ANYWHERE'), ('unknown', 'EXISTING')]:
            with self.assertRaises(WatchError):
                fn(selection, scope, *values)

    def test_intel_draft_uses_record_geometry_without_requiring_or_creating_an_alert(self):
        from uuid import uuid4
        record_id = str(uuid4())
        self.row = {'title': 'Site event', 'message': '', 'latitude': 40.77, 'longitude': -74.02,
                    'municipality': 'Weehawken', 'location_label': 'Site'}
        for kind, table in [('EVENT_INTELLIGENCE', 'event_intelligence'), ('TRANSIT', 'transit_observations')]:
            draft = self.ns['_watch_prefill_from_record'](kind, record_id)
            self.assertEqual(draft['location_kind'], 'MAP_POINT')
            self.assertEqual((draft['latitude'], draft['longitude']), (40.77, -74.02))
            self.assertEqual(draft['from_alert'], '')
            self.assertIn('FROM '+table, self.queries[-1][0])
            self.assertNotIn('INSERT', self.queries[-1][0])
        self.row.update(latitude=None, longitude=None, location_label='Venue name')
        draft = self.ns['_watch_prefill_from_record']('EVENT_INTELLIGENCE', record_id)
        self.assertEqual(draft['location_kind'], 'MUNICIPALITY')
        self.assertEqual(draft['location_query'], 'Weehawken')


if __name__ == '__main__':
    unittest.main()
