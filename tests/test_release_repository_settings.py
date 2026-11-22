"""Repository preparation must preserve established controls and restrict deployments to main."""
import unittest

from tools.release import repository_settings as policy


class FakeGitHub:
    def __init__(self):
        self.immutable = False
        self.environments = {}
        self.branches = {}
        self.rulesets = {}
        self.writes = []

    def __call__(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path))
        if path == "branches/main":
            return {"protected": True}
        if path == "immutable-releases":
            if method == "PUT":
                self.immutable = True
            return {"enabled": self.immutable}
        if path.startswith("rulesets?"):
            return list(self.rulesets.values())
        if path == "rulesets" and method == "POST":
            rule_id = len(self.rulesets) + 1
            self.rulesets[rule_id] = dict(body, id=rule_id)
            return self.rulesets[rule_id]
        if path.startswith("rulesets/"):
            return self.rulesets[int(path.split("/")[1])]
        if path.startswith("environments?"):
            return {"environments": list(self.environments.values())}
        _, name, *suffix = path.split("/")
        if not suffix:
            if method == "PUT":
                rules = [{"type": "required_reviewers", "reviewers": [
                    {"type": "User", "reviewer": {"id": row["id"]}} for row in body["reviewers"]]}] if body["reviewers"] else []
                self.environments[name] = {"name": name, "deployment_branch_policy": body["deployment_branch_policy"], "protection_rules": rules}
                self.branches[name] = []
            return self.environments[name]
        if method == "POST":
            self.branches[name].append(body)
        return {"branch_policies": self.branches[name]}


class RepositorySettingsTests(unittest.TestCase):
    def test_initialization_then_read_only_verification(self):
        api = FakeGitHub()
        self.assertTrue(policy.configure(api, 123, apply=False))
        self.assertEqual(api.writes, [])
        self.assertEqual(policy.configure(api, 123, apply=True), [])
        api.writes.clear()
        self.assertEqual(policy.configure(api, 123, apply=True), [])
        self.assertEqual(api.writes, [])
        self.assertEqual(api.branches["release-acceptance"], [{"type": "branch", "name": "main"}])
        self.assertEqual(api.environments["android-field-review"]["protection_rules"][0]["reviewers"][0]["reviewer"]["id"], 123)

    def test_existing_tag_or_extra_branch_remains_visible_and_is_not_overwritten(self):
        api = FakeGitHub()
        policy.configure(api, 123, apply=True)
        api.branches["release-publication"].append({"type": "tag", "name": "main"})
        api.writes.clear()
        issues = policy.configure(api, 123, apply=True)
        self.assertTrue(any("release-publication" in item for item in issues))
        self.assertEqual(api.writes, [])

    def test_release_tag_updates_and_deletions_are_restricted_without_blocking_creation(self):
        api = FakeGitHub()
        policy.configure(api, 123, apply=True)
        ruleset, = api.rulesets.values()
        self.assertEqual(ruleset["target"], "tag")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual({r["type"] for r in ruleset["rules"]}, {"update", "deletion"})
        self.assertCountEqual(ruleset["conditions"]["ref_name"]["include"], [
            "refs/tags/v0.4.0", "refs/tags/v0.4.0-macos.rc.*", "refs/tags/v0.4.0-windows.rc.*",
            "refs/tags/v0.4.0-pi.rc.*", "refs/tags/linux-amd64-v0.4.0",
            "refs/tags/linux-amd64-v0.4.0-rc.*", "refs/tags/android-field-v0.1.0",
        ])
        ruleset["rules"] = [{"type": "update"}]
        api.writes.clear()
        self.assertTrue(any("tag" in error for error in policy.configure(api, 123, apply=True)))
        self.assertEqual(api.writes, [])
