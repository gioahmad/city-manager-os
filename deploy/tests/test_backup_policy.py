from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_routine_deploy_uses_small_recovery_record_and_72h_full_backup_window():
    gate = (ROOT / "deploy" / "postgis" / "ensure-deploy-backup.sh").read_text()
    assert 'CMOS_DEPLOY_BACKUP_MAX_AGE_HOURS:-72' in gate
    sensitive = gate.split('SENSITIVE_PATHS=(', 1)[1].split(')', 1)[0]
    assert 'deploy/postgis/init' in sensitive
    assert 'dashboard/geo_resolver.py' not in sensitive
    assert 'dashboard/spatial_watch_app.py' not in sensitive
    assert 'release-snapshot.sh' in gate
    assert 'deploy-recovery' not in gate


def test_large_local_backup_retention_is_bounded():
    backup = (ROOT / "deploy" / "postgis" / "backup.sh").read_text()
    maintenance = (ROOT / "deploy" / "cmos-maintenance").read_text()
    source = (ROOT / "deploy" / "cmos-source").read_text()
    assert 'BACKUP_MAX_LOCAL_COUNT:-2' in backup
    assert 'BACKUP_RETENTION_DAYS:-10' in backup
    assert "database.sqlite.before_cmos_sources_*" in maintenance
    assert "backups[2:]" in source
