"""Explicit real-runtime acceptance; never replaced by synthetic components.

Prepare local runtimes/assets and binary build provenance BEFORE this command.
All assembly, relocated probes, installs and foreground children are unprivileged
and use a newly created work directory. Reports remain on failure.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from tools.release import offline_core as release


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    args=parser.parse_args();work=args.work_dir.absolute()
    if work.exists():parser.error('Work directory must not exist')
    work.mkdir(mode=0o700,parents=True)
    spec=release.strict_json(args.spec.read_bytes())
    previous=release.build(spec,work/'assembled',work/'reports/build-previous')
    second=spec|{'release_version':spec['release_version']+'-candidate'}
    candidate=release.build(second,work/'assembled',work/'reports/build-candidate')
    # Identical supplied inputs produce identical payload inventory; only the
    # explicit release identity differs. This does not claim schema downgrade
    # compatibility between different source/runtime versions.
    a=release.verify_inventory(previous);b=release.verify_inventory(candidate)
    assert a['inventory']==b['inventory']
    result=release.rehearse(previous,candidate,work/'rehearsal',work/'reports/rehearsal')
    assert result['latest_measurement_count']==2
    assert result['writer_lease_released'] and result['owned_children_reaped']
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
