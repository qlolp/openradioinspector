"""Optional licensed-archive checks: python -m unittest tests.research_checks.
Not included in public release checks. ORI_RESEARCH_DISTRIBUTION must be external.
"""
from tests.test_analysis_tools import *

@unittest.skipUnless(locate_distribution(), "коммерческий диск отсутствует в рабочей копии")
class TestRealDistribution(unittest.TestCase):
    """Проверки на реальных файлах дистрибутива."""

    def setUp(self) -> None:
        self.root = locate_distribution()
        self.exe = next(self.root.rglob("InspectorRT.exe"), None)
        if self.exe is None:
            self.skipTest("InspectorRT.exe не найден")

    def test_forms_are_parsed(self):
        pe = PEFile(self.exe)
        forms = find_forms(pe)
        self.assertGreater(len(forms), 100)
        names = {name for name, _ in forms}
        self.assertIn("TFINSPECTORRCMF", names)
        self.assertIn("TFRSIGNALSLIST", names)

    def test_installer_package_is_readable(self):
        msi = next(self.root.rglob("SetupRadioInspectorLight.msi"), None)
        if msi is None:
            self.skipTest("SetupRadioInspectorLight.msi не найден")
        cfb = CompoundFile(msi)
        streams = cfb.text_streams()
        self.assertGreater(len(streams), 10)
        self.assertTrue(all(isinstance(blob, bytes) for _, blob in streams))
        self.assertTrue(cfb.string_pool())

    def test_database_identifiers_include_taskplan(self):
        db = next(self.root.rglob("TASKPLAN.ITASKDB"), None)
        if db is None:
            self.skipTest("TASKPLAN.ITASKDB не найден")
        names = identifiers(db)
        self.assertIn("TASKPLAN", names)
        self.assertIn("GEN_TASKTYPEID", names)

@unittest.skipUnless(locate_distribution(), "коммерческий диск отсутствует в рабочей копии")
class TestRealHelpFiles(unittest.TestCase):
    """Разбор настоящих файлов справки."""

    def setUp(self) -> None:
        self.root = locate_distribution()

    def test_help_file_is_parsed(self):
        hlp = next(self.root.rglob("InspectorRT.hlp"), None)
        if hlp is None:
            self.skipTest("InspectorRT.hlp не найден")
        parsed = read_hlp(hlp)
        self.assertEqual(parsed.errors, [])
        self.assertGreater(parsed.topic_count, 10)
        joined = "\n".join(topic.text for topic in parsed.topics)
        self.assertIn("InspectorRT", joined)
        self.assertIn("эталонной панорамой", joined)

    def test_all_help_files_are_readable(self):
        files = sorted(self.root.rglob("*.hlp"))
        if not files:
            self.skipTest("файлы справки не найдены")
        total = 0
        for path in files:
            parsed = read_hlp(path)
            self.assertEqual(parsed.errors, [], f"{path.name}: {parsed.errors}")
            total += parsed.topic_count
        self.assertGreater(total, 300)
