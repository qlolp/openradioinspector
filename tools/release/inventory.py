#!/usr/bin/env python3
"""Read-only Git inventory. Git PR refs do not establish open/closed or CI state."""
import argparse
import json
import subprocess
from pathlib import Path


def inventory(repository, prefix="refs/heads/"):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), '-c', 'core.quotePath=false', *args], text=True).strip()
    refs = dict(line.split(' ', 1) for line in git('for-each-ref', '--format=%(refname) %(objectname)').splitlines())
    branches = {r.removeprefix(prefix): sha for r, sha in refs.items() if r.startswith(prefix) and r != prefix+'HEAD'}
    main = branches['main']
    ancestors = {sha: set(git('rev-list', sha).splitlines()) for sha in set(branches.values())}
    rows = []
    for name, sha in sorted(branches.items()):
        base_candidates = [(len(ancestors[sha] - ancestors[other]), branch) for branch, other in branches.items()
                           if other != sha and other in ancestors[sha]]
        distance = min((n for n, _ in base_candidates), default=None)
        parents = sorted(branch for n, branch in base_candidates if n == distance)
        base = branches[parents[0]] if parents else git('merge-base', main, sha)
        paths = git('diff', '--name-only', base, sha).splitlines()
        equivalent = sorted(branch for branch, other in branches.items() if other == sha and branch != name)
        descendants = sorted(branch for branch, other in branches.items() if other != sha and sha in ancestors[other])
        rows.append(dict(branch=name, sha=sha, nearest_ancestors=parents, identical_heads=equivalent,
                         descendants=descendants, ahead_main=len(ancestors[sha] - ancestors[main]),
                         behind_main=len(ancestors[main] - ancestors[sha]), delta_base=base,
                         delta_commits=git('log', '--format=%h %s', base+'..'+sha).splitlines(),
                         delta_paths=paths, tree=git('rev-parse', sha+'^{tree}'),
                         distribution_in_tip=bool(git('ls-tree', '-r', '--name-only', sha, '--', 'CD Ligtht 32314')),
                         action='retain pending code review and GitHub metadata'))
    pulls = []
    for ref, sha in refs.items():
        if (ref.startswith('refs/pull/') or ref.startswith('refs/archive/github-pull/')) and ref.endswith('/head'):
            pulls.append(dict(number=int(ref.split('/')[-2]), head_sha=sha,
                              matching_branches=sorted(n for n, h in branches.items() if h == sha),
                              state='unknown', reviews='unknown', checks='unknown', base='unknown',
                              action='retain pending code review and GitHub metadata'))
    return dict(main_sha=main, branches=rows, pulls=sorted(pulls, key=lambda p:p['number']),
                tags={r:sha for r,sha in refs.items() if r.startswith('refs/tags/')},
                limitations=['Git refs do not establish PR state, review, checks, issues or release assets.'])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('repository', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--refs-prefix', default='refs/heads/')
    args = p.parse_args()
    data = inventory(args.repository, args.refs_prefix)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    print(f"{len(data['branches'])} branches; {len(data['pulls'])} PR heads; {len(data['tags'])} tags")


if __name__ == '__main__':
    main()
