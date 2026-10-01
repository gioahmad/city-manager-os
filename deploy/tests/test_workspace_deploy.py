"""Run command-flow checks without touching Docker, credentials, or a live database."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
DOCKER='''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
with open(os.environ['MOCK_LOG'],'a') as f:f.write(json.dumps(a)+'\\n')
if a[0]=='inspect':
 if '{{.Config.Image}}' in a:print('test/dashboard:latest')
 elif '{{.Image}}' in a:print('sha256:previous')
 else:print('protected services unchanged')
elif a[0]=='exec':
 if 'pg_dump' in a:sys.stdout.buffer.write(b'PGDMP\\x00\\xffarchive')
 elif 'pg_restore' in a:
  assert sys.stdin.buffer.read()==b'PGDMP\\x00\\xffarchive'
  sys.exit(int(os.environ.get('FAIL_VALIDATE','0')))
 else:
  body=sys.stdin.read();assert body
  if 'citymanager-dashboard' in a:sys.exit(int(os.environ.get('FAIL_LIVE','0')))
elif a[0]=='compose' and 'pytest' in a:
 mounts=[a[i+1] for i,x in enumerate(a) if x=='-v']
 deploy=next(x for x in mounts if x.endswith(':/deploy:ro'))
 assert (Path(deploy[:-11])/'postgis/init/035_contact_directory.sql').is_file()
 sys.exit(int(os.environ.get('FAIL_TESTS','0')))
'''


class ReleaseCommands(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.repo=Path(self.temp.name)/'repo';self.repo.mkdir()
  shutil.copytree(ROOT/'deploy',self.repo/'deploy')
  (self.repo/'dashboard/tests').mkdir(parents=True)
  (self.repo/'deploy/postgis/.env').write_text('POSTGRES_USER=test\nPOSTGRES_DB=test\n')
  tools=Path(self.temp.name)/'bin';tools.mkdir()
  self.log=Path(self.temp.name)/'commands.jsonl'
  for name,body in [('docker',DOCKER),('git','#!/bin/sh\ncase "$1" in status) ;; rev-parse) echo pinned ;; *) exit 1 ;; esac\n')]:
   file=tools/name;file.write_text(body);file.chmod(0o755)
  self.env={**os.environ,'PATH':str(tools)+':'+os.environ['PATH'],'CMOS_REPO':str(self.repo),'MOCK_LOG':str(self.log)}
 def run_script(self,name,**env):
  return subprocess.run(['bash',str(self.repo/name),'pinned'],env={**self.env,**env},capture_output=True,timeout=60)
 def commands(self):return [json.loads(x) for x in self.log.read_text().splitlines()]
 def test_backup_validated_and_binary_clean(self):
  r=self.run_script('deploy/postgis/backup.sh');self.assertEqual(r.returncode,0,r.stderr.decode())
  backups=self.repo/'deploy/postgis/backups'
  archive=next(backups.glob('*.dump'));self.assertEqual(archive.read_bytes(),b'PGDMP\x00\xffarchive')
  self.assertTrue((backups/'LATEST_VALIDATED').exists());self.assertIn(b'5/5 stages complete',r.stderr)
 def test_failed_validation_keeps_previous_marker(self):
  backups=self.repo/'deploy/postgis/backups';backups.mkdir();(backups/'LATEST_VALIDATED').write_text('previous')
  r=self.run_script('deploy/postgis/backup.sh',FAIL_VALIDATE='7');self.assertNotEqual(r.returncode,0)
  self.assertEqual((backups/'LATEST_VALIDATED').read_text(),'previous')
  self.assertFalse(list(backups.glob('*.partial')));self.assertFalse(list(backups.glob('*.dump')))
  self.assertIn(b'FAILED (exit 7)',r.stderr)
 def test_failed_tests_stop_before_restart(self):
  r=self.run_script('deploy/workspace/install_workspace.sh',FAIL_TESTS='9');self.assertEqual(r.returncode,9,r.stderr.decode())
  self.assertFalse(any('up' in a for a in self.commands()));self.assertNotIn(b'8/8 stages complete',r.stderr)
 def test_success_mounts_migrations_and_restarts_only_dashboard(self):
  r=self.run_script('deploy/workspace/install_workspace.sh');self.assertEqual(r.returncode,0,r.stderr.decode())
  ups=[a for a in self.commands() if 'up' in a];self.assertEqual(len(ups),1)
  self.assertEqual(ups[0][-1],'citymanager-dashboard');self.assertIn('--no-deps',ups[0])
  self.assertIn(b'8/8 stages complete',r.stderr)
 def test_live_failure_rolls_back_once(self):
  r=self.run_script('deploy/workspace/install_workspace.sh',FAIL_LIVE='7');self.assertNotEqual(r.returncode,0)
  tags=[a for a in self.commands() if a[:2]==['image','tag']];self.assertEqual(tags,[['image','tag','sha256:previous','test/dashboard:latest']])
  self.assertEqual(len([a for a in self.commands() if 'up' in a]),2)
  self.assertNotIn(b'8/8 stages complete',r.stderr)
 def test_progress_keeps_stdin_and_reports_growth(self):
  output=Path(self.temp.name)/'growing'
  script='source "$1"; run_with_progress "slow dump" "$2" python -c \'import sys,time; assert sys.stdin.read()=="forwarded\\n";sys.stdout.buffer.write(b"abc");sys.stdout.flush();time.sleep(6)\' > "$2"'
  r=subprocess.run(['bash','-c',script,'test',str(self.repo/'deploy/progress.sh'),str(output)],input=b'forwarded\n',env=self.env,capture_output=True,timeout=15)
  self.assertEqual(r.returncode,0,r.stderr.decode());self.assertEqual(output.read_bytes(),b'abc')
  self.assertIn(b'bytes written',r.stderr);self.assertIn(b'elapsed',r.stderr)

if __name__=='__main__':unittest.main()
