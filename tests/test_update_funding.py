import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_funding as funding


class FundingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.policy = {
            "owner": "jeremykenedy", "personal_topic": "personal-project",
            "excluded_patterns": [], "excluded_names": ["laravel-seedster"],
            "reviewed_origins": {"personal": "root"}, "legacy_origins": {},
        }
        self.repo = {
            "name": "personal", "full_name": "jeremykenedy/personal",
            "owner": {"login": "jeremykenedy"}, "fork": False,
            "private": False, "visibility": "public", "archived": False,
            "disabled": False, "topics": [], "default_branch": "main",
        }
        self.root = {
            "sha": "root", "parents": [], "author": {"login": "jeremykenedy"},
            "commit": {"author": {"name": "jeremykenedy"}},
        }

    def test_explicit_public_original_owner_required(self):
        for values in [{"fork": True}, {"fork": None}, {"private": True},
                       {"private": None}, {"visibility": "private"},
                       {"visibility": None}, {"owner": {"login": "other"}}]:
            with self.subTest(values=values):
                self.assertIsNotNone(funding.exclusion(self.repo | values, self.policy))

    def test_work_exclusions_override_review_and_topic(self):
        for values in [{"name": "care-link"}, {"description": "CareLink API"},
                       {"homepage": "https://spotr.example"}, {"topics": ["spotr", "personal-project"]}]:
            with self.subTest(values=values):
                self.assertEqual(funding.exclusion(self.repo | values, self.policy), "work exclusion")

    def test_import_denial_overrides_review_and_topic(self):
        self.policy["reviewed_origins"]["laravel-seedster"] = "root"
        repo = self.repo | {"name": "laravel-seedster", "topics": ["personal-project"]}
        self.assertIsNotNone(funding.exclusion(repo, self.policy))

    def test_new_projects_require_personal_marker(self):
        repo = self.repo | {"name": "new-project"}
        self.assertIsNotNone(funding.exclusion(repo, self.policy))
        self.assertIsNone(funding.exclusion(repo | {"topics": ["personal-project"]}, self.policy))

    def test_controller_and_archived_repositories_are_excluded(self):
        for values in [{"name": ".github", "topics": ["personal-project"]},
                       {"archived": True}, {"archived": None}, {"disabled": True}]:
            self.assertIsNotNone(funding.exclusion(self.repo | values, self.policy))

    def test_foreign_root_and_changed_reviewed_origin_rejected(self):
        for root in [self.root | {"author": {"login": "other"}},
                     self.root | {"sha": "changed"}, self.root | {"parents": [{"sha": "other"}]}]:
            with patch.object(funding, "oldest_commit", return_value=root):
                with self.assertRaises(RuntimeError):
                    funding.verify_origin(self.repo, self.policy)

    def test_legacy_origin_requires_exact_root_and_attributed_proof(self):
        self.policy["legacy_origins"]["personal"] = {"root_commit": "root", "personal_commit": "proof"}
        root = self.root | {"author": None}
        with patch.object(funding, "oldest_commit", return_value=root), patch.object(
            funding, "request", return_value={"author": {"login": "jeremykenedy"}}
        ):
            self.assertEqual(funding.verify_origin(self.repo, self.policy), "root")
        with patch.object(funding, "oldest_commit", return_value=root), patch.object(
            funding, "request", return_value={"author": {"login": "other"}}
        ):
            with self.assertRaises(RuntimeError):
                funding.verify_origin(self.repo, self.policy)

    def test_metadata_becoming_private_prevents_all_mutations(self):
        calls = []
        def request(endpoint, method="GET", **kwargs):
            calls.append(method)
            return self.repo | {"private": True, "visibility": "private"}
        with patch.object(funding, "request", side_effect=request):
            self.assertTrue(funding.update_repository(self.repo, self.policy, "template", True).startswith("skipped:"))
        self.assertEqual(calls, ["GET"])

    def test_second_metadata_check_stops_mutation(self):
        with patch.object(funding, "request", side_effect=[self.repo, self.repo | {"fork": True}]) as api, \
             patch.object(funding, "verify_origin", return_value="root"), \
             patch.object(funding, "verify_new_project"), \
             patch.object(funding, "file_contents", return_value=None), \
             patch.object(funding, "sponsor_state", return_value={"hasSponsorshipsEnabled": False}):
            with self.assertRaises(RuntimeError):
                funding.update_repository(self.repo, self.policy, "template", True)
            self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in api.call_args_list))

    def test_dry_run_never_writes(self):
        with patch.object(funding, "request", return_value=self.repo) as api, \
             patch.object(funding, "verify_origin", return_value="root"), \
             patch.object(funding, "verify_new_project"), \
             patch.object(funding, "file_contents", return_value=None), \
             patch.object(funding, "sponsor_state", return_value={"hasSponsorshipsEnabled": False}):
            self.assertEqual(funding.update_repository(self.repo, self.policy, "template"), "would update")
            self.assertEqual(api.call_count, 1)

    def test_current_funding_creates_no_commit(self):
        state = {"hasSponsorshipsEnabled": True, "fundingLinks": [{"url": url} for url in funding.EXPECTED_URLS]}
        with patch.object(funding, "request", return_value=self.repo) as api, \
             patch.object(funding, "verify_origin", return_value="root"), \
             patch.object(funding, "verify_new_project"), \
             patch.object(funding, "file_contents", return_value={}), \
             patch.object(funding, "decode", return_value="template"), \
             patch.object(funding, "sponsor_state", return_value=state):
            self.assertEqual(funding.update_repository(self.repo, self.policy, "template", True), "current")
            self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))

    def test_canonical_lowercase_readme_import_detected(self):
        repo = self.repo | {"name": "new-project"}
        with patch.object(funding, "request", return_value=[{"name": "readme.md", "path": "readme.md"}]), \
             patch.object(funding, "file_contents", return_value={}), \
             patch.object(funding, "decode", return_value="This package was forked from another project."):
            with self.assertRaises(RuntimeError):
                funding.verify_new_project(repo, self.policy)

    def test_external_package_author_requires_manual_review(self):
        repo = self.repo | {"name": "new-project"}
        with patch.object(funding, "request", return_value=[{"name": "composer.json", "path": "composer.json"}]), \
             patch.object(funding, "file_contents", return_value={}), \
             patch.object(funding, "decode", return_value=json.dumps({"name": "jeremykenedy/new", "authors": [{"name": "Other Developer"}]})):
            with self.assertRaises(RuntimeError):
                funding.verify_new_project(repo, self.policy)

    def test_work_content_blocks_new_projects(self):
        repo = self.repo | {"name": "new-project"}
        with patch.object(funding, "request", return_value=[{"name": "README.md", "path": "README.md"}]), \
             patch.object(funding, "file_contents", return_value={}), \
             patch.object(funding, "decode", return_value="CareLink documentation"):
            with self.assertRaises(RuntimeError):
                funding.verify_new_project(repo, self.policy)

    def test_controller_has_no_inherited_funding_file(self):
        self.assertFalse((funding.ROOT / funding.FUNDING_PATH).exists())
        self.assertFalse((funding.ROOT / "FUNDING.yml").exists())
        self.assertTrue((funding.ROOT / "funding/FUNDING.yml").is_file())

    def test_published_template_contains_all_required_platforms(self):
        lines = (funding.ROOT / "funding/FUNDING.yml").read_text().splitlines()
        entries = dict(line.split(": ", 1) for line in lines if line and not line.startswith("#"))
        self.assertEqual(entries["github"], "jeremykenedy")
        self.assertEqual(entries["patreon"], "jeremykenedy")
        self.assertEqual(entries["ko_fi"], "jeremykenedy")
        self.assertEqual(json.loads(entries["custom"]), [
            "https://www.paypal.com/paypalme/jeremykenedy",
            "https://www.buymeacoffee.com/jeremykenedy",
        ])


if __name__ == "__main__":
    unittest.main()
