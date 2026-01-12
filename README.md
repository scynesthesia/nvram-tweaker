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
- **Safety prompts:** risk keywords open a modal dialog before applying changes.
- **Inline logging:** the console panel captures load, change, skip, and save events. Saving writes a `.bak` backup and updates the loaded file in place.

The window is resizable, uses a dark theme, and remembers the current selection while you filter or browse. Start by opening an NVRAM file, pick one or more questions, adjust the value/option, and click **Save changes** when you are satisfied.

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

- Risky keywords (such as XHCI or ASPM) trigger an extra confirmation before any change is applied.
- Hex input is automatically converted to decimal; the summary prints the conversion for clarity.
- `Min =` / `Max =` ranges are enforced when present to prevent out-of-range writes.
- Use `--dry-run` to preview changes without writing to disk.
- CRC handling defaults to restoring the original `HIICrc32` header. Bypass modes that remove or empty the CRC are flagged as unsafe and are blocked when a CRC marker is detected unless you explicitly force the change. Prefer the placeholder mode if you must bypass CRCs.

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
