"""Portable ownership proofs; kernels are explicit numeric process models."""

import json
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from tests import run_release_checks as release
from tests import release_process_ownership as ownership
from tests.release_process_ownership import (
    OwnedCommand,
    OwnershipError,
    ProcessIdentity,
    SessionAuthority,
)


class Kernel:
    def __init__(self, *identities):
        self.identities = {item.pid: item for item in identities}
        self.sent = []
        self.error = None

    def identity(self, pid):
        if self.error is not None:
            raise self.error
        return self.identities.get(pid)

    def session(self, sid):
        if self.error is not None:
            raise self.error
        # The authority must independently validate returned membership too.
        return list(self.identities.values())

    def signal(self, identity, signum, sid, *, deadline=None):
        self.sent.append((identity.pid, identity.start, signum, sid))


class SessionIdentityTests(unittest.TestCase):
    def anchor(self):
        return ProcessIdentity(
            pid=100, ppid=10, pgid=100, sid=100, start=1234, zombie=False
        )

    def test_reparented_changed_group_is_owned_but_other_session_is_not(self):
        anchor = self.anchor()
        orphan = ProcessIdentity(
            pid=102, ppid=1, pgid=102, sid=100, start=1236, zombie=False
        )
        sibling = ProcessIdentity(
            pid=103, ppid=100, pgid=103, sid=103, start=1237, zombie=False
        )
        # Real host /proc also contains kernel processes with SID/PGID0.
        kernel_process = ProcessIdentity(
            pid=2, ppid=0, pgid=0, sid=0, start=0, zombie=False
        )
        kernel = Kernel(anchor, orphan, sibling, kernel_process)
        authority = SessionAuthority(anchor, kernel=kernel)
        self.assertEqual(authority.members(), [orphan])
        authority.signal(orphan, signal.SIGTERM)
        self.assertEqual(kernel.sent, [(102, 1236, signal.SIGTERM, 100)])

    def test_reused_anchor_pid_never_grants_signal_authority(self):
        anchor = self.anchor()
        replacement = ProcessIdentity(
            pid=100, ppid=1, pgid=100, sid=100, start=9999, zombie=False
        )
        kernel = Kernel(replacement)
        with self.assertRaises(OwnershipError):
            SessionAuthority(anchor, kernel=kernel).members()
        self.assertEqual(kernel.sent, [])

    def test_an_exited_anchor_never_grants_new_signal_authority(self):
        anchor = self.anchor()
        exited = ProcessIdentity(100, 10, 100, 100, 1234, True)
        member = ProcessIdentity(102, 100, 102, 100, 1236)
        kernel = Kernel(exited, member)
        with self.assertRaises(OwnershipError):
            SessionAuthority(anchor, kernel=kernel).members()
        self.assertEqual(kernel.sent, [])

    def released_owner(self):
        anchor = self.anchor()
        replacement = ProcessIdentity(
            pid=100, ppid=1, pgid=100, sid=100, start=9999, zombie=False
        )
        kernel = Kernel(replacement)
        owner = object.__new__(OwnedCommand)
        owner.directory = MagicMock()
        owner.process = None
        owner.meta = {"owner": "a" * 32}
        owner.authority = SessionAuthority(anchor, kernel=kernel)
        owner.status = lambda: {
            "phase": "released",
            "control_serial": 1,
            "cleanup_failed": False,
        }
        return owner, kernel

    def test_terminal_released_proof_is_idempotent_without_signaling_a_reused_pid(self):
        owner, kernel = self.released_owner()
        owner.release(0.1)
        owner.directory.__truediv__.return_value.unlink.assert_called()
        self.assertEqual(kernel.sent, [])

    def test_terminal_released_local_anchor_is_still_joined(self):
        owner, kernel = self.released_owner()
        owner.process = Mock()
        owner.process.returncode = 0
        owner.process.stdin.closed = False
        owner.stop(0.1)
        owner.process.stdin.close.assert_called_once()
        owner.process.wait.assert_called_once()
        self.assertEqual(kernel.sent, [])

    def test_target_pid_reuse_after_snapshot_fails_closed(self):
        anchor = self.anchor()
        member = ProcessIdentity(
            pid=102, ppid=100, pgid=102, sid=100, start=1236, zombie=False
        )
        kernel = Kernel(anchor, member)
        authority = SessionAuthority(anchor, kernel=kernel)
        self.assertEqual(authority.members(), [member])
        kernel.identities[102] = ProcessIdentity(
            pid=102, ppid=1, pgid=102, sid=102, start=9999, zombie=False
        )
        with self.assertRaises(OwnershipError):
            authority.signal(member, signal.SIGTERM)
        self.assertEqual(kernel.sent, [])

    def test_unknown_membership_never_means_empty_or_authorized(self):
        kernel = Kernel(self.anchor())
        kernel.error = PermissionError("unavailable numeric process evidence")
        with self.assertRaises(OwnershipError):
            SessionAuthority(self.anchor(), kernel=kernel).members()
        self.assertEqual(kernel.sent, [])

    def test_anchor_is_never_a_signal_target_and_zombies_have_no_live_io(self):
        anchor = self.anchor()
        zombie = ProcessIdentity(
            pid=102, ppid=1, pgid=102, sid=100, start=1236, zombie=True
        )
        kernel = Kernel(anchor, zombie)
        authority = SessionAuthority(anchor, kernel=kernel)
        self.assertEqual(authority.members(), [])
        with self.assertRaises(OwnershipError):
            authority.signal(anchor, signal.SIGTERM)
        self.assertEqual(kernel.sent, [])


class RegistryRetirementTests(unittest.TestCase):
    """Real directory retirement with an explicit already-released kernel model."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ori-registry-retirement-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.registry = base / "registry"
        self.registry.mkdir(mode=0o700)
        self.registration = self.registry / "session-link-model"
        self.registration.mkdir(mode=0o700)
        self.scope = base / "independent-owner"
        self.scope.mkdir(mode=0o700)
        (self.scope / "groups").mkdir(mode=0o700)
        anchor = ProcessIdentity(100, 10, 100, 100, 1234)
        meta = dict(version=1, owner="a" * 32, anchor=ownership.asdict(anchor),
                    registry=str(self.scope / "groups"), registration=str(self.registration))
        self.link = self.registration / "active.session.link.json"
        self.link.write_text(json.dumps(dict(version=1, owner=meta["owner"], directory=str(self.scope))))
        (self.scope / "active.session.json").write_text(json.dumps(meta))
        (self.scope / "status.json").write_text(json.dumps(dict(meta, phase="released", exit_code=0,
                                                               cleanup_failed=False, control_serial=1)))
        self.kernel = Kernel()  # No actual process or signaling authority exists.
        self.owner = object.__new__(OwnedCommand)
        self.owner.directory = self.scope
        self.owner.meta = meta
        self.owner.process = None
        self.owner.authority = SessionAuthority(anchor, kernel=self.kernel)
        retained = patch.dict(ownership._retained_commands, {self.scope: self.owner}, clear=True)
        retained.start()
        self.addCleanup(retained.stop)
        # This portable test isolates traversal/retirement, not POSIX modes or
        # ACLs. The actual files and original stop/_finish_release paths remain.
        records = patch.object(ownership, "read_record", side_effect=lambda path, **_kwargs: json.loads(Path(path).read_text()))
        records.start()
        self.addCleanup(records.stop)

    def test_retiring_yielded_owner_does_not_invalidate_remaining_registry_walk(self):
        commands = ownership.registered_commands(self.registry)
        self.assertIs(next(commands), self.owner)
        self.owner.stop(0.1)
        self.assertFalse(self.registration.exists())
        self.assertFalse(self.scope.exists())
        try:
            self.assertEqual(list(commands), [])
        except FileNotFoundError as error:
            self.fail("Registry traversal revisited its retired directory: " + str(error))
        self.assertEqual(self.kernel.sent, [])

    def test_nested_cleanup_can_retire_the_registration_it_is_enumerating(self):
        try:
            release._cleanup_registry(self.registry, 0.1)
        except FileNotFoundError as error:
            self.fail("Nested cleanup revisited its retired directory: " + str(error))
        self.assertFalse(self.registration.exists())
        self.assertFalse(self.scope.exists())
        self.assertEqual(self.kernel.sent, [])

    def test_foreign_generation_is_still_rejected_without_retiring_it(self):
        value = json.loads(self.link.read_text())
        value["owner"] = "b" * 32
        self.link.write_text(json.dumps(value))
        with self.assertRaisesRegex(OwnershipError, "another owner"):
            list(ownership.registered_commands(self.registry))
        self.assertTrue(self.registration.exists())
        self.assertTrue(self.scope.exists())
        self.assertEqual(self.kernel.sent, [])

    def test_missing_authority_record_is_still_an_error(self):
        ownership._retained_commands.pop(self.scope)
        (self.scope / "active.session.json").unlink()
        with self.assertRaises((FileNotFoundError, OwnershipError)):
            list(ownership.registered_commands(self.registry))
        self.assertTrue(self.registration.exists())
        self.assertEqual(self.kernel.sent, [])

    def test_record_snapshot_is_bounded_before_any_owner_is_yielded(self):
        second = self.registry / "session-link-second"
        second.mkdir(mode=0o700)
        (second / "active.session.link.json").write_text(self.link.read_text())
        with patch.object(ownership, "MAX_REGISTRIES", 1):
            with self.assertRaisesRegex(OwnershipError, "record budget"):
                next(ownership.registered_commands(self.registry))
        self.assertTrue(self.registration.exists())
        self.assertTrue(self.scope.exists())
        self.assertEqual(self.kernel.sent, [])


class LaunchBoundaryTests(unittest.TestCase):
    def test_uncertain_go_write_retains_ownership_instead_of_killing_the_anchor(self):
        with tempfile.TemporaryDirectory(prefix="ori-go-boundary-") as temporary:
            registry = Path(temporary)
            scope = registry / "independent-model"
            scope.mkdir(mode=0o700)
            process = Mock()
            process.pid = 100
            process.stdin.write.side_effect = KeyboardInterrupt
            process.stdin.closed = False
            identity = ProcessIdentity(100, 10, 100, 100, 1234)
            records = {}
            with (
                patch.object(ownership, "_independent_directory", return_value=scope),
                patch.object(ownership, "local_kernel", return_value=Kernel(identity)),
                patch.object(ownership.subprocess, "Popen", return_value=process),
                patch.object(
                    ownership,
                    "write_record",
                    side_effect=lambda path, value: records.__setitem__(path, value),
                ),
                patch.object(
                    ownership,
                    "_meta",
                    side_effect=lambda path: (records[path], identity),
                ),
                patch.object(
                    ownership.OwnedCommand,
                    "stop",
                    side_effect=OwnershipError("model unavailable"),
                ) as stop,
                patch.dict(ownership._retained_commands, {}, clear=True),
            ):
                with self.assertRaisesRegex(OwnershipError, "recovery record:"):
                    try:
                        ownership.OwnedCommand.start(
                            ["synthetic"],
                            environment={},
                            registry=registry,
                            root=registry,
                            log=Mock(),
                            shutdown_timeout=2,
                        )
                    except KeyboardInterrupt:
                        self.fail("uncertain launch discarded the registered owner")
                stop.assert_called_once()
                process.terminate.assert_not_called()
                process.kill.assert_not_called()
                process.wait.assert_not_called()
                self.assertIn(scope / "active.session.json", records)


class NumericKernelTests(unittest.TestCase):
    def test_linux_numeric_identity_does_not_decode_the_opaque_comm_bytes(self):
        with tempfile.TemporaryDirectory(prefix="ori-proc-bytes-") as temporary:
            root = Path(temporary)
            process = root / "102"
            process.mkdir()
            kernel = ownership.LinuxKernel(root)
            for state in (b"S", b"Z"):
                fields = [state, b"100", b"102", b"100"] + [b"0"] * 15 + [b"1236"]
                (process / "stat").write_bytes(
                    b"102 (opaque\xff )\n)) " + b" ".join(fields)
                )
                try:
                    identity = kernel.identity(102)
                except UnicodeError:
                    self.fail("opaque comm bytes were decoded before numeric parsing")
                self.assertEqual(
                    identity, ProcessIdentity(102, 100, 102, 100, 1236, state == b"Z")
                )

    def test_linux_pidfd_is_checked_against_the_snapshot_before_signal(self):
        original = ProcessIdentity(102, 100, 102, 100, 1236)
        replacement = ProcessIdentity(102, 1, 102, 102, 9999)
        kernel = ownership.LinuxKernel()
        with (
            patch.object(ownership.os, "pidfd_open", return_value=57, create=True),
            patch.object(kernel, "identity", return_value=replacement),
            patch.object(ownership.signal, "pidfd_send_signal", create=True) as send,
            patch.object(ownership.os, "close") as close,
        ):
            with self.assertRaises(OwnershipError):
                kernel.signal(original, signal.SIGTERM, 100)
            send.assert_not_called()
            close.assert_called_once_with(57)

    def test_darwin_libproc_decodes_only_numeric_identity_and_start_fields(self):
        api = Mock()

        def fill(pid, _flavor, _argument, output, size):
            info = output._obj
            info.numeric[3] = pid
            info.numeric[4] = 100
            info.numeric[1] = 5
            info.pgid = 102
            info.start_seconds = 123
            info.start_microseconds = 456
            return size

        api.proc_pidinfo.side_effect = fill
        with patch.object(ownership.os, "getsid", return_value=100, create=True):
            self.assertEqual(
                ownership.DarwinKernel(api).identity(102),
                ProcessIdentity(102, 100, 102, 100, 123000456, True),
            )


class PinnedGroupModelTests(unittest.TestCase):
    def fixture(self):
        anchor = ProcessIdentity(100, 10, 100, 100, 1234)
        member = ProcessIdentity(102, 100, 102, 100, 1236)
        keeper = ProcessIdentity(104, 100, 102, 100, 1238)
        kernel = Kernel(anchor, member, keeper)
        groups = ownership.PinnedProcessGroups(kernel, anchor)
        held = ownership.GroupKeeper(104, 102, 58, 59, identity=keeper, ready=True)
        groups.keepers.append(held)
        groups.current[102] = held
        return groups, kernel, member, held

    def test_darwin_routes_signaling_through_the_group_keeper_without_pid_kill(self):
        api = Mock()
        kernel = ownership.DarwinKernel(api)
        member = ProcessIdentity(102, 100, 102, 100, 1236)
        anchor = ProcessIdentity(100, 10, 100, 100, 1234)
        with (
            patch.object(
                kernel,
                "identity",
                side_effect=lambda pid: {100: anchor, 102: member}.get(pid),
            ),
            patch.object(ownership, "PinnedProcessGroups") as factory,
            patch.object(ownership.os, "kill") as pid_kill,
        ):
            kernel.signal(member, signal.SIGTERM, 100, deadline=123.0)
            factory.assert_called_once_with(kernel, anchor)
            factory.return_value.signal.assert_called_once_with(
                member, signal.SIGTERM, 123.0
            )
            pid_kill.assert_not_called()

    def test_only_a_ready_live_keeper_in_the_held_session_authorizes_killpg(self):
        with patch.object(ownership.os, "getpid", return_value=100):
            groups, _kernel, member, _held = self.fixture()
            with (
                patch.object(ownership.os, "killpg", create=True) as group_kill,
                patch.object(ownership.os, "kill") as pid_kill,
            ):
                groups.signal(member, signal.SIGTERM, time.monotonic() + 1)
                group_kill.assert_called_once_with(102, signal.SIGTERM)
                pid_kill.assert_not_called()

    def test_exited_or_reused_keeper_never_authorizes_a_group_signal(self):
        for replacement in (
            None,
            ProcessIdentity(104, 100, 102, 100, 1238, True),
            ProcessIdentity(104, 1, 102, 100, 1238),
            ProcessIdentity(104, 100, 103, 100, 1238),
            ProcessIdentity(104, 100, 102, 103, 1238),
            ProcessIdentity(104, 1, 104, 104, 9999),
        ):
            with (
                self.subTest(replacement=replacement),
                patch.object(ownership.os, "getpid", return_value=100),
            ):
                groups, kernel, member, _held = self.fixture()
                if replacement is None:
                    kernel.identities.pop(104)
                else:
                    kernel.identities[104] = replacement
                with (
                    patch.object(ownership.os, "killpg", create=True) as group_kill,
                    patch.object(ownership.os, "kill") as pid_kill,
                    self.assertRaises(OwnershipError),
                ):
                    groups.signal(member, signal.SIGTERM, time.monotonic() + 1)
                group_kill.assert_not_called()
                pid_kill.assert_not_called()

    def test_foreign_group_join_failure_has_no_signal_or_pid_fallback(self):
        with patch.object(ownership.os, "getpid", return_value=100):
            groups, _kernel, member, _held = self.fixture()
            groups.keepers.clear()
            groups.current.clear()
            with (
                patch.object(
                    groups,
                    "spawn_keeper",
                    side_effect=PermissionError("foreign session"),
                ),
                patch.object(ownership.os, "killpg", create=True) as group_kill,
                patch.object(ownership.os, "kill") as pid_kill,
                self.assertRaises(OwnershipError),
            ):
                groups.signal(member, signal.SIGTERM, time.monotonic() + 1)
            group_kill.assert_not_called()
            pid_kill.assert_not_called()

    def test_readiness_must_acknowledge_before_a_group_signal(self):
        with patch.object(ownership.os, "getpid", return_value=100):
            groups, _kernel, member, held = self.fixture()
            held.ready = False
            with (
                patch.object(ownership.select, "select", return_value=([59], [], [])),
                patch.object(ownership.os, "read", return_value=b"wrong\n"),
                patch.object(ownership.os, "killpg", create=True) as group_kill,
                self.assertRaises(OwnershipError),
            ):
                groups.signal(member, signal.SIGTERM, time.monotonic() + 1)
            group_kill.assert_not_called()

    def test_an_expected_killed_keeper_requires_a_fresh_live_join_for_another_signal(
        self,
    ):
        with patch.object(ownership.os, "getpid", return_value=100):
            groups, kernel, _member, held = self.fixture()
            held.kill_sent = True
            kernel.identities[104] = ProcessIdentity(104, 100, 102, 100, 1238, True)
            member = ProcessIdentity(103, 100, 102, 100, 1237)
            replacement = ProcessIdentity(106, 100, 102, 100, 1240)
            kernel.identities.update({103: member, 106: replacement})
            fresh = ownership.GroupKeeper(
                106, 102, 60, 61, identity=replacement, ready=True
            )
            with (
                patch.object(groups, "spawn_keeper", return_value=fresh) as join,
                patch.object(ownership.os, "close"),
                patch.object(ownership.os, "killpg", create=True) as group_kill,
            ):
                groups.signal(
                    member, getattr(signal, "SIGKILL", 9), time.monotonic() + 1
                )
            join.assert_called_once_with(102)
            group_kill.assert_called_once_with(102, getattr(signal, "SIGKILL", 9))
            self.assertFalse(held.reaped)
            self.assertIs(groups.current[102], fresh)

    def test_another_process_cannot_launch_a_keeper_into_the_held_session(self):
        anchor = ProcessIdentity(100, 10, 100, 100, 1234)
        with (
            patch.object(ownership.os, "getpid", return_value=101),
            self.assertRaises(OwnershipError),
        ):
            ownership.PinnedProcessGroups(Kernel(anchor), anchor)

    def test_anchor_group_is_never_a_target_and_live_members_prevent_keeper_release(
        self,
    ):
        with patch.object(ownership.os, "getpid", return_value=100):
            groups, kernel, member, _held = self.fixture()
            kernel.identities[102] = ProcessIdentity(102, 100, 100, 100, 1236)
            with (
                patch.object(ownership.os, "killpg", create=True) as group_kill,
                self.assertRaises(OwnershipError),
            ):
                groups.signal(member, signal.SIGTERM, time.monotonic() + 1)
            with (
                patch.object(ownership.os, "close") as close,
                patch.object(ownership.os, "waitpid") as wait,
                self.assertRaises(OwnershipError),
            ):
                groups.release(time.monotonic() + 1)
            group_kill.assert_not_called()
            close.assert_not_called()
            wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
