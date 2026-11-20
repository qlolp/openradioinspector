"""Capture only the installed Windows application's HWND against a private simulator."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

FIXTURE = Path(__file__).with_name('operator-window-fixture.json')
WORKER = Path(__file__).with_name('capture-tauri-window.ps1')
BASENAME = 'windows-installed-tauri'
MIN_SWEEPS = 16


class Unavailable(ValueError):
    """A short diagnostic that contains no paths, system usernames or HTTP bodies."""


def digest(path):
    result = sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def failure(reason, exc):
    if isinstance(exc, Unavailable):
        return str(exc)
    if isinstance(exc, HTTPError):
        return f'{reason}_http_{exc.code}'
    return f'{reason}_{type(exc).__name__}'


def stop_process(process, seconds):
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


@contextmanager
def replay(core, directory):
    ready = directory / 'core.ready'
    command = [str(core), '--mode', 'replay', '--host', '127.0.0.1', '--port', '0',
               '--data-dir', str(directory / 'archive'), '--ready-file', str(ready), '--stdin-lifeline']
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as process:
        try:
            deadline = time.monotonic() + 20
            while not ready.is_file():
                if process.poll() is not None:
                    raise Unavailable(f'private_replay_exit_{process.returncode}')
                if time.monotonic() >= deadline:
                    raise Unavailable('private_replay_ready_timeout')
                time.sleep(0.1)
            origin = ready.read_text(encoding='utf-8').strip()
            if not re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', origin):
                raise Unavailable('private_replay_ready_origin')
            yield origin
        finally:
            process.stdin.close()
            stop_process(process, 8)


def verify_sources(sweeps, observations):
    if (len(sweeps) < MIN_SWEEPS or not observations or
            not all(s.get('session_id') == 'windows-training' and s.get('stream_id') == 'spectrum' and s.get('level_unit') == 'dBm'
                    and s.get('settings', {}).get('adapter') == 'tinysa-sim'
                    and s['settings'].get('simulated') is True and s['settings'].get('generator_output') == 'off'
                    and len(s.get('frequencies_hz', [])) == len(s.get('levels', [])) == 601
                    and s['frequencies_hz'][0] == 100000000 and s['frequencies_hz'][-1] == 106000000 for s in sweeps) or
            not all(o.get('session_id') == 'windows-training' and o.get('stream_id') == 'receiver' and o.get('level_unit') == 's_meter_count'
                    and o.get('metadata', {}).get('adapter') == 'ardv1-sim'
                    and o['metadata'].get('simulated') is True for o in observations)):
        raise Unavailable('simulator_source_evidence')
    return {'verified_sweeps': len(sweeps), 'verified_observations': len(observations)}


def prepare_fixture(origin):
    # No ambient HTTP proxy or remotely supplied URLs participate in the fixture.
    client = build_opener(ProxyHandler({}))

    def api(route, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(origin + '/api/v1/' + route, data=payload, headers={'Content-Type': 'application/json'})
        with client.open(request, timeout=4) as response:
            return json.load(response)['data']

    capabilities = api('capabilities')
    if (capabilities['candidate']['mode'] != 'replay' or
            capabilities['features']['explicit_serial_adapters'] is not False or
            not capabilities['features']['simulated_device_workers'] or
            api('sessions') or api('monitoring-projects')['items']):
        raise Unavailable('replay_not_empty_or_not_simulator_only')
    fixture = json.loads(FIXTURE.read_bytes())
    session = fixture['session']
    if [s['settings']['adapter'] for s in session['streams']] != ['tinysa-sim', 'ardv1-sim']:
        raise Unavailable('unexpected_fixture_adapters')
    api('sessions', session)
    api('monitoring-projects', fixture['project'])
    route = 'sessions/windows-training/'
    api(route + 'start', {})
    api(route + 'workers/start', {})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        sweeps = api(route + f'sweeps?limit={MIN_SWEEPS}')
        observations = api(route + 'observations?limit=4')
        if len(sweeps) >= MIN_SWEEPS and observations:
            return {**verify_sources(sweeps, observations), 'serial_adapters_enabled': False, 'generator_output': 'off'}
        time.sleep(0.2)
    raise Unavailable('simulator_measurement_timeout')


def select_capture(work, reason):
    for scenario in ('monitoring', 'fresh_install_connection'):
        receipt = work / (scenario + '.json')
        image = work / (scenario + '.png')
        if receipt.is_file() and image.is_file():
            geometry = json.loads(receipt.read_bytes())
            with image.open('rb') as stream:
                header = stream.read(24)
            if (header[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR' or len(header) != 24 or
                    struct.unpack('>II', header[16:]) != (geometry['width'], geometry['height'])):
                reason = scenario + '_png_geometry_mismatch'
                continue
            if scenario == 'fresh_install_connection':
                geometry['monitoring_unavailable_reason'] = reason
            return scenario, geometry, image
    raise Unavailable(reason)


def remove_temporary(work):
    for attempt in range(20):
        try:
            shutil.rmtree(work)
            return
        except FileNotFoundError:
            if not work.exists():
                return
        except OSError:
            if attempt == 19:
                raise Unavailable('temporary_replay_or_webview_profile_in_use')
            time.sleep(0.5)


def creation_time(process):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    times = kernel.GetProcessTimes
    times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times.restype = wintypes.BOOL
    started, exited, system, user = (wintypes.FILETIME() for _ in range(4))
    if not times(int(process._handle), ctypes.byref(started), ctypes.byref(exited),
                 ctypes.byref(system), ctypes.byref(user)):
        raise Unavailable(f'installed_process_time_win32_{ctypes.get_last_error()}')
    return (started.dwHighDateTime << 32) | started.dwLowDateTime


def capture(args, metadata, work):
    # Force Windows PowerShell in STA for the system Windows UI Automation assemblies.
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-STA',
               '-ExecutionPolicy', 'RemoteSigned', '-File', str(WORKER), '-Directory', str(work)]
    phase = 'interactive_desktop_probe'
    try:
        subprocess.run(command + ['-Probe'], timeout=20, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        probe = json.loads((work / 'worker.json').read_bytes())
        if probe['status'] != 'ready':
            raise Unavailable(probe['reason'])
        fixture_error = None
        with ExitStack() as owned:
            phase = 'private_replay'
            try:
                origin = owned.enter_context(replay(args.core, work))
                phase = 'prepare_simulator'
                metadata['fixture'].update(prepare_fixture(origin))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                fixture_error = failure(phase, exc)
            scene = ['-FreshOnly'] if fixture_error else ['-ReplayOrigin', origin]
            phase = 'installed_desktop_start'
            environment = {**os.environ, 'WEBVIEW2_USER_DATA_FOLDER': str(work / 'webview'),
                           'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS': '--force-renderer-accessibility'}
            with subprocess.Popen([str(args.desktop)], env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as desktop:
                try:
                    phase = 'owned_window_automation'
                    # A hung UIA provider or Win32 PrintWindow must never hold the installer rehearsal.
                    subprocess.run(command + ['-ClientId', str(desktop.pid), '-InstalledExe', str(args.desktop),
                                              '-CreationTime', str(creation_time(desktop))] + scene, timeout=100, check=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                finally:
                    # The worker normally posts WM_CLOSE to that verified HWND. The process handle
                    # held here identifies precisely the fresh training client even if UIA failed.
                    stop_process(desktop, 5)
        result = json.loads((work / 'worker.json').read_bytes())
        reason = result.get('reason', 'monitoring_window_unavailable')
        if fixture_error:
            metadata['fixture']['unavailable_reason'] = fixture_error
            if reason == 'fresh_install_only':
                reason = fixture_error
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        reason = failure(phase, exc)
    scenario, geometry, image = select_capture(work, reason)
    destination = args.output / (BASENAME + '.png')
    pending = destination.with_suffix('.part')
    shutil.copyfile(image, pending)
    image_digest = digest(pending)
    pending.replace(destination)
    metadata.pop('reason', None)
    metadata.update(status='captured', scenario=scenario,
                    image={'file': destination.name, 'sha256': image_digest, **geometry})


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    photo = commands.add_parser('capture')
    photo.add_argument('--core', type=Path, required=True)
    photo.add_argument('--desktop', type=Path, required=True)
    photo.add_argument('--output', type=Path, required=True)
    commands.add_parser('check-replay', help='exercise the native binary in ORI_REPLAY_TEST_CORE')
    args = parser.parse_args()
    if args.command == 'check-replay':
        path = os.environ.get('ORI_REPLAY_TEST_CORE')
        if not path or not Path(path).is_file():
            parser.error('check-replay requires ORI_REPLAY_TEST_CORE pointing to an ori-core executable')
        with tempfile.TemporaryDirectory(prefix='ori-window-replay-test-') as temp:
            directory = Path(temp)
            with replay(Path(path).resolve(), directory) as origin:
                counts = prepare_fixture(origin)
            if (directory / 'core.ready').exists():
                raise Unavailable('private_replay_did_not_release_readiness')
            print(json.dumps(counts))
        return
    receipt = args.output / (BASENAME + '.json')
    metadata = json.loads(receipt.read_text(encoding='utf-8-sig'))
    metadata['fixture'] = {'source': 'deploy/windows/' + FIXTURE.name, 'sha256': digest(FIXTURE),
                           'mode': 'replay', 'adapters': ['tinysa-sim', 'ardv1-sim']}
    if sys.platform != 'win32':
        metadata.update(status='unavailable', reason='windows_required')
        write_json(receipt, metadata)
        return
    work = Path(tempfile.mkdtemp(prefix='ori-training-window-'))
    try:
        capture(args, metadata, work)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        metadata.update(status='unavailable', reason=failure('window_capture', exc))
    finally:
        try:
            remove_temporary(work)
        except (OSError, Unavailable) as exc:
            metadata['cleanup_error'] = failure('temporary_cleanup', exc)
            raise
        finally:
            write_json(receipt, metadata)


if __name__ == '__main__':
    main()
