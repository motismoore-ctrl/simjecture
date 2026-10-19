# Control Windows applications through a common job interface

`simjecture windows` provides an experimental operator-side interface for installed
Windows engineering applications. Simjecture runs in WSL; a shared Windows runner
manages jobs and dispatches application-specific work to an adapter.

```text
Simjecture CLI / Python client
             |
             v
Common request, snapshot, supervisor and artifact protocol
             |
        application adapter
          /           \
   CST Python/VBA   SOLIDWORKS COM
```

The shared protocol handles discovery, complete input snapshots, a frozen runner
bundle, asynchronous execution, deadlines, cancellation, application process
ownership and output hashes. Adapters supply model staging, operation validation
and native API calls. Product-specific geometry selections, parameter meanings,
units and solver settings remain explicit.

## Discover and submit operations

Discovery works without Windows Python or a vendor installation:

```bash
simjecture windows describe
simjecture windows describe --application cst
simjecture windows describe --application solidworks
```

Each adapter lists its operations, argument meanings, project types, runtime
requirements and qualification limits. `check` loads an installed interface; it
does not start a model or qualify a numerical result:

```bash
simjecture windows check --application cst \
  --installation '/mnt/c/Program Files (x86)/CST Studio Suite 2024' \
  --python '/mnt/c/Program Files (x86)/CST Studio Suite 2024/Opera/code/bin/python.exe'
```

Native agents can read the `windows-applications` skill. API agents can discover
and read it with `read_skill(name="windows-applications")`; this supplies interface
guidance and does not grant Windows host execution permissions.

The common submission form selects an application and an operation. Arguments
are a JSON object validated by that adapter before a job is allocated:

```bash
simjecture windows submit --application cst \
  --installation '/mnt/c/Program Files (x86)/CST Studio Suite 2024' \
  --python '/mnt/c/Program Files (x86)/CST Studio Suite 2024/Opera/code/bin/python.exe' \
  --project '/mnt/c/work/Two Coils.cst' \
  --job-root '/mnt/c/work/application-jobs' \
  --operation magnetostatic.solve \
  --arguments '{"parameters":{"r":2},"results":["1D Results\\Ms Solver\\Inductance Matrix\\L coil1, coil1"]}' \
  --timeout 600
```

The existing [`simjecture cst` commands](cst-windows.md) remain available and submit
through the same shared runner. CST numeric parameter values use the model's
native units.

Use the returned job directory with any of the common lifecycle commands:

```bash
simjecture windows status '/mnt/c/work/application-jobs/cst_JOB_ID'
simjecture windows wait '/mnt/c/work/application-jobs/cst_JOB_ID'
simjecture windows cancel '/mnt/c/work/application-jobs/cst_JOB_ID'
simjecture windows artifacts '/mnt/c/work/application-jobs/cst_JOB_ID'
```

`cancel` requests a stop and returns successfully when the request is recorded.
`wait` returns exit code 130 for a confirmed cancellation and a nonzero exit code
for failed, timed-out or interrupted jobs. A lost supervisor is reported as an
unconfirmed stop. `artifacts` verifies the saved result manifest and every returned
file hash before returning the list.

## Native CST scripts

The `project.python` operation opens a copied CST project and executes an explicitly
supplied trusted Python script using the installed CST APIs. This reaches modeling
features beyond the structured magnetostatic recipe. It does not automatically
launch a solver.

The script receives `project`, `environment`, `work_directory`, `parameters`, and
an initially empty `result` dictionary. Leave JSON-serializable observations in
`result`; generated files under `work_directory` become artifacts. The adapter
saves and closes its working project and application after the script.

```python
# automation.py: use the supplied, owned project for native CST API operations.
result = {"parameters_received": parameters}
```

```bash
simjecture windows submit --application cst \
  --installation '/mnt/c/Program Files (x86)/CST Studio Suite 2024' \
  --python '/mnt/c/Program Files (x86)/CST Studio Suite 2024/Opera/code/bin/python.exe' \
  --project '/mnt/c/work/model.cst' --job-root '/mnt/c/work/application-jobs' \
  --operation project.python --script ./automation.py \
  --arguments '{"parameters":{},"results":[],"script":"inputs/automation.py"}'
```

The script is included in the input snapshot. This is trusted Windows host code,
with the account's access. Read the installed CST API documentation and the
`cst-geometry` skill for modeling history, geometry and assembly verification.

## Experimental SOLIDWORKS part operations

The SOLIDWORKS adapter requires a registered desktop SOLIDWORKS COM server and
`pywin32` in the selected Windows Python interpreter. Its initial operation supports
self-contained `.sldprt` parts, dimension changes, rebuilding and STEP/IGES/STL
exports. It does not stage assembly references or run SOLIDWORKS Simulation.

To inspect an already-open session without changing or saving any document:

```bash
simjecture windows inspect --application solidworks \
  --installation '/mnt/c/Program Files/SOLIDWORKS Corp/SOLIDWORKS' \
  --python '/mnt/c/Python312/python.exe'
```

The response includes the session PID and revision, active document, open document
paths/types, unsaved-change flags and read-only flags. `--project PATH` optionally
filters the open-document list; inspection never opens the requested path. A
session with no open document returns `active_document: null`. Run Windows Python
under the same account and elevation as the desktop application. Install `pywin32`
in that Windows interpreter, rather than in the WSL Python environment.

Inspection binds only a session whose process executable matches the selected
installation. When several matching sessions are open, it reports their PIDs and
requires `--pid NUMBER` to choose one. The chosen PID is still checked against
`--installation`; a missing, inaccessible or mismatched process is not replaced
with another session. The `--pid` option applies only to SOLIDWORKS inspection.

To rebuild and export a copied part in a separate, owned process:

```bash
simjecture windows submit --application solidworks \
  --installation '/mnt/c/Program Files/SOLIDWORKS Corp/SOLIDWORKS' \
  --python '/mnt/c/Python312/python.exe' \
  --project '/mnt/c/work/part.sldprt' --job-root '/mnt/c/work/application-jobs' \
  --operation part.rebuild_export \
  --arguments '{"dimensions_si":{"D1@Sketch1":0.02},"exports":["step"]}'
```

Dimension names must come from the actual part. Values are native API SI values:
metres for lengths and radians for angles, irrespective of the model display
units. The adapter checks the rebuilt values and clears selections before export
to avoid exporting only selected faces or bodies.

Batch submission starts the selected `SLDWORKS.exe`, verifies its creation time
and executable, and claims it in the supervisor's Windows Job Object before
opening a model. The adapter waits for that PID's `SolidWorks_PID_<pid>` entry in
the Windows Running Object Table. Generic COM activation can reuse an existing
desktop session even with `DispatchEx`; the batch adapter never uses that route.
Unreadable entries belonging to other COM objects are skipped. Temporary failures
binding the owned process are retried within the startup deadline. Cancellation
or failure terminates only the owned job.
The adapter releases COM objects on their initialized thread. Read-only inspection
connects to an existing session without taking process ownership or closing it.

Exports use `.step`, `.igs` and `.stl`; SOLIDWORKS may capitalize the STL suffix.
The export list records the actual filenames, matching the hashed artifact
inventory. Export failures include the native error and warning codes. See the
vendor's [SaveAs reference](https://help.solidworks.com/2026/English/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IModelDocExtension~SaveAs.html)
for conversion formats, active-document requirements and selection handling.

### Native commissioning

On 2026-10-19, the shared WSL client, detached Windows supervisor and frozen worker
bundle were exercised with SOLIDWORKS 2025 revision 33.5.0, Windows Python 3.14.2
and pywin32 312. Read-only inspection connected to an existing desktop session.
A copied vendor `samples/tutorial/api/cube.sldprt` rebuilt and exported nonempty
STEP, IGES and STL files in a distinct owned process, while the original desktop
process remained open. Input and artifact hashes were checked through the common
job protocol. Native models and desktop metadata remain in local commissioning
records.

This establishes native orchestration and part export. Geometric equivalence and
numerical accuracy need separate validation. Contract tests cover SI dimension
changes and failure cleanup; the native export commissioning used `dimensions_si: {}`. Validate the
specific model, dimensions and exported geometry before relying on them. Assembly
staging, SOLIDWORKS Simulation and other releases remain unqualified.

## Adapter and protocol contract

Each built-in adapter implements:

- `describe()`: supported operations, units, project types and requirements;
- `check(installation)`: load the native interface without starting a model;
- `input_sources(project, script)`: map source files/directories to snapshot paths;
- `validate(operation, arguments, inputs)`: reject unsupported or ambiguous tasks;
- `execute(job, request, context)`: operate on `job/work`, close the owned
  application and return a JSON object. Use `context["begin_external_activation"]`
  before activating an external COM server and `context["claim_process"]` to adopt
  its new PID before model access.

Keep discovery, staging and validation importable on Linux without vendor packages.
Native modules use Python 3.9-compatible stdlib code and import optional vendor/COM
libraries only when needed. Register a new adapter in `windows_runtime.ADAPTERS`;
its source then joins the complete per-job runtime bundle. A future COMSOL adapter
can use its Java API or batch interface behind this contract. COMSOL execution is
not implemented or advertised by the current registry.

Requests use protocol `simjecture-windows/1` and bind the application, operation,
arguments, input file inventory, complete native source bundle, selected Windows
Python executable hash, creation time and timeout. The worker rejects added,
removed or changed input/runtime files and verifies its writable copy before any
application call. Terminal receipts bind `results.json`; that file contains the
working output inventory and hashes. Historical CST terminal records remain
readable through the lifecycle commands.

The native runner can be invoked directly by Windows Python; it does not import
the Linux Simjecture package. The current client uses local WSL interoperability
and Windows-drive job directories. Remote Windows transport, a governed
`lab.run` execution pool, Windows CPU/RAM quotas and a persistent session API are
outside this iteration. All jobs remain exploratory with `evidence_eligible=false`.

## Validation

`pytest tests/test_windows_applications.py tests/test_cst.py` covers the common
protocol, complete input inventories, working-copy integrity, native-script
dispatch, cancellation exit codes and mocked SOLIDWORKS operations.

`python scripts/check_cst_windows.py` runs the shared native Windows process tests
without vendor applications or additional packages. It includes a frozen-bundle
fixture adapter executed through the real detached supervisor and worker. The
fixture establishes orchestration behavior, not vendor API or solver accuracy.
`--runner PATH` can select a packaged runner; the original `cst_windows.py`
entrypoint remains covered separately for compatibility.
