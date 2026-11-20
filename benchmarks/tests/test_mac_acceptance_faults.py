import copy
from pathlib import Path
import tempfile
import unittest

from benchmarks.ori_bench.config import BenchmarkError


class BoundedFaultVolumeTests(unittest.TestCase):
    def test_only_the_owned_image_on_a_distinct_filesystem_can_be_filled(self):
        from benchmarks.ori_bench.mac_acceptance.faults import validate_fault_mount
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();image=root/'bounded.sparseimage';image.touch();mount=root/'mount';mount.mkdir()
            info={'images':[{'image-path':str(image),'system-entities':[{'mount-point':str(mount),'dev-entry':'/dev/disk99s1'}]}]}
            self.assertEqual(validate_fault_mount(image,mount,info,host_device=1,mount_device=2),'/dev/disk99s1')
            for fault in ('same_device','other_image','other_mount','root_mount'):
                with self.subTest(fault=fault):
                    changed=copy.deepcopy(info);target=mount;device=2
                    if fault=='same_device':device=1
                    elif fault=='other_image':changed['images'][0]['image-path']=str(root/'other')
                    elif fault=='other_mount':changed['images'][0]['system-entities'][0]['mount-point']=str(root/'other')
                    else:target=Path('/')
                    with self.assertRaises(BenchmarkError):
                        validate_fault_mount(image,target,changed,host_device=1,mount_device=device)
