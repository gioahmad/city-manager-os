import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch


DEPLOY_PATH = Path(__file__).resolve().parents[2] / "deploy" / "cmos-deploy"
LOADER = SourceFileLoader("cmos_deploy_rollback_test", str(DEPLOY_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_existing_service_image_is_tagged_for_rollback():
    with patch.object(MODULE, "_image_exists", return_value=True), patch.object(MODULE, "run") as run:
        MODULE._preserve_service_image("citymanager-dashboard", "sha256:present", "rollback:dashboard")

    run.assert_called_once_with(
        ["docker", "image", "tag", "sha256:present", "rollback:dashboard"]
    )


def test_pruned_service_image_falls_back_to_running_container_snapshot():
    with patch.object(MODULE, "_image_exists", return_value=False), patch.object(MODULE, "run") as run:
        MODULE._preserve_service_image("citymanager-integration-engine", "sha256:pruned", "rollback:engine")

    run.assert_called_once_with(
        [
            "docker",
            "commit",
            "--pause=false",
            "citymanager-integration-engine",
            "rollback:engine",
        ]
    )
