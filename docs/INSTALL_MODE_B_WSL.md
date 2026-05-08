# STAN Mode B — Windows Lab Box via WSL2

Run STAN on a **dedicated Windows lab box** (not the instrument PC itself) that ingests
raw files from instrument PCs over an SMB share and runs the full DIA-NN + Sage pipeline
locally inside WSL2. No HPC, no Docker, no separate Linux server needed.

---

## When to Use Mode B vs A vs C

| Mode | What it is | When to pick it |
|------|-----------|-----------------|
| **A — Instrument PC** | STAN runs on the Windows PC attached to the mass spec (stan.bat) | Single instrument, PC has spare CPU/RAM, no network share needed |
| **B — WSL2 lab box (this doc)** | Beefy Windows workstation ingests raws over SMB, searches in WSL2 | Multiple instruments, instrument PCs are underpowered, want one search node |
| **C — SLURM cluster (Hive)** | STAN dispatches search jobs to a cluster via SSH | Big throughput, existing HPC account, DDA/DIA in parallel |

Mode B is the "middle weight": heavier setup than Mode A but no HPC account required.
A lab workstation with 32+ cores and 64+ GB RAM runs DIA-NN and Sage comfortably while
instrument PCs just copy files to a share.

---

## Prerequisites

**On the Windows lab box:**

1. **Windows 10 build 19041+ or Windows 11.**
   Check: `[System.Environment]::OSVersion.Version` in PowerShell — needs `10.0.19041` or higher.

2. **Virtualization enabled in BIOS.**
   Check: `systeminfo | findstr "Virtualization"` — must say `Virtualization Enabled In Firmware: Yes`.
   If not, reboot → BIOS → find `Intel Virtualization Technology` / `VT-x` / `AMD-V` / `SVM Mode` → Enable.
   Common BIOS keys: Dell `F2`, HP `F10`, Lenovo `F1`, ASUS/MSI `Del`.

3. **WSL2 with Ubuntu** — the launcher installs this automatically if missing.

4. **Git for Windows** (optional, only if you want to clone the repo directly).
   Download: https://git-scm.com/download/win

**On the instrument PCs:**

Configure them to copy finished raw files to a shared folder this box can see —
either a Windows share hosted on the lab box (`\\labbox\incoming`) or a NAS mount.
Mode A (`stan.bat`) has a `sync_raw_now` command for this in v0.2.200+.

---

## Quick Start

### Step 1 — Download STAN

Option A — PowerShell (no Git needed):
```powershell
cd $env:USERPROFILE
Invoke-WebRequest -Uri https://github.com/bsphinney/stan/archive/refs/heads/main.zip -OutFile stan.zip
Expand-Archive stan.zip .
Rename-Item stan-main STAN
cd STAN
```

Option B — Git:
```powershell
cd $env:USERPROFILE
git clone https://github.com/bsphinney/stan.git STAN
cd STAN
```

### Step 2 — Double-click `Launch_STAN_WSL.bat`

> **You may need to click it twice on the very first run.**
>
> If WSL/Ubuntu isn't set up yet, the launcher triggers Ubuntu install and exits with:
>
> `Ubuntu install triggered. After it finishes setting up, re-run this launcher.`
>
> A separate Ubuntu terminal window pops up — create a Linux username and password
> (these are **WSL-only credentials**, nothing to do with Windows or any HPC login).
>
> **Write down the Ubuntu password.** The next run prompts for it when installing
> system packages (`sudo apt-get`). Ubuntu does not echo the password as you type — that
> is normal, just type it and press Enter.
>
> Close the Ubuntu window when done, then **double-click the launcher again**.

On that second run the installer begins. It downloads:
- Python packages + STAN (~400 MB)
- DIA-NN Linux binary (~500 MB, academic license prompt)
- Sage Linux binary (~5 MB, no license needed)
- .NET 8 SDK (~800 MB, needed by DIA-NN to read Thermo `.raw` files)

**Total: 10–20 minutes** on a fast connection.

After download you'll be asked one question:
```
Path to incoming raw files [leave blank for ~/stan_incoming]:
```
Type the Windows path to your SMB share (e.g. `Y:\incoming`) or a WSL path (e.g.
`/mnt/y/incoming`). See [SMB Share Configuration](#smb-share-configuration) below.

When setup finishes, the browser opens automatically at **http://localhost:8421**.

### Step 3 — Subsequent runs

Double-click `Launch_STAN_WSL.bat`. Takes about 30 seconds to start.

---

## Where Files Go

### Windows side
| Location | What |
|----------|------|
| `%USERPROFILE%\STAN\` | Launcher + setup script (wherever you cloned) |
| SMB share (e.g. `Y:\incoming\`) | Raw files from instrument PCs |

### WSL2 side (inside Ubuntu)
| Location | What |
|----------|------|
| `~/.stan/venv/` | Python venv with STAN installed |
| `~/.stan/instruments.yml` | Watch directory + instrument config |
| `~/.stan/community.yml` | Community benchmark opt-in |
| `~/.stan/stan.db` | SQLite QC database |
| `~/.stan/diann/` | DIA-NN Linux binary + RawFileReader DLLs |
| `~/.stan/sage/` | Sage Linux binary |
| `~/stan_incoming/` | Default watch dir (if you chose the blank default) |
| `~/STAN/logs/` | Watcher + backfill logs |

All WSL files are accessible from Windows File Explorer at:
```
\\wsl.localhost\Ubuntu\home\<your-linux-username>\
```

---

## SMB Share Configuration

### Scenario A — Lab box hosts the share, instrument PCs push to it

On the **Windows lab box**, share a folder:
1. Create `C:\STAN_incoming` (or wherever you have space)
2. Right-click → Properties → Sharing → Share → add "Everyone" with Read/Write
3. Note the share name (e.g. `\\LABBOX\STAN_incoming`)

On each **instrument PC** (Mode A), configure the sync target in `~/.stan/instruments.yml`
or use `stan sync-raw-now` to push individual files.

When running the STAN WSL setup wizard, enter:
```
C:\STAN_incoming
```
The script converts this to `/mnt/c/STAN_incoming` inside WSL automatically.

### Scenario B — NAS or instrument PC hosts the share, lab box mounts it

Map the share as a drive letter in Windows (e.g. `Y:\`) via:
```
net use Y: \\labserver\proteomics\incoming /persistent:yes
```

Then in the WSL setup wizard enter `Y:\incoming` (or `Y:\` if files land at the root).

### Manual WSL mount for UNC paths

If you prefer the WSL path directly:
```bash
# In an Ubuntu terminal (Start → Ubuntu):
sudo mkdir -p /mnt/smb_incoming
sudo mount -t drvfs '\\labserver\proteomics\incoming' /mnt/smb_incoming
```

To make this mount persist across WSL restarts, add to `/etc/fstab` inside WSL:
```
\\labserver\proteomics\incoming  /mnt/smb_incoming  drvfs  defaults  0  0
```

### Performance note on `/mnt/` paths

Files under `/mnt/c/`, `/mnt/y/`, etc. are accessed via WSL's 9P bridge. Random-access
I/O is somewhat slower than native WSL paths. For sequential reads (`.raw`, `.d` files
during search) this is fine in practice. If you hit search failures on large files or
inconsistent hangs, copy the files to the WSL-native filesystem first:
```bash
cp /mnt/y/incoming/run001.raw ~/stan_incoming/
```

---

## Running the Setup Script Directly in Ubuntu

The `.bat` is a Windows-side wrapper. You can skip it and run the setup script directly
in the Ubuntu terminal — useful if the wrapper hangs, you want one scrollback, or you
need to re-run a single step:

```bash
# Open Ubuntu (Start menu → Ubuntu, or wsl -d Ubuntu from PowerShell)

# Option A — copy from your Windows STAN folder
cp /mnt/c/Users/<you>/STAN/stan_wsl_setup.sh ~/stan_wsl_setup.sh
chmod +x ~/stan_wsl_setup.sh
bash ~/stan_wsl_setup.sh

# Option B — pull latest from GitHub directly
curl -sL https://raw.githubusercontent.com/bsphinney/stan/main/stan_wsl_setup.sh \
    -o ~/stan_wsl_setup.sh
chmod +x ~/stan_wsl_setup.sh
bash ~/stan_wsl_setup.sh
```

Available subcommands:
```bash
bash ~/stan_wsl_setup.sh            # auto: install if needed, then launch
bash ~/stan_wsl_setup.sh install    # one-time install only
bash ~/stan_wsl_setup.sh update     # pip upgrade STAN + refresh tool checks
bash ~/stan_wsl_setup.sh watch      # start watcher only (dashboard already running)
bash ~/stan_wsl_setup.sh dashboard  # start dashboard only
bash ~/stan_wsl_setup.sh diann      # install/reinstall DIA-NN
bash ~/stan_wsl_setup.sh config     # re-run SMB share + instruments.yml wizard
```

The script is **idempotent** — safe to re-run if partially installed (it skips what's
already done).

---

## Editing `instruments.yml` After Setup

The config lives at `~/.stan/instruments.yml` inside WSL:

```bash
# Open Ubuntu shell
nano ~/.stan/instruments.yml
```

Or from Windows, navigate in File Explorer to:
```
\\wsl.localhost\Ubuntu\home\<linux-username>\.stan\instruments.yml
```

Key fields for Mode B:

```yaml
instruments:
- name: "timsTOF HT"
  watch_dir: /mnt/y/incoming          # SMB mount point inside WSL
  enabled: true
  processing_mode: local              # runs DIA-NN/Sage on this machine (not Hive)
  raw_handling: convert_mzml          # Thermo .raw → mzML for Sage; DIA-NN reads .raw natively
  hela_amount_ng: 50.0
  community_submit: false             # set true to opt into community benchmark
  stable_secs: 60                     # seconds of size-stability before triggering search

diann_binary: /home/<you>/.stan/diann/diann-linux
sage_binary: /home/<you>/.stan/sage/sage
```

STAN hot-reloads `instruments.yml` every 30 seconds — no restart needed after edits.

---

## Troubleshooting

### Launcher fails: `WSL_E_DISTRO_NOT_FOUND` or `HCS_E_HYPERV_NOT_INSTALLED`

**Distro missing:**
```
There is no distribution with the supplied name.
Error code: Wsl/Service/WSL_E_DISTRO_NOT_FOUND
```
The launcher detects this and triggers `wsl --install -d Ubuntu` automatically. If that
itself fails, see Hyper-V below.

**Hyper-V / virtualization missing:**
```
WSL2 is not supported with your current machine configuration.
Please enable the "Virtual Machine Platform" optional component and ensure
virtualization is enabled in the BIOS.
Error code: Wsl/...HCS_E_HYPERV_NOT_INSTALLED
```

Fix in order:
1. Enable BIOS virtualization (see Prerequisites).
2. In **admin PowerShell**: `wsl.exe --install --no-distribution` then reboot.
3. In **admin PowerShell**: `wsl --install -d Ubuntu`

Diagnose:
```powershell
systeminfo | Select-String "Hyper-V Requirements" -Context 0,4
# Want: "Virtualization Enabled In Firmware: Yes"
wsl --status
# Want: "WSL version: 2.x.x.x"
```

### "WSL1 is not supported" — misleading, ignore it

```
Default Version: 2
WSL1 is not supported with your current machine configuration.
```
This means WSL2 kernel isn't installed yet, not that you need WSL1. Run
`wsl.exe --install --no-distribution` from admin PowerShell, reboot.

### `wsl --install` says "no access"

Run PowerShell as Administrator. WSL install requires admin rights on first setup.

### Launcher hangs at "Copying setup script into WSL"

Check that `stan_wsl_setup.sh` sits next to `Launch_STAN_WSL.bat`. If STAN is inside
OneDrive, try cloning to `C:\STAN\` instead — OneDrive paths sometimes confuse `wslpath`.

### DIA-NN fails: "cannot read .raw files, please install .NET SDK 8.0.407+"

DIA-NN 2.x needs the .NET 8 **SDK** (not just the runtime) to read Thermo `.raw` files.
The installer uses a 4-tier fallback to get the SDK. If it still fails:

```bash
# Check what's installed
dotnet --list-sdks      # must show an 8.x line
dotnet --list-runtimes  # secondary check

# If --list-sdks is empty, force SDK install:
curl -sSL https://dot.net/v1/dotnet-install.sh | sudo bash -s -- --channel 8.0 --install-dir /usr/share/dotnet
sudo ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet
```

Key distinction:
- `dotnet --list-runtimes` shows `Microsoft.NETCore.App 8.0.x` → you have the **runtime**
- `dotnet --list-sdks` is empty → you do **not** have the SDK
- DIA-NN needs the SDK. Runtime-only installs will fail with the above error.

### DIA-NN exits silently (no output, no error, exit code 134)

Missing .NET 8 system libraries. `dotnet-install.sh` does not pull them automatically:

```bash
sudo apt-get install -y --no-install-recommends \
    libc6 libgcc-s1 libssl3 libstdc++6 libunwind8 zlib1g \
    libgssapi-krb5-2 liblttng-ust1

# libicu version varies by Ubuntu release:
for ic in libicu76 libicu74 libicu72 libicu71 libicu70; do
    if apt-cache show "${ic}" >/dev/null 2>&1; then
        sudo apt-get install -y --no-install-recommends "${ic}"; break
    fi
done
```

Then verify:
```bash
ldd ~/.stan/diann/diann-linux | grep 'not found'
# Empty output = all libs resolve. Any "not found" = install the listed package.
```

### Sage download fails

Sage is a static Linux binary from GitHub Releases. If the auto-detect fails:
```bash
# Find the latest release tag manually:
# https://github.com/lazear/sage/releases/latest

# Download manually and move into place:
wget https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz
tar -xzf sage-*.tar.gz
mv sage ~/.stan/sage/sage
chmod +x ~/.stan/sage/sage
```

### Dashboard not accessible from Windows browser

The dashboard binds to `0.0.0.0:8421` inside WSL2. WSL2 port-forwards to the Windows
host automatically on Windows 11 and recent Windows 10. Open `http://localhost:8421`.

If it still doesn't connect:
```powershell
# In PowerShell — check the port is forwarding
netsh interface portproxy show all
# Should show a forwarding rule for port 8421

# If missing, add it (admin PowerShell):
netsh interface portproxy add v4tov4 listenport=8421 listenaddress=0.0.0.0 connectport=8421 connectaddress=(wsl hostname -I)
```

### `/mnt/` path searches hang or fail on large `.raw` files

WSL2's 9P filesystem bridge for `/mnt/` paths has higher latency for random-access I/O.
DIA-NN and Sage do sequential reads, which is usually fine. If you hit hangs:

```bash
# Copy files to native WSL filesystem before searching
mkdir -p ~/stan_search_cache
cp /mnt/y/incoming/problematic_run.raw ~/stan_search_cache/
# Then update watch_dir in instruments.yml to ~/stan_search_cache
```

### Port 8421 already in use

```bash
# In Ubuntu shell:
wsl -d Ubuntu -e bash -c "STAN_PORT=8422 bash ~/stan_wsl_setup.sh"
```

Or kill whatever is using it:
```bash
wsl -d Ubuntu -e bash -c "lsof -ti:8421 | xargs kill -9 2>/dev/null"
```

### Update STAN to latest

```bash
wsl -d Ubuntu -e bash -c "bash ~/stan_wsl_setup.sh update"
```

Or from inside an Ubuntu terminal:
```bash
bash ~/stan_wsl_setup.sh update
```

---

## Uninstall

Everything lives in `~/.stan/` inside WSL. To wipe it:

```bash
wsl -d Ubuntu -e bash -c "rm -rf ~/.stan ~/stan_wsl_setup.sh ~/STAN/logs"
```

The apt-installed packages stay — remove if you want a completely clean slate:
```bash
wsl -d Ubuntu -e bash -c "sudo apt-get remove -y python3-venv build-essential"
```

---

## Links

- STAN GitHub: https://github.com/bsphinney/stan
- Community dashboard: https://huggingface.co/spaces/brettsp/stan
- Community benchmark data: https://huggingface.co/datasets/brettsp/stan-benchmark
- DIA-NN license: https://github.com/vdemichev/DiaNN/blob/master/LICENSE.md
- Sage releases: https://github.com/lazear/sage/releases
