"""Run without a database or optional web packages: actual functions loaded by AST."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(nodes, type_ignores=[]), str(path), 'exec', dont_inherit=True), namespace)


class ContactChangeChecks(unittest.TestCase):
    def setUp(self):
        ns = {'HTTPException': HTTPException, 're': re, 'json': json, 'hashlib': hashlib,
              'Any': object, 'quote': quote, 'urlencode': urlencode, 'urlsplit': urlsplit}
        functions('microsoft_workspace.py', {'clean', 'digest'}, ns)
        functions('operations_app.py', {'_phone_numbers', '_contact_values', '_contact_phones', '_contact_emails'}, ns)
        ms = SimpleNamespace(clean=ns['clean'], digest=ns['digest'], uid=lambda x:x)
        ns['ms'] = ms
        functions('workspace_calendar.py', {'contact_row'}, ns)
        ns['calendar'] = SimpleNamespace(contact_row=ns['contact_row'])
        tree = ast.parse((ROOT / 'microsoft_contacts.py').read_text())
        constants = [n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id in {'FIELDS', 'ADDRESS_FIELDS'}]
        exec(compile(ast.Module(constants, type_ignores=[]), 'constants', 'exec'), ns)
        functions('microsoft_contacts.py', {'local_values','source_values','phone_key','contact_patch','verify_contact','linked_contact','search_contacts'}, ns)
        self.ns, self.ms = ns, ms
        self.remote = {'id':'jane','displayName':'Jane','companyName':'Town','jobTitle':'A',
                       'emailAddresses':[{'address':'JANE@EXAMPLE.COM','name':'Jane label'}],
                       'mobilePhone':'201-555-1234','homePhones':['+442071838750'], 'businessPhones':[],
                       'homeAddress':{'street':'1 Main','city':'Town'},'changeKey':'v1'}
        self.local = ns['source_values'](self.remote)[0]

    def test_cleanup_normalizes_case_deduplicates_and_preserves_warning(self):
        remote = {**self.remote,'emailAddresses':[{'address':'JANE@EXAMPLE.COM'},{'address':'jane@example.com'}],
                  'businessPhones':['+12015551234','201-555-1234 ext 9']}
        fields, warnings = self.ns['source_values'](remote)
        self.assertEqual(fields['emails'], ['jane@example.com'])
        self.assertEqual(fields['phones'], ['+12015551234','+442071838750'])
        self.assertIn('ext 9', warnings[0])

    def test_unchanged_contact_has_no_patch_and_email_change_keeps_labels(self):
        self.assertEqual(self.ns['contact_patch'](self.local,self.local,self.remote), {})
        edited = {**self.local, 'emails':['jane@example.com','new@example.com']}
        patch = self.ns['contact_patch'](edited,self.local,self.remote)
        self.assertEqual(set(patch), {'emailAddresses'})
        self.assertEqual(patch['emailAddresses'][0]['name'], 'Jane label')

    def test_phone_changes_keep_lanes_and_unusable_original(self):
        remote = {**self.remote,'businessPhones':['201-555-1234 ext 9']}
        edited = {**self.local,'phones':['+442071838750','+12015559876']}
        patch = self.ns['contact_patch'](edited,self.local,remote)
        self.assertIsNone(patch['mobilePhone'])
        self.assertNotIn('homePhones',patch)
        self.assertEqual(patch['businessPhones'],['201-555-1234 ext 9','+12015559876'])

    def test_address_change_requires_explicit_components(self):
        edited = {**self.local,'address':'New address'}
        with self.assertRaises(HTTPException):self.ns['contact_patch'](edited,self.local,self.remote)
        patch = self.ns['contact_patch'](edited,self.local,self.remote,{'kind':'homeAddress','street':'2 Main','city':'Town','state':'NJ','postalCode':'07086','countryOrRegion':'US'})
        self.assertEqual(patch['homeAddress']['city'],'Town')
        self.assertNotIn('businessAddress',patch)
        with self.assertRaises(HTTPException):self.ns['contact_patch'](edited,self.local,self.remote,{'kind':'unknown'})

    def test_owner_and_account_are_in_link_query(self):
        queries = []
        self.ns['query_one'] = lambda sql, params: queries.append((sql,params)) or {}
        with self.assertRaises(HTTPException):self.ns['linked_contact']('gio','local-id')
        self.assertEqual(queries[0][1], ('gio','local-id','gio'))
        self.assertIn('a.account_email=l.account_email',queries[0][0])
        self.assertIn("c.visibility='ALL' OR c.owner_username=%s",queries[0][0])

    def test_stale_local_or_remote_blocks_execution(self):
        row = {**self.local,'provider_key':'jane','account_email':'gio@example.com'}
        payload = {'contact_id':'local-id','provider_key':'jane','account_email':'gio@example.com',
                   'local':copy.deepcopy(self.local),'remote_digest':self.ms.digest(self.remote)}
        self.ns['linked_contact'] = lambda owner,contact_id: row
        self.ns['read_contact'] = lambda *args: self.remote
        self.ns['verify_contact']('gio',payload,None,None)
        row['title'] = 'Changed after review'
        with self.assertRaises(HTTPException):self.ns['verify_contact']('gio',payload,None,None)
        row['title'] = self.local['title'];self.remote['changeKey'] = 'v2'
        with self.assertRaises(HTTPException):self.ns['verify_contact']('gio',payload,None,None)

    def test_retained_search_is_owner_scoped_and_escapes_patterns(self):
        queries = []
        self.ns['query_all'] = lambda sql,params: queries.append((sql,params)) or []
        self.ns['search_contacts']('gio','100%_town','',False)
        self.assertIn('owner_username=%s',queries[0][0])
        self.assertEqual(queries[0][1], ('gio','%100\\%\\_town%','%100\\%\\_town%'))


if __name__ == '__main__':
    unittest.main()
