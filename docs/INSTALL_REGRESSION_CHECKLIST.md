# STAN Mode A Install Regression Checklist

> **Audience**: Lab staff doing a first install or reinstall of STAN on a Windows instrument PC. No programming knowledge required.

---

## 1. Why this checklist exists

On 2026-05-08, Brett's lab installed STAN on a Lumos instrument PC using `install_stan_lumosrox.bat`. The install was designed to be one-click. In practice it hit **six distinct bugs** that took roughly two hours and four STAN version bumps to diagnose and fix in real time:

| # | Bug | Symptom | Commit fixed |
|---|-----|---------|-------------|
| 1 | `install_stan.ps1` contained em-dash characters that Windows PowerShell 5.1 misread as garbled text | `Missing closing }` error at line 406, install aborts immediately | 4da0f4a |
| 2 | The wrapper `install-stan.bat` only downloaded itself `if not exist` — operators with a cached old copy kept getting the buggy PS1 even after the fix shipped | Install appeared to run but used an outdated installer | 9a18279 |
| 3 | `stan init` was piped to `Out-Null`, hiding its interactive prompt | Step 7 hung silently for minutes; operator saw a blank command window | 96236b3 |
| 4 | DIA-NN version picker used alphabetical order, picking 1.8.1 over 2.3.2 | Dashboard reported DIA-NN 1.x; community benchmark rejected submissions | 96236b3 |
| 5 | `configure_instruments_yml.ps1` wrote to `~/.stan/instruments.yml` but the watcher read from `~/STAN/instruments.yml` | Hive mode never activated; watcher ran in local mode silently | c5bf09a |
| 6 | `install_stan_lumosrox.bat` re-ran the base STAN install even when STAN was already installed | Redundant reinstall added 3+ minutes and could clobber a working venv | 96236b3 |

This checklist captures what a clean install looks like and what to watch for if any of these bugs return.

---

## 2. Pre-flight — before clicking anything

Confirm all of the following before running `install_stan_lumosrox.bat` (or any other instrument-specific installer):

- [ ] No existing `%USERPROFILE%\STAN\` directory, OR you are deliberately reinstalling and understand you may overwrite config.
- [ ] No cached `install-stan.bat` or `install_stan.ps1` files sitting in the installer folder from a previous run. Delete them if present — the installer will download fresh copies.
- [ ] The instrument PC has outbound internet access on port 443 (HTTPS). The installer downloads from `github.com`, `python.org`, `pypi.org`, and `github.com/lazear/sage`.
- [ ] PowerShell 5.1 is available (default on Windows 10/11). Run `powershell -Command $PSVersionTable.PSVersion` in CMD to confirm Major is 5.
- [ ] The Y: drive is mapped to `\\proteomics-grp` (the Quobyte share). The SSH key auto-install in Step 2 of `install_stan_lumosrox.bat` reads from `Y:\STAN\temp_keys\lumosRox\`.
- [ ] You are running the installer as the normal instrument user account, NOT as Administrator. STAN installs into the user profile, not system directories.

---

## 3. During install — what to watch for

### "Missing closing }" / garbled characters at line ~406
**What it means**: Bug #1 has returned. The PS1 file contains non-ASCII characters (em-dashes or similar) that PowerShell 5.1 misreads when the file encoding is wrong.

**What to do**: Do not retry. Note the exact error line number, report it as a GitHub issue with the output pasted. Delete `install_stan.ps1` from the installer folder before retrying.

### Step 7 hangs silently for more than 30 seconds
**What it means**: Bug #3 has returned. The installer is running `stan init` with its output suppressed, and `stan init` is waiting for keyboard input that it will never receive.

**What to do**: Press Ctrl+C to kill the installer. Check the STAN version (`stan --version` in a new CMD window if stan.exe already exists). If the version is 0.2.350 or older, the bug is not yet fixed in that build. Download the latest `install-stan.bat` directly from GitHub and run it manually.

### "DIA-NN already installed: 1.x" printed in Step 4 or Step 6
**What it means**: Bug #4 has returned. The version picker chose an older DIA-NN installation by alphabetical order instead of by version number.

**What to do**: Let the installer finish. Then check `%USERPROFILE%\STAN\instruments.yml` (or `%USERPROFILE%\.stan\instruments.yml`) for the `diann_binary:` line. If it points to a 1.x path, manually edit it to point to the 2.x binary (usually under `C:\DIA-NN\2.3.2\`). Report as a GitHub issue.

### Install completes but `stan watch` immediately exits or shows 0 active watchers
**What it means**: Either the `instruments.yml` was written to the wrong path (bug #5) or `watch_dir` points to a directory that does not exist.

**What to do**: See the post-install verification checklist below, especially questions 2 and 4.

---

## 4. Post-install verification (10-question checklist)

Run these checks in a **new** CMD window (not the one the installer used) so that the updated PATH is loaded.

- [ ] **Q1. Does `stan --version` work?**
  Open CMD, type `stan --version`. Expected: a version number like `0.2.354`. If CMD says `stan is not recognized`, the venv Scripts folder is not on PATH. Open a new CMD window and try again. If it still fails, check that `%USERPROFILE%\STAN\venv\Scripts` appears in your user PATH (`sysdm.cpl` > Advanced > Environment Variables).

- [ ] **Q2. Is `instruments.yml` in the right place?**
  Run `dir "%USERPROFILE%\STAN\instruments.yml"`. It should exist. Then run `dir "%USERPROFILE%\.stan\instruments.yml"` — this path should NOT exist (or if it does, it should be empty / a stub). If the file is only at `%USERPROFILE%\.stan\` and NOT at `%USERPROFILE%\STAN\`, you have hit bug #5. Edit the file at the old path and copy it to the new path, or reinstall.

- [ ] **Q3. Does `dir "C:\DIA-NN\"` show a 2.x folder?**
  Run `dir "C:\DIA-NN"` in CMD. You should see a subfolder with `2.` in its name (e.g., `2.3.2`). If the only folder is `1.8.1` or similar, DIA-NN 2.x was not installed. Re-run the installer or install DIA-NN 2.3+ manually from https://github.com/vdemichev/DiaNN/releases.

- [ ] **Q4. Does `stan watch` start without errors?**
  Run `stan watch`. Expected output includes `Active watchers: 1` and the watch directory path. If you see `Active watchers: 0`, open `%USERPROFILE%\STAN\instruments.yml` and confirm the `watch_dir:` path exists on disk (e.g., `E:\Data`). If it does not exist, create it or update the YAML.

- [ ] **Q5. Does `http://localhost:8421` load in the browser?**
  Open Chrome, navigate to `http://localhost:8421`. The STAN dashboard should load. If it does not, run `stan dashboard` in a separate CMD window and try again.

- [ ] **Q6. Does the DIA-NN binary path in `instruments.yml` point to 2.x?**
  Open `%USERPROFILE%\STAN\instruments.yml` in Notepad. Find the line starting with `diann_binary:`. Confirm the path contains `2.` (e.g., `C:/DIA-NN/2.3.2/DiaNN.exe`). A path containing `1.` means bug #4 affected this install.

- [ ] **Q7. Does `sage --version` work in CMD?**
  Run `sage --version`. If it says `sage is not recognized`, the Sage directory is not on PATH. Find `sage.exe` under `%USERPROFILE%\STAN\tools\sage\` and add its parent folder to your user PATH, or reinstall.

- [ ] **Q8. Can you drop a `.raw` file into the watch directory and see it picked up?**
  Copy any `.raw` file into `E:\Data` (or whatever `watch_dir` is set to). Within 30-60 seconds the STAN watcher log should print a line about the file being detected. If nothing happens after 2 minutes, check that `stan watch` is still running and that the file is in the exact directory specified in `instruments.yml`.

- [ ] **Q9. Is the Y: drive mapped and the SSH key present?**
  Run `dir "Y:\STAN\incoming\lumosRox"` in CMD. If this fails with "path not found", the Quobyte share is not mounted. Map `Y:` to `\\proteomics-grp` in Windows Explorer (Map Network Drive). Then check that `%USERPROFILE%\.ssh\id_ed25519` exists — if not, Brett needs to drop the key at `Y:\STAN\temp_keys\lumosRox\id_ed25519` and you re-run `install_stan_lumosrox.bat`.

- [ ] **Q10. Does `stan dashboard --help` and `stan watch --help` each exit cleanly?**
  Run both commands. Each should print a help message and return to the prompt without errors. If either hangs or crashes, run `scripts\test_fresh_install.bat --report-only` to collect a full diagnostic report.

---

## 5. Known regression failure modes

| Symptom | Likely bug | Commit that fixed it | How to confirm |
|---------|-----------|----------------------|---------------|
| `Missing closing }` error at install start, aborts immediately | Bug #1: em-dash mojibake in PS1 on PS5.1 | 4da0f4a | The error message will mention a line number near 406 |
| Install appears to run but installs an old STAN version despite a fix being shipped | Bug #2: stale cached `install-stan.bat` | 9a18279 | Compare `stan --version` output to the latest GitHub release tag |
| Step 7 hangs for minutes with no output | Bug #3: `stan init` piped to `Out-Null` | 96236b3 | Ctrl+C to kill; check STAN version |
| `diann_binary` in `instruments.yml` points to a 1.x path | Bug #4: alphabetical DIA-NN picker | 96236b3 | Open `instruments.yml`, check `diann_binary:` line |
| Hive mode never activates, watcher runs locally | Bug #5: `instruments.yml` written to `~/.stan/` instead of `~/STAN/` | c5bf09a | Check both paths; file should be at `%USERPROFILE%\STAN\instruments.yml` |
| Re-install takes 3+ extra minutes; watcher config is overwritten | Bug #6: base install not skipped when already installed | 96236b3 | Watch for "Installing STAN" step running even when `stan.exe` already exists |
| `stan watch` shows 0 active watchers | `watch_dir` in `instruments.yml` does not exist on disk | n/a | Create the directory or correct the path in the YAML |
| Community benchmark submissions rejected | DIA-NN 1.x in use; benchmark requires 2.3+ | bug #4 | See Q6 in checklist above |

---

## 6. If a regression appears

1. **Capture the full output** — right-click the CMD window title bar, click Edit > Select All, then Edit > Copy. Paste into a text file.
2. **Note the STAN version** — run `stan --version` and include the output.
3. **Run the automated diagnostic**: open CMD in the STAN install folder and run:
   ```
   scripts\test_fresh_install.bat --report-only
   ```
   This runs all 10 checks without touching your existing install and prints a PASS/FAIL summary. Paste the output into the GitHub issue.
4. **File a GitHub issue** at https://github.com/bsphinney/stan/issues with:
   - The full CMD output from step 1
   - The STAN version from step 2
   - The `test_fresh_install.bat --report-only` output from step 3
   - Windows version (`winver` in Run dialog)
   - Whether the machine had a previous STAN install

Brett monitors GitHub issues and will push a patch version within one business day for install-blocking bugs.
