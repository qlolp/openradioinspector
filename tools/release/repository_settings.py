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


def pages(api, path, key):
    rows = []
    page = 1
    while True:
        chunk = api("GET", f"{path}?per_page=100&page={page}")[key]
        rows.extend(chunk)
        if len(chunk) < 100:
            return rows
        page += 1


def configure(api, reviewer_id, *, apply=False):
    if api("GET", "branches/main").get("protected") is not True:
        return ["main must be protected before release environments can trust it"]
    issues = []
    if api("GET", "immutable-releases").get("enabled") is not True:
        if apply:
            api("PUT", "immutable-releases")
        if not apply or api("GET", "immutable-releases").get("enabled") is not True:
            issues.append("immutable releases disabled")

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
    parser.add_argument("--apply", action="store_true", help="Enable immutability and initialize missing environments")
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
    print(f"{args.repo}: immutable releases and {len(ENVIRONMENTS)} main-only environments verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
