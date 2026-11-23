"""Assemble and extract both native packages with the Linux release runner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_pi_rc_package import package


class InstalledLifecycleScenarioTests(unittest.TestCase):
    def run_scenarios(self, commands, *, install_exit=0, http_exit=0, reset_exit=0,
                      automatic_restarts=0, active_exit=0, show_exit=0):
        # Execute the real verifier body without the destructive VM setup. Its
        # external install/HTTP/systemd commands use an isolated shell fixture.
        source = (Path(__file__).resolve().parents[1] / 'packaging/pi/test-installed.sh').read_text()
        start = source.index('\nverify() {\n') + 1
        end = source.index('\n}\n', start) + 2
        verifier = source[start:end]
        fixture = r'''
unit=ori-fixture.service
package=ori-fixture
checks=fixture-check.py
manifest=fixture-manifest.json
report=$ORI_TEST_REPORT
starts=0
event() { printf '%s\n' "$1" >> "$ORI_TEST_EVENTS"; }
function /usr/lib/ori-fixture/check-install() {
    event check-install
    return "$ORI_TEST_INSTALL_EXIT"
}
offline() {
    [ "$#" -eq 5 ] && [ "$1" = python3 ] && [ "$2" = "$checks" ]
    [ "$4" = --manifest ] && [ "$5" = "$manifest" ]
    event "http:$3"
    [ "$ORI_TEST_HTTP_EXIT" -eq 0 ] || return "$ORI_TEST_HTTP_EXIT"
    printf '{"phase":"%s","result":"passed"}\n' "$3"
}
systemctl() {
    case "$1" in
        start)
            [ "$#" -eq 2 ] && [ "$2" = "$unit" ] || return 82
            starts=$((starts + 1))
            if [ "$starts" -gt 5 ]; then
                event start-limit-hit
                printf 'start-limit-hit\n' >&2
                return 75
            fi
            event start
            ;;
        reset-failed)
            [ "$#" -eq 2 ] && [ "$2" = "$unit" ] || return 82
            event reset-request
            [ "$ORI_TEST_RESET_EXIT" -eq 0 ] || return "$ORI_TEST_RESET_EXIT"
            starts=0
            event reset
            ;;
        is-active)
            [ "$#" -eq 3 ] && [ "$2" = --quiet ] && [ "$3" = "$unit" ] || return 82
            event active-check
            return "$ORI_TEST_ACTIVE_EXIT"
            ;;
        show)
            [ "$#" -eq 5 ] && [ "$2" = -p ] && [ "$3" = NRestarts ] || return 82
            [ "$4" = --value ] && [ "$5" = "$unit" ] || return 82
            event "automatic-restarts:$ORI_TEST_AUTOMATIC_RESTARTS"
            [ "$ORI_TEST_SHOW_EXIT" -eq 0 ] || return "$ORI_TEST_SHOW_EXIT"
            printf '%s\n' "$ORI_TEST_AUTOMATIC_RESTARTS"
            ;;
        *) return 83 ;;
    esac
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / 'events'
            environment = dict(os.environ, ORI_TEST_REPORT=str(root), ORI_TEST_EVENTS=str(events),
                               ORI_TEST_INSTALL_EXIT=str(install_exit), ORI_TEST_HTTP_EXIT=str(http_exit),
                               ORI_TEST_RESET_EXIT=str(reset_exit), ORI_TEST_AUTOMATIC_RESTARTS=str(automatic_restarts),
                               ORI_TEST_ACTIVE_EXIT=str(active_exit), ORI_TEST_SHOW_EXIT=str(show_exit))
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', fixture + verifier + '\n' + commands],
                                    env=environment, capture_output=True, text=True, timeout=5)
            recorded = events.read_text().splitlines() if events.exists() else []
            return result, recorded

    def test_verified_scenarios_do_not_consume_each_others_start_limit(self):
        result, events = self.run_scenarios('''
for phase in install upgrade rollback restart existing-v6 existing-v6; do
    systemctl start "$unit"
    verify "$phase"
done
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events.count('start'), 6)
        self.assertEqual(events.count('reset'), 6)

    def test_unverified_starts_still_hit_the_rate_limit(self):
        result, events = self.run_scenarios('for attempt in 1 2 3 4 5 6; do systemctl start "$unit"; done')
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(events[-1], 'start-limit-hit')
        self.assertNotIn('reset', events)

    def test_failed_install_or_http_check_never_resets_or_continues(self):
        for values, expected in [({'install_exit': 22}, 22), ({'http_exit': 23}, 23)]:
            with self.subTest(values=values):
                result, events = self.run_scenarios('verify upgrade\nevent continued', **values)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn('reset-request', events)
                self.assertNotIn('continued', events)

    def test_failed_reset_remains_a_lifecycle_failure(self):
        result, events = self.run_scenarios('verify upgrade\nevent continued', reset_exit=24)
        self.assertEqual(result.returncode, 24, result.stderr)
        self.assertNotIn('reset', events)
        self.assertNotIn('continued', events)

    def test_automatic_restart_is_not_erased_by_a_successful_health_check(self):
        result, events = self.run_scenarios('verify upgrade\nevent continued', automatic_restarts=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('http:upgrade', events)
        self.assertIn('automatic-restarts:1', events)
        self.assertNotIn('reset-request', events)
        self.assertNotIn('continued', events)

    def test_inactive_or_unreadable_restart_state_never_resets(self):
        for values, expected in [({'active_exit': 3}, 3), ({'show_exit': 25}, 25),
                                 ({'automatic_restarts': ''}, 1)]:
            with self.subTest(values=values):
                result, events = self.run_scenarios('verify upgrade\nevent continued', **values)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn('reset-request', events)
                self.assertNotIn('continued', events)


class DebianPackageAssemblyTests(unittest.TestCase):
    def test_both_packages_stage_their_own_unit_config_and_maintainer_scripts(self):
        profiles = [(package.PI, 'openradioinspector-pi-rc', 'ori-pi-rc', 'arm64'),
                    (package.AMD64, 'openradioinspector-linux', 'ori-linux', 'amd64')]
        for profile, name, user, arch in profiles:
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                stage = root / 'stage'
                package.stage_service(stage, profile)
                doc = stage / 'usr/share/doc' / name
                doc.mkdir(parents=True)
                (doc / 'README.md').write_text('test fixture')
                products = root / 'out'
                info = dict(release_version='0.4.0', target=profile.target, package=name, debian_architecture=arch)
                package.assemble(stage, products, 1, os.environ.copy(), info, profile)
                deb = products / f'{name}_0.4.0-1_{arch}.deb'
                self.assertEqual({p.name for p in products.iterdir()}, {deb.name, 'SHA256SUMS', 'release.json', 'README.md'})
                for line in (products / 'SHA256SUMS').read_text().splitlines():
                    sha, file = line.split('  ')
                    self.assertEqual(hashlib.sha256((products / file).read_bytes()).hexdigest(), sha)
                for field, expected in [('Package', name), ('Architecture', arch), ('Version', '0.4.0-1')]:
                    self.assertEqual(subprocess.check_output(['dpkg-deb', '-f', deb, field], text=True).strip(), expected)
                dependencies = subprocess.check_output(['dpkg-deb', '-f', deb, 'Depends'], text=True).strip()
                self.assertIn('ca-certificates', dependencies.split(', '))
                installed = root / 'extracted'
                subprocess.run(['dpkg-deb', '-R', deb, installed], check=True)
                unit = (installed / 'usr/lib/systemd/system' / f'{name}.service').read_text()
                self.assertIn(f'User={user}\nGroup={user}\nSupplementaryGroups=dialout\n', unit)
                self.assertIn(f'StateDirectory={name}\nStateDirectoryMode=0700\n', unit)
                self.assertIn(f'/usr/lib/{name}/ori-supervisor run /etc/{name}/supervisor.json', unit)
                self.assertIn(f'u {user} ', (installed / 'usr/lib/sysusers.d' / f'{name}.conf').read_text())
                config = json.loads((installed / profile.config).read_text())
                self.assertEqual(config['data_directory'], '/var/lib/' + name)
                self.assertEqual(config['backend']['program'], f'/usr/lib/{name}/ori-core')
                self.assertIn('--allow-serial', config['backend']['args'])
                self.assertEqual(config['backend']['control_addr'], '127.0.0.1:8770')
                self.assertEqual((installed / 'DEBIAN/conffiles').read_text(), f'/etc/{name}/supervisor.json\n')
                for script in ('postinst', 'prerm', 'postrm'):
                    maintainer = installed / 'DEBIAN' / script
                    self.assertIn(f'{name}.service', maintainer.read_text())
                    self.assertTrue(os.access(maintainer, os.X_OK))
                prerm = (installed / 'DEBIAN/prerm').read_text()
                self.assertIn('dpkg --compare-versions "$2" lt \'0.4.0-1\'', prerm)
                self.assertIn(f'runuser -u {user} -- /usr/lib/{name}/ori-supervisor archive-prepare-rollback --data-dir /var/lib/{name}', prerm)
                for script in ('prepare-archive', 'wait-ready', 'check-install'):
                    path = installed / profile.lib / script
                    self.assertTrue(os.access(path, os.X_OK))
                    self.assertIn(f'/var/lib/{name}', path.read_text())
                self.assertEqual((installed / profile.doc / 'release.json').read_bytes(),
                                 (products / 'release.json').read_bytes())
                self.assertEqual(json.loads((products / 'release.json').read_text()), dict(info, debian_version='0.4.0-1'))
