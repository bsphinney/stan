"""Search thread budget: DIA-NN and Sage must fit the CPUs STAN may use.

``os.cpu_count()`` counts the host, not the process. A LIVE install test
ran the watcher in a SLURM job on 4 allocated CPUs, where
``os.cpu_count()`` said 128, and DIA-NN was launched with ``--threads 64``.
Sage had no budget at all: it ran on Rayon's default pool, every core.

What these pin:

* the CPU count comes from the affinity mask, then any cgroup v2
  ``cpu.max`` quota (leaf or any ancestor, tightest wins), and only then
  from ``os.cpu_count()``;
* the instrument-PC policy is unchanged: ``max(2, cpus // 2)``;
* inside a SLURM job the whole allocation is used, so Hive's DDA jobs keep
  the 8 Sage threads Rayon gave them before the cap existed;
* DIA-NN receives ``--threads`` exactly once (community params carry
  their own ``threads: 8``, which used to become a second flag);
* Sage gets the budget as ``RAYON_NUM_THREADS`` in its environment.

Everything is monkeypatched: no test reads the real /proc or /sys, and no
search engine runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import stan.search.local as local

SLURM_VARS = ("SLURM_JOB_ID", "SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE")


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A fake host: no affinity mask, no cgroup, no SLURM, 16 CPUs.

    Returns a small controller so each test states only what differs.
    """
    for var in SLURM_VARS + ("RAYON_NUM_THREADS",):
        monkeypatch.delenv(var, raising=False)

    proc = tmp_path / "proc_self_cgroup"
    root = tmp_path / "cgroup"
    root.mkdir()
    monkeypatch.setattr(local, "_PROC_SELF_CGROUP", proc)
    monkeypatch.setattr(local, "_CGROUP_ROOT", root)
    monkeypatch.setattr(local.os, "cpu_count", lambda: 16)
    # macOS/Windows have no sched_getaffinity; start from that state.
    monkeypatch.delattr(local.os, "sched_getaffinity", raising=False)

    class Host:
        cgroup_root = root

        @staticmethod
        def affinity(n: int) -> None:
            monkeypatch.setattr(
                local.os, "sched_getaffinity",
                lambda pid: set(range(n)), raising=False,
            )

        @staticmethod
        def cpu_count(n: int | None) -> None:
            monkeypatch.setattr(local.os, "cpu_count", lambda: n)

        @staticmethod
        def cgroup(rel: str, limits: dict[str, str]) -> None:
            """``rel`` is the /proc/self/cgroup path; ``limits`` maps a
            path under the cgroup mount ('' = the mount root) to the
            contents of its cpu.max."""
            proc.write_text(f"0::{rel}\n")
            for sub, content in limits.items():
                d = root / sub if sub else root
                d.mkdir(parents=True, exist_ok=True)
                (d / "cpu.max").write_text(content + "\n")

        @staticmethod
        def slurm(**env: str) -> None:
            for k, v in env.items():
                monkeypatch.setenv(k, v)

    return Host


# ── CPU count ────────────────────────────────────────────────────────


def test_affinity_mask_beats_host_cpu_count(host):
    """The LIVE failure, outside SLURM: 4 usable CPUs on a 128-CPU host."""
    host.cpu_count(128)
    host.affinity(4)
    assert local._available_cpus() == 4
    assert local.default_search_threads() == 2  # was 64


def test_no_affinity_api_falls_back_to_cpu_count(host):
    """Windows and macOS: no sched_getaffinity, no cgroup -- unchanged."""
    assert not hasattr(local.os, "sched_getaffinity")
    assert local._available_cpus() == 16
    assert local.default_search_threads() == 8


def test_unknown_cpu_count_keeps_the_old_fallback(host):
    host.cpu_count(None)
    assert local._available_cpus() == 4
    assert local.default_search_threads() == 2


def test_affinity_error_falls_back_to_cpu_count(host, monkeypatch):
    def boom(pid):
        raise OSError("no")
    monkeypatch.setattr(local.os, "sched_getaffinity", boom, raising=False)
    assert local._available_cpus() == 16


def test_cgroup_v2_quota_lowers_the_count(host):
    """systemd CPUQuota=600% on the watcher's unit, 64-CPU affinity."""
    host.affinity(64)
    host.cgroup("/system.slice/stan-watch.service",
                {"system.slice/stan-watch.service": "600000 100000"})
    assert local._cgroup_cpu_quota() == 6
    assert local._available_cpus() == 6
    assert local.default_search_threads() == 3


def test_a_parent_quota_limits_the_child(host):
    host.affinity(64)
    host.cgroup("/system.slice/stan-watch.service", {
        "system.slice": "200000 100000",
        "system.slice/stan-watch.service": "max 100000",
    })
    assert local._cgroup_cpu_quota() == 2


def test_tightest_quota_in_the_chain_wins(host):
    host.cgroup("/a/b", {"a": "800000 100000", "a/b": "300000 100000"})
    assert local._cgroup_cpu_quota() == 3


def test_container_namespace_reads_the_mount_root(host):
    """docker --cpus=4 with a cgroup namespace: /proc says 0::/."""
    host.affinity(32)
    host.cgroup("/", {"": "400000 100000"})
    assert local._available_cpus() == 4


def test_escaping_path_uses_the_root_only(host):
    """A cgroup outside the namespace shows as 0::/../.. -- never walk it."""
    host.cgroup("/../../elsewhere", {"": "300000 100000"})
    assert local._cgroup_cpu_quota() == 3


def test_path_missing_under_the_mount_uses_the_root_only(host):
    """No cgroup namespace: /proc names a host path the container can't see."""
    host.cgroup("/kubepods/burstable/pod123/abc", {"": "200000 100000"})
    assert local._cgroup_cpu_quota() == 2


def test_quota_never_raises_the_count(host):
    host.affinity(4)
    host.cgroup("/", {"": "3200000 100000"})
    assert local._available_cpus() == 4


@pytest.mark.parametrize(("content", "expected"), [
    ("max 100000", None),
    ("150000 100000", 1),      # 1.5 CPUs rounds down, never up
    ("50000 100000", 1),       # half a CPU still gets one thread
    ("200000", 2),             # period omitted: kernel default 100000
    ("garbage 100000", None),
    ("", None),
    ("-1 100000", None),
])
def test_cpu_max_parsing(tmp_path, content, expected):
    f = tmp_path / "cpu.max"
    f.write_text(content)
    assert local._read_cpu_max(f) == expected


def test_missing_cpu_max_is_no_limit(tmp_path):
    assert local._read_cpu_max(tmp_path / "absent") is None


def test_cgroup_v1_only_host_has_no_v2_quota(host):
    local._PROC_SELF_CGROUP.write_text(
        "12:cpu,cpuacct:/user.slice\n11:cpuset:/\n")
    (host.cgroup_root / "cpu.max").write_text("100000 100000\n")
    assert local._cgroup_cpu_quota() is None


def test_no_procfs_is_no_quota(host):
    """macOS/Windows: /proc/self/cgroup does not exist."""
    assert not local._PROC_SELF_CGROUP.exists()
    assert local._cgroup_cpu_quota() is None


# ── Policy ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("cpus", "threads"), [(1, 2), (2, 2), (3, 2), (8, 4),
                                               (16, 8), (33, 16)])
def test_instrument_pc_policy_is_half_with_a_floor_of_two(host, cpus, threads):
    host.affinity(cpus)
    assert local.default_search_threads() == threads


def test_slurm_job_uses_its_whole_allocation(host):
    """The LIVE run itself: SLURM, nproc=4, os.cpu_count()=128."""
    host.cpu_count(128)
    host.affinity(4)
    host.slurm(SLURM_JOB_ID="24193071", SLURM_CPUS_ON_NODE="4")
    assert local.default_search_threads() == 4  # was 64


def test_hive_dda_job_keeps_eight_sage_threads(host):
    """dispatch_hive renders --cpus-per-task=8 for hive-process."""
    host.cpu_count(128)
    host.affinity(8)
    host.slurm(SLURM_JOB_ID="1", SLURM_CPUS_PER_TASK="8", SLURM_CPUS_ON_NODE="8")
    assert local.default_search_threads() == 8
    assert local._sage_thread_env(0)["RAYON_NUM_THREADS"] == "8"


def test_slurm_without_affinity_is_bounded_by_the_allocation(host):
    """A cluster that sets no affinity mask: trust SLURM's count."""
    host.cpu_count(128)
    host.slurm(SLURM_JOB_ID="1", SLURM_CPUS_PER_TASK="6", SLURM_CPUS_ON_NODE="128")
    assert local.default_search_threads() == 6


def test_slurm_without_a_cpu_count_uses_the_usable_cpus(host):
    host.affinity(12)
    host.slurm(SLURM_JOB_ID="1", SLURM_CPUS_PER_TASK="not-a-number")
    assert local.default_search_threads() == 12


# ── Sage environment ─────────────────────────────────────────────────


def test_sage_env_defaults_to_the_budget(host):
    host.affinity(8)
    assert local._sage_thread_env(0)["RAYON_NUM_THREADS"] == "4"


def test_sage_env_explicit_threads_win(host, monkeypatch):
    monkeypatch.setenv("RAYON_NUM_THREADS", "5")
    assert local._sage_thread_env(3)["RAYON_NUM_THREADS"] == "3"


@pytest.mark.parametrize("value", ["5", " 5\n"])
def test_sage_env_keeps_an_operator_choice(host, monkeypatch, value):
    """Kept, and normalised: Rayon's parse of " 5" fails to every CPU."""
    host.affinity(8)
    monkeypatch.setenv("RAYON_NUM_THREADS", value)
    assert local._sage_thread_env(0)["RAYON_NUM_THREADS"] == "5"


@pytest.mark.parametrize("value", ["0", "", "lots"])
def test_sage_env_zero_or_junk_is_not_a_choice(host, monkeypatch, value):
    """RAYON_NUM_THREADS=0 means "every CPU" to Rayon -- the thing capped."""
    host.affinity(8)
    monkeypatch.setenv("RAYON_NUM_THREADS", value)
    assert local._sage_thread_env(0)["RAYON_NUM_THREADS"] == "4"


def test_sage_env_passes_the_rest_of_the_environment(host, monkeypatch):
    monkeypatch.setenv("STAN_TEST_MARKER", "kept")
    assert local._sage_thread_env(0)["STAN_TEST_MARKER"] == "kept"


# ── The argv / env that reach the engines ────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    """Replace subprocess.run in local.py; record calls, fake the output."""
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        if "--out" in cmd:  # DIA-NN
            Path(cmd[cmd.index("--out") + 1]).write_bytes(b"PAR1")
        else:  # Sage: output_directory is next to its config
            cfg = Path(cmd[-1])
            (cfg.parent / "results.sage.parquet").write_bytes(b"PAR1")

        class Done:
            returncode = 0
        return Done()

    monkeypatch.setattr(local.subprocess, "run", fake_run)
    # A failed search copies its log to the Hive mirror; never from a test.
    monkeypatch.setattr(local, "_mirror_log_to_hive", lambda *a, **k: None)
    return calls


def _threads_flags(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--threads"]


def test_diann_community_mode_passes_threads_once(host, captured, tmp_path):
    host.affinity(16)
    raw = tmp_path / "HeLa_50ng.d"
    raw.mkdir()
    out = tmp_path / "out" / "HeLa_50ng"
    report = local.run_diann_local(raw, out, vendor="bruker",
                                   search_mode="community")
    assert report == out / "report.parquet"
    cmd = captured[0]["cmd"]
    assert _threads_flags(cmd) == ["8"]  # 16 usable // 2; not "8" and "8"
    assert "--lib" in cmd and "--qvalue" in cmd  # frozen params still there


def test_diann_community_mode_on_a_small_box(host, captured, tmp_path):
    """The captured LIVE argv had '--threads 8 ... --threads 5'."""
    host.affinity(10)
    raw = tmp_path / "HeLa_50ng.d"
    raw.mkdir()
    local.run_diann_local(raw, tmp_path / "o" / "HeLa_50ng", vendor="bruker",
                          search_mode="community")
    assert _threads_flags(captured[0]["cmd"]) == ["5"]


def test_diann_explicit_threads_are_honoured(host, captured, tmp_path):
    host.affinity(16)
    raw = tmp_path / "run.d"
    raw.mkdir()
    fasta = tmp_path / "db.fasta"
    fasta.write_text(">p\nPEPTIDEK\n")
    lib = tmp_path / "lib.parquet"
    lib.write_bytes(b"x")
    local.run_diann_local(raw, tmp_path / "o" / "run", vendor="bruker",
                          threads=3, fasta_path=str(fasta), lib_path=str(lib))
    assert _threads_flags(captured[0]["cmd"]) == ["3"]


def test_diann_under_slurm_uses_the_allocation(host, captured, tmp_path):
    host.cpu_count(128)
    host.affinity(4)
    host.slurm(SLURM_JOB_ID="24193071", SLURM_CPUS_ON_NODE="4")
    raw = tmp_path / "HeLa.d"
    raw.mkdir()
    local.run_diann_local(raw, tmp_path / "o" / "HeLa", vendor="bruker",
                          search_mode="community")
    assert _threads_flags(captured[0]["cmd"]) == ["4"]


def test_sage_gets_the_budget_in_its_environment(host, captured, tmp_path):
    host.affinity(12)
    raw = tmp_path / "dda.d"
    raw.mkdir()
    fasta = tmp_path / "db.fasta"
    fasta.write_text(">p\nPEPTIDEK\n")
    res = local.run_sage_local(raw, tmp_path / "o" / "dda", vendor="bruker",
                               fasta_path=str(fasta))
    assert res is not None and res.name == "results.sage.parquet"
    call = captured[0]
    assert call["env"]["RAYON_NUM_THREADS"] == "6"
    assert "--threads" not in call["cmd"]  # Sage has no such option


def test_sage_explicit_threads_reach_rayon(host, captured, tmp_path):
    raw = tmp_path / "dda.d"
    raw.mkdir()
    fasta = tmp_path / "db.fasta"
    fasta.write_text(">p\nPEPTIDEK\n")
    local.run_sage_local(raw, tmp_path / "o" / "dda", vendor="bruker",
                         fasta_path=str(fasta), threads=2)
    assert captured[0]["env"]["RAYON_NUM_THREADS"] == "2"
