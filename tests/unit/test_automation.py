"""Verify pre-commit checks exercise staged content and reject broken snapshots."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from scripts.check_staged import run_staged_checks


class StagedCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="staged-check-test-")
        self.root = Path(directory.name).resolve()
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(directory.cleanup)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        for name in ("scripts", "src/market_catalog"):
            (self.root / name).mkdir(parents=True)
        (self.root / "scripts/__init__.py").write_text("", encoding="utf-8")
        (self.root / "src/market_catalog/__init__.py").write_text("", encoding="utf-8")
        (self.root / "scripts/check.py").write_text('''import argparse,json,pathlib,sys
p=argparse.ArgumentParser();p.add_argument('--suite');p.add_argument('--report');a=p.parse_args()
ok=pathlib.Path('value.txt').read_text()=='pass'
report=pathlib.Path(a.report);report.parent.mkdir(parents=True,exist_ok=True)
report.write_text(json.dumps({'successful':ok,'package_path':str(pathlib.Path('src/market_catalog/__init__.py').resolve())}))
sys.exit(0 if ok else 1)
''', encoding="utf-8")

    def stage(self, value):
        (self.root / "value.txt").write_text(value, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True, capture_output=True)

    def check(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return run_staged_checks(self.root)

    def test_passing_staged_source_ignores_broken_working_copy(self):
        self.stage("pass")
        (self.root / "value.txt").write_text("fail", encoding="utf-8")
        self.assertEqual(self.check(), 0)

    def test_broken_staged_source_cannot_be_hidden_by_unstaged_fix(self):
        self.stage("fail")
        (self.root / "value.txt").write_text("pass", encoding="utf-8")
        self.assertNotEqual(self.check(), 0)

    def test_missing_staged_runner_is_rejected(self):
        self.stage("pass")
        subprocess.run(["git", "-C", str(self.root), "rm", "--cached", "scripts/check.py"], check=True, capture_output=True)
        self.assertNotEqual(self.check(), 0)
