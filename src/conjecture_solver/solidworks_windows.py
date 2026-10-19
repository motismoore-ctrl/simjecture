"""Experimental SOLIDWORKS COM adapter for self-contained parts.

The optional pywin32 dependency belongs to the selected Windows interpreter.
Native part rebuild/export is commissioned on SOLIDWORKS 2025; model geometry
and numerical accuracy still require independent validation.
"""

from __future__ import annotations

import ctypes
import math
import re
import subprocess
import sys
import time
from pathlib import Path


def describe():
    return {
        "application": "solidworks",
        "project_suffixes": [".sldprt"],
        "automation": "COM via pywin32",
        "session": "owned desktop COM process",
        "operations": {
            "part.rebuild_export": {
                "arguments_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["dimensions_si", "exports"],
                    "properties": {
                        "dimensions_si": {
                            "type": "object",
                            "maxProperties": 100,
                            "propertyNames": {"type": "string", "minLength": 1, "maxLength": 256},
                            "additionalProperties": {"type": "number"},
                        },
                        "exports": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 3,
                            "uniqueItems": True,
                            "items": {"enum": ["step", "iges", "stl"]},
                        },
                    },
                },
                "arguments": {
                    "dimensions_si": "dimension names and finite SI values (metres/radians)",
                    "exports": "one or more of step, iges, stl",
                },
            },
        },
        "requirements": ["registered SOLIDWORKS COM server", "Windows Python with pywin32"],
        "qualification": (
            "experimental adapter; part rebuild/export commissioned on SOLIDWORKS 2025; "
            "assemblies and SOLIDWORKS Simulation are not qualified"
        ),
    }


def load_com():
    if sys.platform != "win32":
        raise RuntimeError("The SOLIDWORKS adapter requires Windows Python")
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise RuntimeError("Install pywin32 in the selected Windows Python interpreter") from error
    return pythoncom, win32com.client


def com_methods(dispatch, *names):
    """Keep pywin32's late binding from invoking no-argument methods as properties."""
    flag = getattr(dispatch, "_FlagAsMethod", None)
    if flag is not None:
        flag(*names)
    return dispatch


def check(installation):
    load_com()
    from pywintypes import IID

    executable = Path(installation) / "SLDWORKS.exe"
    if not executable.is_file():
        raise ValueError("Choose the SOLIDWORKS directory containing SLDWORKS.exe")
    return {
        "interfaces_loaded": True,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "installation": installation,
        "com_class": str(IID("SldWorks.Application")),
        "application_started": False,
        "model_tested": False,
    }


def document_info(model):
    model = com_methods(
        model, "GetTitle", "GetPathName", "GetType", "GetSaveFlag", "IsOpenedReadOnly"
    )
    kind = model.GetType()
    return {
        "title": model.GetTitle(),
        "path": model.GetPathName(),
        "type": {1: "part", 2: "assembly", 3: "drawing"}.get(kind, "unknown"),
        "modified": bool(model.GetSaveFlag()),
        "read_only": bool(model.IsOpenedReadOnly()),
    }


def process_executable(pid):
    """Read a process image using query-only access, without claiming its process."""
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)
    ]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(image.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def running_entries(pythoncom):
    """Enumerate SOLIDWORKS monikers without depending on unrelated COM servers."""
    table = pythoncom.GetRunningObjectTable()
    binding = pythoncom.CreateBindCtx(0)
    for moniker in table:
        try:
            name = moniker.GetDisplayName(binding, None)
        except pythoncom.com_error:
            # Some monikers have no display name; others disappear during a scan.
            continue
        match = re.fullmatch(r"SolidWorks_PID_([1-9][0-9]*)", name)
        if match:
            yield int(match[1]), table, moniker


def bind_application(pid, table, moniker, pythoncom, client):
    application = com_methods(
        client.Dispatch(table.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)),
        "GetProcessID", "RevisionNumber", "ExitApp",
    )
    if application.GetProcessID() != pid:
        raise RuntimeError("SOLIDWORKS automation returned a different application process")
    return application


def inspection_sessions(executable, pythoncom, pid):
    sessions = {}
    for candidate, table, moniker in running_entries(pythoncom):
        if pid is not None and candidate != pid:
            continue
        try:
            image = process_executable(candidate)
        except OSError as error:
            if pid is not None:
                raise RuntimeError("Cannot verify the selected SOLIDWORKS process") from error
            continue  # Exited or inaccessible processes cannot be inspected safely.
        if image != executable:
            if pid is not None:
                raise ValueError("The selected PID belongs to a different installation")
            continue
        sessions[candidate] = (table, moniker)
    return sessions


def inspect_project(installation, project=None, paths=(), allow_open=False, *, pid=None):
    """Read an existing desktop session without owning, changing or closing it."""
    if paths or allow_open:
        raise ValueError(
            "SOLIDWORKS inspection reads open documents; omit --result and --allow-open"
        )
    if pid is not None and (type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF):
        raise ValueError("Choose a positive Windows process ID with --pid")
    check(installation)
    pythoncom, client = load_com()
    pythoncom.CoInitialize()
    application = active = table = moniker = None
    sessions = {}
    try:
        executable = (Path(installation) / "SLDWORKS.exe").resolve()
        sessions = inspection_sessions(executable, pythoncom, pid)
        if not sessions:
            raise RuntimeError(
                "No matching SOLIDWORKS session is accessible for this installation/PID; "
                "open it under the same Windows user and elevation as the selected interpreter"
            )
        if len(sessions) > 1:
            raise ValueError(
                "Multiple SOLIDWORKS sessions match this installation; select --pid from: "
                + ", ".join(map(str, sorted(sessions)))
            )
        selected = next(iter(sessions))
        table, moniker = sessions[selected]
        try:
            application = bind_application(selected, table, moniker, pythoncom, client)
        except pythoncom.com_error as error:
            raise RuntimeError(
                f"SOLIDWORKS PID {selected} is unavailable or busy; retry"
            ) from error
        com_methods(application, "GetProcessID", "RevisionNumber", "GetDocuments")
        documents = [document_info(model) for model in (application.GetDocuments() or ())]
        if project is not None:
            requested = str(Path(project).resolve()).casefold()
            documents = [
                item for item in documents
                if item["path"] and str(Path(item["path"]).resolve()).casefold() == requested
            ]
            if not documents:
                raise ValueError("The requested document is not open in the SOLIDWORKS session")
        active = application.ActiveDoc
        return {
            "pid": application.GetProcessID(),
            "revision": application.RevisionNumber(),
            "active_document": document_info(active) if active is not None else None,
            "documents": documents,
            "application_started": False,
            "session_owned": False,
            "model_modified": False,
        }
    finally:
        active = application = table = moniker = None
        sessions.clear()
        pythoncom.CoUninitialize()


def start_application(executable, context, pythoncom, client, *, timeout=120):
    """Start an owned process and bind its PID-specific Running Object Table entry."""
    started = context["begin_external_activation"](executable)
    process = subprocess.Popen([str(executable)])
    context["claim_process"](process.pid, started, executable)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("SOLIDWORKS exited before registering its automation interface")
        for candidate, table, moniker in running_entries(pythoncom):
            if candidate != process.pid:
                continue
            try:
                return bind_application(candidate, table, moniker, pythoncom, client)
            except pythoncom.com_error:
                # Registration can race startup or a busy server. Retry within the deadline.
                continue
        time.sleep(0.2)
    raise TimeoutError(
        "SOLIDWORKS did not register its automation interface before startup timed out"
    )


def input_sources(project, script=None):
    project = Path(project)
    if script is not None:
        raise ValueError("This SOLIDWORKS operation does not accept a native script")
    if project.suffix.lower() != ".sldprt" or not project.is_file():
        raise ValueError("Supply a self-contained SOLIDWORKS .sldprt part")
    return {"inputs/project.sldprt": project}


def validate(operation, arguments, inputs):
    if operation != "part.rebuild_export" or not isinstance(arguments, dict):
        raise ValueError("Unsupported SOLIDWORKS operation")
    if set(arguments) != {"dimensions_si", "exports"}:
        raise ValueError("Supply dimensions_si and exports")
    dimensions = arguments["dimensions_si"]
    if not isinstance(dimensions, dict) or len(dimensions) > 100:
        raise ValueError("Invalid SOLIDWORKS dimensions")
    for name, number in dimensions.items():
        if not isinstance(name, str) or not name or len(name) > 256 or "\x00" in name:
            raise ValueError("Invalid SOLIDWORKS dimension name")
        if type(number) not in (float, int) or not math.isfinite(number):
            raise ValueError("Dimension values must be finite SI numbers")
    exports = arguments["exports"]
    if (
        not isinstance(exports, list)
        or not exports
        or any(name not in ("step", "iges", "stl") for name in exports)
        or len(set(exports)) != len(exports)
    ):
        raise ValueError("Choose distinct step, iges or stl exports")
    if set(inputs) != {"inputs/project.sldprt"}:
        raise ValueError("Only a self-contained part is supported; assemblies are not staged")


def execute(job, request, context):
    pythoncom, client = load_com()
    pythoncom.CoInitialize()
    application = None
    model = active = dimension = None
    owned = False
    try:
        # DispatchEx may return an existing SOLIDWORKS session. Launch the selected
        # executable instead and bind only the owned PID's automation moniker.
        executable = Path(request["installation"]) / "SLDWORKS.exe"
        application = start_application(executable, context, pythoncom, client)
        pid = application.GetProcessID()
        owned = True
        application.Visible = False
        application.UserControl = False

        def integer():
            return client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)

        errors, warnings = integer(), integer()
        project = job / "work/project.sldprt"
        application.SetCurrentWorkingDirectory(str(project.parent))
        model = com_methods(
            application.OpenDoc6(str(project), 1, 1, "", errors, warnings), "GetTitle"
        )
        if model is None or errors.value:
            raise RuntimeError("SOLIDWORKS failed to open the part: " + str(errors.value))
        open_warnings = warnings.value
        observed = {}
        for name, number in request["arguments"]["dimensions_si"].items():
            dimension = model.Parameter(name)
            if dimension is None:
                raise ValueError("Unknown SOLIDWORKS dimension: " + name)
            dimension.SystemValue = number
        if not model.ForceRebuild3(False):
            raise RuntimeError("SOLIDWORKS part rebuild failed")
        for name, expected in request["arguments"]["dimensions_si"].items():
            actual = model.Parameter(name).SystemValue
            if not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-12):
                raise ValueError("Rebuilt dimension differs from request: " + name)
            observed[name] = actual
        errors, warnings = integer(), integer()
        if not model.Save3(1, errors, warnings) or errors.value:
            raise RuntimeError("SOLIDWORKS failed to save the working part")
        activation = integer()
        active = application.ActivateDoc3(model.GetTitle(), False, 0, activation)
        if active is None or activation.value:
            raise RuntimeError("SOLIDWORKS failed to activate the part for export")
        active.ClearSelection2(True)
        exported = []
        for extension in request["arguments"]["exports"]:
            target = project.with_suffix("." + {"iges": "igs"}.get(extension, extension))
            errors, warnings = integer(), integer()
            export_data = client.VARIANT(pythoncom.VT_DISPATCH, None)
            if not active.Extension.SaveAs(str(target), 0, 1, export_data, errors, warnings):
                raise RuntimeError(
                    "SOLIDWORKS export failed: " + extension
                    + " (errors=" + str(errors.value) + ", warnings=" + str(warnings.value) + ")"
                )
            # SOLIDWORKS can normalize the suffix (for example .stl to .STL).
            # Report the actual spelling used by the complete artifact inventory.
            produced = [
                p for p in target.parent.iterdir() if p.name.casefold() == target.name.casefold()
            ]
            if (
                errors.value or len(produced) != 1 or not produced[0].is_file()
                or produced[0].stat().st_size == 0
            ):
                raise RuntimeError("SOLIDWORKS did not produce a valid export: " + extension)
            exported.append(produced[0].relative_to(job).as_posix())
        application.CloseDoc(model.GetTitle())
        return {
            "dimensions_si": observed,
            "exports": exported,
            "solidworks_runtime": {"pid": pid, "revision": application.RevisionNumber()},
            "open_warnings": open_warnings,
        }
    finally:
        try:
            if owned:
                application.ExitApp()
        finally:
            model = active = dimension = None
            application = None
            pythoncom.CoUninitialize()
