"""Exercise the release backup policy with real Git history, without a database."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('git'), 'Deployment policy runs on the host; Git is not installed in the application image')
class ApplicationBackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.db = self.repo / 'deploy/postgis'
        self.db.mkdir(parents=True)
        shutil.copyfile(ROOT / 'deploy/postgis/ensure-deploy-backup.sh', self.db / 'ensure-deploy-backup.sh')
        (self.db / 'verify-backup.sh').write_text('echo VERIFIED; exit "${VERIFY_FAIL:-0}"\n')
        (self.db / 'backup.sh').write_text('echo FULL\n')
        (self.db / 'release-snapshot.sh').write_text('echo SNAPSHOT; exit "${SNAPSHOT_FAIL:-0}"\n')
        self.env = {**os.environ, 'CMOS_REPO': str(self.repo),
                    'CMOS_FORCE_FULL_BACKUP': 'false', 'CMOS_DEPLOY_RETRY_FROM': '',
                    'CMOS_DEPLOY_RETRY_BACKUP': '', 'VERIFY_FAIL': '0', 'SNAPSHOT_FAIL': '0'}
        self.git('init', '-q')
        self.git('config', 'user.email', 'backup-test@example.invalid')
        self.git('config', 'user.name', 'Backup test')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, text=True).strip()

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'test recovery policy')
        return self.git('rev-parse', 'HEAD')

    def migration(self, reviewed=True):
        path = self.db / 'init/043_outlook_contacts.sql'
        path.parent.mkdir(exist_ok=True)
        path.write_text('CREATE TABLE outlook_contact_links(id integer);\n')
        blob = self.git('hash-object', str(path))
        if reviewed:
            (self.db / 'application-migrations.txt').write_text(blob + ' deploy/postgis/init/043_outlook_contacts.sql\n')
        return path

    def gate(self, previous=None, **overrides):
        return subprocess.run(['bash', str(self.db / 'ensure-deploy-backup.sh'),
                               self.base if previous is None else previous, self.git('rev-parse', 'HEAD')],
                              env={**self.env, **overrides}, capture_output=True, text=True, timeout=10)

    def test_routine_release_uses_fresh_snapshot(self):
        result = self.gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SNAPSHOT', result.stdout)
        self.assertNotIn('\nFULL\n', result.stdout)

    def test_reviewed_migration_uses_snapshot_instead_of_full_dump(self):
        self.migration()
        self.commit()
        result = self.gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reviewed application-only migrations', result.stdout)
        self.assertIn('SNAPSHOT', result.stdout)
        self.assertNotIn('\nFULL\n', result.stdout)

    def test_edited_reviewed_sql_requires_full_dump(self):
        path = self.migration()
        path.write_text('DROP TABLE contacts;\n')
        self.commit()
        self.assertIn('\nFULL\n', self.gate().stdout)

    def test_unreviewed_migration_requires_full_dump(self):
        self.migration(reviewed=False)
        self.commit()
        self.assertIn('\nFULL\n', self.gate().stdout)

    def test_deleted_migration_requires_full_dump(self):
        path = self.migration()
        self.base = self.commit()
        path.unlink()
        self.commit()
        self.assertIn('\nFULL\n', self.gate().stdout)

    def test_gis_change_requires_full_dump(self):
        self.migration()
        gis = self.repo / 'deploy/gis'
        gis.mkdir()
        (gis / 'refresh.py').write_text('# reference data change\n')
        self.commit()
        self.assertIn('\nFULL\n', self.gate().stdout)

    def test_invalid_full_backup_requires_new_full_dump(self):
        result = self.gate(VERIFY_FAIL='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('\nFULL\n', result.stdout)
        self.assertNotIn('SNAPSHOT', result.stdout)

    def test_failed_snapshot_stops_release(self):
        result = self.gate(SNAPSHOT_FAIL='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('BACKUP GATE: PASS', result.stdout)

    def test_missing_helper_requires_full_dump(self):
        (self.db / 'release-snapshot.sh').unlink()
        self.assertIn('\nFULL\n', self.gate().stdout)

    def test_explicit_force_and_unknown_previous_require_full_dump(self):
        self.assertIn('\nFULL\n', self.gate(CMOS_FORCE_FULL_BACKUP='true').stdout)
        self.assertIn('\nFULL\n', self.gate(previous='').stdout)


if __name__ == '__main__':
    unittest.main()
