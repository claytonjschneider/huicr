import os
from pathlib import Path
import secrets
import shutil
import string
import subprocess
import unittest

from test_review import RepositoryFixture


@unittest.skipUnless(shutil.which("lefthook") and shutil.which("gitleaks"), "optional hook tools not installed")
class HookTest(RepositoryFixture):
    def test_staged_secrets_block_but_unstaged_content_is_not_scanned(self):
        source = Path(__file__).resolve().parents[1] / "lefthook.yml"
        shutil.copyfile(source, self.root / "lefthook.yml")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("LEFTHOOK", "GITLEAKS"))}
        subprocess.run(["lefthook", "install"], cwd=self.root, env=env, check=True, capture_output=True)
        # Synthetic, randomly generated test data, never a real credential and
        # never written to this project's index or Git history.
        token = "ghp_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))
        self.write("example.env", f'GITHUB_TOKEN="{token}"\n')
        self.git("add", "example.env")
        self.write("example.env", "clean worktree version\n")
        blocked = subprocess.run([str(self.root / ".git/hooks/pre-commit")], cwd=self.root, env=env, capture_output=True)
        self.assertNotEqual(blocked.returncode, 0, "The installed hook must inspect the staged blob, not the clean worktree")
        self.git("add", "example.env")
        self.write("example.env", f'GITHUB_TOKEN="{token}"\n')
        clean = subprocess.run([str(self.root / ".git/hooks/pre-commit")], cwd=self.root, env=env, capture_output=True)
        self.assertEqual(clean.returncode, 0, clean.stderr.decode(errors="replace"))
