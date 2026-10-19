"""Exercise the shared Windows supervisor without vendor applications or dependencies.

Run with Windows Python 3.9+; the ordinary Linux tests cover snapshot and VBA policy.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

RUNNER = None


def process_alive(pid):
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = w.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def worker_fixture(job, hold):
    """Spawn a descendant only after native Job Object assignment, then fail or wait."""
    deadline = time.monotonic() + 10
    while not (job / "assigned").exists():
        if time.monotonic() > deadline:
            return 2
        time.sleep(0.01)
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    (job / "descendant.pid").write_text(str(child.pid))
    while hold:
        time.sleep(0.1)
    return 1


class NativeSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="simjecture-cst-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = self.root / "job"
        self.job.mkdir()
        self.profile = patch.dict(os.environ, LOCALAPPDATA=str(self.root / "profile"))
        self.profile.start()
        self.addCleanup(self.profile.stop)
        self.installation = "C:\\CST-Test-" + self.root.name
        self.request = dict(
            protocol=RUNNER.PROTOCOL,
            installation=self.installation,
            python=sys.executable,
            parameters={"r": 2.0},
            results=[r"1D Results\L"],
            timeout=30,
            solver="LF MStatic",
            inputs={"inputs/project.cst": "a" * 64},
            created_at=time.time(),
            runner_sha256="b" * 64,
            python_sha256="c" * 64,
        )
        if RUNNER.PROTOCOL == "simjecture-windows/1":
            self.request.update(
                application="cst",
                operation="magnetostatic.solve",
                arguments={
                    "parameters": self.request.pop("parameters"),
                    "results": self.request.pop("results"),
                },
                runtime={"runtime/" + name: "b" * 64 for name in RUNNER.BUNDLE},
            )
            self.request.pop("solver")
            self.request.pop("runner_sha256")
        RUNNER.write(self.job / "request.json", self.request)
        RUNNER.write(self.job / "state.json", {"protocol": RUNNER.PROTOCOL, "status": "queued"})
        key = hashlib.sha256(self.installation.lower().encode()).hexdigest()
        self.lock = self.root / "profile/Simjecture/cst-locks" / (key + ".lock")

    def lock_available(self):
        import msvcrt

        with self.lock.open("a+b") as handle:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return True

    def worker_command(self, hold=False):
        command = [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "--worker-job",
            str(self.job),
        ]
        if hold:
            command.append("--hold-worker")
        return command

    def test_receipt_write_survives_a_native_windows_reader(self):
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE
        ]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        path = self.job / "state.json"
        original = RUNNER.read(path)
        # FILE_SHARE_READ allows polling but deliberately denies replacement.
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
        if handle == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        blocked = threading.Event()
        errors = []
        replace = os.replace

        def observed_replace(*args):
            try:
                return replace(*args)
            except OSError:
                blocked.set()
                raise

        def save():
            try:
                RUNNER.write(path, {"status": "running"})
            except Exception as error:
                errors.append(error)

        writer = threading.Thread(target=save, daemon=True)
        with patch.object(os, "replace", observed_replace):
            try:
                writer.start()
                self.assertTrue(blocked.wait(5), "Native sharing conflict was not exercised")
                self.assertEqual(RUNNER.read(path), original)
            finally:
                kernel.CloseHandle(handle)
                writer.join(timeout=10)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(RUNNER.read(path), {"status": "running"})

    def test_lock_covers_descendant_cleanup_and_terminal_receipt(self):
        real_popen, real_terminate, real_write = (
            subprocess.Popen,
            RUNNER.WindowsJob.terminate,
            RUNNER.write,
        )
        observations = []
        unrelated = real_popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])

        def launch(*args, **kwargs):
            return real_popen(self.worker_command(), **kwargs)

        def terminate(native):
            pid = int((self.job / "descendant.pid").read_text())
            observations.append((process_alive(pid), self.lock_available()))
            return real_terminate(native)

        def write(path, value):
            if path == self.job / "state.json" and "finished_at" in value:
                self.assertFalse(self.lock_available(), "Lock released before receipt")
            return real_write(path, value)

        try:
            with (
                patch.object(RUNNER, "verify_inputs"),
                patch.object(subprocess, "Popen", launch),
                patch.object(RUNNER.WindowsJob, "terminate", terminate),
                patch.object(RUNNER, "write", write),
            ):
                RUNNER.supervise(self.job)
            self.assertEqual(RUNNER.read(self.job / "state.json")["status"], "failed")
            self.assertEqual(observations, [(True, False)])
            self.assertFalse(process_alive(int((self.job / "descendant.pid").read_text())))
            self.assertTrue(process_alive(unrelated.pid))
            self.assertTrue(self.lock_available())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=10)

    def stop_active_worker(self, outcome):
        real_popen, real_stopped = subprocess.Popen, RUNNER.stopped
        now = [self.request["created_at"] + 0.1]
        deadline = time.monotonic() + 10

        def launch(*args, **kwargs):
            return real_popen(self.worker_command(hold=True), **kwargs)

        def stopped(job, request):
            if (job / "descendant.pid").exists():
                if outcome == "cancelled":
                    (job / "cancel").touch()
                else:
                    now[0] = request["created_at"] + request["timeout"] + 1
            # Bound this fixture even if a child fails before writing its PID.
            if time.monotonic() > deadline:
                now[0] = request["created_at"] + request["timeout"] + 1
            return real_stopped(job, request)

        with (
            patch.object(RUNNER, "verify_inputs"),
            patch.object(subprocess, "Popen", launch),
            patch.object(RUNNER, "stopped", stopped),
            patch.object(time, "time", lambda: now[0]),
        ):
            RUNNER.supervise(self.job)
        self.assertEqual(RUNNER.read(self.job / "state.json")["status"], outcome)
        self.assertFalse(process_alive(int((self.job / "descendant.pid").read_text())))
        self.assertTrue(self.lock_available())

    def test_cancel_terminates_assigned_descendant(self):
        self.stop_active_worker("cancelled")

    def test_deadline_terminates_assigned_descendant(self):
        self.stop_active_worker("timed_out")

    def test_supervisor_creation_failure_is_terminal(self):
        with patch.object(
            subprocess, "Popen", side_effect=OSError("CreateProcess failed")
        ) as launch:
            state = RUNNER.submit(self.job)
            self.assertEqual(state["status"], "failed")
            self.assertIn("CreateProcess failed", state["error"])
            self.assertFalse((self.job / "submitted").exists())
            self.assertEqual(RUNNER.submit(self.job), state)
            self.assertEqual(launch.call_count, 1)

    def test_duplicate_submission_starts_only_once(self):
        with patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=42)) as launch:
            self.assertEqual(RUNNER.submit(self.job)["status"], "queued")
            self.assertEqual(RUNNER.submit(self.job)["status"], "queued")
            self.assertEqual(launch.call_count, 1)

    def test_invalid_request_is_terminal_before_worker_creation(self):
        request = dict(self.request, timeout=-1)
        RUNNER.write(self.job / "request.json", request)
        with patch.object(subprocess, "Popen", side_effect=AssertionError("must not launch")):
            RUNNER.supervise(self.job)
        state = RUNNER.read(self.job / "state.json")
        self.assertEqual(state["status"], "failed")
        self.assertIn("Invalid request", state["error"])
        self.assertIn("finished_at", state)

    def test_cancel_during_verification_does_not_start_worker(self):
        def verify(*args):
            (self.job / "cancel").touch()

        with (
            patch.object(RUNNER, "verify_inputs", verify),
            patch.object(subprocess, "Popen", side_effect=AssertionError("must not launch")),
        ):
            RUNNER.supervise(self.job)
        self.assertEqual(RUNNER.read(self.job / "state.json")["status"], "cancelled")

    def test_solidworks_inspection_reads_process_image_without_ownership(self):
        if not hasattr(RUNNER, "adapter"):
            self.skipTest("Legacy CST compatibility entrypoint")
        solidworks = RUNNER.adapter("solidworks")
        self.assertEqual(solidworks.process_executable(os.getpid()), Path(sys.executable).resolve())

    def test_new_external_process_can_be_adopted_by_named_job(self):
        if not hasattr(RUNNER, "claim_process"):
            self.skipTest("Legacy CST compatibility entrypoint")
        native = RUNNER.WindowsJob(name=RUNNER.job_name(self.job))
        before = RUNNER.begin_external_activation(self.job, sys.executable)
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
        try:
            RUNNER.claim_process(self.job, child.pid, before, sys.executable)
            self.assertEqual(RUNNER.read(self.job / "external-activation.json")["stage"], "owned")
            native.terminate()
            child.wait(timeout=10)
            self.assertFalse(process_alive(child.pid))
        finally:
            native.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_existing_process_is_not_adopted_or_killed(self):
        if not hasattr(RUNNER, "claim_process"):
            self.skipTest("Legacy CST compatibility entrypoint")
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
        native = RUNNER.WindowsJob(name=RUNNER.job_name(self.job))
        try:
            with self.assertRaisesRegex(RuntimeError, "existing application session"):
                RUNNER.claim_process(self.job, child.pid, time.time() + 1)
            native.terminate()
            self.assertTrue(process_alive(child.pid))
        finally:
            native.close()
            child.terminate()
            child.wait(timeout=10)

    def test_unknown_external_activation_stop_is_reported_as_interrupted(self):
        if not hasattr(RUNNER, "begin_external_activation"):
            self.skipTest("Legacy CST compatibility entrypoint")
        RUNNER.begin_external_activation(self.job, "C:\\UnknownApplication.exe")
        real_popen = subprocess.Popen

        def launch(*args, **kwargs):
            return real_popen(self.worker_command(), **kwargs)

        with patch.object(RUNNER, "verify_inputs"), patch.object(subprocess, "Popen", launch):
            RUNNER.supervise(self.job)
        state = RUNNER.read(self.job / "state.json")
        self.assertEqual(state["status"], "interrupted")
        self.assertIn("unconfirmed", state["error"])
        self.assertFalse(process_alive(int((self.job / "descendant.pid").read_text())))

    def test_selected_executable_is_checked_after_new_process_adoption(self):
        if not hasattr(RUNNER, "claim_process"):
            self.skipTest("Legacy CST compatibility entrypoint")
        native = RUNNER.WindowsJob(name=RUNNER.job_name(self.job))
        before = RUNNER.begin_external_activation(self.job, sys.executable)
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
        try:
            with self.assertRaisesRegex(RuntimeError, "selected installation"):
                RUNNER.claim_process(self.job, child.pid, before, self.root / "wrong.exe")
            native.terminate()
            child.wait(timeout=10)
        finally:
            native.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_frozen_bundle_executes_a_fixture_adapter_end_to_end(self):
        if not hasattr(RUNNER, "BUNDLE"):
            self.skipTest("Legacy CST compatibility entrypoint")
        (self.job / "runtime").mkdir()
        (self.job / "inputs").mkdir()
        (self.job / "inputs/project.cst").write_bytes(b"fixture input, not a CST model")
        for name in RUNNER.BUNDLE:
            shutil.copyfile(Path(RUNNER.__file__).with_name(name), self.job / "runtime" / name)
        # This deliberately replaces the application adapter, not the shared
        # protocol, staging, verification, supervisor, worker or artifact code.
        # It exercises Windows execution without implying vendor qualification.
        (self.job / "runtime/cst_windows.py").write_text(
            "def validate(operation, arguments, inputs):\n"
            "    assert operation == 'magnetostatic.solve'\n"
            "    assert set(inputs) == {'inputs/project.cst'}\n"
            "def execute(job, request, context):\n"
            "    source = job / 'work/project.cst'\n"
            "    assert source.read_bytes() == b'fixture input, not a CST model'\n"
            "    (job / 'work/output.txt').write_text('fixture output')\n"
            "    return {'fixture': True}\n",
            encoding="utf-8",
        )
        request = dict(self.request)
        request.update(
            python=sys.executable,
            python_sha256=RUNNER.sha(sys.executable),
            inputs=RUNNER.inventory(self.job / "inputs", "inputs"),
            runtime=RUNNER.inventory(self.job / "runtime", "runtime"),
            created_at=time.time(),
            timeout=20,
        )
        RUNNER.write(self.job / "request.json", request)
        RUNNER.write(
            self.job / "state.json",
            {
                "protocol": RUNNER.PROTOCOL,
                "status": "queued",
                "request_sha256": RUNNER.sha(self.job / "request.json"),
            },
        )
        runner = self.job / "runtime/windows_runtime.py"
        subprocess.run(
            [sys.executable, "-I", "-B", str(runner), "submit", "--job", str(self.job)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            state = RUNNER.read(self.job / "state.json")
            if state["status"] in RUNNER.TERMINAL:
                break
            time.sleep(0.05)
        diagnostics = {"state": state}
        if state["status"] != "succeeded":
            for name in ("supervisor.log", "worker.log"):
                path = self.job / name
                diagnostics[name] = (
                    path.read_text(encoding="utf-8", errors="replace")
                    if path.exists()
                    else "missing"
                )
        self.assertEqual(state["status"], "succeeded", diagnostics)
        while process_alive(state["supervisor_pid"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(process_alive(state["supervisor_pid"]))
        result = RUNNER.read(self.job / "results.json")
        self.assertTrue(result["fixture"])
        self.assertFalse(result["evidence_eligible"])
        self.assertEqual(result["application"], "cst")
        self.assertEqual(RUNNER.sha(self.job / "results.json"), state["results_sha256"])
        for artifact in result["artifacts"]:
            self.assertEqual(RUNNER.sha(self.job / artifact["path"]), artifact["sha256"])
        self.assertEqual((self.job / "work/output.txt").read_text(), "fixture output")
        self.assertEqual(
            (self.job / "inputs/project.cst").read_bytes(), b"fixture input, not a CST model"
        )


def main():
    global RUNNER

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src/conjecture_solver/windows_runtime.py",
    )
    parser.add_argument("--worker-job", type=Path)
    parser.add_argument("--hold-worker", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run this check with Windows Python 3.9+")
    if args.worker_job:
        return worker_fixture(args.worker_job, args.hold_worker)
    spec = importlib.util.spec_from_file_location("cst_windows_runner", args.runner)
    RUNNER = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(RUNNER)
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(NativeSupervisorTests)
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
