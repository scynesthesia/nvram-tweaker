# NVRAM Tweaker

Browse, edit and safely write back the hidden BIOS settings of AMI-based boards. NVRAM Tweaker works on the text files that AMI's **SCEWIN / AMISCE** exports (`nvram.txt`), and can drive SCEWIN itself to read your settings from the BIOS and write them back. It comes with a desktop GUI and a command-line tool for scripts and technical users.

> **Status: v2 is in development.** The file handling is tested against a real SCEWIN export. Writing to a real BIOS through SCEWIN has **not** been verified on hardware yet; see [What is and is not verified](#what-is-and-is-not-verified). Changing BIOS settings can leave a machine unable to boot. You need a way to clear CMOS before you try anything.

## Downloads

Download `NVRAM_Tweaker.exe` from [Releases](https://github.com/scynesthesia/nvram-tweaker/releases). Each release is built from its tagged commit by the [build workflow](.github/workflows/build.yml) and comes with `SHA256SUMS.txt`. Check your download in PowerShell before running it:

```powershell
(Get-FileHash .\NVRAM_Tweaker.exe -Algorithm SHA256).Hash.ToLower()   # must match SHA256SUMS.txt
```

The exe is not code-signed, so Windows SmartScreen may warn about it.

## What keeps your settings safe

| Protection | What it does |
|---|---|
| **Byte-exact saves** | Every line you did not edit is written back unchanged, including SCEWIN's odd `\r\r\n` line endings and non-UTF-8 bytes (such as `85°C`). A save with no edits leaves the file identical. |
| **Atomic writes** | The file is replaced in one step, so a crash cannot leave a half-written export. |
| **Four copies** | `nvram.txt.bak` is the version before the last save **or export** (an export overwrites the file, so your hand-edited script is kept here first); `nvram.original.txt` is the untouched file from before your first save and is never overwritten; `nvram.preimport.txt` is the BIOS as it was just before your latest import (your undo for it); SCEWIN's own output goes to `log-file.txt`. |
| **`HIICrc32` header is never modified** | It is a checksum of the BIOS's setup database, not of the text, so editing settings never makes it stale. SCEWIN uses it to recognise scripts that belong to the running BIOS. An import is blocked when the header differs from your original export (a script from another BIOS version). |
| **Value limits** | Numbers must fit the question: the `Width` line is the storage size in bytes (`Width=01` → 0–255, `Width=02` → 0–65535). An explicit `Min`/`Max`, when a file has one, wins. The help text sometimes states a tighter range (for example `[0-800]`); the tool only enforces what the file defines, so read the help. |
| **Inactive questions are read-only** | SCEWIN comments out questions that do not apply to your board (often a twin of a live question with the same name). They are kept in the file but cannot be selected or edited. |
| **Risk levels** | See below. |
| **Import checks** | Before writing to the BIOS the file is compared with a fresh read of the live BIOS (`nvram.preimport.txt`): exactly one `*` per option question, same `HIICrc32` header, no question missing (a truncated or damaged file), and **no line changed other than the selected option or a number**. An edit to `Offset`, `Width`, `Token`, an option list, the help text, a string, or an un-commented question blocks the import. Commenting a question out with `//` is the accepted way to skip a setting and is allowed. An import with nothing to compare against is refused. |
| **Change list** | Before an import or a save you see exactly which settings change, from what to what, against what the BIOS holds right now. |

**Risk levels.** Questions whose names contain certain words are treated in two tiers (the lists are in [`nvram_safety.py`](nvram_safety.py)):

- **Confirm**: `XHCI`, `CSM`, `Secure Boot`, `TPM`/`fTPM`/`PTT`, `iGPU`/`IGD`, `Primary Display`/`Primary Video`, `Voltage`/`VCore`, power limits (`Power Limit`, `PL1`, `PL2`, `IccMax`, `Current Limit`), `BIOS Lock`, `Flash Protection`, `Erase`. The GUI shows a confirmation dialog and the CLI needs you to type `YES` (or pass `--allow-risky`).
- **Note**: `USB`, `SATA`, `PCIe`, `ASPM`, `NVMe`, `VMD`, `Fast Boot`, `Watchdog`. Marked `[note: ...]` in the change list, with no extra prompt. These words appear in a quarter of all question names, so asking about each would only teach people to click through.

On one real export (1,819 active questions) the confirm tier covers 168 questions (9%).

## Quick start (GUI)

Run the `.exe`, or from source (Python 3.10 or newer):

```bash
pip install dearpygui
python nvram_gui.py
```

- **Open NVRAM file** loads an export; the list shows each question with its token, with a search box and *Select All Filtered* for batch edits.
- Pick a question: option questions get radio buttons; numeric ones get an input that shows the allowed range.
- **Save changes** shows a before/after list and writes the file. Closing the window or opening another file with unsaved edits asks first.
- The **SCEWIN** section reads and writes the BIOS (below).

## Command line (technical users and scripts)

```text
nvram_tweaker_cli.py list    FILE [QUERY]          what is in an export (--json for scripts)
nvram_tweaker_cli.py show    FILE QUERY            one question: range, options, help
nvram_tweaker_cli.py set     FILE QUERY VALUE      change a setting in the file
nvram_tweaker_cli.py export | import | restore     read / write the BIOS through SCEWIN
nvram_tweaker_cli.py diagnose LOG                  explain a SCEWIN log (any OS)
nvram_tweaker_cli.py --version
```

```bash
python nvram_tweaker_cli.py list nvram.txt "fan"
python nvram_tweaker_cli.py show nvram.txt "Fan Start" --token 2B
python nvram_tweaker_cli.py set  nvram.txt "Fan Start" 45 --dry-run     # preview, writes nothing
python nvram_tweaker_cli.py set  nvram.txt "ASPM Mode" "Disabled" --yes
python nvram_tweaker_cli.py set  nvram.txt "XHCI" "Enabled" --yes --allow-risky
```

**Matching.** `QUERY` matches any part of a question name (case-insensitive); `--exact` requires the whole name. `Boot Option #1` therefore also matches `#10` to `#13`; use `--exact`. `--token` takes the hexadecimal token printed in the export; `7D1`, `0x7d1` and `007D1` are the same. When several questions match, an interactive run asks which; a script must narrow it with `--token`/`--exact` or pass `--all` (it is never guessed).

**What `set` does.** The mode is chosen per question: a number for numeric questions, part of an option's text (`Disabled`) for the others (`--mode value|option` forces it). Numbers are decimal, or hexadecimal with `0x`. `set` edits the *file*; use `import` to write it to the BIOS.

**Scripting rules.** Nothing is written without confirmation: an interactive run asks; a script must pass `--yes`, and changes to confirm-level settings also need `--allow-risky` (`--yes` alone never covers them). `--dry-run` prints the change list and asks nothing. Exit codes: `0` success, `1` error or cancelled, `2` ambiguous match or bad usage.

`nvram_editor.py FILE QUERY VALUE --mode option|value` (the original command) still works and forwards to `set`.

### Expert mode

If you know what you are doing and the confirm-level prompts only slow you down, turn them off. It is **off by default** and needs an acknowledgement ("this can brick my machine and I know how to clear CMOS").

```bash
python nvram_tweaker_cli.py config set expert true     # asks you to type: I UNDERSTAND
python nvram_tweaker_cli.py config set expert true --i-understand   # same, without the question
python nvram_tweaker_cli.py config set expert false
python nvram_tweaker_cli.py config show                # state and settings file location
```

In the GUI it is the *Expert mode (skip risk prompts)* checkbox in the sidebar; the window title shows `[EXPERT MODE]` while it is on, and the CLI prints a one-line banner on `set`, `import` and `restore`.

| Expert mode removes | Expert mode keeps |
|---|---|
| the CLI's typed `YES` for confirm-level settings | the change list with its `[RISK: ...]` tags |
| the GUI's risk dialog when you pick a risky option | the integrity checks: one `*` per option, same `HIICrc32`, no missing questions, no edits outside the selected option or a number |
| the GUI's "I understand" tick box before an import | the snapshot of the live BIOS before an import (your undo) |
| | the final "write these changes now?" question |
| | `--allow-risky` for scripts: a stored setting never widens what an unattended run may write |

The setting lives in `settings.json` (`%APPDATA%\nvram-tweaker\` on Windows, `~/.config/nvram-tweaker/` elsewhere; override with the `NVRAM_TWEAKER_CONFIG` environment variable).

## Reading and writing the BIOS with SCEWIN

NVRAM Tweaker can drive AMI's SCEWIN to get the file from your machine and write your edits back. It does **not** ship SCEWIN. Get the `SCEWIN` folder (`SCEWIN_64.exe`, `amifldrv64.sys`, `amigendrv64.sys`) with [SCEHUB](https://github.com/ab3lkaizen/SCEHUB)'s `DL_SCEWIN.exe`, which extracts it from MSI Center. SCEWIN only works on Windows with an AMI BIOS and must run as administrator, so start NVRAM Tweaker as administrator.

1. **Export** saves the current settings (e.g. `nvram.txt`) and keeps the untouched copy `nvram.original.txt`: your **restore point**.
2. Edit and save the file (GUI or `set`).
3. **Import** first reads the live BIOS into `nvram.preimport.txt` (read-only), runs the import checks against it, lists every difference, and asks for confirmation (with an extra acknowledgement for confirm-level settings). Reboot to apply. If you export once more after a BIOS update, the old restore point is kept; delete it to start fresh. In the CLI, `import --no-snapshot` skips the live read and compares with the restore point instead.
4. **Restore** re-imports the restore point and puts every setting back.

SCEWIN's output is saved to `log-file.txt` and explained in plain language (driver not loaded, HII not published, write-protected variables, ...); the same explanations are available offline with `diagnose`.

```bash
python nvram_tweaker_cli.py export  --scewin C:\SCEWIN --file nvram.txt
python nvram_tweaker_cli.py import  --scewin C:\SCEWIN --file nvram.txt
python nvram_tweaker_cli.py restore --scewin C:\SCEWIN --file nvram.txt
```

Without `--scewin` the current folder and the app folder (and a `SCEWIN/<version>/` subfolder of either) are searched.

## About the file format

Notes from real exports (AMISCE 5.05.01.0002), useful if you write your own tooling:

- Lines end in `\r\n`, but a few header lines end in `\r\r\n`. The `HIICrc32=` line is on line 6, after a `//` comment block. Files can contain non-UTF-8 bytes (`85°C`), so they are read and written as latin-1 when UTF-8 fails.
- A numeric setting is `Value =<30>` or the bare `Value =0`; both occur. `Width =01` is its size in bytes. Real exports contain no `Min`/`Max` lines.
- `Setup Question = ...` headers may contain `/`, `#`, `;` and `()` as part of the name (`PCIe/GFX Lanes Configuration`, `Boot Option #1`); only whitespace followed by `//` starts a comment.
- Tokens are hexadecimal without a prefix and are not unique across the file, and neither are names (one real export has 290 distinct names that appear more than once).

## Development

```bash
python -m pip install --require-hashes -r requirements-build.txt   # exact versions, SHA-256 hashes
python -m pytest -q
```

- Python 3.10 or newer. The locked environment was tested on 3.10, 3.12 and 3.14 on Linux; CI runs 3.10 and 3.12 on Windows and Linux.
- GUI tests run headless and skip themselves when `dearpygui` is missing. `NVRAM_REAL_EXPORT=path/to/nvram.txt` additionally runs whole-file checks on a full export (never in CI; it works on a temporary copy). `tests/fixtures/real_export_trimmed.txt` is a byte-exact 19-question excerpt of a real export; `.gitattributes` keeps git from rewriting its line endings.
- `requirements-build.txt` is generated from `requirements-build.in`; see the command at the top of that file to upgrade.

**Build the exe yourself** (what CI does, on Windows):

```bash
python -m pip install --require-hashes -r requirements-build.txt
python scripts/fetch_fonts.py        # downloads DejaVu fonts, verified by SHA-256
python -m PyInstaller --clean --noconfirm nvram_tweaker.spec
```

**Release:** set `__version__` in [`nvram_version.py`](nvram_version.py), commit, then push a tag `v<version>`. The workflow refuses to build if the tag and the version disagree, then tests, builds and publishes the exe with its checksum.

## What is and is not verified

Verified by automated tests (all passing; Linux, Python 3.10 / 3.12 / 3.14):

- Parsing, editing and saving a real SCEWIN export, including byte-identical saves with no edits and edits that change only the intended lines.
- Value limits, inactive questions, risk tiers, the import checks, the CLI and the GUI flows (against a fake SCEWIN).

**Not verified yet**

- Writing to a real BIOS with SCEWIN (export → edit → import → reboot → restore). Nothing in this project has done that yet.
- Windows runs: the CI matrix covers them on every pull request, but the results are not in yet.
- SCEWIN's exit codes: success is currently judged from its output messages.
- The GUI freezes while SCEWIN runs (it runs on the main thread).

Found a mismatch with your board? Please open an issue with the relevant lines of your export (remove anything personal first; boot-option names can contain your disk models).

## License

MIT, see [LICENSE](LICENSE). Dear PyGui and the bundled DejaVu fonts keep their own licenses (fonts: [`fonts/DejaVu-LICENSE.txt`](fonts/DejaVu-LICENSE.txt) after running `scripts/fetch_fonts.py`).
