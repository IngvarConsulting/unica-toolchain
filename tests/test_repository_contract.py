from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_build_module():
    script = REPO_ROOT / "scripts" / "build_rlm.py"
    spec = importlib.util.spec_from_file_location("build_rlm", script)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepositoryContractTests(unittest.TestCase):
    def test_checked_in_manifest_pins_first_rlm_release(self) -> None:
        module = load_build_module()
        manifest = module.load_manifest(REPO_ROOT / "manifests" / "rlm-tools-bsl.json")
        self.assertEqual(manifest["upstreamCommit"], "dcfff95ce678f49971b14d8acd82b042a6855470")
        self.assertEqual(module.release_tag(manifest), "rlm-tools-bsl-v1.26.0-build.2")

    def test_release_workflow_builds_only_on_explicit_dispatch(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "release-rlm.yml").read_text(encoding="utf-8")
        trigger = text.split("permissions:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger)
        self.assertNotIn("pull_request:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertIn("uv sync --frozen --no-dev", text)
        self.assertIn('"pyinstaller==${PYINSTALLER_VERSION}"', text)
        self.assertIn("softprops/action-gh-release@v3", text)

    def test_ci_workflow_checks_python_contracts_without_building_rlm(self) -> None:
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests", text)
        self.assertIn("python -m py_compile scripts/*.py tests/*.py", text)
        self.assertNotIn("PyInstaller", text)


if __name__ == "__main__":
    unittest.main()
