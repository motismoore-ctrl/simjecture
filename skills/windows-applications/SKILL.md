---
name: windows-applications
description: Control installed Windows engineering applications from WSL through the shared Simjecture job protocol. Use for CST and experimental SOLIDWORKS automation, operation discovery, native scripts, parameter changes, geometry export, job monitoring, cancellation and artifacts. COMSOL execution is not implemented.
---

# Windows application jobs

Use the common `simjecture windows` interface through operator-authorized host
tools. It dispatches to vendor-specific adapters and records exploratory jobs.
It does not provide an evidence-stage `lab.run` capability or grant an API agent
access outside its configured execution boundary.

## Discover the actual interface

Start with `simjecture windows describe`, optionally selecting `--application cst`
or `--application solidworks`. Discovery needs no vendor installation and reports
operation argument schemas, units, supported project types and qualification limits.
COMSOL is a future adapter, not an executable operation in the current registry.

Use `windows check --application NAME --installation PATH --python PATH` to load
the installed interface without starting a model. A successful check establishes
interface availability only. The Windows account supplies the applications,
compatible interpreter, optional libraries and licenses.

Use `windows inspect --application solidworks --installation PATH --python PATH`
to read the running desktop session's PID, version, active document and open
document metadata. It reports unsaved changes without saving, editing or closing
the session. An optional `--project PATH` filters already-open documents; it does
not open the file. Use the same Windows account and elevation as SOLIDWORKS.
Inspection verifies the session executable against `--installation`. If several
sessions match, select one of the reported process IDs with `--pid NUMBER`.
An explicit PID must still belong to the selected installation; inspection never
falls back to another session when the selected one is unavailable.

## Submit native work

`windows submit` requires the application, installation, Windows Python, project,
Windows-drive job root, operation, JSON `--arguments` and a bounded `--timeout`.
Derive argument names and observables from the model and adapter schema.

- CST `magnetostatic.solve` accepts finite numeric `parameters` in model units and
  explicit saved 0D/1D `results` tree paths. Existing `simjecture cst` commands
  use the same runner. Read the `cst` skill for commissioning and result meaning.
- CST `project.python` stages `--script PATH` as `inputs/automation.py`. Its
  arguments contain `parameters`, `results` and `script`. The script receives the
  owned `project`, `environment`, `work_directory`, a parameter copy and `result`.
  Leave JSON-serializable observations in `result`. The adapter saves and closes
  its copy; no solver starts implicitly. Read `cst-geometry` for modeling history
  and geometry verification.
- SOLIDWORKS `part.rebuild_export` accepts self-contained `.sldprt` parts,
  `dimensions_si` and `exports` from `step`, `iges`, `stl`. Dimension values are
  metres/radians, independent of display units. This adapter needs Windows
  `pywin32`. Part rebuilding and export have been commissioned on SOLIDWORKS 2025;
  a particular model still needs its own geometry checks. Batch work starts an
  owned process and binds its PID-specific automation entry, preserving existing
  desktop sessions. Mock tests are not CAD validation.

The runner snapshots complete inputs and its native source bundle, then verifies
the writable copy before application calls. Native scripts execute as trusted
Windows host code. Use the supplied owned project and preserve the original.

## Monitor and inspect

Use the returned directory with `windows status`, `windows wait`, `windows cancel`
and `windows artifacts`. Cancellation is a request until confirmed. `wait` exits
130 for a cancelled job; a successful cancellation request exits zero. Artifact
collection verifies the recorded hashes.

If a receipt is `interrupted` or says the stop is unconfirmed, inspect its logs
and activation record before starting another job on that installation. COM
activation may fail before a process can be identified; do not claim it stopped.

Report the application/version, operation, input and output paths, parameters and
units, actual checks, and remaining model/solver limitations. Keep execution
success, geometry correctness and numerical accuracy distinct. Jobs remain
`stage="exploration"` and `evidence_eligible=false`.
