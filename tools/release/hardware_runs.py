"""Project installed physical shifts into a privacy-preserving platform release receipt."""
from .release_artifacts import keys, require


CONFIGURATIONS = {
    'tinysa': ('tinysa',),
    'rf-explorer': ('rf-explorer',),
    'ardv1': ('ardv1',),
    'tinysa-ardv1': ('tinysa', 'ardv1'),
    'rf-explorer-ardv1': ('rf-explorer', 'ardv1'),
}
SOURCE_FORMAT = {'tinysa': ('sweep', 'dBm'), 'rf-explorer': ('sweep', 'dBm'),
                 'ardv1': ('observation', 's_meter_count')}
SHIFT_SECONDS = 8 * 60 * 60
RUN_FIELDS = ('physical', 'seconds', 'unexplained_losses', 'duplicates', 'session_sha256',
              'coverage_sha256', 'report_sha256', 'sources')


def template():
    shifts = {}
    for name, roles in CONFIGURATIONS.items():
        run = dict.fromkeys(RUN_FIELDS)
        run['sources'] = {role: dict(kind=SOURCE_FORMAT[role][0], unit=SOURCE_FORMAT[role][1],
                                     records=None, ids_sha256=None) for role in roles}
        shifts[name] = run
    return shifts


def validate(shifts, paired):
    """Return the digests that must be distinct from every other qualification report."""
    keys(shifts, CONFIGURATIONS, 'five hardware configurations')
    hashes = []
    for name, roles in CONFIGURATIONS.items():
        run = shifts[name]
        keys(run, RUN_FIELDS, name + ' shift')
        require(run['physical'] is True and type(run['seconds']) is int and run['seconds'] >= SHIFT_SECONDS
                and all(type(run[k]) is int and run[k] == 0 for k in ('duplicates', 'unexplained_losses')),
                name + ' requires eight verified physical hours without unexplained gaps or duplicate IDs')
        keys(run['sources'], roles, name + ' physical sources')
        hashes.extend(run[k] for k in ('session_sha256', 'coverage_sha256', 'report_sha256'))
        for role, source in run['sources'].items():
            keys(source, ('kind', 'unit', 'records', 'ids_sha256'), role + ' source')
            require((source['kind'], source['unit']) == SOURCE_FORMAT[role]
                    and type(source['records']) is int and source['records'] >= 2,
                    'wrong native source, unit or count for ' + role)
            hashes.append(source['ids_sha256'])
    pair = shifts['tinysa-ardv1']
    require(pair['sources']['tinysa']['records'] == paired['sweeps']
            and pair['sources']['ardv1']['records'] == paired['observations'],
            'paired counters differ from the tinySA and AR-DV1 shift')
    if 'coverage_sha256' in paired:
        require(paired['coverage_sha256'] == pair['coverage_sha256'],
                'the Debian paired shift must use the same original archive export')
    return hashes
