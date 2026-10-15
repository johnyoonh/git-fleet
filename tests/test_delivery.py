import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/git_fleet"))
from publication_guard import github_slug, publication_block_reason
from verify_pr import verify, git, test_environment, task_lock, canonical, DOMAIN, load_checks


class PublicationTests(unittest.TestCase):
    def test_default_blocks_even_with_admin_bypass(self):
        calls = []
        def request(path):
            calls.append(path)
            return {"default_branch": "main", "permissions": {"admin": True}}
        self.assertIn("requires a PR", publication_block_reason("git@github.com:a/b.git", "main", request=request))
        self.assertEqual(len(calls), 1)

    def test_unknown_fails_closed(self):
        self.assertIn("fail closed", publication_block_reason("https://github.com/a/b", "topic", request=lambda p: {}))

    def test_ruleset_protects_nondefault(self):
        def request(path):
            if path.endswith("a/b"): return {"default_branch": "main"}
            if "/rules/" in path: return [{"type": "pull_request"}]
            return {"protected": False}
        self.assertIn("protected", publication_block_reason("https://github.com/a/b", "release/v1", request=request))

    def test_non_github_preserves_existing_policy(self):
        self.assertEqual(publication_block_reason("/tmp/bare.git", "main"), "")

    def test_remote_forms(self):
        for remote in ["git@github.com:a/b.git", "https://github.com/a/b.git", "ssh://git@github.com/a/b"]:
            self.assertEqual(github_slug(remote), "a/b")
        self.assertIsNone(github_slug("https://github.com.evil/a/b"))

    def test_no_secret_in_test_environment(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "JULES_A_API_KEY": "secret", "GIT_FLEET_EVIDENCE_KEY": "secret"}):
            env = test_environment("/tmp/test-home")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("JULES_A_API_KEY", env)
        self.assertNotIn("GIT_FLEET_EVIDENCE_KEY", env)

    def test_lease_exclusion(self):
        with tempfile.TemporaryDirectory() as d:
            with task_lock(Path(d), "r#1"):
                with self.assertRaises(RuntimeError):
                    with task_lock(Path(d), "r#1"): pass
            with task_lock(Path(d), "r#1"): pass


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "initial").write_text("base")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "switch", "-c", "chatgpt/test")
        (self.repo / "feature").write_text("feature")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "feature")
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "switch", "main")
        self.revs = {"head_sha": head, "base_sha": base}
        (self.repo / "dirty-local-work").write_text("keep me")

    def tearDown(self):
        self.temp.cleanup()

    def run_checks(self, code, **kwargs):
        return verify(self.repo, "a/b", 7, self.revs, [[sys.executable, "-c", code]],
                      self.root / "receipt.json", root=self.root / "worktrees", **kwargs)

    def test_actual_merge_and_signed_receipt(self):
        result = self.run_checks("from pathlib import Path; assert Path('feature').read_text() == 'feature'", signing_key="test-key")
        self.assertEqual(result["status"], "passed", result)
        signature = result.pop("signature")
        self.assertEqual(signature, hmac.new(b"test-key", DOMAIN + canonical(result), hashlib.sha256).hexdigest())
        self.assertEqual(git(self.repo, "branch", "--show-current").stdout.strip(), "main")
        self.assertEqual((self.repo / "dirty-local-work").read_text(), "keep me")
        self.assertEqual(result["merge_authorization"], "none")

    def test_failed_tests_keep_worktree(self):
        result = self.run_checks("raise SystemExit(1)")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(Path(result["worktree"]).exists())

    def test_ref_movement_invalidates(self):
        result = self.run_checks("pass", reread=lambda: {**self.revs, "head_sha": "f" * 40})
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed", result["error"])

    def test_modified_tracked_files_invalidates(self):
        result = self.run_checks("from pathlib import Path; Path('feature').write_text('tampered')")
        self.assertEqual(result["status"], "failed")

    def test_timeout(self):
        result = self.run_checks("import time; time.sleep(2)", timeout=0.05)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["checks"][0]["timed_out"])

    def test_empty_plan_rejected(self):
        p = self.root / "checks.json"
        p.write_text("[]")
        with self.assertRaises(ValueError): load_checks(p)

class RepairIntegrationTests(unittest.TestCase):
    setUp = VerifyTests.setUp
    tearDown = VerifyTests.tearDown
    def test_repair_merges_into_original_source_lineage(self):
        from integrate_fix import compose
        git(self.repo, "switch", "-c", "repair", self.revs["head_sha"])
        (self.repo / "repair").write_text("fix")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "repair")
        fix = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        git(self.repo, "switch", "main")
        receipt = verify(self.repo, "a/b", 7, self.revs,
            [[sys.executable, "-c", "from pathlib import Path; assert Path('repair').read_text() == 'fix'"]],
            self.root / "combined.json", root=self.root / "verify", include=[fix])
        self.assertEqual(receipt["status"], "passed")
        new_head = compose(self.repo, self.root / "integration", self.revs, [fix], receipt["merge_tree_sha"], 7)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", self.revs["head_sha"], new_head).returncode, 0)
        self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", fix, new_head).returncode, 0)


if __name__ == "__main__":
    unittest.main()
