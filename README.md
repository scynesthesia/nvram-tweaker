# NVRAM Tweaker

NVRAM Tweaker is a Python utility for exploring and updating AMISCE-style BIOS *Setup Question* blocks. It supports precise command-line edits and a Dear PyGui-powered desktop interface so you can browse questions, move the selected option, or adjust numeric values with confidence.

## Downloads

- Download the prebuilt Windows executable from Releases (**NVRAM_Tweaker_v1**) for the recommended GUI workflow.

## Requirements (source builds)

- Python 3.9+.
- Optional: `dearpygui` for the GUI (`pip install dearpygui`).
- No third-party dependencies for the CLI.

## Quick start (GUI)

The Windows `.exe` is the easiest path. If you are running from source, install Dear PyGui and launch the interface:

```bash
pip install dearpygui
python nvram_gui.py
```

Key capabilities:

- **Searchable question list** with token visibility and multi-selection (use *Select All Filtered* to build a batch).
- **Context-aware controls:** radio buttons for option lists, numeric inputs for `Value =` blocks, and displayed help strings for each question.
- **Batch edits:** apply the same option substring or numeric value across multiple selected questions with per-block compatibility checks.
- **Safety prompts:** questions whose names match risk keywords (USB/XHCI, PCIe/ASPM, CSM, Secure Boot, TPM, voltages, power limits, SATA/NVMe, ...) open a confirmation dialog before the change is applied. The list lives in `nvram_safety.py`.
- **Read and write the BIOS directly** through SCEWIN (see below): export, edit, import, and one-click restore.
- **Inline logging:** the console panel captures load, change, skip, and save events. Saving writes a `.bak` backup and updates the loaded file in place.

The window is resizable, uses a dark theme, and remembers the current selection while you filter or browse. Start by opening an NVRAM file (or exporting one from the BIOS), pick one or more questions, adjust the value/option, and click **Save changes** when you are satisfied.

## Reading and writing the BIOS with SCEWIN

nvram-tweaker can drive AMI's SCEWIN to get the NVRAM file from your machine and write your edits back. It does **not** ship SCEWIN. Get the `SCEWIN` folder (`SCEWIN_64.exe`, `amifldrv64.sys`, `amigendrv64.sys`) with [SCEHUB](https://github.com/ab3lkaizen/SCEHUB)'s `DL_SCEWIN.exe`, which extracts it from MSI Center. SCEWIN only works on Windows with an AMI BIOS and must run as administrator, so start nvram-tweaker as administrator.

1. **Export from BIOS** saves the current settings (e.g. `nvram.txt`) and loads them in the editor. The first export also saves an untouched copy, `nvram.original.txt`: your **restore point**.
2. Edit and **Save changes** as usual.
3. **Import to BIOS** runs preflight checks (the file must round-trip through the parser and every option question needs exactly one `*`), shows every setting that differs from the restore point, and asks for an extra acknowledgement if any of them are risky. Reboot to apply.
4. **Restore original** re-imports the restore point and puts every setting back.

SCEWIN's output is saved to `log-file.txt` next to the script and explained in the console (driver not loaded, HII not published, write-protected variables, and so on).

The same workflow from the command line:

```bash
python scewin_runner.py export  --scewin path\to\SCEWIN --file nvram.txt
python scewin_runner.py import  --scewin path\to\SCEWIN --file nvram.txt
python scewin_runner.py restore --scewin path\to\SCEWIN --file nvram.txt
python scewin_runner.py diagnose log-file.txt   # explain an existing SCEWIN log (works on any OS)
```

If `--scewin` is omitted, the current folder and the app folder (and a `SCEWIN/<version>/` subfolder in either) are searched.

## Quick start (CLI, advanced)

1. Make a copy of your NVRAM dump for safekeeping (the tool also creates a `.bak` backup alongside the file you edit).
2. Decide whether you are updating an **option** (move the `*` to a different choice) or a numeric **value** (the `Value = <...>` line) and run the corresponding command below.
3. Review the printed before/after summary. The tool asks for confirmation before writing changes.

```bash
# Move the selected option to "Disabled"
python nvram_editor.py nvram.txt "Fast Boot" "Disabled" --mode option

# Set a numeric value (decimal, signed, or 0x-prefixed hexadecimal)
python nvram_editor.py nvram.txt "Power Limit" 0x7d --mode value
```

### Targeting the right block

- **Partial matches by default:** The `query` argument matches any question name containing the text. Add `--exact` to require an exact match.
- **Token disambiguation:** Use `--token` when multiple questions share the same name (common for PCIe ports). Example: `--token 0x1234`.
- **Batch updates:** Add `--all` to apply the change to every matching block. Otherwise you will be prompted to choose which matches to modify when more than one is found.

### Safety and validation

- Risky keywords (such as XHCI or ASPM) require typing `YES` before any change is applied.
- Hex input is automatically converted to decimal; the summary prints the conversion for clarity.
- `Min =` / `Max =` ranges are enforced when present to prevent out-of-range writes.
- Use `--dry-run` to preview changes without writing to disk.
- CRC handling defaults to restoring the original `HIICrc32` header. Every bypass mode (empty, remove, placeholder) writes something other than the original checksum, so all of them are flagged as unsafe and blocked when a CRC marker is detected unless you explicitly force the change.

### What gets written

- A `.bak` file with the original contents is saved next to the edited file before any modifications are written.
- Line endings are preserved (Windows or Unix) so the file structure remains consistent.
- HIICrc32 headers are left untouched; if your firmware loader enforces CRC checks you may need to force import the updated file.

### GUI smoke test checklist (manual)

Use this quick pass to ensure the Dear PyGui interface remains responsive when working with files:

1. Launch `python nvram_gui.py`.
2. Click **Open NVRAM file** and pick any text file (dialog should appear without freezing the UI).
3. After the file loads, click **Save changes** and cancel the confirmation dialog to confirm the UI stays responsive.
4. Close and reopen the **Open NVRAM file** dialog a second time to verify it reopens cleanly without zombie windows or hanging threads.

### Layout behavior and visual check

- The sidebar scales with the viewport (roughly one-third of the width) and clamps between **320px** and **520px** so it remains usable on ultrawide and compact screens alike.
- Question titles, tokens, and help text wrap to a dynamic width (about 70% of the viewport) with a minimum of **420px** to avoid clipping on low resolutions.
- Child panes include horizontal scrollbars when space is tight, and panel heights adapt to smaller windows while preserving room for the console.

Quick visual regression sweep:

1. Launch `python nvram_gui.py`.
2. Shrink the window to ~1280×720: verify the question list gains a scrollbar, text wraps cleanly, and controls remain readable.
3. Expand to ~1920×1080: confirm the sidebar grows to its cap (around 520px) and detail text uses the extra width without overflowing.
