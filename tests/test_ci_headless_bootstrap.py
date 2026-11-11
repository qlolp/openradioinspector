"""Offline CI bootstrap behavior; no package manager or display is launched."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.ci import headless_bootstrap as bootstrap


class Host:
    def __init__(self, *, missing=(), selected_tk=True, broken=(), repair=True, apt_error=False):
        self.missing=set(missing);self.selected_tk=selected_tk;self.broken=set(broken)
        self.repair=repair;self.apt_error=apt_error;self.commands=[]
        self.selected="/selected/toolcache/python"

    def which(self,name):
        return None if name in self.missing else "/prepared/"+name

    def run(self,argv,**options):
        self.commands.append(argv)
        if argv[0]=="sudo":
            if self.apt_error:raise subprocess.CalledProcessError(100,argv,stderr="Temporary failure resolving archive.ubuntu.com")
            if "install" in argv and self.repair:
                self.missing.clear();self.broken.clear();self.selected_tk=True
            return SimpleNamespace(returncode=0,stdout="")
        if argv[0]==self.selected:
            return SimpleNamespace(returncode=0 if self.selected_tk else 1,stdout="selected Tk ready" if self.selected_tk else "selected _tkinter unavailable")
        if argv[0].startswith("/prepared/"):
            return SimpleNamespace(returncode=1 if Path(argv[0]).name in self.broken else 0,stdout="")
        # A generic/system interpreter being healthy does not prove that the
        # setup-python interpreter can import its own tkinter extension.
        return SimpleNamespace(returncode=0,stdout="distro Tk ready")


class HeadlessBootstrapTests(unittest.TestCase):
    def test_prepared_host_with_selected_tk_uses_no_apt_or_network(self):
        host=Host()
        result=bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)
        self.assertTrue(result.ready)
        self.assertFalse(any(command[0]=="sudo" for command in host.commands))
        self.assertTrue(any(command[0]==host.selected and "-I" in command for command in host.commands))

    def test_good_distro_tk_cannot_hide_missing_selected_interpreter_tk(self):
        host=Host(selected_tk=False,repair=False)
        with self.assertRaisesRegex(bootstrap.BootstrapError,"selected|tkinter"):
            bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)
        imports=[command for command in host.commands if "-c" in command]
        self.assertTrue(imports)
        self.assertTrue(all(command[0]==host.selected for command in imports))

    def test_missing_display_dependency_is_not_ready_without_install_authorization(self):
        host=Host(missing=("xvfb-run",))
        with self.assertRaisesRegex(bootstrap.BootstrapError,"xvfb"):
            bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run,install=False)
        self.assertFalse(any(command[0]=="sudo" for command in host.commands))

    def test_broken_xauth_installs_only_its_package_then_revalidates(self):
        host=Host(broken=("xauth",))
        result=bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)
        self.assertTrue(result.ready)
        installs=[command for command in host.commands if command[0]=="sudo" and "install" in command]
        self.assertEqual(len(installs),1)
        self.assertEqual(installs[0][installs[0].index("-y")+1:],["xauth"])
        self.assertGreaterEqual(sum(command[0]==host.selected for command in host.commands),2)

    def test_missing_all_dependencies_installs_required_xauth_and_rechecks(self):
        host=Host(missing=("xvfb-run","Xvfb","xauth"),selected_tk=False)
        result=bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)
        self.assertTrue(result.ready)
        install=next(command for command in host.commands if "install" in command)
        self.assertEqual(set(install[install.index("-y")+1:]),{"xvfb","xauth","python3-tk"})

    def test_dns_or_apt_failure_stays_visible_and_does_not_retry(self):
        host=Host(missing=("Xvfb",),apt_error=True)
        with self.assertRaises(subprocess.CalledProcessError):
            bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)
        self.assertEqual(sum(command[0]=="sudo" for command in host.commands),1)

    def test_successful_apt_exit_cannot_claim_broken_dependencies_ready(self):
        host=Host(missing=("Xvfb",),repair=False)
        with self.assertRaisesRegex(bootstrap.BootstrapError,"Xvfb|xvfb"):
            bootstrap.ensure_headless(executable=host.selected,which=host.which,run=host.run)

    def test_real_selected_tk_import_creates_no_window_or_local_startup_side_effects(self):
        with tempfile.TemporaryDirectory(prefix="ori-ci-import-") as directory:
            home=Path(directory);marker=home/"profile-sourced"
            script="from pathlib import Path;Path("+repr(str(marker))+").write_text('unexpected')"
            for name in (".Tk.py",".tk.py"):(home/name).write_text(script,encoding="utf-8")
            with patch.dict(os.environ,{"HOME":str(home),"USERPROFILE":str(home)}):
                result=bootstrap.probe_headless(executable=sys.executable,which=lambda _:None)
            self.assertNotIn("python3-tk",result.packages)
            self.assertFalse(marker.exists())


class StatusArtifactTests(unittest.TestCase):
    def script(self):
        lines=(Path(__file__).resolve().parents[1]/".github/workflows/windows-core.yml").read_text().splitlines()
        start=next(i for i,line in enumerate(lines) if line.strip()=="- name: Record CI status before artifact upload")
        block=next(i for i in range(start+1,len(lines)) if lines[i].strip()=="run: |")+1
        result=[]
        for line in lines[block:]:
            if line and not line.startswith("          "):break
            result.append(line[10:])
        return "\n".join(result)+"\n"

    def bash(self):
        if os.name=="nt":
            for key in ("ProgramFiles","ProgramFiles(x86)"):
                if key in os.environ:
                    path=Path(os.environ[key])/"Git/bin/bash.exe"
                    if path.is_file():return str(path)
        value=shutil.which("bash")
        self.assertIsNotNone(value,"The workflow's Bash shell must be available")
        return value

    def test_actual_status_step_survives_missing_python_setup_and_keeps_exact_head(self):
        with tempfile.TemporaryDirectory(prefix="ori-ci-status-") as directory:
            environment=dict(os.environ,CI_JOB_STATUS="failure",CI_HEAD_SHA="a"*40)
            script="python() { return 97; }; export -f python\n"+self.script()
            subprocess.run([self.bash(),"-e","-c",script],cwd=directory,env=environment,check=True,capture_output=True)
            status=Path(directory)/"release-reports/ci-status.log"
            self.assertEqual(status.read_text(),"job_status=failure\nhead_sha="+"a"*40+"\n")
            self.assertLessEqual(status.stat().st_size,128)

    def test_actual_status_step_has_a_fixed_output_bound(self):
        with tempfile.TemporaryDirectory(prefix="ori-ci-status-bound-") as directory:
            environment=dict(os.environ,CI_JOB_STATUS="failure"*1000,CI_HEAD_SHA="b"*4000)
            subprocess.run([self.bash(),"-e","-c",self.script()],cwd=directory,env=environment,check=True,capture_output=True)
            self.assertLessEqual((Path(directory)/"release-reports/ci-status.log").stat().st_size,128)


if __name__=="__main__":unittest.main()
