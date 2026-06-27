"""Run model-generated code against unit tests in an isolated container on a node.

Security posture (lab-grade): ``--network none``, capped memory/CPU/PIDs, a hard
wall-clock timeout, and a throwaway temp dir. The test harness defines
``check(candidate)``; the run is pass@1 (binary) — all asserts pass or it fails.
"""

from __future__ import annotations

import re
import shlex

from ..ssh import SSHClient
from . import nodeops

_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

_HARNESS = """
import importlib.util, json, traceback
spec = importlib.util.spec_from_file_location("solution", "/work/solution.py")
mod = importlib.util.module_from_spec(spec)
result = {{"passed": False, "error": None}}
try:
    spec.loader.exec_module(mod)
    candidate = getattr(mod, {entry!r})
    check(candidate)
    result["passed"] = True
except Exception:
    result["error"] = traceback.format_exc()[-2000:]
print("SPARK_RESULT:" + json.dumps(result))
"""


def extract_code(response: str) -> str:
    """Pull the first fenced Python block from a model response; fall back to the
    whole response if there's no fence."""
    m = _FENCE_RE.search(response)
    return (m.group(1) if m else response).strip()


async def run_code_tests(
    ssh: SSHClient,
    *,
    code: str,
    test_code: str,
    entry_point: str,
    image: str = "python:3.12-slim",
    timeout: int = 20,
) -> tuple[bool, str]:
    """Returns (passed, detail). Best-effort cleanup of the temp dir + container."""
    runner = test_code.rstrip() + "\n" + _HARNESS.format(entry=entry_point)
    mk = await ssh.run("mktemp -d /tmp/sparkeval.XXXXXX")
    if not mk.ok or not mk.stdout.strip():
        return False, f"could not create temp dir: {mk.stderr or mk.stdout}"
    d = mk.stdout.strip()
    cname = "spark-eval-" + d.rsplit(".", 1)[-1]
    try:
        await ssh.write_file(f"{d}/solution.py", code)
        await ssh.write_file(f"{d}/runner.py", runner)

        need_sudo = await nodeops.docker_needs_sudo(ssh)
        run = (
            f"timeout {timeout + 5} docker run --rm --name {shlex.quote(cname)} "
            f"--network none --memory 512m --cpus 1 --pids-limit 256 "
            f"-e PYTHONDONTWRITEBYTECODE=1 -v {shlex.quote(d)}:/work:ro -w /work "
            f"{shlex.quote(image)} python runner.py"
        )
        # rm -f guards against a leftover container if the wall-clock timeout fired
        cmd = f"{run}; rc=$?; docker rm -f {shlex.quote(cname)} >/dev/null 2>&1 || true; exit $rc"
        res = await ssh.run(cmd, sudo=need_sudo, timeout=timeout + 40)

        for line in res.stdout.splitlines():
            if line.startswith("SPARK_RESULT:"):
                import json

                data = json.loads(line[len("SPARK_RESULT:"):])
                if data.get("passed"):
                    return True, "all tests passed"
                return False, (data.get("error") or "tests failed")[-1500:]
        if res.exit_status == 124:
            return False, f"timed out after {timeout}s"
        tail = (res.stderr or res.stdout or "no output")[-800:]
        return False, f"no result (exit {res.exit_status}): {tail}"
    finally:
        await ssh.run(f"rm -rf {shlex.quote(d)}", check=False)
