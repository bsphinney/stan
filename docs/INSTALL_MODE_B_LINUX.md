# STAN Mode B — a separate Linux box that processes your raw files

Mode B puts STAN on a Linux machine that is **not** an instrument PC. The
instrument PCs acquire and copy each finished raw file to this box. The box
watches for new files, runs DIA-NN or Sage, and serves the QC dashboard. One
box can serve several instruments.

This guide is written for an **AI coding agent** (Claude Code, Cursor, Codex,
Aider and similar) that a lab has pointed at this repository. A human
administrator can follow it too.

On a Windows machine, WSL2 is one way to get this Linux box. Read this guide
first, then [`INSTALL_MODE_B_WSL.md`](INSTALL_MODE_B_WSL.md), which covers only
what is different inside WSL2.

> **Agent operating rules**
>
> 1. Work through the sections in order. Each step ends with **Verify**. If a
>    Verify fails, stop and fix it before you continue.
> 2. Code blocks are labelled `# as root` (run with `sudo`) or `# as stan`
>    (run as the service account through a login shell, for example
>    `sudo -iu stan <command>` or a shell opened with `sudo -iu stan`). A bare
>    `stan` in this guide means `/home/stan/.stan/venv/bin/stan`. If a shell
>    answers `stan: command not found`, use that full path (§5 explains why).
> 3. Paths such as `/quobyte/...`, `/nfs/lssc0/...`, `Y:\STAN`, Hive, Flinders
>    and PG Farm belong to UC Davis, where STAN was built. They appear here only
>    as labelled reference examples. Never write them into this lab's config.
> 4. **Stop and ask the human** at every point marked **ASK**. These are:
>    the inputs in §0, the DIA-NN licence (§4), share credentials (§6), how the
>    dashboard is exposed (§9) and the community email code (§11).
> 5. The only thing you install on an instrument PC is the copy task in §6.3.
>    Never install STAN, Python, DIA-NN or Sage on an acquisition PC for Mode B.
> 6. This install does not need `stan init`, and you must not answer the
>    `stan setup` wizard for the human. §8 explains why.

## Contents

- [0. Collect the inputs](#0-collect-the-inputs)
- [1. What you are building](#1-what-you-are-building)
- [2. Box requirements](#2-box-requirements)
- [3. System packages and the service account](#3-system-packages-and-the-service-account)
- [4. Search engines: DIA-NN 2.3.0 and Sage 0.14.7](#4-search-engines-dia-nn-230-and-sage-0147)
- [5. Install STAN](#5-install-stan)
- [6. Get raw files onto the box](#6-get-raw-files-onto-the-box)
- [7. Thermo only: .NET 8, ThermoRawFileParser and PEG](#7-thermo-only-net-8-thermorawfileparser-and-peg)
- [8. Configure STAN](#8-configure-stan)
- [9. Run STAN as systemd services](#9-run-stan-as-systemd-services)
- [10. End-to-end test with a real QC file](#10-end-to-end-test-with-a-real-qc-file)
- [11. Community benchmark and PEG sharing (optional)](#11-community-benchmark-and-peg-sharing-optional)
- [12. Operating the box](#12-operating-the-box)
- [13. Troubleshooting](#13-troubleshooting)
- [14. The Postgres backend is not for Mode B](#14-the-postgres-backend-is-not-for-mode-b)

---

## 0. Collect the inputs

**ASK** the human for every row before you install anything.

| Question | Why it matters | If they do not know |
|---|---|---|
| Which instruments? Give vendor, model and a short label for each. | Each instrument needs its own `instruments.yml` block. The label must contain the model word (§8.2). | Nothing can proceed without this. |
| Does any instrument write Thermo `.raw`? | Thermo needs .NET 8 on this box (§7). A Bruker-only box does not. | Assume yes if any Orbitrap or Astral is listed. |
| Where does each instrument PC save acquisitions? | The copy task (§6.3) reads from there. | For timsControl it is often `D:\Data`. |
| Can this box host an SMB share that the instrument PCs write to? | This is the recommended delivery path (§6.1). | Fall back to mounting an existing share (§6.2). |
| Who may open the dashboard, and from where? | The dashboard has no login (§9.2). | Keep it on loopback and use an SSH tunnel. |
| Will the lab join the community benchmark or share PEG? | This needs a claimed pseudonym and an email code (§11). | Skip §11. It can be done later. |
| May STAN send crash reports to the STAN relay? | Error telemetry is off unless `community.yml` sets `error_telemetry: true` (§8.3). | Leave it off. |
| Does the lab accept the DIA-NN licence? | DIA-NN is free for academic and non-commercial use only (§4.1). | Do not install DIA-NN. |

---

## 1. What you are building

```
Instrument PC (acquires only)
   │  scheduled copy task: robocopy, copy-only, waits until the run has finished
   ▼
SMB share on the Mode B box          /srv/stan/incoming/<instrument>/
   │  stan watch (systemd): notices the new file, waits until it stops growing
   ▼
DIA-NN 2.3.0 (DIA)  or  Sage 0.14.7 (DDA)      results in /srv/stan/qc_output/<instrument>/
   │  metrics, IPS score, PEG, optional pass/fail gating
   ▼
~/.stan/stan.db (SQLite)  ──►  stan dashboard (systemd), port 8421
   │
   └──► optional timer: stan submit-all + stan peg-sync  ──►  community relay
```

| Mode | Where searches run | Guide |
|---|---|---|
| A | On the instrument PC itself. Not recommended: searching on the acquisition PC has frozen a timsTOF at UC Davis. | [README](../README.md#quick-install--pick-your-mode) |
| **B** | **On a separate Linux box (this guide)** | this file, plus [WSL2 notes](INSTALL_MODE_B_WSL.md) |
| C | On the lab's SLURM cluster | [`INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md) |

---

## 2. Box requirements

| Item | Requirement | How to check |
|---|---|---|
| CPU architecture | x86_64. The DIA-NN Linux build is x86_64 only. | `uname -m` prints `x86_64` |
| Cores | At least 8. Each DIA-NN or Sage search uses `max(2, cores/2)` threads. `cores` is the CPUs the watcher process may use: its CPU affinity mask, lowered by a cgroup v2 CPU quota when there is one. So a systemd `CPUAffinity=` or `CPUQuota=` on `stan-watch`, or a container's `--cpus`, shrinks the searches with it. `instruments.yml` has no thread setting. For Sage only, a `RAYON_NUM_THREADS` in the service's environment overrides the count. | `nproc` prints the affinity count. After §5, `~/.stan/venv/bin/python -c "from stan.search.local import default_search_threads as t; print(t())"` prints the thread count STAN would use in that shell (a service's own limits apply to the service) |
| RAM | At least 32 GB for one instrument. UC Davis's cluster jobs request 32 GB per DIA-NN search. Add more if several instruments may finish a QC at the same moment. | `free -g` |
| Disk | Enough for every raw file you keep. STAN never deletes raw files. A 1 h Orbitrap `.raw` is 2–4 GB. Thermo DDA also needs 3–6 GB of temporary mzML per search. | `df -h /srv` |
| OS and Python | See the next table. Python must be 3.10, 3.11 or 3.12. | `python3 --version` |
| Network | Outbound HTTPS to `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com`, `pypi.org` and `files.pythonhosted.org`. Add these only if you use the feature in brackets: `codeload.github.com` (the install without git in §5), Microsoft's .NET package sources (Thermo), `quay.io` (§7.3 container), `astral.sh` (the `uv` fallback). `brettsp-stan.hf.space` is contacted even with every sharing option off: the watcher sends a keep-alive `GET /api/health` at start-up and every 12 hours, carrying no data. If that host is blocked, the watcher logs a `keep-alive ping failed` warning every 12 hours and carries on. It also receives error telemetry if that is turned on (§8.3), and §11's submissions. | `curl -sI https://pypi.org` |

| OS | System Python | Status |
|---|---|---|
| Ubuntu 24.04 LTS | 3.12 | Recommended |
| Ubuntu 22.04 LTS | 3.10 | Works |
| Debian 12 | 3.11 | Works. DIA-NN's own Dockerfile is based on Debian 12. |
| RHEL, Rocky or Alma 9 | 3.9 | Install `python3.12` with `dnf`. STAN does not test this route, so adjust the package names in §3. |
| Anything whose `python3` is 3.13 or newer | 3.13+ | `numpy<2` has no wheels for 3.13+, so the `[peg]` and `[full]` extras cannot install. Use the `uv` fallback in §5 to get a 3.12 interpreter. |

**Concurrency.** Each instrument block runs its own watcher thread. Searches
within one instrument run one at a time. Different instruments can search at
the same time, and each DIA-NN or Sage search takes half the cores. With two
instruments the box can be fully loaded. With three or more it will be
oversubscribed.

**Timeouts.** The watcher stops a DIA-NN or Sage search after 20 minutes, and a
Thermo mzML conversion after 10 minutes. Neither limit can be configured.
Choose hardware on which one QC search finishes well within 20 minutes.

---

## 3. System packages and the service account

```bash
# as root — Ubuntu / Debian
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl unzip libgomp1
useradd --create-home --shell /bin/bash stan
mkdir -p /srv/stan/incoming /srv/stan/qc_output /opt/stan/bin /opt/stan/containers
chown -R stan:stan /srv/stan
```

Everything STAN keeps lives in the service account's config directory,
`/home/stan/.stan` (`~/.stan` for that user). This includes the config, the
SQLite database, logs and downloaded tools.

**Verify**

```bash
# as root
id stan && ls -ld /srv/stan/incoming /srv/stan/qc_output /opt/stan/bin
sudo -iu stan python3 --version        # 3.10, 3.11 or 3.12 (otherwise see §5)
```

---

## 4. Search engines: DIA-NN 2.3.0 and Sage 0.14.7

STAN does not install or update search engines in Mode B. Install exactly these
versions.

| Engine | Version | Why this exact version |
|---|---|---|
| DIA-NN | **2.3.0** | The community benchmark pins 2.3.0. `stan submit-all` rejects any DIA-NN whose major.minor is not 2.3. 2.3.1 and 2.3.2 are accepted too, and their rows are asset-verified just like 2.3.0 rows. |
| Sage | **0.14.7** | This is the pinned DDA engine. Do not use `releases/latest`, which will move. |

Local QC works with any DIA-NN version. The pin matters only for community
submission. Upstream's "latest" DIA-NN (2.5 to 2.7 as of this writing) will be
**rejected** by the community check.

### 4.1 DIA-NN 2.3.0 (native)

**ASK.** DIA-NN is free for academic and non-commercial use. Commercial use
needs a licence from its author. Show the human
<https://github.com/vdemichev/DiaNN/blob/master/LICENSE.md> and get an explicit
"yes" before you download it.

The 2.3.0 Linux build is published as a **`-Preview.zip`**. The same name
without `-Preview` returns 404.

```bash
# as root
cd /tmp
curl -fLO https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Linux-Preview.zip
mkdir -p /opt/diann
unzip -q DIA-NN-2.3.0-Academia-Linux-Preview.zip -d /opt/diann
chmod +x /opt/diann/diann-2.3.0/diann-linux
```

The zip unpacks to `/opt/diann/diann-2.3.0/`, next to upstream's `Dockerfile`
and `make-docker.sh`. The executable is **`diann-linux`**, not `diann`.

STAN runs the program named by each instrument's `diann_path`, which defaults to
`diann`. `stan submit-all` also runs `diann` from `PATH` to read the version of
any run that has none recorded, as DDA runs do. So create a wrapper called
`diann` that sets the library path and runs the real binary:

```bash
# as root
cat > /opt/stan/bin/diann <<'EOF'
#!/bin/sh
export LD_LIBRARY_PATH="/opt/diann/diann-2.3.0${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec /opt/diann/diann-2.3.0/diann-linux "$@"
EOF
chmod 755 /opt/stan/bin/diann
```

Bruker `.d` needs nothing more. Thermo `.raw` also needs the .NET 8 SDK (§7.1).

**Verify**

```bash
/opt/stan/bin/diann 2>&1 | head -3                          # a line starting "DIA-NN 2.3.0"
ldd /opt/diann/diann-2.3.0/diann-linux | grep 'not found'   # prints nothing
```

### 4.2 DIA-NN in a container (alternative)

Use this if the host must not carry .NET, or if the lab standardises on
containers. The `Dockerfile` in the zip builds a Debian 12 image with
`dotnet-sdk-8.0` installed. That image reads `.raw` without .NET on the host.
It puts the binary at **`/diann-2.3.0/diann-linux`**. Nothing named `diann` is
on its `PATH`.

```bash
# as root, in /opt/diann (the unzipped folder that holds the Dockerfile)
docker build -t diann:2.3.0 .
# optional, for apptainer / singularity:
apptainer build /opt/stan/containers/diann_2.3.0.sif docker-daemon://diann:2.3.0
```

Then make `/opt/stan/bin/diann` a wrapper that runs the container. Bind every
directory DIA-NN reads or writes: the watch directories, `output_dir` and the
library/FASTA folder.

```sh
#!/bin/sh
exec apptainer exec --bind /srv/stan:/srv/stan --bind /home/stan/.stan:/home/stan/.stan \
    /opt/stan/containers/diann_2.3.0.sif /diann-2.3.0/diann-linux "$@"
```

> **Reference example (UC Davis Hive).** Two images on that cluster have
> nearly the same name. One has .NET; the other silently skips every `.raw`
> file. Check that any image you did not build yourself reads a `.raw` before
> you trust it. See [`external_tools.md`](external_tools.md#dia-nn-containers-on-hive--critical).

**Verify:** `/opt/stan/bin/diann 2>&1 | head -3` shows `DIA-NN 2.3.0`.

### 4.3 Sage 0.14.7

```bash
# as root
cd /tmp
curl -fLO https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz
echo "e3dc6b41015cb167574f6c82525b75e946c094f30bd700271b05c051c30cbe8a  sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz" | sha256sum -c
tar -xzf sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz -C /opt      # -> /opt/sage-v0.14.7-x86_64-unknown-linux-gnu/sage
ln -sf /opt/sage-v0.14.7-x86_64-unknown-linux-gnu/sage /opt/stan/bin/sage
```

**Verify:** `/opt/stan/bin/sage --version` prints `sage 0.14.6`. That is
correct: the v0.14.7 release binary still carries the old version string, and
the `sha256sum -c` line above (it must print `OK`) is what proves you have the
v0.14.7 release. Do not replace the binary in search of one that prints
`0.14.7`.

---

## 5. Install STAN

Install from a git clone into a venv. The clone gives you `scripts/` and
`docs/` locally, and a commit you can pin and roll back to. STAN's only release
tag (`v1.0.0`) is older than `main`, so pin a **commit**, not a tag.

```bash
# as stan
git clone https://github.com/bsphinney/stan.git ~/stan-src
git -C ~/stan-src log -1 --format='%h %cd %s'      # write this down: it is your installed version
python3 -m venv ~/.stan/venv
~/.stan/venv/bin/pip install --upgrade pip
~/.stan/venv/bin/pip install -e "$HOME/stan-src[peg]"      # choose the extra from the table below
for f in ~/.profile ~/.bashrc; do
  echo 'export PATH="$HOME/.stan/venv/bin:/opt/stan/bin:$PATH"' >> "$f"
done
```

The `PATH` line goes into both files. `~/.profile` is read by login shells,
including `sudo -iu stan <command>`. Ubuntu's stock `~/.bashrc` returns at its
top in any non-interactive shell, so a line only there never reaches the
commands an agent runs. (If `~/.bash_profile` exists, bash reads it instead of
`~/.profile`: add the line there too.) A plain `bash -c` or `sudo -u stan`
without `-i` reads neither file; use the full path
`~/.stan/venv/bin/stan` there.

If `python3` is 3.13 or newer, create the venv with `uv` instead. Replace the
`python3 -m venv` line with:

```bash
# as stan
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv venv --seed --python 3.12 ~/.stan/venv
```

| Instruments on this box | Install | What it adds |
|---|---|---|
| Bruker timsTOF (with or without Thermo) | `"$HOME/stan-src[peg]"` | alphatims 1.0.8 + numpy<2 + pandas<3. Needed for Bruker PEG and DIA-window drift. Needs Python ≤ 3.12. |
| Thermo only | `"$HOME/stan-src"` | Base install. Thermo PEG comes from the container route in §7.3. |
| Add fisher_py for Thermo | `[full]` instead of `[peg]`, or `[thermo]` on its own | fisher_py reads `.raw` in-process through pythonnet and needs .NET 8 (§7.3). |

- Do not install `[dev]`. It holds test and lint tools, not runtime features.
- Do not install `[hpc]`. It only adds paramiko for the legacy
  `execution_mode: slurm` path
  ([Mode C guide, Appendix C](INSTALL_MODE_C_HPC.md#appendix-c--other-slurm-code-paths-do-not-use-for-a-new-install)),
  which a new install does not use. Mode C itself installs only `[peg]`.
- Prefer the `[peg]` extra over `stan install-peg-deps`. The command does not pin
  `pandas<3`, and under pandas 3 Bruker PEG is refused and stays empty.

Without git, install the same thing straight from a commit:
`~/.stan/venv/bin/pip install "stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/<commit>.zip"`.

**Verify**

```bash
# as stan
~/.stan/venv/bin/stan version       # STAN v1.2.x ("stan --version" does not exist)
~/.stan/venv/bin/stan doctor
bash -lc 'command -v stan'          # /home/stan/.stan/venv/bin/stan
```

If the last line prints nothing, the `PATH` line did not reach `~/.profile`
(or `~/.bash_profile`). Fix that before you go on: the rest of this guide
writes `stan` without its path.

In the `stan doctor` output, check:

- Python is 3.10–3.12, `numpy` is 1.26.x and `pandas` is 2.x.
- For a `[peg]` install, `alphatims` is 1.0.8, and under "Critical compat
  checks" you see `alphatims 1.0.8 + numpy 1.26.4 pair looks OK` and
  `alphatims.bruker imports cleanly`.
- Ignore the final `Synced to Hive mirror.` line. On a Linux box there is no
  mirror unless you configure one, and nothing is sent.
- `stan doctor` does not check DIA-NN, Sage or .NET. The Verify steps in §4 and
  §7 do that.

---

## 6. Get raw files onto the box

### The four rules for anything that delivers files

The watcher reacts only to files that are **created** in a watch directory.
These rules come from that and from how the watcher decides that a run has
finished.

1. **Write under the final name, in place.** A file that appears by being
   renamed is not seen until the watcher restarts. This covers
   `run.raw.partial` → `run.raw`, rsync's default temp-then-rename, and `mv`
   within one filesystem. `robocopy` (including `/Z`) writes in place. For
   rsync, use `--inplace`.
2. **Copy only finished acquisitions.** If a partial `.raw` stops growing for
   `stable_secs` seconds, the watcher searches it. The copier must wait until
   the source has stopped changing.
3. **Stream, do not drop.** A Bruker `.d` that is already complete when the
   watcher first measures it never fires, because the watcher waits to see it
   grow. A paced `robocopy` stream is fine. A `.d` moved in whole is picked up
   only by the startup catch-up scan.
4. **Never delete or move a run out of the watch directory after STAN has seen
   it.** STAN stores each run's path and reads the raw file again later, for
   example for PEG backfills.

### Choose a delivery path

| Option | `watch_dir` | Live pickup |
|---|---|---|
| **A. This box hosts an SMB share on its own disk (recommended)** | the local directory, e.g. `/srv/stan/incoming/timsTOF_HT` | Yes. The SMB server writes to local disk, so inotify sees each new file. |
| B. This box mounts an existing NAS or archive (CIFS/NFS) | the mount path, **prefixed with `//`** | inotify does not see writes made by other machines. The `//` prefix switches STAN to polling (§6.2). |
| C. Another server pushes with rsync | a local directory | Only with `rsync --inplace` (rule 1). |

### 6.1 Host the share on this box (Samba)

**ASK** the human for a username for the instrument PCs, and let them type the
password themselves.

```bash
# as root
apt-get install -y samba
useradd --no-create-home --shell /usr/sbin/nologin stanupload
smbpasswd -a stanupload                     # the human types the password
mkdir -p /srv/stan/incoming/timsTOF_HT      # one folder per instrument
chown -R stan:stan /srv/stan/incoming
cat >> /etc/samba/smb.conf <<'EOF'

[stan_incoming]
   path = /srv/stan/incoming
   read only = no
   valid users = stanupload
   force user = stan
   create mask = 0644
   directory mask = 0755
EOF
testparm -s >/dev/null && systemctl restart smbd
```

`force user = stan` makes every delivered file owned by the service account. If
a host firewall is active, allow SMB (TCP 445) from the instrument PCs only,
for example `ufw allow from <instrument-PC-IP> to any port 445 proto tcp`.

**Verify.** From an instrument PC, in PowerShell, run
`Test-Path \\<box>\stan_incoming\timsTOF_HT`. It should print `True`. On the
box, run `smbclient -L localhost -U stanupload` and check that `stan_incoming`
is listed.

### 6.2 Mount an existing share instead

Mount it read-only with the method the file server requires, for example
`mount -t cifs` or `mount -t nfs`, and add it to `/etc/fstab`. Then write the
mount path in `watch_dir` with a **leading `//`**:

```yaml
    watch_dir: //mnt/archive/raw_data/Exploris480     # the leading // selects the polling observer
```

STAN uses its polling observer only for paths that start with `\\` or `//`.
Linux treats `//mnt/...` as `/mnt/...`. Polling re-lists the whole tree every
10 seconds, so point `watch_dir` at the narrowest folder you can.

Treat the `//` prefix as a workaround until STAN gains an explicit polling
switch. Without it, only the startup catch-up scan finds new files.

**Verify:** `ls //mnt/archive/raw_data/Exploris480 | head` lists runs. After
§9, the instrument's `watcher: started` line in the watcher log shows
`observer=PollingObserver` (§9.4 gives the command).

### 6.3 The copy task on each instrument PC

The reference implementation is
[`scripts/flinders_copy.ps1`](../scripts/flinders_copy.ps1) plus
[`scripts/install_flinders_copy.bat`](../scripts/install_flinders_copy.bat).
UC Davis uses them to copy its timsTOF runs into its archive. They already
follow the four rules:

- pure PowerShell and `robocopy`; no Python and no STAN on the instrument;
- a scheduled task every 5 minutes, run as the logged-on user (not SYSTEM, so
  it can reach the share);
- a run is copied only when its file count and byte total have not changed
  between two passes;
- copy-only; the source is never modified;
- `robocopy` at BelowNormal priority with `/IPG:20` pacing, which gives
  bandwidth back to the acquisition;
- logs and state in `%USERPROFILE%\STAN\logs\flinders_*` (`flinders_copy.log`
  records events; `flinders_status.txt` is rewritten on every pass).

To reuse it for Mode B on a timsTOF:

1. Map the share (§6.1) to a drive letter for the user who is logged on to the
   instrument PC. Create the instrument's folder on the share if it does not
   exist yet.
2. Copy both files to the instrument PC. Edit the settings block near the top
   of the `.ps1`:
   - `$SourceDir` — where timsControl writes, e.g. `D:\Data`.
   - `$InstrumentDir` — the folder name on the share, e.g. `timsTOF_HT`.
   - `$LookbackHours` — only runs modified within this many hours are
     considered on each pass (default 72).

   The installer searches the mapped drives for a folder called
   `$InstrumentDir`, then stores the UNC path.
3. Double-click `install_flinders_copy.bat` and accept the one administrator
   prompt. If the runs already on disk should not be copied, run
   `powershell -ExecutionPolicy Bypass -File flinders_copy.ps1 -SkipBacklog`
   once; it marks them as done.
4. Remove it later with `install_flinders_copy.bat /remove`.

It handles Bruker `.d` only. It also normalises UC Davis month-folder names;
that is harmless elsewhere. **No Thermo `.raw` copier ships with STAN.** For
Thermo, adapt the same script:

- enumerate `*.raw` files instead of `*.d` folders;
- call a file finished when its size is unchanged across two passes.

Test the adapted script with the approach in `tests/test_flinders_copy.ps1`
(run under `pwsh`). Report the adaptation to the human as new code that needs
review.

**Verify.** Run one pass by hand on the instrument PC:
`powershell -ExecutionPolicy Bypass -File flinders_copy.ps1 -Show`. Then check
that a finished run appears under `/srv/stan/incoming/<instrument>/` on the
box. Scheduled passes write a one-line status file,
`%USERPROFILE%\STAN\logs\flinders_status.txt`, on every run.

---

## 7. Thermo only: .NET 8, ThermoRawFileParser and PEG

Skip this section if every instrument is Bruker.

| Component | What STAN uses it for | Needs |
|---|---|---|
| ThermoRawFileParser (TRFP) | Detecting DIA or DDA on every `.raw`, reading metadata, and converting to mzML before Sage | .NET 8 runtime, found as `dotnet` on `PATH` |
| DIA-NN reading `.raw` natively | Thermo DIA searches | .NET 8 **SDK**. DIA-NN's own error message asks for 8.0.407 or later. Not needed with the container in §4.2. |
| fisher_py (optional) | Thermo PEG and full TIC traces | pythonnet plus .NET 8 |

### 7.1 .NET 8

```bash
# as root — Ubuntu 22.04 / 24.04
apt-get install -y dotnet-sdk-8.0
```

If your distribution has no `dotnet-sdk-8.0` package, follow the tiered
fallback in `install_dotnet8_sdk()` and `install_dotnet_system_deps()` in
[`stan_wsl_setup.sh`](../stan_wsl_setup.sh). It adds Microsoft's package
repository, and if that fails runs `dotnet-install.sh --channel 8.0`, then
installs the ICU and SSL libraries that `dotnet-install.sh` leaves out.

**Verify**

```bash
dotnet --list-sdks          # a line starting "8.0."
command -v dotnet           # must resolve through the PATH in the systemd unit (§9), e.g. /usr/bin/dotnet
```

Missing system libraries make .NET programs exit silently with code 134. The
libicu and libssl list in `install_dotnet_system_deps()` fixes that.

### 7.2 ThermoRawFileParser

STAN downloads TRFP by itself the first time it sees a Thermo file. On Linux
it fetches the `v.2.0.0-dev` net8 build into
`~/.stan/tools/ThermoRawFileParser/`. Fetch it now so the check happens while
you are watching:

```bash
# as stan
~/.stan/venv/bin/python -c "from stan.tools.trfp import ensure_installed; print(ensure_installed())"
```

**Do not set `trfp_path` on Linux.** Mode detection runs
`dotnet <trfp_path>`, but mzML conversion runs `<trfp_path>` directly. No single
value works for both. Leave it unset, and both use the downloaded DLL through
`dotnet`.

If detection cannot run (for example, no `dotnet` on `PATH`), the watcher
searches the file as DIA. Set `forced_mode: dia` or `forced_mode: dda` on an
instrument whose QC runs always use one mode.

**Verify:** the command prints
`/home/stan/.stan/tools/ThermoRawFileParser/ThermoRawFileParser.dll`.

### 7.3 Thermo PEG

STAN scores PEG on every QC run it can read. For Thermo it tries fisher_py
first, then a ThermoRawFileParser **container**. If neither works, PEG stays
empty for that run. Search results are not affected.

The container route is the one UC Davis runs on Linux. It needs `apptainer` or
`singularity` on the box:

```bash
# as root
apptainer pull /opt/stan/containers/trfp_1.4.5.sif \
    docker://quay.io/biocontainers/thermorawfileparser:1.4.5--ha8f3691_0
```

Then set `STAN_TRFP_SIF=/opt/stan/containers/trfp_1.4.5.sif` in the watcher's
unit (§9.1). Its built-in default is a UC Davis path that does not exist on
your box. Set `STAN_APPTAINER` only if the runtime is not on `PATH`.

**Verify**

```bash
# as stan
STAN_TRFP_SIF=/opt/stan/containers/trfp_1.4.5.sif ~/.stan/venv/bin/python -c \
  "from stan.metrics.peg_trfp import find_trfp_container; print(find_trfp_container())"
# -> TrfpContainer(apptainer='/usr/bin/apptainer', sif=PosixPath('/opt/stan/containers/trfp_1.4.5.sif'))
```

For the fisher_py route, install `[thermo]` or `[full]` (§5) on a box that has
.NET 8. Verify it with
`~/.stan/venv/bin/python -c "from fisher_py import RawFile"`. Upstream fisher_py
says it was tested on Ubuntu 20.04; STAN's Linux reference deployment does not
use it.

---

## 8. Configure STAN

| File in `~/.stan/` | Written by | Purpose |
|---|---|---|
| `instruments.yml` | you (§8.2) | What to watch and how to search it |
| `community.yml` | you (§8.3); `stan community-claim` adds `auth_token` | Pseudonym, sharing switches, telemetry |
| `thresholds.yml` | you, optional (§8.4) | Pass/fail gates |
| `community_assets/` | you (§8.1) | Frozen FASTA and spectral libraries |
| `stan.db` | the watcher | SQLite database of every run |
| `logs/` | every command | `watch_<ts>.log`, `doctor_<ts>.log`, `submit_all_<date>.jsonl`, `peg_sync_<ts>.jsonl` |
| `tools/` | STAN | Auto-downloaded ThermoRawFileParser |
| `error_log.json` | STAN | Last 100 errors, always kept locally |

The wizards, and why this guide writes the files by hand:

- **`stan init`** is not needed. It creates a minimal `instruments.yml` (no
  instruments), an empty `thresholds.yml` and a `community.yml` with every
  sharing option and error telemetry off, but only for files that do not
  exist yet; it never overwrites one. It then asks one fleet-sync question
  whose default, `3` (None), is right here. With no terminal
  (`stan init </dev/null`) it takes that default.
- **`stan setup`** writes a usable instrument block, but it is an interactive
  wizard whose questions include community sharing, a claim email, a
  daily-report email and error reports. Those answers belong to the human
  (§0), so do not run it for them. It also writes no `lib_path`,
  `fasta_path`, `diann_path` or `sage_path`.
- **`stan add-watch <dir> --vendor bruker|thermo --name "<label>" -y`** writes
  `name`, `vendor`, `watch_dir`, `extensions`, `stable_secs`, `enabled: true`,
  `qc_only` and `output_dir` (`~/.stan/qc_output/<label>`, spaces turned into
  `_`). You can start a block with it, then add `diann_path`, `sage_path`,
  `lib_path`, `fasta_path` and the optional keys from §8.2 by hand. The
  directory must already exist. Run on a folder that already has a block,
  it changes nothing that is set and only adds missing keys.

### 8.1 Spectral library and FASTA

A DIA search needs a spectral library. Download the frozen community assets and
check them against the hashes that STAN's submission validator expects:

```bash
# as stan
mkdir -p ~/.stan/community_assets && cd ~/.stan/community_assets
base=https://github.com/bsphinney/stan/releases/download/v0.1.0-assets
curl -fLO $base/human_hela_202604.fasta
curl -fLO $base/hela_timstof_202604.parquet      # Bruker instruments
curl -fLO $base/hela_orbitrap_202604.parquet     # Thermo instruments
md5sum human_hela_202604.fasta hela_*.parquet
```

**Verify:** each file's md5 matches the table.

| File | md5 |
|---|---|
| `human_hela_202604.fasta` | `8de1d9bd0a052b175f88f66f82500d92` |
| `hela_timstof_202604.parquet` | `ad72bfb2730644c69147ba8f34bfe982` |
| `hela_orbitrap_202604.parquet` | `ac84e40f5b2f23e1286f28a7baeccec2` |

The same files are also in the Hugging Face dataset `brettsp/stan-benchmark`,
under `community_library/` and `community_fasta/`.

Always set `lib_path` for every instrument. Two things happen when it is
missing:

- The watcher uses `~/.stan/instrument_library.parquet` if one exists.
  `stan build-library` writes that one file for the whole box, whatever each
  instrument's vendor is.
- Otherwise it switches to "community" mode, which expects the assets in
  `<output_dir>/_community_assets/`. Nothing downloads them there, so every DIA
  search fails.

### 8.2 `instruments.yml`

Write `~/.stan/instruments.yml`. The example has one Bruker and one Thermo
instrument. Delete the block you do not need, and repeat a block for each
further instrument.

```yaml
instruments:
  - name: "timsTOF HT"                 # unique; must contain the model word (see below)
    vendor: bruker
    enabled: true
    watch_dir: /srv/stan/incoming/timsTOF_HT
    extensions: [".d"]
    stable_secs: 60
    output_dir: /srv/stan/qc_output/timsTOF_HT
    diann_path: /opt/stan/bin/diann
    sage_path: /opt/stan/bin/sage
    lib_path: /home/stan/.stan/community_assets/hela_timstof_202604.parquet
    fasta_path: /home/stan/.stan/community_assets/human_hela_202604.fasta
    exclude_pattern: '(?i)(wash|blank|blnk|blk|tune)'
    startup_catchup_days: 7
    hela_amount_ng: 50

  - name: "Exploris 480"
    vendor: thermo
    enabled: true
    watch_dir: /srv/stan/incoming/Exploris480
    extensions: [".raw"]
    stable_secs: 30
    output_dir: /srv/stan/qc_output/Exploris480
    diann_path: /opt/stan/bin/diann
    sage_path: /opt/stan/bin/sage
    lib_path: /home/stan/.stan/community_assets/hela_orbitrap_202604.parquet
    fasta_path: /home/stan/.stan/community_assets/human_hela_202604.fasta
    exclude_pattern: '(?i)(wash|blank|blnk|blk|tune)'
    startup_catchup_days: 7
    hela_amount_ng: 50
```

Create each `output_dir` as the `stan` user (`mkdir -p`). Create each
`watch_dir` too if §6 did not already.

| Key | Required here | Default in code | Meaning |
|---|---|---|---|
| `name` | yes | — | Label on the dashboard. It is stored on every run. The community cohort family is taken from this string by substring (`timsTOF`, `Astral`, `Exploris`, `Lumos`, `Fusion`, `Eclipse`, `Orbitrap`), so include the model. Give two identical instruments different names, e.g. `timsTOF HT A` and `timsTOF HT B`. |
| `vendor` | yes | `""` | `bruker` or `thermo`. Without it, mode detection fails and every run is searched as Orbitrap DIA. |
| `enabled` | yes | `false` | Only blocks with `true` are watched. |
| `watch_dir` | yes | — | Directory the raws land in. Subfolders are watched too. Add a leading `//` to force polling (§6.2). |
| `extensions` | yes | `[]` | `[".d"]` for Bruker, `[".raw"]` for Thermo. If empty, every file is ignored. |
| `stable_secs` | no | `60` | Seconds without growth before a run counts as finished. `stan add-watch` and `stan setup` write 60 for Bruker and 30 for Thermo. |
| `output_dir` | yes | `""` | Where search results go, one subfolder per run. If empty, results land relative to the watcher's working directory. |
| `diann_path`, `sage_path` | yes | `diann`, `sage` | Executables the watcher runs. |
| `lib_path`, `fasta_path` | yes | see §8.1 | Spectral library and FASTA. |
| `qc_only` | no | `true` | Search only files whose name matches `qc_pattern`. |
| `qc_pattern` | no | `(?i)(he(l[_\-\s]?[a5\d]\|[_\-\s]?\d)\|qc\|std[_\-\s]?he)` | Matches names containing, for example, `HeLa`, `He5`, `QC` or `STD_HE`. |
| `exclude_pattern` | no | none | Names matching this are always skipped. |
| `monitor_all_files` | no | `false` | Record non-QC files as sample-health rows. They are not searched. |
| `startup_catchup_days` | no | `30` | On every start, search QC files from this many days back that are not in the database yet. `0` turns it off. |
| `forced_mode` | no | auto | `dia` or `dda`. Skips mode detection. |
| `hela_amount_ng` | no | `50` | Injected amount, used for community cohorts. |
| `column_vendor`, `column_model` | no | none | LC column, shown on the dashboard and in submissions. |
| `family` | no | none | IPS reference cohort. Built-in references exist only for `timsTOF HT`, `Exploris 480` and `Lumos`. Any other value, or none, scores against the global reference. |
| `model` | no | `""` | Name used to look up `thresholds.yml` (§8.4). |
| `trfp_path` | **leave unset** | auto | See §7.2. |
| `search_mode`, `execution_mode`, `processing_mode` | leave unset | `local` | The non-local values belong to Mode C. |
| `spd` | leave unset | from the raw file | STAN reads samples-per-day from the method or gradient in each raw file. |

**Verify.** This script checks every block against the rules above:

```bash
# as stan
~/.stan/venv/bin/python - <<'EOF'
import shutil
from pathlib import Path
import yaml
from stan.config import get_user_config_dir

path = get_user_config_dir() / "instruments.yml"
cfg = yaml.safe_load(path.read_text()) or {}
problems = 0
for inst in cfg.get("instruments") or []:
    name = inst.get("name", "<no name>")
    errs = []
    want = {"bruker": [".d"], "thermo": [".raw"]}.get(inst.get("vendor"))
    if want is None:
        errs.append("vendor must be bruker or thermo")
    elif inst.get("extensions") != want:
        errs.append(f"extensions must be {want}")
    if inst.get("enabled") is not True:
        errs.append("enabled: true is missing (the watcher skips this block)")
    if not Path(str(inst.get("watch_dir", ""))).is_dir():
        errs.append(f"watch_dir is not a directory: {inst.get('watch_dir')}")
    if not str(inst.get("output_dir", "")).startswith("/"):
        errs.append("output_dir must be an absolute path")
    for key in ("diann_path", "sage_path"):
        if not shutil.which(str(inst.get(key, ""))):
            errs.append(f"{key} is not an executable: {inst.get(key)}")
    for key in ("lib_path", "fasta_path"):
        if not Path(str(inst.get(key, ""))).is_file():
            errs.append(f"{key} is not a file: {inst.get(key)}")
    if inst.get("trfp_path"):
        errs.append("remove trfp_path (on Linux leave it unset)")
    for e in errs:
        print(f"FAIL  {name}: {e}")
    if not errs:
        print(f"OK    {name}")
    problems += len(errs)
print(f"{path}: {problems} problem(s)")
raise SystemExit(1 if problems else 0)
EOF
stan list-watch
```

The script must print `OK` for every instrument and `0 problem(s)`.

### 8.3 `community.yml`

Write `~/.stan/community.yml` even if the lab will not take part, so that it
states every sharing choice. It is also where error telemetry is switched on;
it is off unless this file says `error_telemetry: true`.

```yaml
display_name: ""          # public pseudonym, needed only for §11
community_submit: false   # true lets `stan submit-all` send benchmark rows (§11)
peg_share: false          # true lets `stan peg-sync` share PEG results (§11)
error_telemetry: false    # true sends crash reports to the STAN relay; off when this key is absent
```

When `error_telemetry` is `true`, each error sends the following to
`https://brettsp-stan.hf.space`:

- the error type and message. The message is not stripped, so it can include
  file paths: a failed search sends its full command line;
- a traceback with directories stripped;
- the STAN, Python and OS versions and the CPU architecture;
- where known, the search engine, vendor, acquisition mode, instrument model
  and the raw file's name without its folder.

Set the value the human chose in §0.

### 8.4 `thresholds.yml` (optional)

STAN ships no thresholds. Without this file every run passes its gates, and the
dashboard colours runs by IPS score alone. To gate runs, write
`~/.stan/thresholds.yml` using the schema in
[`STAN_MASTER_SPEC.md`](../STAN_MASTER_SPEC.md) under "`config/thresholds.yml`
structure". For example:

```yaml
thresholds:
  default:
    dia: { n_precursors_min: 5000, ips_score_min: 50 }
    dda: { n_psms_min: 10000 }
```

A failed run gets a `HOLD_<run>.txt` in `<output_dir>/<run>/`. A raw file that
fails validation gets one too, with or without thresholds. In Mode B that flag
sits on this box, where the acquisition software cannot see it. Treat it as
information, not as a queue stop.

---

## 9. Run STAN as systemd services

Install the unit files as root in `/etc/systemd/system/`. Both services run as
`stan`, so they read the same `~/.stan/stan.db`.

### 9.1 `stan-watch.service`

```ini
[Unit]
Description=STAN QC watcher
Wants=network-online.target
After=network-online.target remote-fs.target
# List every watch_dir mount point, so the watcher never starts before its shares:
RequiresMountsFor=/srv/stan/incoming

[Service]
Type=simple
User=stan
Group=stan
WorkingDirectory=/home/stan
Environment=HOME=/home/stan
Environment=PATH=/opt/stan/bin:/home/stan/.stan/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
# Thermo PEG through the container (§7.3); delete for Bruker-only boxes:
Environment=STAN_TRFP_SIF=/opt/stan/containers/trfp_1.4.5.sif
ExecStart=/home/stan/.stan/venv/bin/stan watch --no-keep-awake
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

`PATH` must include the `diann` and `sage` wrappers and `dotnet`. A watch
directory that does not exist when the watcher starts stays unwatched until the
next restart. That is why `RequiresMountsFor` is there.

### 9.2 `stan-dashboard.service`

```ini
[Unit]
Description=STAN QC dashboard
After=network-online.target

[Service]
Type=simple
User=stan
Group=stan
WorkingDirectory=/home/stan
Environment=HOME=/home/stan
Environment=PATH=/home/stan/.stan/venv/bin:/usr/local/bin:/usr/bin:/bin
# Keeps the Postgres mirror task off (§14), even if PGPASSWORD is ever set:
Environment=STAN_PG_REFRESH_SECONDS=0
ExecStart=/home/stan/.stan/venv/bin/stan dashboard --host localhost --port 8421 --backend sqlite
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

- `--backend sqlite` skips a start-up probe for UC Davis's Postgres (§14).
  The dashboard's background task that copies that Postgres into SQLite
  every 5 minutes starts only when `STAN_DB_BACKEND=pg` is set or the host
  has a PG Farm credential (a `PGPASSWORD` or UC Davis's token file). A
  Mode B box has neither, so the task never runs.
  `STAN_PG_REFRESH_SECONDS=0` keeps it off even if a `PGPASSWORD` for some
  other tool reaches the service's environment.
- `--host localhost` keeps the dashboard on loopback. It is not the same as
  `--host 127.0.0.1`: if Tailscale is logged in on the box, STAN rewrites a
  literal `127.0.0.1` to `0.0.0.0`.

**ASK** how the dashboard should be reached. It has **no login**. Anyone who
can reach the port can rewrite `instruments.yml` through it, and that file names
the programs the watcher runs. Treat dashboard access as shell access for the
`stan` account.

| Access | Do this |
|---|---|
| Admins only (default) | Keep `--host localhost`. Users run `ssh -L 8421:localhost:8421 <user>@<box>` and open <http://localhost:8421>. |
| A trusted lab subnet | Use `--host 0.0.0.0` and restrict the port with a firewall, e.g. `ufw allow from 10.1.2.0/24 to any port 8421 proto tcp`. The dashboard accepts writes from the host name it is served on, so no origin setting is needed. |

Use Chrome, Edge or Firefox.

### 9.3 Nightly restart (optional safety net)

Every watcher start runs the catch-up scan. That scan finds files the live
watcher missed: files renamed into place, a `.d` that arrived complete, and
files that landed while a mount was down. A nightly restart runs it every day.
It stops a search in progress; that run is searched again after the restart.

```ini
# /etc/systemd/system/stan-watch-restart.service
[Unit]
Description=Restart the STAN watcher (runs the catch-up scan)

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart stan-watch.service
```

```ini
# /etc/systemd/system/stan-watch-restart.timer
[Unit]
Description=Nightly STAN watcher restart

[Timer]
OnCalendar=*-*-* 03:30:00

[Install]
WantedBy=timers.target
```

### 9.4 Enable and verify

```bash
# as root
systemctl daemon-reload
systemctl enable --now stan-watch.service stan-dashboard.service
systemctl enable --now stan-watch-restart.timer        # only if you installed §9.3
```

**Verify**

```bash
systemctl is-active stan-watch stan-dashboard          # active, active
curl -s http://localhost:8421/api/version              # {"version":"1.2.x"}
```

```bash
# as stan: the newest watcher log (a new one starts with every watcher start)
log=$(ls -t ~/.stan/logs/watch_[0-9]*.log | head -1); echo "$log"
grep -E "watcher: started|Active watchers|Watch directory does not exist" "$log"
```

There must be one `watcher: started <name> → <watch_dir> (observer=...,
extensions=[...], ...)` line per instrument, and then `Active watchers: N`. If
an instrument's line is missing, look for `Watch directory does not exist`.

Read these lines from the log file, not from `journalctl`. The service has no
terminal, so the console copy in the journal wraps long records at 80 columns:
a `grep` there finds `watcher: started <name> →` but loses the watch directory,
`observer=` and `extensions=` to continuation lines. The log file holds each
record on one line.

---

## 10. End-to-end test with a real QC file

**ASK** the human for one real HeLa QC acquisition from each instrument. Its
name must match `qc_pattern` (for example, it contains `HeLa` or `QC`). Deliver
it the real way, through the copy task. If that is not ready yet, copy it into
the watch directory under its final name.

Follow the watcher log (not the journal, which wraps long lines; see §9.4):

```bash
# as stan
tail -f "$(ls -t ~/.stan/logs/watch_[0-9]*.log | head -1)"
```

Expect these lines, in order (`<file>` is the run's name):

1. `watcher: tracking new QC acquisition: <file> (stable_secs=...)`
2. `watcher: file stable, dispatching: <file> (mode=qc)`
3. `Detected mode: <mode> for <file>`
4. `Local DIA-NN search for <file>` (or `Local Sage search for <file>`)
5. `Command: /opt/stan/bin/diann --f ... --lib <your lib_path> ...`. If `--lib`
   points into `_community_assets`, `lib_path` is missing (§8.1).
6. `DIA-NN complete: <file>`
7. `watcher: acquisition_complete OK: <file>`

Then:

```bash
# as stan
ls /srv/stan/qc_output/<instrument>/<run>/     # report.parquet (DIA) or results.sage.parquet (DDA), diann.log
stan watch-status --days 1                      # the file is listed as matched and present in runs
stan test --n 1                                 # which fields of the newest run are filled
```

`stan test` lists every field it expects for a community submission. Fields
that only you can supply, such as `column_vendor` and `column_model`, are
listed as broken until you set them in the instrument block. That is expected
and does not fail the install.

Finally, open the dashboard. The run appears under "This Week's QCs" with an
IPS score. PEG fills in when §7.3 or the `[peg]` extra applies.

| Symptom during the test | Go to |
|---|---|
| Line 1 never appears | §13, first row |
| Line 5 shows `_community_assets` | §8.1 |
| A DIA-NN or Sage error | `<output_dir>/<run>/diann.log`, then §13 |

---

## 11. Community benchmark and PEG sharing (optional)

The watcher never submits anything by itself. Sharing happens only when you run
`stan submit-all` (benchmark metrics) and `stan peg-sync` (PEG). Neither needs
a Hugging Face token. What each sends is listed in the
[user guide](user_guide.md#the-community-benchmark) and its
[PEG section](user_guide.md#sharing-peg-with-the-community).

1. **Claim the pseudonym.** Put the chosen name in `display_name` in
   `~/.stan/community.yml`, then run this in an interactive terminal:
   ```bash
   # as stan
   stan community-claim
   ```
   **ASK** the human to type their email address. A 6-digit code comes from
   `noreply@stan-proteomics.org`; tell them to check their spam folder. The
   human types the code. STAN writes `auth_token` into `community.yml`. The
   relay keeps one token per name, so copy that line to any other machine that
   shares as this lab.
2. **Opt in.** Set `community_submit: true` for the benchmark and/or
   `peg_share: true` for PEG.
3. **Preview, then send once by hand:**
   ```bash
   # as stan
   stan submit-all --dry-run
   stan submit-all
   stan peg-sync --dry-run
   stan peg-sync
   ```
   The first `submit-all` sends every eligible run already in the database.
   Rows appear on <https://community.stan-proteomics.org> after the nightly
   consolidation.
4. **Schedule it.** UC Davis runs both every 6 hours.

```ini
# /etc/systemd/system/stan-community.service
[Unit]
Description=STAN community sync (benchmark + PEG)

[Service]
Type=oneshot
User=stan
Environment=HOME=/home/stan
Environment=PATH=/opt/stan/bin:/home/stan/.stan/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=-/home/stan/.stan/venv/bin/stan submit-all
ExecStart=/home/stan/.stan/venv/bin/stan peg-sync
```

```ini
# /etc/systemd/system/stan-community.timer
[Unit]
Description=STAN community sync every 6 hours

[Timer]
OnCalendar=*-*-* 00/6:15:00
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
```

The leading `-` on the first `ExecStart` lets `peg-sync` run even if
`submit-all` fails.

**Verify**

```bash
# as root
systemctl enable --now stan-community.timer
systemctl start stan-community.service && journalctl -u stan-community -n 30 --no-pager
ls -t /home/stan/.stan/logs/ | grep -E 'submit_all|peg_sync' | head -2
```

Each rejected run's reason is in `~/.stan/logs/submit_all_<date>.jsonl`.
`DIA-NN version mismatch` or `could not be detected` has two causes. For a DIA
run, the DIA-NN that searched it (see its `diann.log`) was not 2.3.x. For a DDA
run, the `diann` on the unit's `PATH` is missing or is not 2.3.x (§4.1).

---

## 12. Operating the box

**Config reloads.** The watcher re-reads `instruments.yml` every 30 seconds.
That picks up **added, removed, enabled or disabled** instruments. It does
**not** pick up edits to an instrument that is already running (`watch_dir`,
`extensions`, `output_dir`, `lib_path` and so on). After such an edit, run
`systemctl restart stan-watch`.

**Where to look**

| What | Where |
|---|---|
| Watcher | `~/.stan/logs/watch_<start time>.log`, one file per watcher start, one record per line. `journalctl -u stan-watch` has the same records wrapped at 80 columns, plus anything printed before logging starts. |
| One search | `<output_dir>/<run>/diann.log` |
| Why a file was not processed | `stan watch-status --days 3` |
| Environment | `stan doctor` (also written to `~/.stan/logs/doctor_<ts>.log`) |
| Community syncs | `~/.stan/logs/submit_all_<date>.jsonl`, `~/.stan/logs/peg_sync_<ts>.jsonl` |
| Errors | `~/.stan/error_log.json` (last 100) |

**Update STAN.** Review the changes first. Updates are never automatic in this
layout.

```bash
# as stan
cd ~/stan-src
git fetch origin
git log --oneline HEAD..origin/main          # read what changes; see CHANGELOG.md
git checkout <commit>                        # or: git merge --ff-only origin/main
~/.stan/venv/bin/pip install -e "$HOME/stan-src[peg]"    # same extra as before; picks up new dependencies
```

```bash
# as root
systemctl restart stan-watch stan-dashboard
```

Verify with `stan version`, `stan doctor` and §9.4. To roll back, check out the
commit you recorded in §5, reinstall and restart.

**Search engines stay pinned.** STAN never changes DIA-NN or Sage. Change them
only when the pinned versions in `stan/search/community_params.py` change.

**Uninstall**

```bash
# as root
systemctl disable --now stan-watch stan-dashboard stan-watch-restart.timer stan-community.timer 2>/dev/null
rm -f /etc/systemd/system/stan-*.service /etc/systemd/system/stan-*.timer && systemctl daemon-reload
```

Then decide with the human whether to keep `/home/stan/.stan` (database and
config), `/srv/stan` (raw files and results) and `/opt/stan`, `/opt/diann`,
`/opt/sage-*`. On each instrument PC, run `install_flinders_copy.bat /remove`.

---

## 13. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| A file is in the watch directory but nothing happens | `stan watch-status --days 1`; `grep -h "QC filter rejected" ~/.stan/logs/watch_[0-9]*.log`; the §8.2 check script | A `QC filter rejected` line means the name does not match `qc_pattern` (with `qc_only: true`). Wrong `vendor`/`extensions` and `exclude_pattern` matches are **not** logged, so run the §8.2 check. A file renamed into place, or delivered while the watcher was down, needs `systemctl restart stan-watch`. On a network mount, use the `//` prefix (§6.2). |
| One instrument never started | `grep -h "Watch directory does not exist" ~/.stan/logs/watch_[0-9]*.log` | The mount or directory was missing at start. Fix `RequiresMountsFor`, then restart. |
| A Bruker `.d` never becomes stable | `stan watch-status` shows it matched but not in runs | It arrived complete (rule 3). Restart the watcher; the catch-up scan picks it up. |
| `DIA-NN executable not found` | `sudo -u stan env PATH=... which diann` | Set `diann_path` to `/opt/stan/bin/diann` and include `/opt/stan/bin` in the unit's `PATH`. |
| The search runs but no `report.parquet` | `<output_dir>/<run>/diann.log` | `0 files will be processed` means DIA-NN could not parse the name. `invalid raw MS data format` on Thermo means .NET is missing (§7.1). A library error means `lib_path` is wrong (§8.1). |
| Every Thermo run is searched as DIA, and DDA runs show 0 precursors | `grep -h "Could not detect acquisition mode" ~/.stan/logs/watch_[0-9]*.log` | `dotnet` is not on the unit's `PATH`, or the TRFP download failed (§7.2). Otherwise set `forced_mode`. |
| A search is killed at 20 minutes | `grep -h "timed out after 20 min" ~/.stan/logs/watch_[0-9]*.log` | The box is too slow for this gradient, or oversubscribed (§2). |
| Bruker PEG or drift is empty | `stan doctor`, "Critical compat checks" | Reinstall with the `[peg]` extra on Python ≤ 3.12 (§5), then `stan backfill-peg`. |
| Thermo PEG is empty | the §7.3 Verify | Set `STAN_TRFP_SIF` in the unit, or install fisher_py with .NET. Then run `stan backfill-peg` with `STAN_TRFP_SIF` exported in that shell as well. |
| `submit-all` says `community_submit is not enabled` | `grep community_submit ~/.stan/community.yml` | Set it to `true` in `community.yml` (§11), with the human's consent. A `community_submit` key in an `instruments.yml` block is not what submissions read. |
| `peg-sync` returns HTTP 403 | community.yml has no valid `auth_token` | Run `stan community-claim` (§11). |
| The dashboard cannot be reached from another PC | `ss -ltnp \| grep 8421` | By design it is on loopback. See the access table in §9.2. |

---

## 14. The Postgres backend is not for Mode B

Leave `STAN_DB_BACKEND` unset. A single box keeps its data in SQLite at
`~/.stan/stan.db`.

The Postgres backend (`STAN_DB_BACKEND=pg`) is wired to UC Davis's central PG
Farm. The host, database and service account are fixed in `PG_DEFAULTS` in
`stan/db_pg.py`, and `psycopg2` is not a STAN dependency, so the backend cannot
point at another lab's Postgres. [`PG_FARM.md`](PG_FARM.md) describes the UC
Davis deployment for reference.

`stan dashboard --backend sqlite` (§9.2) makes the choice explicit.
