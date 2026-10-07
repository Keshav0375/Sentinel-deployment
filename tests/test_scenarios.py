"""The scenario catalog and the 30 scenario branches (deployment §4, §4.1).

`scenarios/branches.yaml` is checked against §4.1: 30 entries, 10 per case, the label
enums, and the case-ii stage table. Each branch is then checked against its own entry
through git refs (`origin/<branch>`, else a local branch): one `scenario:` commit on top
of `main`, only deployable paths, a clean merge, and the fault itself.

The fault is run, not grepped. The branch's tree is extracted and its app imported in a
subprocess with a scrubbed environment. That probe calls what verify calls (`/health`,
`/version`), ages the package files past runtime/07's 5 minutes, then calls what the
synthetics call (`/`, `/health`). The synthetics' own assertions, read from
datadog/synthetics/, judge those responses. Build- and deploy-stage faults never run
app code, so their trees are checked for what the build step or Oryx trips on.

Without the scenario refs (a fresh clone that never fetched them) the branch checks
skip and say why. A partial set fails: the catalog and origin must agree.
"""

import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "scenarios" / "branches.yaml"
SYNTHETICS = ROOT / "datadog" / "synthetics"

FIELDS = {
    "branch",
    "case",
    "fault",
    "expected_signal_type",
    "expected_resolution",
    "also_expected",
    "expected_culprit",
}
# case → (branch prefix, expected_signal_type, expected_resolution, expected_culprit)
CASES = {
    "i": ("pass", "none", "none", "none"),
    "ii": ("deployfail", "deploy_failure", "rollback", "self"),
    "iii": ("runtime", "runtime_error", "rollback_or_escalate", "self"),
}
# §4.1's case-ii table: stage and also_expected per branch.
CASE_II = {
    "deployfail/01": ("build", []),
    "deployfail/02": ("build", []),
    "deployfail/03": ("deploy", []),
    "deployfail/04": ("deploy", []),
    "deployfail/05": ("verify", ["runtime_error"]),
    "deployfail/06": ("verify", []),
    "deployfail/07": ("verify", ["runtime_error"]),
    "deployfail/08": ("verify", ["runtime_error"]),
    "deployfail/09": ("verify", ["runtime_error"]),
    "deployfail/10": ("verify", ["runtime_error"]),
}
# The synthetic each case-iii branch trips (§4.1); every other one trips GET /.
RUNTIME_TARGET = {"runtime/07": "/health"}

ENTRIES = yaml.safe_load(CATALOG.read_text())["branches"]
BY_BRANCH = {entry["branch"]: entry for entry in ENTRIES}


# ── The catalog ───────────────────────────────────────────────────────────────


def test_catalog_lists_ten_branches_per_case_in_order():
    expected = [
        f"{prefix}/{n:02d}" for prefix, *_ in CASES.values() for n in range(1, 11)
    ]
    assert [entry["branch"] for entry in ENTRIES] == expected


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e["branch"])
def test_entry_carries_the_labels_of_its_case(entry):
    case = entry["case"]
    prefix, signal, resolution, culprit = CASES[case]
    stage_field = {"expected_failed_stage"} if case == "ii" else set()
    assert set(entry) == FIELDS | stage_field
    assert entry["branch"].startswith(f"{prefix}/")
    assert isinstance(entry["fault"], str) and entry["fault"].strip()
    assert entry["expected_signal_type"] == signal
    assert entry["expected_resolution"] == resolution
    assert entry["expected_culprit"] == culprit
    assert isinstance(entry["also_expected"], list)
    assert set(entry["also_expected"]) <= {"runtime_error"}
    if case == "ii":
        stage, also = CASE_II[entry["branch"]]
        assert entry["expected_failed_stage"] == stage
        assert entry["also_expected"] == also
    else:
        assert entry["also_expected"] == []


def test_catalog_passes_yamllint():
    if shutil.which("yamllint") is None:
        pytest.skip("yamllint is not on PATH")
    proc = subprocess.run(
        ["yamllint", "-s", "-c", ".yamllint.yaml", "scenarios/branches.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ── Branches, through git refs ───────────────────────────────────────────────


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, check=False
    )


def _refs() -> dict[str, str]:
    """refname → sha for local branches and origin's, or {} outside a git checkout."""
    try:
        proc = git(
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/remotes/origin",
        )
    except OSError:
        return {}
    if proc.returncode != 0:
        return {}
    return dict(line.split() for line in proc.stdout.decode().splitlines())


REFS = _refs()


def _resolve(branch: str) -> str | None:
    """origin's copy first: that is what a scenario run merges."""
    for name in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        if name in REFS:
            return REFS[name]
    return None


MAIN = _resolve("main")
SCENARIO_SHAS = {branch: _resolve(branch) for branch in BY_BRANCH}


@pytest.fixture
def sha(request) -> str:
    branch = request.node.callspec.params["branch"]
    if MAIN is None:
        pytest.skip("no main ref: not a git checkout, or main was never fetched")
    if not any(SCENARIO_SHAS.values()):
        pytest.skip(
            "no scenario refs locally; run `git fetch origin` to check the branches"
        )
    if SCENARIO_SHAS[branch] is None:
        pytest.fail(f"{branch} is in branches.yaml but not on origin or local")
    return SCENARIO_SHAS[branch]


BRANCHES = pytest.mark.parametrize("branch", list(BY_BRANCH))


@BRANCHES
def test_branch_is_one_scenario_commit_on_main(branch, sha):
    count = git("rev-list", "--count", f"{MAIN}..{sha}").stdout.decode().strip()
    assert count == "1", f"{branch} carries {count} commits not on main"
    assert git("merge-base", "--is-ancestor", f"{sha}^", MAIN).returncode == 0
    subject = git("log", "-1", "--format=%s", sha).stdout.decode()
    assert subject.startswith(f"scenario: {branch} ")


@BRANCHES
def test_branch_touches_only_what_deploys(branch, sha):
    """The deploy's path filter is app/** + requirements.txt: anything else and the
    merge never deploys. A pass/* branch may also keep tests/test_app.py in step."""
    proc = git("diff-tree", "-r", "-z", "--no-commit-id", "--name-only", f"{sha}^", sha)
    paths = [p for p in proc.stdout.decode().split("\0") if p]
    deploys = [p for p in paths if p.startswith("app/") or p == "requirements.txt"]
    assert deploys, f"{branch} changes nothing the deploy ships: {paths}"
    allowed = {"tests/test_app.py"} if BY_BRANCH[branch]["case"] == "i" else set()
    assert set(paths) - set(deploys) <= allowed
    if BY_BRANCH[branch]["case"] == "i":
        assert any(p.startswith("app/") for p in deploys)


@BRANCHES
def test_branch_merges_cleanly_into_main(branch, sha):
    proc = git("merge-tree", "--write-tree", MAIN, sha)
    assert proc.returncode == 0, proc.stdout.decode() + proc.stderr.decode()


# ── The fault itself ─────────────────────────────────────────────────────────

PROBE_VERSION = "pr-0-scenario"
PROBE_TIMEOUT = 10  # s per request; runtime/04 must still be blocked when it expires
PACKAGE_AGE = 360  # s; past runtime/07's 5 minutes, as by the time a synthetic runs

# Runs inside the branch's tree. Prints one `PROBE <json>` line.
PROBE = r"""
import asyncio, json, os, sys, time
from pathlib import Path

age, timeout = float(sys.argv[1]), float(sys.argv[2])
out = {"import": None, "startup": None, "verify": {}, "synthetics": {}}

def stamp(mtime):
    for f in Path("app").rglob("*.py"):
        os.utime(f, (mtime, mtime))

# Just deployed: `git archive` stamps every file with the commit time, but the build
# step's `cp` gives the deployed package the time of the run.
stamp(time.time())

def emit():
    print("PROBE " + json.dumps(out), flush=True)
    sys.exit(0)

try:
    from app import main
except BaseException as exc:
    out["import"] = type(exc).__name__
    emit()
out["import"] = "ok"

from fastapi.testclient import TestClient
try:
    with TestClient(main.app):
        pass
except BaseException as exc:
    out["startup"] = type(exc).__name__
    emit()
out["startup"] = "ok"

import httpx

async def get(client, path):
    try:
        r = await asyncio.wait_for(client.get(path), timeout)
    except TimeoutError:
        return {"status": None, "content_type": "", "json": None}
    try:
        body = r.json()
    except ValueError:
        body = None
    return {"status": r.status_code, "content_type": r.headers.get("content-type", ""),
            "json": body}

async def run():
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as c:
        for path in ("/health", "/version"):
            out["verify"][path] = await get(c, path)
        stamp(time.time() - age)
        for path in ("/", "/health"):
            out["synthetics"][path] = await get(c, path)

asyncio.run(run())
emit()
"""


def scrubbed_env() -> dict[str, str]:
    """No ambient APP_VERSION, DD_*, or whatever env a fault reads."""
    return {
        "PATH": os.environ["PATH"],
        "APP_VERSION": PROBE_VERSION,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def extract(sha: str, dest: Path) -> Path:
    proc = git("archive", "--format=tar", sha)
    assert proc.returncode == 0, proc.stderr.decode()
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        tar.extractall(dest, filter="tar")  # keeps symlinks: deployfail/01 is one
    return dest


def probe(tree: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, str(PACKAGE_AGE), str(PROBE_TIMEOUT)],
        cwd=tree,
        env=scrubbed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")]
    assert lines, f"the probe printed no result:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(lines[-1].removeprefix("PROBE "))


SYNTHETIC_ASSERTIONS = {
    json.loads(p.read_text())["config"]["request"]["url"].removeprefix(
        "__APP_URL__"
    ): json.loads(p.read_text())["config"]["assertions"]
    for p in SYNTHETICS.glob("*.json")
}


def synthetic_passes(route: str, resp: dict | None) -> bool:
    """Datadog's verdict on one response, from the test's own assertions."""
    if resp is None or resp["status"] is None:  # down, or timed out
        return False
    for assertion in SYNTHETIC_ASSERTIONS[route]:
        match assertion:
            case {"type": "statusCode", "operator": "is", "target": target}:
                ok = resp["status"] == target
            case {
                "type": "header",
                "property": "content-type",
                "operator": "contains",
                "target": target,
            }:
                ok = target in resp["content_type"]
            case {
                "type": "body",
                "operator": "validatesJSONPath",
                "target": {"jsonPath": path, "operator": "is", "targetValue": value},
            } if path.count(".") == 1 and path.startswith("$."):
                body = resp["json"]
                ok = isinstance(body, dict) and body.get(path[2:]) == value
            case _:
                raise AssertionError(f"the probe cannot evaluate {assertion}")
        if not ok:
            return False
    return True


def verify_passes(result: dict) -> bool:
    """ci_app_deployment.yml's verify: `curl -sf /health`, then /version's version."""
    if result["import"] != "ok" or result["startup"] != "ok":
        return False
    health, version = result["verify"]["/health"], result["verify"]["/version"]
    if health["status"] is None or not 200 <= health["status"] < 400:
        return False
    body = version["json"]
    return (
        version["status"] == 200
        and isinstance(body, dict)
        and body.get("version") == PROBE_VERSION
    )


def fired(result: dict) -> set[str]:
    """The synthetics that fail once the deploy is > 5 min old."""
    routes = ("/", "/health")
    if result["import"] != "ok" or result["startup"] != "ok":
        return set(routes)  # the app is down
    return {r for r in routes if not synthetic_passes(r, result["synthetics"][r])}


def requirement_pins(tree: Path) -> dict[str, str]:
    pins = {}
    for line in (tree / "requirements.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            req = Requirement(line)
            (spec,) = req.specifier
            assert spec.operator == "==", f"not an exact pin: {line}"
            pins[req.name.lower()] = spec.version
    return pins


def build_deploy_fault(branch: str, tree: Path) -> None:
    """Case ii, build or deploy stage: what the build step or Oryx refuses."""
    main_pins = requirement_pins(ROOT)
    if branch == "deployfail/01":
        # The build step's own test: `find app requirements.txt -type l`.
        links = [p for p in (tree / "app").rglob("*") if p.is_symlink()]
        assert links, "no symlink under app/"
        assert not (tree / "requirements.txt").is_symlink()
    elif branch == "deployfail/02":
        # The build step's `cp requirements.txt deploy_package/` has nothing to copy.
        assert not (tree / "requirements.txt").exists()
        assert (tree / "app" / "main.py").is_file()
    elif branch == "deployfail/03":
        pins = requirement_pins(tree)
        assert pins.pop("nonexistent-package") == "1.0.0"
        assert pins == main_pins
    elif branch == "deployfail/04":
        pins = requirement_pins(tree)
        starlette = pins.pop("starlette")
        assert pins == main_pins
        # fastapi's own declared range, from the pinned fastapi installed here.
        assert importlib.metadata.version("fastapi") == pins["fastapi"]
        (declared,) = [
            Requirement(r)
            for r in importlib.metadata.requires("fastapi")
            if Requirement(r).name == "starlette" and Requirement(r).marker is None
        ]
        assert starlette not in declared.specifier, (
            f"starlette=={starlette} satisfies fastapi's {declared}: pip can resolve it"
        )
    else:
        raise AssertionError(f"no build/deploy check for {branch}")


def test_every_build_or_deploy_fault_has_a_check():
    staged = {
        b for b, e in BY_BRANCH.items() if e.get("expected_failed_stage") in ("build", "deploy")
    }
    assert staged == {"deployfail/01", "deployfail/02", "deployfail/03", "deployfail/04"}


@BRANCHES
def test_branch_implements_its_fault(branch, sha, tmp_path):
    entry = BY_BRANCH[branch]
    tree = extract(sha, tmp_path / "tree")
    if entry.get("expected_failed_stage") in ("build", "deploy"):
        build_deploy_fault(branch, tree)
        return

    result = probe(tree)
    if entry["case"] == "i":
        assert verify_passes(result), result
        assert fired(result) == set(), result
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "tests/test_app.py"],
            cwd=tree,
            env=scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert tests.returncode == 0, tests.stdout + tests.stderr
    elif entry["case"] == "ii":  # verify stage: the new version is live and red
        assert not verify_passes(result), result
        assert bool(fired(result)) == ("runtime_error" in entry["also_expected"]), result
    else:
        assert verify_passes(result), result
        assert fired(result) == {RUNTIME_TARGET.get(branch, "/")}, result
