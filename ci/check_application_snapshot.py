"""Restore a real PostgreSQL 17 release snapshot in disposable databases."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
container = subprocess.check_output(
    ['docker', 'ps', '--filter', 'ancestor=postgis/postgis:17-3.5', '--format', '{{.ID}}'],
    text=True).strip()
assert container and '\n' not in container, 'Expected exactly one CI PostGIS container'
source, restored = ('backup_' + uuid4().hex for _ in range(2))


def sql(database, statement):
    return subprocess.check_output(
        ['docker', 'exec', '-i', container, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
         '-U', 'postgres', '-d', database, '-Atq'],
        input=statement, text=True)


try:
    for database in (source, restored):
        sql('postgres', f'CREATE DATABASE {database};')
    sql(source, """
        CREATE TABLE contacts(id integer PRIMARY KEY, name text NOT NULL);
        CREATE TABLE workspace_microsoft_operations(id integer PRIMARY KEY, status text NOT NULL);
        CREATE TABLE outlook_contact_links(contact_id integer REFERENCES contacts, provider_key text);
        CREATE INDEX contact_name ON contacts(name);
        CREATE TABLE gis_addresses(id integer PRIMARY KEY, address text);
        CREATE TABLE gis_parcels(id integer, county integer) PARTITION BY LIST(county);
        CREATE TABLE parcels_hudson PARTITION OF gis_parcels FOR VALUES IN (17);
        CREATE TABLE gis_dataset_versions(id integer, version text);
        CREATE TABLE gis_refresh_runs(id integer, status text);
        CREATE TABLE stg_nj_addresses(id integer);
        INSERT INTO contacts VALUES (1,'Saved contact');
        INSERT INTO workspace_microsoft_operations VALUES(1,'REVIEW');
        INSERT INTO outlook_contact_links VALUES(1,'outlook-123');
        INSERT INTO gis_addresses SELECT i,'Reference address' FROM generate_series(1,20000) i;
        INSERT INTO gis_parcels VALUES(1,17);
        INSERT INTO gis_dataset_versions VALUES(1,'Current reference');
        INSERT INTO gis_refresh_runs VALUES(1,'COMPLETE');
        INSERT INTO stg_nj_addresses VALUES(1);
    """)
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        deploy = repo / 'deploy/postgis'
        deploy.mkdir(parents=True)
        shutil.copyfile(ROOT / 'deploy/postgis/release-snapshot.sh', deploy / 'release-snapshot.sh')
        (deploy / '.env').write_text(f'POSTGRES_USER=postgres\nPOSTGRES_DB={source}\n')
        binary = repo / 'bin'
        binary.mkdir()
        # Route only this disposable test's fixed container name to the CI service.
        real_docker = shutil.which('docker')
        (binary / 'docker').write_text(f"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
assert args[0] == 'exec'
index = args.index('citymanager-postgis')
args[index:index+1] = ['--env', 'POSTGRES_USER=postgres', '--env', 'POSTGRES_DB={source}', '{container}']
os.execv({real_docker!r}, [{real_docker!r}] + args)
""")
        (binary / 'docker').chmod(0o755)
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        env = {**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH'],
               'CMOS_REPO': str(repo), 'CMOS_RELEASE_SNAPSHOT_DIR': str(repo / 'snapshots')}
        subprocess.run(['bash', str(deploy / 'release-snapshot.sh'), '', 'test-release'],
                       env=env, check=True, timeout=60)
        snapshots = list((repo / 'snapshots').iterdir())
        assert len(snapshots) == 1 and not snapshots[0].name.startswith('.')
        snapshot = snapshots[0]
        assert 'public.gis_addresses' in (snapshot / 'excluded-data-tables.txt').read_text()
        subprocess.run(['sha256sum', '-c', 'application.dump.sha256'], cwd=snapshot, check=True)
        with (snapshot / 'application.dump').open('rb') as archive:
            subprocess.run(['docker', 'exec', '-i', container, 'pg_restore', '-U', 'postgres',
                            '-d', restored, '--no-owner', '--no-privileges', '--exit-on-error'],
                           stdin=archive, check=True, timeout=60)
        assert sql(restored, 'SELECT name FROM contacts;').strip() == 'Saved contact'
        assert sql(restored, 'SELECT provider_key FROM outlook_contact_links;').strip() == 'outlook-123'
        assert sql(restored, 'SELECT status FROM workspace_microsoft_operations;').strip() == 'REVIEW'
        assert sql(restored, "SELECT to_regclass('contact_name');").strip() == 'contact_name'
        assert sql(restored, 'SELECT version FROM gis_dataset_versions;').strip() == 'Current reference'
        assert sql(restored, 'SELECT status FROM gis_refresh_runs;').strip() == 'COMPLETE'
        for table in ('gis_addresses', 'gis_parcels', 'parcels_hudson', 'stg_nj_addresses'):
            assert sql(restored, f'SELECT count(*) FROM {table};').strip() == '0', table
        constraints = sql(restored, "SELECT count(*) FROM pg_constraint WHERE conrelid='outlook_contact_links'::regclass AND contype='f';")
        assert constraints.strip() == '1'
    print('APPLICATION SNAPSHOT RESTORE: PASS — contacts, workspace, links, indexes, FK and GIS exclusions')
finally:
    for database in (restored, source):
        sql('postgres', f'DROP DATABASE IF EXISTS {database} WITH (FORCE);')
