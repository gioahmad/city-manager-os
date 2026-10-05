"""Exercise the production provider-summary SQL in an isolated CI PostgreSQL DB."""
import ast
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
source = ast.parse((ROOT / 'dashboard/transit_app.py').read_text())
query = next(ast.literal_eval(node.value) for node in source.body
             if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == 'TRANSIT_PROVIDER_SUMMARY_SQL'
                     for target in node.targets))
original = '''
    SELECT p.*,
           count(DISTINCT a.id) FILTER (WHERE a.active) AS asset_count,
           count(DISTINCT o.id) FILTER (WHERE o.active AND o.impact_level IN ('WATCH','ALERT')) AS active_impact_count
    FROM transit_providers p
    LEFT JOIN transit_assets a ON a.provider_id=p.id
    LEFT JOIN transit_observations o ON o.provider_id=p.id
    GROUP BY p.id ORDER BY p.phase,p.name
'''

with psycopg.connect(os.environ['CMOS_TEST_POSTGRES_DSN'], connect_timeout=10) as connection:
    # TEMP tables cannot change the application's real transit tables.
    connection.execute('''
        CREATE TEMP TABLE transit_providers(id integer PRIMARY KEY,name text,phase integer);
        CREATE TEMP TABLE transit_assets(id integer PRIMARY KEY,provider_id integer,active boolean);
        CREATE TEMP TABLE transit_observations(id integer PRIMARY KEY,provider_id integer,active boolean,impact_level text);
        INSERT INTO transit_providers VALUES(1,'Mixed',1),(2,'No assets',2),(3,'No observations',3),(4,'Empty',4);
        INSERT INTO transit_assets VALUES(1,1,true),(2,1,true),(3,1,false),(4,3,true);
        INSERT INTO transit_observations VALUES
            (1,1,true,'WATCH'),(2,1,true,'ALERT'),(3,1,true,'AWARENESS'),
            (4,1,false,'ALERT'),(5,2,true,'ALERT');
    ''')
    assert connection.execute(query).fetchall() == connection.execute(original).fetchall()
    connection.execute('TRUNCATE transit_assets,transit_observations')
    connection.execute('INSERT INTO transit_assets SELECT n,1,true FROM generate_series(1,20000) AS n')
    connection.execute("INSERT INTO transit_observations SELECT n,1,true,'WATCH' FROM generate_series(1,40000) AS n")
    connection.execute('ANALYZE transit_providers')
    connection.execute('ANALYZE transit_assets')
    connection.execute('ANALYZE transit_observations')
    connection.execute("SET LOCAL statement_timeout='5s'")
    plan = connection.execute('EXPLAIN (ANALYZE,FORMAT JSON) ' + query).fetchone()[0][0]
    result = connection.execute(query).fetchall()
    assert result[0][-2:] == (20000,40000), result
    assert all(row[-2:] == (0,0) for row in result[1:]), result

    def join_rows(node):
        count = []
        if 'Join' in node['Node Type'] or node['Node Type'] == 'Nested Loop':
            count.append(node['Actual Rows'] * node['Actual Loops'])
        for child in node.get('Plans',[]):
            count.extend(join_rows(child))
        return count

    counts = join_rows(plan['Plan'])
    assert counts and max(counts) <= 4, counts
    print('TRANSIT POSTGRES: PASS — original count semantics preserved')
    print(f"TRANSIT SCALE: PASS — 20,000 assets + 40,000 observations; {plan['Execution Time']:.3f} ms; largest provider join {max(counts)} rows")
    print('Old raw join would combine 800,000,000 asset/observation pairs; new query aggregates before joining.')
