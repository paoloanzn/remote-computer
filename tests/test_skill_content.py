import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SkillContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (ROOT / "SKILL.md").read_text()

    def test_required_workflow_is_present(self):
        required = (
            "aws sts get-caller-identity",
            "gcloud auth list",
            "t3.medium",
            "e2-medium",
            "e2-micro",
            "create-key",
            "~/.vms.json",
            "vm_bookkeeper.py sync",
            "github-discover --verify-ssh",
            "github_auth",
            "references/github.md",
            "pbcopy",
            "wl-copy",
            "Create these cloud resources now?",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, self.skill)

    def test_skill_has_no_scaffold_placeholders(self):
        self.assertNotIn("TODO", self.skill)
        self.assertNotIn("[TODO:", self.skill)

    def test_provider_references_use_official_sources(self):
        aws = (ROOT / "references" / "aws.md").read_text()
        gcp = (ROOT / "references" / "gcp.md").read_text()
        self.assertIn("https://docs.aws.amazon.com/", aws)
        self.assertIn("https://cloud.google.com/", gcp)
        self.assertNotIn("0.0.0.0/0", aws + gcp)

    def test_github_reference_preserves_key_roles(self):
        github = (ROOT / "references" / "github.md").read_text()
        self.assertIn("GitHub authentication", github)
        self.assertIn("Commit signing", github)
        self.assertIn("github-upload-key", github)
        self.assertIn("github-install-gpg", github)
        self.assertIn("github-verify-gpg", github)
        self.assertIn("pinentry-curses", github)
        self.assertIn("gpg-agent", github)
        self.assertIn("Never replace or remove signing configuration", github)

    def test_default_prompt_names_skill(self):
        metadata = (ROOT / "agents" / "openai.yaml").read_text()
        self.assertIn("$remote-computer", metadata)


if __name__ == "__main__":
    unittest.main()
