"""RoCE plumbing for multi-node tensor parallelism, and the per-instance env map.

None of this can be tested against real RDMA hardware in CI, so the tests are
split by what *can* be established here: that the generated scripts are
well-formed and degrade correctly, that hostile input cannot reach argv, and
that the probe output is parsed conservatively. The hardware behaviour itself
(whether NCCL then actually uses the fabric) is only observable on a Spark.

The most important test in this file is
``test_rendered_script_actually_invokes_docker_with_the_image``. It exists
because the natural way to write this feature — interpolating a pre-formatted
block of flags — puts a blank line in the middle of the backslash-continued
``docker run`` whenever the block is empty, which silently truncates the
command. ``bash -n`` parses the result as valid, because it *is* valid; it is
just a different command that runs no container. That would have broken every
existing instance in the fleet, so it is checked by executing the script.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from app.services import roce
from app.services import templates as t


# --- the truncation trap -------------------------------------------------

def _run_script(tmp_path, script: str) -> subprocess.CompletedProcess:
    """Execute a rendered launch script with `docker` stubbed on PATH.

    A PATH stub, not a shell function: the scripts end in `exec docker …`, and
    `exec` replaces the shell, so a function would never be consulted.
    """
    binfmt = tmp_path / "bin"
    binfmt.mkdir(exist_ok=True)
    stub = binfmt / "docker"
    stub.write_text("#!/usr/bin/env bash\nfor a in \"$@\"; do echo \"ARG:$a\"; done\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    path = str(binfmt) + os.pathsep + os.environ.get("PATH", "")
    script_file = tmp_path / "launch.sh"
    script_file.write_text(script)
    return subprocess.run(
        ["bash", str(script_file)], capture_output=True, text=True,
        env={**os.environ, "PATH": path}, timeout=60,
    )


ALL_RENDERS = {
    "single": lambda **kw: t.render_instance_docker_run_single(
        name="i", image="img:1", hf_home="/h", models_dir="/m", shm="10gb",
        serve_cmd="vllm serve /m/i", **kw,
    ),
    "distributed-head": lambda **kw: t.render_instance_docker_run_distributed(
        name="i", role="head", image="img:1", hf_home="/h", models_dir="/m",
        shm="10gb", iface="eth9", host_qsfp="10.0.0.1", master_addr="10.0.0.1",
        serve_cmd="vllm serve /m/i", **kw,
    ),
    "distributed-worker": lambda **kw: t.render_instance_docker_run_distributed(
        name="i", role="worker", image="img:1", hf_home="/h", models_dir="/m",
        shm="10gb", iface="eth9", host_qsfp="10.0.0.2", master_addr="10.0.0.1",
        serve_cmd="vllm serve /m/i", **kw,
    ),
    "ray-head": lambda **kw: t.render_ray_head_script(
        image="img:1", hf_home="/h", models_dir="/m", head_qsfp="10.0.0.1",
        iface="eth9", ray_port=6379, shm="10gb", dashboard_port=8265,
        **{k: v for k, v in kw.items() if k != "env_vars"},
    ),
    "ray-worker": lambda **kw: t.render_ray_worker_script(
        image="img:1", hf_home="/h", models_dir="/m", head_qsfp="10.0.0.1",
        worker_qsfp="10.0.0.2", iface="eth9", ray_port=6379, shm="10gb",
        **{k: v for k, v in kw.items() if k != "env_vars"},
    ),
}


@pytest.mark.parametrize("name", sorted(ALL_RENDERS))
@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_rendered_script_actually_invokes_docker_with_the_image(name, tmp_path):
    """The image and the serve command must REACH docker's argv.

    With every optional group empty — no env, no RoCE, no RDMA devices on the
    host, which is the state of the entire existing fleet — a truncated
    command would run `docker run` with no image and crash-loop the unit.
    """
    script = ALL_RENDERS[name]()
    res = _run_script(tmp_path, script)
    args = [ln[4:] for ln in res.stdout.splitlines() if ln.startswith("ARG:")]

    assert "img:1" in args, f"{name}: image never reached docker argv:\n{res.stdout}\n{res.stderr}"
    assert args[0] == "run", f"{name}: first arg was {args[0]!r}"
    # The last arg is the command the container runs; its absence is the exact
    # symptom of the truncation bug.
    assert any("vllm serve" in a or "ray start" in a for a in args), f"{name}: no command"


@pytest.mark.parametrize("name", sorted(ALL_RENDERS))
def test_no_blank_line_inside_the_docker_run(name):
    """A blank line between `docker run` and its last argument ends the command.

    Asserted structurally as well as functionally because this is the failure
    that is invisible to a syntax check.
    """
    script = ALL_RENDERS[name]()
    lines = script.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("exec docker"))
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    for ln in lines[start:end + 1]:
        assert ln.strip(), f"{name}: blank continuation line would truncate the command"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_launch_succeeds_whatever_rdma_the_host_has(tmp_path):
    """A node with no RoCE has to keep working exactly as it does today.

    Deliberately asserts only what is true on BOTH kinds of host, because this
    test runs on CI machines and developer laptops whose /dev/infiniband we do
    not control. An earlier version asserted the "no devices" diagnostic
    appeared on stderr; that passed locally and failed on the runner, which is
    a test coupled to its environment rather than to the behaviour. The
    behaviour is: the launch works either way, and `--device` appears only when
    there is a device to map.
    """
    script = ALL_RENDERS["distributed-head"](roce_hca="rocep1s0f1:1", roce_gid_index="3")
    res = _run_script(tmp_path, script)
    args = [ln[4:] for ln in res.stdout.splitlines() if ln.startswith("ARG:")]

    assert res.returncode == 0, res.stderr
    assert "img:1" in args, "the launch must reach docker with an image either way"
    assert "NCCL_IB_HCA=rocep1s0f1:1" in args
    assert "NCCL_IB_GID_INDEX=3" in args

    # --device is only legitimate when a real char device backs it.
    for i, a in enumerate(args):
        if a == "--device":
            path = args[i + 1].split(":")[0]
            assert os.path.exists(path), f"mapped a device that does not exist: {path}"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_device_discovery_maps_what_is_there_and_nothing_else(tmp_path):
    """The preflight logic itself, against a directory we control.

    Testing it here rather than through a rendered script keeps it independent
    of the host's real /dev/infiniband, which is what made the previous version
    of this test flaky.
    """
    fake = tmp_path / "ib"
    fake.mkdir()
    # /dev/null is a char device, so it stands in for a verbs node.
    (fake / "uverbs0").symlink_to("/dev/null")
    (fake / "notachardevice").write_text("x")

    snippet = t._RDMA_PREFLIGHT.replace("/dev/infiniband", str(fake))
    probe = f"{snippet}\nprintf '%s\\n' \"${{RDMA_ARGS[@]+${{RDMA_ARGS[@]}}}}\"\n"
    script = tmp_path / "p.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + probe)
    res = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "uverbs0" in out, "an existing char device must be mapped"
    assert "notachardevice" not in out, "a regular file must not be mapped as a device"
    assert "IPC_LOCK" in out, "capability is added when any device is mapped"


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_device_discovery_emits_nothing_when_there_is_nothing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    snippet = t._RDMA_PREFLIGHT.replace("/dev/infiniband", str(empty))
    probe = f"{snippet}\nprintf 'COUNT=%d\\n' \"${{#RDMA_ARGS[@]}}\"\n"
    script = tmp_path / "p.sh"
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + probe)
    res = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)

    assert res.returncode == 0, res.stderr
    assert "COUNT=0" in res.stdout, "no devices must mean no flags, not a failure"


def test_roce_env_only_when_both_values_are_known():
    """Naming the device without the GID index is worse than doing nothing: the
    wrong RoCE v2 GID connects and then stalls, which is harder to diagnose
    than the TCP fallback it replaced."""
    assert t.roce_env_flags("rocep1s0f1:1", "3") == [
        "-e NCCL_IB_HCA=rocep1s0f1:1", "-e NCCL_IB_GID_INDEX=3",
    ]
    assert t.roce_env_flags("rocep1s0f1:1", None) == []
    assert t.roce_env_flags(None, "3") == []
    assert t.roce_env_flags(None, None) == []


def test_single_node_gets_no_rdma_plumbing():
    """TP=1 has no cross-node all-reduce, so mapping verbs devices into it
    would be privilege bought for nothing."""
    script = ALL_RENDERS["single"]()
    assert "RDMA_ARGS" not in script
    assert "/dev/infiniband" not in script


# --- env map: the hostile cases -----------------------------------------

def test_a_key_with_whitespace_cannot_inject_docker_flags():
    """The sharpest edge here, and it needs no shell metacharacter at all.

    Quoting only the VALUE (`-e {key}={quote(value)}`) lets a key containing
    spaces split into extra argv words that docker reads as its own flags —
    `--privileged -v /:/host` lands before the image name and that is a
    container escape. Quoting the whole KEY=VALUE token contains it.
    """
    flags = t.env_flags({"A=1 --privileged -v /:/host -e B": "1"})
    assert flags == [], "a key that is not a legal env name must be dropped entirely"


def test_a_trailing_newline_in_a_key_is_rejected():
    """`re.match` with a `$` anchor accepts "NCCL_DEBUG\\n" — Python's `$` also
    matches before a trailing newline — and a newline in a key truncates the
    generated command. Hence `\\A…\\Z`."""
    assert t.env_flags({"NCCL_DEBUG\n": "INFO"}) == []


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_a_hostile_value_stays_one_argv_word(tmp_path):
    marker = tmp_path / "PWNED"
    script = ALL_RENDERS["single"](
        env_vars={"GOOD": f"x'; touch {marker} #", "SPACED": "a b c"}
    )
    res = _run_script(tmp_path, script)
    args = [ln[4:] for ln in res.stdout.splitlines() if ln.startswith("ARG:")]
    assert f"GOOD=x'; touch {marker} #" in args, "value was split or mangled"
    assert "SPACED=a b c" in args
    assert not marker.exists(), "value escaped its quoting and executed"


def test_env_map_is_rendered_for_distributed():
    script = ALL_RENDERS["distributed-head"](env_vars={"VLLM_X": "1"})
    assert "-e VLLM_X=1" in script


# --- probe parsing --------------------------------------------------------

_OK = (
    "status=ok iface=enp1s0f1np1 ip=10.88.124.33 dev=rocep1s0f1 port=1 gid_index=3 "
    "gid=x gid_ip=10.88.124.33 gid_type=RoCEv2 ndev=enp1s0f1np1 match=ip state=ACTIVE "
    "phys_state=LinkUp link_layer=Ethernet rate=200 uverbs=/dev/infiniband/uverbs1 "
    "uverbs_present=1 rdma_cm_present=1 nccl_ib_hca=rocep1s0f1:1"
)


def test_probe_parses_the_reported_hardware():
    info = roce.parse_probe(_OK)
    assert info.usable
    assert (info.hca, info.gid_index, info.device) == ("rocep1s0f1:1", "3", "rocep1s0f1")
    assert info.uverbs == "/dev/infiniband/uverbs1"


@pytest.mark.parametrize(
    "status", ["no-rdma", "no-device", "link-down", "no-rocev2", "ip-mismatch", "not-roce"]
)
def test_every_unusable_status_is_unusable_and_explained(status):
    info = roce.parse_probe(f"status={status} dev= port= gid_index= nccl_ib_hca= uverbs=")
    assert not info.usable
    assert info.detail, f"{status} has no explanation for the operator"
    assert "TCP" in info.summary()


def test_probe_rejects_a_device_path_it_did_not_expect():
    """The path is interpolated into a root-run script. The probe is ours, but
    a restored backup bundle or an edited row is not."""
    info = roce.parse_probe(_OK.replace("/dev/infiniband/uverbs1", "/dev/infiniband/uverbs0;id"))
    assert info.uverbs is None


def test_empty_probe_output_is_not_usable():
    info = roce.parse_probe("")
    assert not info.usable and info.status == "no-output"


# --- profiles -------------------------------------------------------------

def test_env_is_refused_from_an_imported_profile():
    """LD_PRELOAD / PYTHONSTARTUP in a stranger's JSON is arbitrary code as
    root on a DGX; HF_ENDPOINT silently redirects every weight download."""
    from app.services.profiles import sanitize_settings

    settings, dropped = sanitize_settings(
        {"env_vars": {"LD_PRELOAD": "/models/x/evil.so"}, "max_model_len": 4096},
        trusted=False,
    )
    assert "env_vars" not in settings
    assert "env_vars" in dropped, "the drop must be reported, not silent"
    assert settings["max_model_len"] == 4096


def test_env_is_kept_from_your_own_instance():
    from app.services.profiles import sanitize_settings

    settings, _ = sanitize_settings({"env_vars": {"NCCL_DEBUG": "INFO"}}, trusted=True)
    assert settings["env_vars"] == {"NCCL_DEBUG": "INFO"}


def test_blocked_fields_are_a_subset_of_known_fields():
    """A name in IMPORT_BLOCKED_FIELDS that is not in PROFILE_FIELDS blocks
    nothing — sanitize_settings drops unknown keys first, so the block reads as
    protection while doing nothing. A spelling drift between the two tuples is
    exactly how that happens."""
    from app.services.profiles import IMPORT_BLOCKED_FIELDS, PROFILE_FIELDS

    assert IMPORT_BLOCKED_FIELDS <= set(PROFILE_FIELDS)


# --- the Ray dashboard must not reach the management LAN ------------------

def test_the_ray_dashboard_never_binds_a_wildcard():
    """Ray's dashboard hosts the Jobs API, which submits arbitrary code and has
    no authentication. This container runs --network host --gpus all as root
    with the models directory mounted, so a wildcard bind put unauthenticated
    root-equivalent execution on the management LAN for anyone who could open a
    TCP connection to port 8265.

    Asserted on the RENDERED script rather than the source, because the bind
    address is interpolated and a refactor could reintroduce the wildcard
    without touching the literal this test would otherwise grep for.
    """
    import re

    script = t.render_ray_head_script(
        image="img:1", hf_home="/h", models_dir="/m", head_qsfp="10.10.10.1",
        iface="eth9", ray_port=6379, shm="10gb", dashboard_port=8265,
    )
    host = re.search(r"--dashboard-host=(\S+)", script)
    assert host, "the dashboard bind address is no longer explicit"
    assert host.group(1) == "10.10.10.1", "the dashboard must bind the QSFP address"
    assert "0.0.0.0" not in script


def test_a_node_without_qsfp_falls_back_to_loopback_not_a_wildcard():
    """The failure mode of guessing wrong here is an open RCE endpoint, so an
    absent QSFP address must degrade to the most closed option, never the most
    open one."""
    import re

    script = t.render_ray_head_script(
        image="i", hf_home="/h", models_dir="/m", head_qsfp="",
        iface="e", ray_port=6379, shm="1gb", dashboard_port=8265,
    )
    assert re.search(r"--dashboard-host=(\S+)", script).group(1) == "127.0.0.1"
    assert "0.0.0.0" not in script
