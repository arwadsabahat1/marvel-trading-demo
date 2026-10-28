from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from public_files import allowed_name, audit, build_archive, findings, parse_manifest, plain_file


class PublicationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".test-tmp").mkdir(exist_ok=True)

    def test_runtime_and_credential_paths_cannot_enter_manifest(self):
        for name in (".env", "var/paper.sqlite3", "reports/account.json", "../secrets.txt",
                     "/etc/passwd", "config/credentials.json", "scripts/local_account_setup.py"):
            with self.subTest(name=name):
                self.assertFalse(allowed_name(name))

    def test_duplicate_or_unsorted_manifest_is_rejected(self):
        for document in ("README.md\nREADME.md\n", "README.md\n.gitignore\n", "../README.md\n"):
            with self.assertRaises(ValueError):
                parse_manifest(document)

    def test_credential_patterns_report_labels_without_secret_values(self):
        for text, expected in (("ghp_" + "A" * 36, "github_token"),
                               ("api_key=" + repr("Z" * 64), "literal_api_credential")):
            result = findings(text)
            self.assertIn(expected, result)
            self.assertNotIn(text, str(result))
        self.assertEqual(findings('key="K" * 64; secret="S" * 64'), [])

    def test_personal_path_patterns_catch_constructed_machine_details(self):
        private_path = "C:" + chr(92) + "Users" + chr(92) + "Example" + chr(92) + "project"
        private_ip = ".".join(("192", "168", "1", "42"))
        self.assertIn("personal_windows_path", findings(private_path))
        self.assertIn("private_ipv4", findings(private_ip))

    def test_archive_includes_only_manifest_even_when_local_secrets_exist(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".test-tmp") as folder:
            root = Path(folder)
            (root / "README.md").write_text("A synthetic publication fixture.\n")
            (root / "PUBLIC_FILES.txt").write_text("PUBLIC_FILES.txt\nREADME.md\n")
            (root / ".env").write_text("LOCAL_FIXTURE_ONLY=not-a-real-secret\n")
            (root / "var").mkdir()
            (root / "var/account.json").write_text('{"fixture": true}')
            result = build_archive(root)
            with ZipFile(root / result["archive"]) as archive:
                self.assertEqual(archive.namelist(), ["marvel-trading-demo/PUBLIC_FILES.txt",
                                                      "marvel-trading-demo/README.md"])
            self.assertFalse(audit(root)["runtime_and_private_files_included"])

    def test_source_link_outside_repository_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".test-tmp") as folder:
            root = Path(folder)
            (root / "outside.txt").write_text("fixture")
            (root / "repo").mkdir()
            try:
                (root / "repo/README.md").symlink_to(root / "outside.txt")
            except OSError:
                self.skipTest("Creating symbolic links is unavailable on this platform")
            with self.assertRaises(ValueError):
                plain_file(root / "repo", "README.md")

    @unittest.skipUnless(shutil.which("git"), "Git is not installed")
    def test_force_staged_secret_is_rejected_despite_ignore_rules(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".test-tmp") as folder:
            root = Path(folder)
            (root / "README.md").write_text("Synthetic fixture\n")
            (root / "PUBLIC_FILES.txt").write_text("PUBLIC_FILES.txt\nREADME.md\n")
            (root / ".env").write_text("FIXTURE_ONLY=local\n")
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "--force", "--", ".env"], cwd=root, check=True, capture_output=True)
            with self.assertRaises(ValueError):
                audit(root)
