import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_remote_install_defaults_to_the_fork(self):
        script = (ROOT / "install-skill.sh").read_text()
        readme = (ROOT / "README.md").read_text()
        self.assertIn('REPO="${REPO:-aidvgg/remote-computer}"', script)
        self.assertIn("raw.githubusercontent.com/aidvgg/remote-computer/main/install-skill.sh", readme)

    def test_ci_uses_the_runtime_only_script_boundary(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertNotIn('diff -ru scripts "$installed/scripts"', ci)
        self.assertIn('diff -u scripts/vm_bookkeeper.py "$installed/scripts/vm_bookkeeper.py"', ci)
        self.assertIn('test ! -e "$installed/scripts/quick_validate.py"', ci)

    def test_local_install_and_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary) / "skills"
            env = os.environ.copy()
            env.update({"SOURCE_DIR": str(ROOT), "SKILLS_DIR": str(skills), "AGENT": "codex"})
            for _ in range(2):
                result = subprocess.run(["sh", str(ROOT / "install-skill.sh")], env=env, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
            installed = skills / "remote-computer"
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertTrue((installed / "scripts" / "vm_bookkeeper.py").is_file())
            self.assertTrue((installed / "scripts" / "remote_github_setup.py").is_file())
            self.assertTrue((installed / "references" / "github.md").is_file())
            self.assertFalse((installed / "tests").exists())
            self.assertFalse((installed / ".git").exists())
            self.assertFalse((installed / "check").exists())
            self.assertFalse((installed / "scripts" / "quick_validate.py").exists())


if __name__ == "__main__":
    unittest.main()
