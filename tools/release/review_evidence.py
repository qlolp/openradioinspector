"""Validate sanitized proposed evidence before a human reviews its exact bytes in Actions."""
import argparse
import os
from pathlib import Path

from . import release_acceptance as gate
from .macos_rc_package import digest, read_json
from .publish_release import BUILDER, Repository, collect_dispatch_file
from .release_artifacts import ARTIFACTS, check_manifest, require, verify_sums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preview', 'approval'])
    parser.add_argument('platform', choices=ARTIFACTS)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--run-id', type=int)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    repo = Repository()
    if args.operation == 'approval':
        reviews = repo.api(f"actions/runs/{int(os.environ['GITHUB_RUN_ID'])}/approvals")
        require(any(row['state'] == 'approved' and any(e['name'] == 'release-acceptance' for e in row['environments'])
                    for row in reviews), 'an actual GitHub environment reviewer must approve the bench evidence')
        require(args.out.is_file() and digest(args.out) == os.environ['REVIEWED_DIGEST'], 'reviewed receipt changed')
        return
    repo.run(args.run_id, BUILDER, args.platform)
    name = ARTIFACTS[args.platform]
    manifest = args.candidate / f'{args.platform}-manifest.json'
    verify_sums(args.candidate, {name, manifest.name})
    build = read_json(manifest)
    check_manifest(build, args.platform, repo.revision, args.candidate / name)
    collect_dispatch_file(args.out.parent, args.out.name, 'ORI_RECEIPT_B64', 48 * 1024)
    receipt = read_json(args.out)
    gate.validate_receipt(receipt, args.platform, repo.revision, build['artifact']['sha256'], digest(manifest), args.run_id)
    for workflow, run_id in receipt['software_runs'].items():
        repo.run(run_id, workflow)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
        out.write(f'digest={digest(args.out)}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('## Target bench evidence for operator review\n\n')
        summary.write(f"Package `{name}` from `{repo.revision}`\n\nSHA-256 `{build['artifact']['sha256']}`\n\n")
        summary.write(f"Receipt SHA-256 `{digest(args.out)}`\n\n")
        summary.write('Compare the following measurements and report hashes to the private bench originals before approving.\n\n')
        # The strict schema accepts no operator-provided prose, paths or private identifiers.
        import json
        summary.write('```json\n' + json.dumps(receipt, indent=2, sort_keys=True) + '\n```\n')


if __name__ == '__main__':
    main()
