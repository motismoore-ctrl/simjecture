"""Shared Windows application runner; stdlib only, including on Python 3.9.

This file and its application adapters are copied into each job before launch.
They are trusted host automation, not a numerical sandbox or an evidence worker.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import ExitStack
from pathlib import Path

PROTOCOL = "simjecture-windows/1"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}
ADAPTERS = {"cst": "cst_windows.py", "solidworks": "solidworks_windows.py"}
BUNDLE = ("windows_runtime.py", *ADAPTERS.values())


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    # Windows readers and antivirus scanners can briefly deny replacement even
    # after the complete new receipt is closed. Keep the old receipt intact and
    # bound retries so persistent access errors still fail explicitly.
    deadline = time.monotonic() + 5
    while True:
        try:
            os.replace(temporary, path)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(root, prefix):
    """Bind all files, including additions; never follow a directory symlink."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a regular snapshot directory: " + str(root))
    result = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Snapshot inputs must not contain symlinks")
            if not (path.is_dir() or path.is_file()):
                raise ValueError("Snapshot inputs must contain regular files and directories")
        for name in files:
            path = Path(directory) / name
            relative = prefix + "/" + path.relative_to(root).as_posix()
            result[relative] = sha(path)
    return dict(sorted(result.items()))


def adapter(application):
    if not isinstance(application, str) or application not in ADAPTERS:
        raise ValueError("Unknown Windows application: " + str(application))
    path = Path(__file__).resolve().with_name(ADAPTERS[application])
    spec = importlib.util.spec_from_file_location("simjecture_windows_" + application, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def describe(application=None):
    applications = [application] if application else list(ADAPTERS)
    return {
        "protocol": PROTOCOL,
        "stage": "exploration",
        "evidence_eligible": False,
        "applications": [adapter(name).describe() for name in applications],
    }


def _manifest(value, prefix):
    if not isinstance(value, dict) or not value:
        raise ValueError("A complete " + prefix + " manifest is required")
    for name, digest in value.items():
        if not isinstance(name, str):
            raise ValueError("Invalid snapshot path")
        parts = name.split("/")
        if (
            parts[0] != prefix
            or len(parts) < 2
            or any(part in ("", ".", "..") for part in parts)
            or any(character in name for character in ("\\", ":", "\x00"))
        ):
            raise ValueError("Invalid snapshot path: " + name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Invalid snapshot hash")
        if any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid snapshot hash")


def validate_request(value, *, validate_operation=True):
    fields = {
        "protocol",
        "application",
        "operation",
        "installation",
        "python",
        "arguments",
        "inputs",
        "runtime",
        "python_sha256",
        "created_at",
        "timeout",
    }
    if not isinstance(value, dict) or set(value) != fields or value["protocol"] != PROTOCOL:
        raise ValueError("Invalid Windows application request")
    for field in ("created_at", "timeout"):
        number = value[field]
        if type(number) not in (float, int) or not math.isfinite(number) or number <= 0:
            raise ValueError(field + " must be positive and finite")
    if value["timeout"] > 86400:
        raise ValueError("Windows application timeout exceeds one day")
    for field in ("installation", "python"):
        if not isinstance(value[field], str) or not value[field] or "\x00" in value[field]:
            raise ValueError("Invalid Windows " + field)
    _manifest(value["inputs"], "inputs")
    _manifest(value["runtime"], "runtime")
    if set(value["runtime"]) != {"runtime/" + name for name in BUNDLE}:
        raise ValueError("Windows runtime bundle is incomplete or unexpected")
    _manifest({"python/executable": value["python_sha256"]}, "python")
    if not isinstance(value["application"], str) or value["application"] not in ADAPTERS:
        raise ValueError("Unknown Windows application")
    if not isinstance(value["operation"], str) or not isinstance(value["arguments"], dict):
        raise ValueError("Invalid Windows application operation or arguments")
    if validate_operation:
        application = adapter(value["application"])
        application.validate(value["operation"], value["arguments"], value["inputs"])
    return value


def verify_inputs(job, request):
    if inventory(job / "inputs", "inputs") != request["inputs"]:
        raise ValueError("Windows application input inventory changed")
    if inventory(job / "runtime", "runtime") != request["runtime"]:
        raise ValueError("Windows runtime inventory changed")
    if sha(__file__) != request["runtime"]["runtime/windows_runtime.py"]:
        raise ValueError("Windows runner changed since submission")
    if sha(request["python"]) != request["python_sha256"]:
        raise ValueError("Windows Python changed since submission")
    if sha(job / "request.json") != read(job / "state.json")["request_sha256"]:
        raise ValueError("Windows application request changed since submission")


def stopped(job, request):
    if (job / "cancel").exists():
        return "cancelled"
    if time.time() >= request["created_at"] + request["timeout"]:
        return "timed_out"
    return None


def job_name(job):
    identity = hashlib.sha256(str(Path(job).resolve()).casefold().encode()).hexdigest()
    return "Local\\Simjecture-" + identity


def begin_external_activation(job, executable):
    started = time.time()
    if sys.platform == "win32":
        from ctypes import wintypes as w

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(w.FILETIME)]
        timestamp = w.FILETIME()
        kernel.GetSystemTimePreciseAsFileTime(ctypes.byref(timestamp))
        ticks = (timestamp.dwHighDateTime << 32) | timestamp.dwLowDateTime
        started = ticks / 10_000_000 - 11_644_473_600
    write(
        job / "external-activation.json",
        {
            "stage": "pending",
            "started_at": started,
            "executable": str(executable),
        },
    )
    return started


def _record_activation(job, stage, pid, created):
    path = job / "external-activation.json"
    if path.is_file():
        record = read(path)
        record.update(stage=stage, pid=pid, process_created_at=created)
        write(path, record)


def claim_process(job, pid, not_before, expected_executable=None):
    """Own a newly activated COM server before any model access or mutation.

    COM activation can create a server outside the worker's descendant tree.
    Refuse an existing server, and assign the new server to the supervisor's job.
    """
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
    kernel.OpenJobObjectW.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE,
        w.DWORD,
        w.LPWSTR,
        ctypes.POINTER(w.DWORD),
    ]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    process = kernel.OpenProcess(0x1101, False, pid)  # query, set quota, terminate
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    native = None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *(ctypes.byref(x) for x in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        created = ticks / 10_000_000 - 11_644_473_600
        if created < not_before:
            _record_activation(job, "refused_existing", pid, created)
            raise RuntimeError("Refusing an existing application session")
        native = kernel.OpenJobObjectW(0x5, False, job_name(job))  # assign, query
        if not native:
            raise ctypes.WinError(ctypes.get_last_error())
        member = w.BOOL()
        if not kernel.IsProcessInJob(process, native, ctypes.byref(member)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not member.value and not kernel.AssignProcessToJobObject(native, process):
            raise ctypes.WinError(ctypes.get_last_error())
        _record_activation(job, "owned", pid, created)
        if expected_executable is not None:
            size = w.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(image.value).resolve() != Path(expected_executable).resolve():
                raise RuntimeError("Activated application differs from the selected installation")
    finally:
        if native:
            kernel.CloseHandle(native)
        kernel.CloseHandle(process)


def worker(job):
    request = validate_request(read(job / "request.json"), validate_operation=False)
    while not (job / "assigned").exists():
        if stopped(job, request):
            raise TimeoutError("Windows Job assignment deadline expired")
        time.sleep(0.05)
    verify_inputs(job, request)
    application = adapter(request["application"])
    application.validate(request["operation"], request["arguments"], request["inputs"])
    work = job / "work"
    shutil.copytree(job / "inputs", work)
    if inventory(work, "inputs") != request["inputs"]:
        raise ValueError("Working copy differs from the recorded input snapshot")
    verify_inputs(job, request)
    os.chdir(work)
    context = {
        "claim_process": lambda pid, start, executable=None: claim_process(
            job, pid, start, executable
        ),
        "begin_external_activation": lambda executable: begin_external_activation(job, executable),
    }
    result = application.execute(job, request, context)
    verify_inputs(job, request)
    # Native applications must be closed before collecting their final artifacts.
    files = inventory(work, "work")
    result.update(
        protocol=PROTOCOL,
        application=request["application"],
        operation=request["operation"],
        arguments=request["arguments"],
        inputs=request["inputs"],
        runtime=request["runtime"],
        python_sha256=request["python_sha256"],
        stage="exploration",
        evidence_eligible=False,
        execution_boundary="trusted-windows-process",
        solver_execution="local",
        artifacts=[{"path": path, "sha256": digest} for path, digest in files.items()],
    )
    if (job / "external-activation.json").is_file():
        result["application_process"] = read(job / "external-activation.json")
    write(job / "results.json", result)


def main(default_application="cst"):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "describe",
            "check",
            "inspect",
            "submit",
            "supervise",
            "worker",
        ),
    )
    parser.add_argument("--application", choices=tuple(ADAPTERS))
    parser.add_argument("--installation")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--project")
    parser.add_argument("--result", action="append", default=[])
    parser.add_argument("--allow-open", action="store_true")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.job is not None:
        args.job = args.job.resolve()
    application = args.application or default_application
    if args.action == "describe":
        result = describe(args.application)
    elif args.action == "check":
        result = adapter(application).check(args.installation)
        result.update(protocol=PROTOCOL, application=application)
    elif args.action == "inspect":
        if args.pid is not None and application != "solidworks":
            raise ValueError("--pid is supported only for SOLIDWORKS inspection")
        module = adapter(application)
        if not hasattr(module, "inspect_project"):
            raise ValueError("This adapter exposes inspection through submitted jobs")
        result = module.inspect_project(
            args.installation,
            args.project,
            args.result,
            args.allow_open,
            **({"pid": args.pid} if args.pid is not None else {}),
        )
        result.update(protocol=PROTOCOL, application=application)
    elif args.action == "submit":
        result = submit(args.job)
    elif args.action == "supervise":
        supervise(args.job)
        return
    else:
        worker(args.job)
        return
    print(json.dumps(result, allow_nan=False))


# WindowsJob, supervise and submit below also serve the legacy CST entrypoint.


class WindowsJob:
    """Closing the native job kills exactly its assigned processes and descendants."""

    def __init__(self, name=None):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_longlong),
                ("JobTime", ctypes.c_longlong),
                ("LimitFlags", w.DWORD),
                ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t),
                ("ActiveProcesses", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", w.DWORD),
                ("Scheduling", w.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("IO", IO),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_void_p,
        ]
        self.handle = self.kernel.CreateJobObjectW(None, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def terminate(self):
        """Confirm assigned Windows processes exit before reporting a stop."""
        from ctypes import wintypes as w

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("UserTime", ctypes.c_longlong),
                ("KernelTime", ctypes.c_longlong),
                ("PeriodUserTime", ctypes.c_longlong),
                ("PeriodKernelTime", ctypes.c_longlong),
                ("PageFaults", w.DWORD),
                ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD),
                ("TerminatedProcesses", w.DWORD),
            ]

        if not self.kernel.TerminateJobObject(self.handle, 125):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            info = Accounting()
            if not self.kernel.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.ActiveProcesses == 0:
                return
            time.sleep(0.05)
        raise TimeoutError("Windows job termination is unconfirmed")


def supervise(job, *, _compat=None):
    import msvcrt

    read_state = _compat.read if _compat else read
    write_state = _compat.write if _compat else write
    validate = (
        _compat.validate_request
        if _compat
        else lambda value: validate_request(value, validate_operation=False)
    )
    verify = _compat.verify_inputs if _compat else verify_inputs
    stop = _compat.stopped if _compat else stopped
    entrypoint = _compat.entrypoint if _compat else Path(__file__).resolve()
    state = read_state(job / "state.json")
    if state["status"] in TERMINAL:
        return
    state.update(supervisor_pid=os.getpid())
    write_state(job / "state.json", state)
    try:
        request = validate(read_state(job / "request.json"))
    except Exception as error:
        state.update(
            status="failed", error="Invalid request: " + str(error), finished_at=time.time()
        )
        write_state(job / "state.json", state)
        return
    application = "cst" if _compat else request["application"]
    native_job = None
    child = None
    # One job per application installation, including across different job roots.
    locks = Path(os.environ["LOCALAPPDATA"]) / ("Simjecture/" + application + "-locks")
    key = hashlib.sha256(request["installation"].lower().encode()).hexdigest()
    # ExitStack closes the lock only after cleanup and the terminal receipt,
    # including when either execution or final-state writing raises.
    with ExitStack() as resources:
        try:
            locks.mkdir(parents=True, exist_ok=True)
            lock = resources.enter_context((locks / (key + ".lock")).open("a+b"))
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            while not stop(job, request):
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                state["status"] = stop(job, request)
                return
            verify(job, request)
            if not _compat:
                application_adapter = adapter(application)
                application_adapter.validate(
                    request["operation"],
                    request["arguments"],
                    request["inputs"],
                )
            outcome = stop(job, request)
            if outcome:
                state["status"] = outcome
                return
            native_job = _compat.WindowsJob() if _compat else WindowsJob(name=job_name(job))
            with (job / "worker.log").open("ab") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-u",
                        "-X",
                        "utf8",
                        str(entrypoint),
                        "worker",
                        "--job",
                        str(job),
                    ],
                    stdout=log,
                    stderr=log,
                    cwd=str(job),
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                native_job.assign(child)
                (job / "assigned").touch()
                state.update(status="running", worker_pid=child.pid, started_at=time.time())
                write_state(job / "state.json", state)
                while child.poll() is None and not stop(job, request):
                    time.sleep(0.1)
                outcome = stop(job, request)
                if outcome:
                    native_job.terminate()
                    child.wait(timeout=30)
                    state["status"] = outcome
                elif child.returncode == 0 and (job / "results.json").is_file():
                    state.update(status="succeeded", results_sha256=sha(job / "results.json"))
                else:
                    detail = (job / "worker.log").read_text(encoding="utf-8", errors="replace")
                    state.update(
                        status="failed", return_code=child.returncode, error=detail[-2000:]
                    )
        except Exception as error:
            state.update(status="failed", error=str(error))
            traceback.print_exc()
        finally:
            if native_job:
                try:
                    native_job.terminate()
                except Exception as error:
                    state.update(status="interrupted", error="Stop unconfirmed: " + str(error))
                finally:
                    native_job.close()
            if child and child.poll() is None:
                try:
                    child.kill()
                    child.wait(timeout=30)
                except Exception as error:
                    state.update(status="interrupted", error="Stop unconfirmed: " + str(error))
            activation = job / "external-activation.json"
            if activation.is_file():
                try:
                    if read(activation)["stage"] not in ("owned", "refused_existing"):
                        state.update(
                            status="interrupted",
                            error="External application activation stop is unconfirmed; "
                            "inspect external-activation.json",
                        )
                except Exception as error:
                    state.update(status="interrupted", error="Stop unconfirmed: " + str(error))
            state["finished_at"] = time.time()
            write_state(job / "state.json", state)


def submit(job, *, _compat=None):
    """Claim a launch once; record a known failure without leaving an ownerless queue."""
    read_state = _compat.read if _compat else read
    write_state = _compat.write if _compat else write
    entrypoint = _compat.entrypoint if _compat else Path(__file__).resolve()
    state = read_state(job / "state.json")
    if state["status"] in TERMINAL:
        return state
    marker = job / "submitted"
    try:
        marker.open("x").close()
    except FileExistsError:
        return read_state(job / "state.json")
    supervisor = None
    try:
        # Another submitter may have failed and released its marker after our
        # first read. Recheck terminal state only after claiming this launch.
        state = read_state(job / "state.json")
        if state["status"] in TERMINAL:
            marker.unlink()
            return state
        with (job / "supervisor.log").open("ab") as log:
            supervisor = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-X",
                    "utf8",
                    str(entrypoint),
                    "supervise",
                    "--job",
                    str(job),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
    except Exception as error:
        if supervisor is not None:
            # A failure after Popen returned does not mean the supervisor stopped.
            # Preserve ownership; the coordinator's lost-reply path requests cancel.
            raise
        state.update(
            status="failed",
            error="Failed to start Windows supervisor: " + str(error),
            finished_at=time.time(),
        )
        write_state(job / "state.json", state)
        marker.unlink()
        return state
    return read_state(job / "state.json")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
