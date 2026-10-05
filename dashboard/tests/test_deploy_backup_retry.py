"""Test the backup gate without Docker access or production data."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT.parent / 'deploy/postgis/ensure-deploy-backup.sh'


@pytest.mark.parametrize('overrides,reuse,verify_expected', [
    ({},True,True),
    ({'CMOS_FORCE_FULL_BACKUP':'true'},False,False),
    ({'SENSITIVE_CHANGED':'1'},False,False),
    ({'ANCESTOR_OK':'0'},False,False),
    ({'VERIFY_FAIL':'1'},False,True),
    ({'CMOS_DEPLOY_RETRY_BACKUP':'/different/archive.dump'},False,True),
    ({'CMOS_DEPLOY_RETRY_FROM':''},False,False),
    ({'CMOS_DEPLOY_RETRY_FROM':'not-a-commit'},False,False),
])
def test_retry_only_reuses_named_verified_unchanged_recovery_point(tmp_path,overrides,reuse,verify_expected):
    target = tmp_path / 'deploy/postgis'
    target.mkdir(parents=True)
    shutil.copyfile(GATE,target / 'ensure-deploy-backup.sh')
    (target / 'backup.sh').write_text('printf "FRESH_BACKUP\\n"\n')
    (target / 'verify-backup.sh').write_text('''
        printf 'verified\n' >> "$TRACE"
        [[ "${VERIFY_FAIL:-0}" == 0 ]] || exit 1
        printf 'BACKUP VERIFY: PASS\nfile=/verified/recovery.dump\n'
    ''')
    binary = tmp_path / 'bin'
    binary.mkdir()
    git = binary / 'git'
    git.write_text('''#!/usr/bin/env bash
case "$1" in
  cat-file) exit 0 ;;
  merge-base) [[ "${ANCESTOR_OK:-1}" == 1 ]] ;;
  diff) [[ "${SENSITIVE_CHANGED:-0}" == 0 ]] ;;
  *) echo "Unexpected git operation: $*" >&2; exit 2 ;;
esac
''')
    git.chmod(0o755)
    trace = tmp_path / 'trace'
    env = {**os.environ,
           'PATH':str(binary)+os.pathsep+os.environ['PATH'],
           'CMOS_REPO':str(tmp_path),'TRACE':str(trace),
           'CMOS_FORCE_FULL_BACKUP':'false',
           'CMOS_DEPLOY_RETRY_FROM':'a'*40,
           'CMOS_DEPLOY_RETRY_BACKUP':'/verified/recovery.dump',
           **overrides}
    result = subprocess.run(['bash',str(target / 'ensure-deploy-backup.sh'),'','b'*40],
                            env=env,text=True,capture_output=True,timeout=10,check=True)
    assert ('reusing named, checksum-verified retry backup' in result.stdout) is reuse
    assert ('FRESH_BACKUP' in result.stdout) is not reuse
    assert trace.exists() is verify_expected
    # The gate must never manufacture a successful application-release marker.
    assert not (tmp_path / 'last-successful-release').exists()


def test_retry_gate_shell_syntax():
    subprocess.run(['bash','-n',str(GATE)],check=True,timeout=5)
