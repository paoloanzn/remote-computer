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
            "plan-cost",
            "render-cost-guard",
            "cost-status",
            "compute budget",
            "systemctl is-enabled remote-computer-cost-guard.timer",
            "systemctl is-enabled remote-computer-cost-guard.service",
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
        self.assertIn("--user-data file://<cost-guard-script>", aws)
        self.assertIn("--credit-specification CpuCredits=standard", aws)
        self.assertIn("startup-script=<cost-guard-script>", gcp)
        self.assertIn("does not cap the total cloud bill", aws + gcp)
        self.assertIn("60-second minimum", aws)
        self.assertIn("60-second minimum", gcp)
        self.assertIn("PENDING_STOP", gcp)

    def test_single_lifecycle_budget_boundary_is_explicit(self):
        readme = (ROOT / "README.md").read_text()
        aws = (ROOT / "references" / "aws.md").read_text()
        gcp = (ROOT / "references" / "gcp.md").read_text()
        registry = (ROOT / "references" / "registry.md").read_text()
        phrase = "single uninterrupted provider billing lifecycle"
        for name, content in (
            ("README", readme),
            ("SKILL", self.skill),
            ("AWS", aws),
            ("GCP", gcp),
            ("registry", registry),
        ):
            with self.subTest(name=name):
                self.assertIn(phrase, content)

    def test_github_reference_preserves_key_roles(self):
        github = (ROOT / "references" / "github.md").read_text()
        self.assertIn("GitHub authentication", github)
        self.assertIn("Commit signing", github)
        self.assertIn("github-upload-key", github)
        self.assertIn("Never replace or remove signing configuration", github)

    def test_default_prompt_names_skill(self):
        metadata = (ROOT / "agents" / "openai.yaml").read_text()
        self.assertIn("$remote-computer", metadata)


if __name__ == "__main__":
    unittest.main()
