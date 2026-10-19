"""Operator-side client for application adapters running on Windows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path, PureWindowsPath

from . import windows_runtime as runtime


def linux_path(value):
    value = os.fspath(value)
    if len(value) > 2 and value[1:3] in (":\\", ":/"):
        return Path(subprocess.check_output(["wslpath", "-u", value], text=True).strip())
    return Path(value).expanduser().resolve()


def windows_path(value):
    return subprocess.check_output(["wslpath", "-w", str(Path(value).resolve())], text=True).strip()


class WindowsBridge:
    """One job protocol, with product-specific argument and model semantics."""

    def __init__(self, application, installation, python):
        runtime.adapter(application)
        if not installation or not python:
            raise ValueError(
                "Specify the application --installation and compatible Windows --python"
            )
        self.application = application
        self.installation = linux_path(installation)
        self.python = linux_path(python)
        self.runner = Path(runtime.__file__).resolve()
        if not self.installation.is_dir() or not self.python.is_file():
            raise ValueError("Application installation or Windows Python was not found")

    def _windows_path(self, value):
        return windows_path(value)

    def call(self, action, *args, timeout=60, runner=None):
        command = [
            str(self.python),
            "-I",
            "-B",
            "-X",
            "utf8",
            self._windows_path(runner or self.runner),
            action,
            "--application",
            self.application,
            "--installation",
            self._windows_path(self.installation),
            *args,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
        if result.returncode:
            raise RuntimeError(
                "Windows application bridge failed: " + result.stderr.strip()[-4000:]
            )
        lines = result.stdout.splitlines()
        if not lines:
            raise RuntimeError("Windows application bridge returned no response")
        payload = json.loads(lines[-1])
        if not isinstance(payload, dict) or payload.get("protocol") != runtime.PROTOCOL:
            raise ValueError("Unexpected Windows application protocol")
        if payload.get("application") != self.application:
            raise ValueError("Unexpected Windows application response")
        return payload

    def check(self):
        return self.call("check")

    def inspect(self, project=None, results=(), *, allow_open=False, pid=None):
        if pid is not None and self.application != "solidworks":
            raise ValueError("--pid is supported only for SOLIDWORKS inspection")
        if pid is not None and (type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF):
            raise ValueError("Choose a positive Windows process ID with --pid")
        if self.application == "cst" and project is None:
            raise ValueError("CST inspection requires --project")
        args = []
        if project is not None:
            args.extend(("--project", self._windows_path(linux_path(project))))
        for result in results:
            args.extend(("--result", result))
        if allow_open:
            args.append("--allow-open")
        if pid is not None:
            args.extend(("--pid", str(pid)))
        return self.call("inspect", *args)

    @staticmethod
    def _sources_manifest(sources):
        files = {}
        for target, source in sources.items():
            if source.is_symlink():
                raise ValueError("Application inputs must not contain symlinks")
            if source.is_dir():
                files.update(runtime.inventory(source, target))
            elif source.is_file():
                files[target] = runtime.sha(source)
            else:
                raise ValueError("Application inputs must be regular files or directories")
        return dict(sorted(files.items()))

    def submit_application(
        self, project, job_root, operation, arguments, *, script=None, timeout=600
    ):
        source = linux_path(project)
        root = linux_path(job_root)
        application = runtime.adapter(self.application)
        sources = application.input_sources(source, linux_path(script) if script else None)
        win_root = self._windows_path(root)
        if len(win_root) < 3 or win_root[1:3] != ":\\":
            raise ValueError("Choose --job-root on a Windows drive mounted in WSL")
        windows_root = PureWindowsPath(win_root)
        if hasattr(application, "validate_job_root"):
            application.validate_job_root(PureWindowsPath(self._windows_path(source)), windows_root)
        for path in sources.values():
            if path.is_dir() and windows_root.is_relative_to(
                PureWindowsPath(self._windows_path(path))
            ):
                raise ValueError("Keep the job root outside the source input directories")
        # Validate the complete task before allocating its directory or launching.
        expected = self._sources_manifest(sources)
        native_root = self.runner.parent
        bundle = {"runtime/" + name: runtime.sha(native_root / name) for name in runtime.BUNDLE}
        request = runtime.validate_request(
            dict(
                protocol=runtime.PROTOCOL,
                application=self.application,
                operation=operation,
                installation=self._windows_path(self.installation),
                python=self._windows_path(self.python),
                arguments=arguments,
                inputs=expected,
                runtime=bundle,
                python_sha256=runtime.sha(self.python),
                created_at=time.time(),
                timeout=timeout,
            )
        )
        identifier = self.application + "_" + uuid.uuid4().hex
        job = root / identifier
        job.mkdir(parents=True)
        launch_attempted = False
        try:
            (job / "inputs").mkdir()
            for target, path in sources.items():
                destination = job / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir():
                    shutil.copytree(path, destination)
                else:
                    shutil.copyfile(path, destination)
            copied = runtime.inventory(job / "inputs", "inputs")
            current_sources = application.input_sources(
                source, linux_path(script) if script else None
            )
            if self._sources_manifest(current_sources) != expected or copied != expected:
                raise ValueError("Source project or Model files changed during snapshotting")
            (job / "runtime").mkdir()
            for name in runtime.BUNDLE:
                shutil.copyfile(native_root / name, job / "runtime" / name)
            copied_bundle = runtime.inventory(job / "runtime", "runtime")
            current_bundle = {
                "runtime/" + name: runtime.sha(native_root / name) for name in runtime.BUNDLE
            }
            if copied_bundle != bundle or current_bundle != bundle:
                raise ValueError("Windows runtime changed during snapshotting")
            runtime.write(job / "request.json", request)
            runtime.write(
                job / "state.json",
                dict(
                    protocol=runtime.PROTOCOL,
                    id=identifier,
                    application=self.application,
                    operation=operation,
                    status="queued",
                    created_at=request["created_at"],
                    stage="exploration",
                    evidence_eligible=False,
                    execution_boundary="trusted-windows-process",
                    solver_execution="local",
                    request_sha256=runtime.sha(job / "request.json"),
                ),
            )
            launch_attempted = True
            self.call(
                "submit",
                "--job",
                self._windows_path(job),
                runner=job / "runtime/windows_runtime.py",
            )
        except Exception as error:
            if launch_attempted:
                (job / "cancel").touch()
                raise RuntimeError(
                    "Windows submission reply was lost; cancellation requested. "
                    "Check the job before retrying: " + str(job)
                ) from error
            runtime.write(
                job / "state.json",
                dict(
                    protocol=runtime.PROTOCOL,
                    id=identifier,
                    application=self.application,
                    operation=operation,
                    status="failed",
                    finished_at=time.time(),
                    error="Submission failed; inspect the request and coordinator error",
                    stage="exploration",
                    evidence_eligible=False,
                ),
            )
            raise
        return status(job)


def status(job):
    job = linux_path(job)
    state = runtime.read(job / "state.json")
    state["directory"] = str(job)
    if (
        state["status"] == "succeeded"
        and runtime.sha(job / "results.json") != state["results_sha256"]
    ):
        raise ValueError("Recorded Windows application results changed")
    return state


def cancel(job):
    job = linux_path(job)
    state = status(job)
    if state["status"] not in runtime.TERMINAL:
        (job / "cancel").touch()
        state["cancel_requested"] = True
    return state


def wait(job, *, poll_seconds=0.2):
    while True:
        state = status(job)
        if state["status"] in runtime.TERMINAL:
            return state
        request = runtime.read(linux_path(job) / "request.json")
        if time.time() > request["created_at"] + request["timeout"] + 30:
            raise RuntimeError(
                "Windows supervisor has not reported a terminal state; stop is unconfirmed"
            )
        time.sleep(poll_seconds)


def artifacts(job):
    state = status(job)
    if state["status"] != "succeeded":
        raise ValueError("Artifacts require a successful terminal job")
    root = linux_path(job)
    result = runtime.read(root / "results.json")
    if "artifacts" not in result:
        raise ValueError(
            "This historical job predates artifact manifests; inspect its job directory"
        )
    for item in result["artifacts"]:
        if runtime.sha(root / item["path"]) != item["sha256"]:
            raise ValueError("Recorded application artifact changed: " + item["path"])
    return {"protocol": runtime.PROTOCOL, "directory": str(root), "artifacts": result["artifacts"]}


def exit_code(action, result):
    if action == "cancel":
        return 0
    if result.get("status") == "cancelled":
        return 130
    return 1 if result.get("status") in {"failed", "timed_out", "interrupted"} else 0


def configure_parser(parser):
    actions = parser.add_subparsers(dest="windows_action", required=True)
    describe = actions.add_parser("describe")
    describe.add_argument("--application", choices=tuple(runtime.ADAPTERS))
    describe.set_defaults(handler=main)
    for name in ("check", "inspect", "submit"):
        action = actions.add_parser(name)
        action.add_argument("--application", choices=tuple(runtime.ADAPTERS), required=True)
        action.add_argument("--installation", required=True)
        action.add_argument("--python", required=True)
        if name == "inspect":
            action.add_argument("--project")
            action.add_argument("--result", action="append", default=[])
            action.add_argument("--allow-open", action="store_true")
            action.add_argument("--pid", type=int, help="Select a SOLIDWORKS session process ID")
        if name == "submit":
            action.add_argument("--project", required=True)
            action.add_argument("--job-root", required=True)
            action.add_argument("--operation", required=True)
            action.add_argument(
                "--arguments", required=True, help="JSON object; see windows describe"
            )
            action.add_argument("--script")
            action.add_argument("--timeout", type=float, default=600)
        action.set_defaults(handler=main)
    for name in ("status", "cancel", "wait", "artifacts"):
        action = actions.add_parser(name)
        action.add_argument("directory")
        action.set_defaults(handler=main)


def main(args):
    action = args.windows_action
    if action == "describe":
        result = runtime.describe(args.application)
    elif action in ("status", "cancel", "wait", "artifacts"):
        result = globals()[action](args.directory)
    else:
        bridge = WindowsBridge(args.application, args.installation, args.python)
        if action == "check":
            result = bridge.check()
        elif action == "inspect":
            result = bridge.inspect(
                args.project, args.result, allow_open=args.allow_open, pid=args.pid
            )
        else:
            arguments = json.loads(args.arguments)
            result = bridge.submit_application(
                args.project,
                args.job_root,
                args.operation,
                arguments,
                script=args.script,
                timeout=args.timeout,
            )
    print(json.dumps(result, indent=2, allow_nan=False))
    return exit_code(action, result)
