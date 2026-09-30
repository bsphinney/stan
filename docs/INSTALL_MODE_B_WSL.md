# STAN Mode B on Windows — WSL2 notes

Mode B runs STAN on a machine that is **not** an instrument PC. The primary
guide is [`INSTALL_MODE_B_LINUX.md`](INSTALL_MODE_B_LINUX.md). WSL2 is one way to
get that Linux machine on a Windows workstation. This page covers only what is
different inside WSL2. Everything else, including versions, config, services,
verification and troubleshooting, is in the Linux guide. Do not duplicate it
here.

The page is written for an **AI coding agent** that a lab has pointed at this
repository, and a human administrator can follow it too. The agent operating
rules at the top of the Linux guide apply here as well. That includes every
**ASK** point and the rule that nothing except a copy task goes on an
instrument PC.

> **Arrived from an old link?** Earlier versions of this page had the
> instrument PCs run a full STAN install with `processing_mode: hive` and
> `submit_after_upload: false`. Do not do that. It puts STAN on the acquisition
> PC. Its upload also copies to `<name>.partial` and then renames the file, and
> the Mode B watcher does not see renamed files. Use the copy task in
> [Linux guide §6.3](INSTALL_MODE_B_LINUX.md#63-the-copy-task-on-each-instrument-pc)
> instead.

## Contents

- [1. When WSL2 fits](#1-when-wsl2-fits)
- [2. Prerequisites](#2-prerequisites)
- [3. Recommended install: Ubuntu 24.04 with systemd](#3-recommended-install-ubuntu-2404-with-systemd)
- [4. Raw files on Windows drives and shares](#4-raw-files-on-windows-drives-and-shares)
- [5. Reaching the dashboard](#5-reaching-the-dashboard)
- [6. The one-click launcher (quick trial)](#6-the-one-click-launcher-quick-trial)
- [7. Where files live](#7-where-files-live)
- [8. WSL-specific troubleshooting](#8-wsl-specific-troubleshooting)
- [9. Update and uninstall](#9-update-and-uninstall)

---

## 1. When WSL2 fits

| Situation | Use |
|---|---|
| A spare Windows workstation, not attached to an instrument, with the cores and RAM in [Linux guide §2](INSTALL_MODE_B_LINUX.md#2-box-requirements) | WSL2 (this page) |
| A machine that can run Linux natively | The [Linux guide](INSTALL_MODE_B_LINUX.md) alone. It is simpler and avoids the file-event limitation in §4. |
| The Windows PC attached to the mass spectrometer | Neither. That is Mode A, which is not recommended ([README](../README.md#quick-install--pick-your-mode)). |
| A SLURM cluster is available | Mode C, [`INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md) |

---

## 2. Prerequisites

| Requirement | Check (PowerShell) |
|---|---|
| Windows 10 build 19041 or later, or Windows 11. Mirrored networking (§5) needs Windows 11 22H2 or later. | `[System.Environment]::OSVersion.Version` |
| Virtualization enabled in firmware | `systeminfo \| Select-String "Virtualization"` shows `Virtualization Enabled In Firmware: Yes`. If not, enable Intel VT-x or AMD-V/SVM in the BIOS. Common keys: Dell `F2`, HP `F10`, Lenovo `F1`, ASUS/MSI `Del`. |
| Administrator rights for the first WSL install | Run PowerShell as Administrator |
| A Windows user who stays logged on | WSL2 runs inside a user session (§3.5) |

---

## 3. Recommended install: Ubuntu 24.04 with systemd

This route runs the Linux guide unchanged, with the watcher and dashboard as
systemd services.

### 3.1 Install the distribution

Name the release explicitly. The plain `Ubuntu` distribution follows the newest
LTS, and newer releases may ship a Python too new for the `[peg]` extra
([Linux guide §2](INSTALL_MODE_B_LINUX.md#2-box-requirements)).

```powershell
# Administrator PowerShell
wsl --install -d Ubuntu-24.04
```

Create the Linux username and password when prompted. They are WSL-only
credentials. Ubuntu does not echo the password as you type.

**Verify:** `wsl -l -v` lists `Ubuntu-24.04` with `VERSION 2`.

### 3.2 Enable systemd

```bash
# inside Ubuntu-24.04
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04 -e systemctl is-system-running
```

**Verify:** the last command prints `running` or `degraded`, not an error about
systemd.

### 3.3 Give the VM enough CPU and memory

WSL2 caps its VM at a share of the host's RAM (by default half on current
builds). STAN sizes each DIA-NN and Sage search from the CPUs the VM reports
(half of them). Write
`%USERPROFILE%\.wslconfig` on the Windows side:

```ini
[wsl2]
memory=48GB        # at least 32 GB per concurrently searching instrument; leave room for Windows
processors=16
```

Run `wsl --shutdown` to apply it.

**Verify:** inside Ubuntu, `free -g` and `nproc` show the new values.

### 3.4 Follow the Linux guide, with these differences

Work through [Linux guide §3 to §12](INSTALL_MODE_B_LINUX.md#3-system-packages-and-the-service-account)
inside Ubuntu-24.04. Change these steps:

| Linux guide step | Change in WSL2 |
|---|---|
| §6 Get raw files onto the box | Windows hosts the incoming share, not Samba inside WSL. See §4 of this page. |
| §8.2 `watch_dir` | A folder on a Windows drive is under `/mnt/<letter>/`. Write it with a leading `//` (§4.2). |
| §8.2 `output_dir` | Keep it on the Linux filesystem, e.g. `/srv/stan/qc_output/<instrument>`. `/mnt/` paths are slower (§4.4). |
| §9.1 `RequiresMountsFor` | Use the Linux-side directory, e.g. `/srv/stan`. The `/mnt/<letter>` drives are mounted by WSL itself. |
| §9.2 dashboard access | See §5 of this page. |

### 3.5 Keep WSL running

WSL can stop the distribution when no Windows-side session is attached. When
it stops, the watcher stops too. Start a long-lived process from a scheduled
task at logon. This is one way to do it; STAN has not tested it:

```powershell
# PowerShell as the Windows user who owns the distribution
$action   = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu-24.04 --exec /bin/sleep infinity"
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "STAN WSL keep-alive" -Action $action -Trigger $trigger -Settings $settings
```

**Verify.** Log off and on again, or reboot. Close every Ubuntu window and wait
five minutes. `wsl -l -v` still shows `Ubuntu-24.04` as `Running`, and
<http://localhost:8421> still answers.

---

## 4. Raw files on Windows drives and shares

### 4.1 Windows hosts the incoming share

**ASK** which Windows account the instrument PCs will use. Then, in
Administrator PowerShell:

```powershell
New-Item -ItemType Directory -Force -Path D:\STAN_incoming\timsTOF_HT     # one folder per instrument
New-SmbShare -Name stan_incoming -Path D:\STAN_incoming -ChangeAccess "<DOMAIN\account>"
icacls D:\STAN_incoming /grant "<DOMAIN\account>:(OI)(CI)M"
```

On each instrument PC, install the copy task from
[Linux guide §6.3](INSTALL_MODE_B_LINUX.md#63-the-copy-task-on-each-instrument-pc),
with `\\<this-pc>\stan_incoming` mapped to a drive letter. The four delivery
rules in [Linux guide §6](INSTALL_MODE_B_LINUX.md#the-four-rules-for-anything-that-delivers-files)
apply unchanged.

**Verify:** on an instrument PC, `Test-Path \\<this-pc>\stan_incoming\timsTOF_HT`
prints `True`. Inside Ubuntu, `ls /mnt/d/STAN_incoming` lists the folder.

### 4.2 `watch_dir` under `/mnt/` needs a leading `//`

WSL2 does not deliver Linux file-change (inotify) events for files that Windows
programs write under `/mnt/<letter>/`. The SMB server is such a program. With a
plain path, the watcher only finds new files at its startup catch-up scan.
Prefix the path with `//` so STAN uses its polling observer:

```yaml
    watch_dir: //mnt/d/STAN_incoming/timsTOF_HT
```

**Verify.** After the watcher starts, run
`grep "watcher: started" "$(ls -t ~/.stan/logs/watch_[0-9]*.log | head -1)"` as the
account that runs STAN. The line shows `observer=PollingObserver`. Use the log
file, not `journalctl`, which wraps the line before the `observer=` part
([Linux guide §9.4](INSTALL_MODE_B_LINUX.md#94-enable-and-verify)).

### 4.3 Mapped drive letters and UNC paths

WSL mounts fixed local drives automatically. It does **not** mount mapped
network drive letters or UNC paths. Mount them yourself, read-only is enough,
and use the `//` prefix for `watch_dir` as in §4.2:

```bash
sudo mkdir -p /mnt/y && sudo mount -t drvfs Y: /mnt/y -o ro
sudo mkdir -p /mnt/nas && sudo mount -t drvfs '\\labserver\proteomics\incoming' /mnt/nas -o ro
```

To remount at every start, add a line to `/etc/fstab` inside WSL:

```
\\labserver\proteomics\incoming  /mnt/nas  drvfs  ro  0  0
```

### 4.4 Performance of `/mnt/` paths

Reading `/mnt/<letter>/` goes through WSL's 9P bridge, which is slower than the
Linux filesystem. DIA-NN and Sage read raw files mostly sequentially, which is
usually fine. Keep `output_dir` on the Linux side. If searches of large files
hang or fail only under `/mnt/`, test by copying one file to the Linux side
(`cp /mnt/d/STAN_incoming/<inst>/<run> /srv/stan/test/`) and searching it from
there.

---

## 5. Reaching the dashboard

| Who needs it | How |
|---|---|
| Someone at this Windows PC | <http://localhost:8421>. WSL forwards Windows `localhost` to services bound to localhost or to all addresses inside WSL, so the Linux guide's `--host localhost` works as is. |
| Other PCs on the lab network | **ASK** first; the dashboard has no login ([Linux guide §9.2](INSTALL_MODE_B_LINUX.md#92-stan-dashboardservice)). On Windows 11 22H2+, set `networkingMode=mirrored` under `[wsl2]` in `.wslconfig`. Change the dashboard unit to `--host 0.0.0.0`, as in the Linux guide's trusted-subnet row. Allow TCP 8421 from the lab subnet only, in Windows Firewall. If connections still fail, check the Hyper-V firewall settings for WSL. |

For the default NAT mode, an alternative is a port proxy. It needs the
dashboard bound to `0.0.0.0` inside WSL. The WSL address changes at every WSL
restart, so re-run it after each one:

```powershell
# Administrator PowerShell
$ip = ((wsl -d Ubuntu-24.04 hostname -I).Trim() -split '\s+')[0]
netsh interface portproxy add v4tov4 listenport=8421 listenaddress=0.0.0.0 connectport=8421 connectaddress=$ip
New-NetFirewallRule -DisplayName "STAN dashboard" -Direction Inbound -Protocol TCP -LocalPort 8421 -RemoteAddress <lab-subnet> -Action Allow
```

**Verify:** from another PC, open `http://<this-pc>:8421`. Check the rules with
`netsh interface portproxy show all`.

---

## 6. The one-click launcher (quick trial)

`Launch_STAN_WSL.bat` plus `stan_wsl_setup.sh` install STAN into a distribution
named exactly **`Ubuntu`**, and run it in a console window that must stay open.
This is fine for a trial. For an unattended box, use §3.

### 6.1 What it does

Put `Launch_STAN_WSL.bat` and `stan_wsl_setup.sh` in the same folder, for
example by cloning the repository to `C:\STAN`. Keep them out of OneDrive, which
confuses `wslpath`. Double-click the `.bat`.

1. If no working `Ubuntu` distribution exists, it runs `wsl --install -d Ubuntu`
   and exits. Create the Linux user, close that window and double-click again.
2. It copies `stan_wsl_setup.sh` to `~/stan_wsl_setup.sh` and runs it in `auto`
   mode. **On every launch** that:
   - runs `sudo apt-get update` and installs packages, prompting for the
     Ubuntu password;
   - upgrades STAN from GitHub `main` (unpinned, no extras);
   - checks the .NET 8 dependencies and verifies DIA-NN.
3. On first run it asks you to accept the DIA-NN licence (type `yes`), then
   installs:
   - .NET 8 SDK;
   - DIA-NN **2.3.2** into `~/.stan/diann/`, symlinked as `~/.local/bin/diann`;
   - Sage from GitHub's *latest* release into `~/.stan/sage/`, symlinked as
     `~/.local/bin/sage`.

   Then it asks for the incoming path and an instrument name, and writes
   `~/.stan/instruments.yml` and `~/.stan/community.yml`.
4. It starts `stan dashboard --host 0.0.0.0 --port 8421` in the background and
   `stan watch` in the foreground. The `.bat` opens a browser when port 8421
   starts listening. Press Ctrl+C to stop both.

### 6.2 Fixes needed after the first run

The generated config and some of the script's choices do not match the current
code. Stop STAN with Ctrl+C, then fix these inside Ubuntu:

| Problem | Effect | Fix |
|---|---|---|
| The generated instrument block has no `vendor`, `extensions` or `output_dir`, and no library | The watcher ignores every raw file. Output would land in the launcher's Windows working folder, and DIA searches would have no spectral library. | Download the library and FASTA ([Linux guide §8.1](INSTALL_MODE_B_LINUX.md#81-spectral-library-and-fasta)). Rewrite `~/.stan/instruments.yml` as in [Linux guide §8.2](INSTALL_MODE_B_LINUX.md#82-instrumentsyml), using `diann_path: /home/<you>/.local/bin/diann` and `sage_path: /home/<you>/.stan/sage/sage`, and the `//` rule from §4.2. Delete `raw_handling`, `diann_binary` and `sage_binary`; nothing reads them. Then run the Linux guide's check script. |
| `community.yml` holds `submit`, `lab_name`, `instrument_serial` | Nothing reads these keys. With no `error_telemetry` key, error telemetry is off. | Replace the file with [Linux guide §8.3](INSTALL_MODE_B_LINUX.md#83-communityyml). |
| The first launch stops at a fleet-sync question | With no `thresholds.yml`, the launcher runs `stan init`. That creates an empty `thresholds.yml` (no gates, so everything passes), leaves the launcher's `instruments.yml` and `community.yml` alone, and asks one fleet-sync question. Later launches skip it, because `thresholds.yml` now exists. | Press Enter: the default, `3` (None), is right. Add real thresholds later ([Linux guide §8.4](INSTALL_MODE_B_LINUX.md#84-thresholdsyml-optional)). |
| Sage comes from `releases/latest` | It can drift away from the pinned v0.14.7, and `--version` cannot tell you which release you have: the v0.14.7 binary prints `sage 0.14.6` | Download v0.14.7 and check its sha256 as in [Linux guide §4.3](INSTALL_MODE_B_LINUX.md#43-sage-0147), then copy its `sage` binary over `~/.stan/sage/sage`. The launcher skips its own Sage download while that file exists. `~/.stan/sage/sage --version` then prints `sage 0.14.6`, which is correct. |
| No `[peg]` extra | Bruker PEG and window drift stay empty | `~/.stan/venv/bin/pip install "stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"`. This needs Python ≤ 3.12 in the distribution (`python3 --version`). |
| The log says `STAN installed: unknown` | Cosmetic. The script calls `stan --version`, which does not exist. | Check with `~/.stan/venv/bin/stan version`. |

**DIA-NN 2.3.2 or 2.3.0.** The launcher installs DIA-NN 2.3.2 rather than the
pinned 2.3.0. That needs no fix: the community benchmark accepts any 2.3.x and
marks 2.3.2 rows asset-verified just like 2.3.0 rows. Never set
`DIANN_VERSION=latest`, which installs a version the community check rejects.

**Optional: install DIA-NN 2.3.0 for the launcher.** Do this only if the lab
wants the exact pin. `DIANN_VERSION=2.3.0` does not work: the script builds a
file name without `-Preview`, which returns 404. The script skips its own
download, and its licence prompt, when `~/.stan/diann/diann-linux` already
exists. So **ASK** for licence acceptance first, as in
[Linux guide §4.1](INSTALL_MODE_B_LINUX.md#41-dia-nn-230-native). Then put
2.3.0 there before the first launch, or after `rm -rf ~/.stan/diann` on an
existing install:

```bash
cd /tmp
curl -fLO https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Linux-Preview.zip
unzip -q DIA-NN-2.3.0-Academia-Linux-Preview.zip 'diann-2.3.0/*' -d /tmp/diann230
mkdir -p ~/.stan/diann && mv /tmp/diann230/diann-2.3.0/* ~/.stan/diann/
chmod +x ~/.stan/diann/diann-linux
```

**Verify:** run the Linux guide's §8.2 check script and
`~/.local/bin/diann 2>&1 | head -3`. The check prints `0 problem(s)` and DIA-NN
reports `2.3.2` (`2.3.0` if you installed it). Then relaunch and run the
[Linux guide §10](INSTALL_MODE_B_LINUX.md#10-end-to-end-test-with-a-real-qc-file)
end-to-end test.

If setup stops right after `All dynamic libs resolve (ldd clean)`, the DIA-NN
smoke test returned non-zero and the script's `set -e` ended it before printing
why. Run `~/.stan/diann/diann-linux --help; echo $?` to see the error.

### 6.3 Subcommands

Run these inside Ubuntu:

```bash
bash ~/stan_wsl_setup.sh            # auto: install what is missing, then run dashboard + watcher
bash ~/stan_wsl_setup.sh install    # install only
bash ~/stan_wsl_setup.sh update     # pip upgrade STAN from main, re-check tools
bash ~/stan_wsl_setup.sh watch      # watcher only
bash ~/stan_wsl_setup.sh dashboard  # dashboard only (binds 0.0.0.0)
bash ~/stan_wsl_setup.sh diann      # install .NET 8, download DIA-NN only if ~/.stan/diann/diann-linux is missing, verify it
bash ~/stan_wsl_setup.sh config     # re-run the incoming-path wizard (overwrites instruments.yml)
```

`STAN_PORT=8422 bash ~/stan_wsl_setup.sh` changes the port. The `.bat` still
waits for 8421 before opening a browser, so open the page yourself.

---

## 7. Where files live

| Location | What |
|---|---|
| `~/.stan/` inside WSL | Config, `stan.db`, `logs/`, `tools/`, venv (launcher route: also `diann/`, `sage/`) |
| `\\wsl.localhost\<distro>\home\<user>\.stan\` | The same folder, seen from Windows File Explorer |
| `D:\STAN_incoming\` (example) | The share the instrument PCs write to, `/mnt/d/STAN_incoming` inside WSL |
| `%USERPROFILE%\.wslconfig` | VM CPU and memory limits (§3.3) |
| `%USERPROFILE%\AppData\Local\Packages\CanonicalGroupLimited.*\LocalState\ext4.vhdx` | The virtual disk holding the Linux filesystem |

---

## 8. WSL-specific troubleshooting

For STAN problems (files not picked up, DIA-NN errors, PEG, community sync), use
the [Linux guide's troubleshooting table](INSTALL_MODE_B_LINUX.md#13-troubleshooting).

**`WSL_E_DISTRO_NOT_FOUND`.** The distribution is missing. Run
`wsl --install -d Ubuntu-24.04`. The launcher looks only for one named
`Ubuntu`.

**`HCS_E_HYPERV_NOT_INSTALLED`, or "WSL2 is not supported with your current
machine configuration".** Fix in this order:

1. Enable virtualization in the BIOS (§2).
2. In Administrator PowerShell, run `wsl.exe --install --no-distribution`, then
   reboot.
3. Install the distribution again.

`wsl --status` should report WSL version 2.

**"WSL1 is not supported with your current machine configuration".** This means
the WSL2 kernel is not installed yet, not that you need WSL1. Run
`wsl.exe --install --no-distribution` from Administrator PowerShell and reboot.

**`wsl --install` says access is denied.** The first install needs an
Administrator PowerShell.

**Launcher hangs at "Copying setup script into WSL".** Check that
`stan_wsl_setup.sh` sits next to the `.bat`. If it is inside OneDrive, move the
folder to `C:\STAN`.

**DIA-NN: "cannot read .raw files, please install .NET SDK 8.0.407+".**
`dotnet --list-sdks` must show an `8.0` line; a runtime alone
(`dotnet --list-runtimes`) is not enough. Fix it with
`bash ~/stan_wsl_setup.sh diann`, or with the manual tiers in
[Linux guide §7.1](INSTALL_MODE_B_LINUX.md#71-net-8).

**DIA-NN exits silently with code 134.** .NET 8 system libraries are missing:

```bash
sudo apt-get install -y --no-install-recommends libc6 libgcc-s1 libssl3 libstdc++6 libunwind8 zlib1g libgssapi-krb5-2 liblttng-ust1
for ic in libicu76 libicu74 libicu72 libicu71 libicu70; do apt-cache show "$ic" >/dev/null 2>&1 && { sudo apt-get install -y --no-install-recommends "$ic"; break; }; done
ldd ~/.stan/diann/diann-linux | grep 'not found'     # empty = every library resolves
```

**New raw files are only found after a restart.** `watch_dir` is under `/mnt/`
without the leading `//` (§4.2).

**Dashboard not reachable from Windows.** Check that it is running inside WSL
(`curl -s http://localhost:8421/api/version` inside Ubuntu). Then check that
`localhostForwarding` is not set to `false` in `.wslconfig`. For other PCs, see
§5.

**Port 8421 already in use.** Inside Ubuntu, `ss -ltnp | grep 8421` shows the
owner. Stop it, or use another port (§6.3).

---

## 9. Update and uninstall

**Update.** On the systemd route, follow
[Linux guide §12](INSTALL_MODE_B_LINUX.md#12-operating-the-box). On the launcher
route, every launch already upgrades STAN to GitHub `main`.
`bash ~/stan_wsl_setup.sh update` does the same without starting STAN. Neither
route changes DIA-NN or Sage.

**Uninstall.** First take the services down as in
[Linux guide §12](INSTALL_MODE_B_LINUX.md#12-operating-the-box). Then decide
with the human whether to keep `~/.stan` (database and config). To remove the
whole distribution, including every file inside it:

```powershell
wsl --unregister Ubuntu-24.04        # or: Ubuntu, for the launcher route
Unregister-ScheduledTask -TaskName "STAN WSL keep-alive" -Confirm:$false
```

This does not touch `D:\STAN_incoming` or the SMB share. Remove those with
`Remove-SmbShare -Name stan_incoming` if they are no longer needed.

## Links

- Primary guide: [`INSTALL_MODE_B_LINUX.md`](INSTALL_MODE_B_LINUX.md)
- STAN: <https://github.com/bsphinney/stan>
- Community site: <https://community.stan-proteomics.org>
- DIA-NN licence: <https://github.com/vdemichev/DiaNN/blob/master/LICENSE.md>
- Sage releases: <https://github.com/lazear/sage/releases>
