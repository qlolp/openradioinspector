"""Audit the server-side prerequisites for public release, or initialize missing controls."""
import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote

# Native package freeze, witness approval and promotion use separate environments.
# A build with access to a signing key also requires a human review.
ENVIRONMENTS = {
    "release-build-macos": True,
    "release-build-windows": True,
    "release-build-pi": False,
    "release-build-linux-amd64": False,
    "release-acceptance": True,
    "release-publication": True,
    "android-field-signing": True,
    "android-field-review": True,
    "android-field-publication": True,
}
MAIN_ONLY = {"protected_branches": False, "custom_branch_policies": True}
RELEASE_TAG_RULE = "OpenRadioInspector distribution tags"
RELEASE_TAG_REFS = [
    "refs/tags/v0.4.0", "refs/tags/v0.4.0-macos.rc.*", "refs/tags/v0.4.0-windows.rc.*",
    "refs/tags/v0.4.0-pi.rc.*", "refs/tags/linux-amd64-v0.4.0",
    "refs/tags/linux-amd64-v0.4.0-rc.*", "refs/tags/android-field-v0.1.0",
]



def pages(api, path, key=None):
    rows = []
    page = 1
    while True:
        response = api("GET", f"{path}?per_page=100&page={page}")
        chunk = response[key] if key else response
        rows.extend(chunk)
        if len(chunk) < 100:
            return rows
        page += 1


def protect_release_tags(api, *, apply):
    """Prevent a ref move between the publisher's last read and GitHub's publish operation."""
    matches = [r for r in pages(api, "rulesets") if r["name"] == RELEASE_TAG_RULE]
    if not matches and apply:
        matches = [api("POST", "rulesets", {
            "name": RELEASE_TAG_RULE, "target": "tag", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": RELEASE_TAG_REFS, "exclude": []}},
            "rules": [{"type": "update"}, {"type": "deletion"}],
        })]
    if len(matches) != 1:
        return ["expected a single release tag ruleset"]
    rule = api("GET", f"rulesets/{matches[0]['id']}")
    refs = rule.get("conditions", {}).get("ref_name", {})
    if (rule.get("target") != "tag" or rule.get("enforcement") != "active"
            or rule.get("bypass_actors") != [] or refs.get("exclude") != []
            or set(refs.get("include", [])) != set(RELEASE_TAG_REFS)
            or {r["type"] for r in rule.get("rules", [])} != {"update", "deletion"}):
        return ["expected the release tag ruleset to restrict updates and deletions for distribution tags"]
    return []


def configure(api, reviewer_id, *, apply=False):
    if api("GET", "branches/main").get("protected") is not True:
        return ["main must be protected before release environments can trust it"]
    issues = []
    if api("GET", "immutable-releases").get("enabled") is not True:
        if apply:
            api("PUT", "immutable-releases")
        if not apply or api("GET", "immutable-releases").get("enabled") is not True:
            issues.append("immutable releases disabled")

    issues.extend(protect_release_tags(api, apply=apply))
    existing = {row["name"] for row in pages(api, "environments", "environments")}
    for name, human_review in ENVIRONMENTS.items():
        path = f"environments/{name}"
        if name not in existing:
            if not apply:
                issues.append(f"{name}: environment missing")
                continue
            api("PUT", path, {"deployment_branch_policy": MAIN_ONLY,
                              "reviewers": [{"type": "User", "id": reviewer_id}] if human_review else []})
        env = api("GET", path)
        if env.get("deployment_branch_policy") != MAIN_ONLY:
            issues.append(f"{name}: expected a custom deployment branch allowlist")
            continue
        if human_review:
            reviewers = [item for rule in env["protection_rules"] if rule["type"] == "required_reviewers"
                         for item in rule["reviewers"]]
            if len(reviewers) != 1 or reviewers[0].get("type") != "User" or reviewers[0]["reviewer"]["id"] != reviewer_id:
                issues.append(f"{name}: expected the designated release reviewer")
                continue
        branches_path = f"{path}/deployment-branch-policies"
        branches = pages(api, branches_path, "branch_policies")
        if not branches and apply:
            api("POST", branches_path, {"type": "branch", "name": "main"})
            branches = pages(api, branches_path, "branch_policies")
        if {(b.get("type"), b["name"]) for b in branches} != {("branch", "main")}:
            issues.append(f"{name}: expected main as the only allowed branch and no allowed tags")
    return issues


def github(method, endpoint, body=None):
    args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
    if body is not None:
        args.extend(["--input", "-"])
    result = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{method} {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--reviewer", required=True, help="GitHub login of the bench operator")
    parser.add_argument("--apply", action="store_true", help="Enable immutability and initialize missing distribution tag and environment rules")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or not re.fullmatch(r"[\w-]+", args.reviewer):
        parser.error("invalid GitHub repository or reviewer")
    try:
        user = github("GET", f"users/{quote(args.reviewer)}")
        if user.get("type") != "User":
            raise ValueError("the release reviewer must be a user")
        def api(method, path, body=None):
            return github(method, f"repos/{args.repo}/{path}", body)
        permission = api("GET", f"collaborators/{quote(args.reviewer)}/permission").get("permission")
        if permission not in {"read", "triage", "write", "maintain", "admin"}:
            raise ValueError("the release reviewer must have repository access")
        issues = configure(api, user["id"], apply=args.apply)
    except (RuntimeError, ValueError, OSError, KeyError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if issues:
        print("\n".join(issues), file=sys.stderr)
        return 1
    print(f"{args.repo}: immutable releases, distribution tag protection and {len(ENVIRONMENTS)} main-only environments verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
