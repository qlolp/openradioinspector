"""Evaluate observed transitions; operator acknowledgement is never sufficient."""
from __future__ import annotations


def progressed(before, after, role):
    count = role + '_sources'
    source = role + '_source_id'
    return (type(before.get(count)) is int and type(after.get(count)) is int
            and after[count] > before[count] >= 0
            and isinstance(after.get(source), str) and bool(after[source])
            and before.get(source) != after[source])


def assess(name, samples):
    rows = list(samples)
    if not rows:
        return False, 'No observations collected'
    first, last = rows[0], rows[-1]
    ready = last.get('active') is True and last.get('preparation_valid') is True
    if name == 'baseline':
        passed = (last.get('baseline_confirmed') is True
                  and type(last.get('baseline_sources')) is int and last['baseline_sources'] >= 20
                  and type(last.get('baseline_span')) in (int, float) and last['baseline_span'] >= 60
                  and bool(last.get('baseline_id')))
    elif name == 'guard':
        passed = ready and last.get('coverage') == 'wide' and progressed(first, last, 'analyzer')
    elif name == 'event':
        passed = (bool(last.get('event_id')) and bool(last.get('event_source_ids'))
                  and last.get('event_id') != first.get('event_id'))
    elif name == 'receiver':
        passed = (ready and bool(last.get('receiver_evidence_id'))
                  and progressed(first, last, 'receiver') and progressed(first, last, 'analyzer'))
    elif name == 'source_search':
        passed = (ready and first.get('coverage') == 'wide' and last.get('coverage') == 'wide'
                  and any(row.get('coverage') == 'narrow' for row in rows[1:-1])
                  and progressed(first, last, 'analyzer'))
    elif name == 'ui_quit':
        closed = [row for row in rows[1:-1] if row.get('ui_alive') is False and row.get('ui_exit_code') == 0]
        runtime = first.get('runtime_instance_id')
        passed = (ready and first.get('ui_alive') is True and last.get('ui_alive') is True
                  and bool(runtime) and all(row.get('runtime_instance_id') == runtime for row in rows)
                  and any(progressed(first, row, 'analyzer') for row in closed)
                  and progressed(first, last, 'analyzer'))
    elif name in ('usb_analyzer', 'usb_receiver'):
        role = name.removeprefix('usb_')
        presence = role + '_present'
        absent = [row for row in rows[1:-1] if row.get(presence) is False]
        passed = (ready and first.get(presence) is True and last.get(presence) is True
                  and bool(absent) and progressed(absent[-1], last, role))
        if role == 'analyzer':
            passed = passed and any(progressed(first, row, 'receiver') for row in absent)
        else:
            passed = passed and any(progressed(first, row, 'analyzer') for row in absent)
    elif name == 'sleep':
        # The controller observes continuous minus awake Mach clocks, not an
        # operator's claim or a long pause of this Python process.
        gaps = [row.get('sleep_delta_ns', 0) for row in rows]
        passed = (ready and any(type(gap) is int and 2_000_000_000 <= gap <= 900_000_000_000 for gap in gaps)
                  and progressed(first, last, 'analyzer'))
    elif name == 'history_restore':
        proof = last.get('restore_proof', {})
        passed = (proof.get('status') == 'passed' and proof.get('sweeps', 0) > 0
                  and proof.get('observations', 0) > 0
                  and isinstance(proof.get('original_sha256'), str) and len(proof['original_sha256']) == 64
                  and proof['original_sha256'] == proof.get('restored_sha256'))
    else:
        return False, 'Unknown checkpoint'
    return bool(passed), 'Observed transition verified' if passed else 'Required state/source transition was not observed'
