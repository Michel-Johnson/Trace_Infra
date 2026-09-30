import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_collector_skill.py"


class InstallSkillTests(unittest.TestCase):
    def test_installs_trace_hunter_router_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory) / "skills"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--skill",
                    "trace-hunter",
                    "--skills-dir",
                    str(skills_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            target = skills_dir / "trace-hunter"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ROOT / "skills" / "trace-hunter")
            self.assertIn("Installed:", completed.stdout)

    def test_installs_trace_hunter_cli_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory) / "skills"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--skill",
                    "trace-hunter-cli",
                    "--skills-dir",
                    str(skills_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            target = skills_dir / "trace-hunter-cli"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ROOT / "skills" / "trace-hunter-cli")
            self.assertIn("Installed:", completed.stdout)

    def test_installs_trace_eval_designer_as_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory) / "skills"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--skill",
                    "trace-eval-designer",
                    "--skills-dir",
                    str(skills_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            target = skills_dir / "trace-eval-designer"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), ROOT / "skills" / "trace-eval-designer")
            self.assertIn("Installed:", completed.stdout)

    def test_preserves_existing_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            skills_dir = Path(temporary_directory) / "skills"
            target = skills_dir / "trace-eval-designer"
            target.mkdir(parents=True)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(INSTALLER),
                    "--skill",
                    "trace-eval-designer",
                    "--skills-dir",
                    str(skills_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Existing skill was preserved:", completed.stderr)
            self.assertTrue(target.is_dir())
            self.assertFalse(target.is_symlink())


if __name__ == "__main__":
    unittest.main()
