"""Assemble and extract both native packages with the Linux release runner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_pi_rc_package import package


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
