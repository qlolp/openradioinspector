#!/usr/bin/env python3
"""Relocate an actual CPython runtime for the separate, offline engineering kit."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.release import macos_rc_package as package


def command(*args):
    return subprocess.run([str(a) for a in args],check=True,capture_output=True,text=True).stdout


def resolve_dependency(name,source,prefix,rpaths):
    if name.startswith(('/usr/lib/','/System/Library/')):return None
    if name.startswith('@loader_path/'):
        candidates=[source.parent/name[len('@loader_path/'):]]
    elif name.startswith('@rpath/'):
        candidates=[Path(path.replace('@loader_path',str(source.parent)))/name[len('@rpath/'):] for path in rpaths]
    elif name.startswith('/'):
        candidates=[Path(name)]
    else:raise ValueError('Unsupported runtime library reference: '+name)
    target=next((path.resolve() for path in candidates if path.is_file()),None)
    if target is None or not target.is_relative_to(prefix.resolve()):
        raise ValueError('Unbundled development dependency: '+name)
    return target


def macho(path):
    with path.open('rb') as source:magic=source.read(4)
    return magic in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf')


def copy_payload(destination):
    destination.mkdir(parents=True,exist_ok=False)
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','tests','node_modules')
    shutil.copytree(ROOT/'benchmarks',destination/'benchmarks',ignore=ignore)
    for relative in ['tests/__init__.py','tests/release_process_ownership.py','tests/run_release_checks.py','tests/loopback_dns.py',
                     'tools/modernization/api_parity.py','tools/release/macos_rc_package.py']:
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/relative,target)


def runtime(kit):
    original=Path(sys.base_prefix).resolve();version=f'{sys.version_info.major}.{sys.version_info.minor}'
    prefix=kit/'runtime'/('Python.framework/Versions/'+version if sysconfig.get_config_var('PYTHONFRAMEWORK') else 'python')
    prefix.mkdir(parents=True)
    (prefix/'bin').mkdir()
    executable=prefix/'bin/python3'
    shutil.copy2(Path(sys.executable).resolve(),executable)
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','site-packages','test','tests','ensurepip','idlelib','tkinter','turtledemo','_tkinter*','config-*')
    shutil.copytree(original/('lib/python'+version),prefix/('lib/python'+version),ignore=ignore)
    originals={executable:Path(sys.executable).resolve()}
    if (original/'Python').is_file():shutil.copy2(original/'Python',prefix/'Python')
    for source in (original/'lib').glob('*.dylib'):
        shutil.copy2(source,prefix/'lib'/source.name)
    # Resolve dependency paths against the source, but install only relative
    # loader paths into the copied files. No Homebrew/library downloads.
    for target in prefix.rglob('*'):
        if target.is_file() and macho(target):
            originals.setdefault(target,(original/target.relative_to(prefix)).resolve())
    for target,source in originals.items():
        architectures=command('lipo','-archs',target).split()
        if 'arm64' not in architectures:raise ValueError('Python runtime lacks arm64')
        if len(architectures)>1:
            thin=target.with_name(target.name+'.arm64-pending')
            command('lipo',target,'-thin','arm64','-output',thin);os.replace(thin,target)
        target.chmod(target.stat().st_mode|0o200)
        subprocess.run(['codesign','--remove-signature',str(target)],capture_output=True)
        linked=command('otool','-arch','arm64','-L',source).splitlines()[1:]
        ids=command('otool','-arch','arm64','-D',source).splitlines()[1:]
        install_id=ids[0].strip() if ids else None
        detail=command('otool','-arch','arm64','-l',source).splitlines();rpaths=[]
        for index,line in enumerate(detail):
            if line.strip()=='cmd LC_RPATH':
                rpaths.append(detail[index+2].strip().split('path ',1)[1].rsplit(' (offset ',1)[0])
        for line in linked:
            name=line.strip().split(' (',1)[0]
            if name==install_id:continue
            dep=resolve_dependency(name,source,original,rpaths)
            if dep is None:continue
            copied=prefix/dep.relative_to(original)
            if not copied.is_file():raise ValueError('Excluded module is still a runtime dependency: '+str(dep))
            relative='@loader_path/'+os.path.relpath(copied,target.parent)
            command('install_name_tool','-change',name,relative,target)
        if install_id:command('install_name_tool','-id','@rpath/'+target.name,target)
        for path in rpaths:
            if path.startswith('/') and not path.startswith(('/usr/lib/','/System/Library/')):
                command('install_name_tool','-delete_rpath',path,target)
    # Sign leaves before the interpreter; all modified Mach-O files are covered.
    for target in sorted(originals,key=lambda p:p==executable):
        command('codesign','--force','--sign','-','--timestamp=none',target)
        command('codesign','--verify','--strict',target)
    licenses=[*original.glob('LICENSE*'),*(original/'Resources').glob('LICENSE*'),*(original/('lib/python'+version)).glob('LICENSE*')]
    if not licenses:raise ValueError('Python distribution license missing')
    notices=kit/'Notices';notices.mkdir()
    for index,path in enumerate(licenses):
        if path.is_file():shutil.copy2(path,notices/(str(index)+'-'+path.name))
    shutil.copy2(ROOT/'LICENSE',notices/'OpenRadioInspector-MIT.txt')
    probe='import sys,ssl,sqlite3,ctypes,json; from pathlib import Path; p=Path(sys.argv[1]).resolve(); assert Path(sys.base_prefix).resolve().is_relative_to(p),sys.base_prefix; assert all(not v or Path(v).resolve().is_relative_to(p) for v in sys.path),sys.path; print(json.dumps({"version":sys.version,"prefix":sys.base_prefix,"sqlite":sqlite3.sqlite_version,"openssl":ssl.OPENSSL_VERSION}))'
    environment={k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','DYLD_'))}
    subprocess.run([str(executable),'-I','-S','-c',probe,str(kit/'runtime')],env=environment,cwd=kit,check=True)
    return executable


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--app-manifest',type=Path,required=True)
    args=parser.parse_args()
    if platform.system()!='Darwin' or platform.machine()!='arm64':raise SystemExit('Build the runtime on an arm64 Mac')
    if sys.version_info[:2]!=(3,12):raise SystemExit('Engineering runtime is pinned to CPython3.12 build environment')
    manifest=package.read_json(args.app_manifest,32*1024*1024)
    source=command('git','-C',ROOT,'rev-parse','HEAD').strip()
    if source!=manifest['source_revision'] or command('git','-C',ROOT,'status','--porcelain','--untracked-files=no').strip():
        raise SystemExit('Engineering payload and signed app must use the same clean source revision')
    os.umask(0o077)
    output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=False)
    kit=output/'OpenRadioInspector-Mac-Tests';kit.mkdir()
    python=runtime(kit);copy_payload(kit/'payload')
    shutil.copy2(args.app_manifest,kit/'app-manifest.json')
    shutil.copy2(ROOT/'packaging/macos/ACCEPTANCE-README.ru.md',kit/'READ-ME.ru.md')
    start=kit/'StartTests.command'
    start.write_text('#!/bin/bash\nset -euo pipefail\nori_kit_dir="$(cd -- "$(dirname -- "$0")" && pwd)"\nexec /usr/bin/env -u PYTHONHOME -u PYTHONPATH -u DYLD_LIBRARY_PATH -u DYLD_FRAMEWORK_PATH -u ORI_BENCH_TOKEN PYTHONDONTWRITEBYTECODE=1 "$ori_kit_dir"/'+shlex.quote(str(python.relative_to(kit)))+' -I -S -B "$ori_kit_dir/payload/benchmarks/ori_bench/mac_acceptance/entry.py" --kit-root "$ori_kit_dir" "$@"\n')
    start.chmod(0o755)
    package.write_json(kit/'kit-manifest.json',dict(schema_version=1,source_revision=manifest['source_revision'],
        python_version=sys.version,python_executable=str(python.relative_to(kit)),
        hardware_validation='not_run',full_load_validation='not_run',files=package.inventory(kit)))
    archive=output/'OpenRadioInspector-Mac-Tests.zip'
    command('ditto','-c','-k','--sequesterRsrc','--keepParent',kit,archive)
    (output/'SHA256SUMS').write_text(package.digest(archive)+'  '+archive.name+'\n')
    print(json.dumps(dict(archive=str(archive),source_revision=manifest['source_revision'],hardware_tested=False)))


if __name__=='__main__':main()
