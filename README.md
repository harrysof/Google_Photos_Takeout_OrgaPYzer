# Google Photos Fixer 📸🔧

A GUI tool for fixing your Google Takeout mess — sort matched files into folders, stamp correct dates from JSON metadata, and rename everything to a clean `YYYYMMDD_HHMMSS` format. No command line needed.

<img width="1079" height="890" alt="Screenshot" src="https://github.com/user-attachments/assets/5eae933b-4968-4784-b7f9-5c532aafa538" />

---

## Why does this exist?

Mad respect to [TheLastGimbus](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper) and his **GooglePhotosTakeoutHelper** — a brilliant project with 5.5k stars that I've personally used and relied on. Seriously, go star it.

This script is something different though — built from the ground up with a specific workflow in mind: a point-and-click GUI that walks you through the process step by step, shows you a full preview before touching anything, and keeps your matched and unmatched files clearly separated so you always know where things stand.

---

## What it does

Google Takeout dumps your photos and their `.json` sidecar files in a chaotic structure. This tool fixes that in four steps:

```
Source folder (your photos)   +   JSON folder (your sidecars)
              │
              ▼
    1. Search  — find which media files have a matching JSON
              │
              ▼
    2. Organise — MOVE files into two clean output folders:
         source/found/         ← matched media + their JSONs
         source/missing_json/  ← media with no JSON found
              │
              ▼
    3. Apply Dates — read each JSON in found/ and stamp the
                     correct original photo date onto the file
              │
              ▼
    4. Smart Rename — rename files in found/ to YYYYMMDD_HHMMSS.ext
```

Every step has a **Preview → Confirm** flow. Nothing gets moved, renamed, or touched until you explicitly confirm it.

---

## Features

- 🖱️ **Pure GUI** — no terminal, no arguments, just browse and click
- 👁️ **Preview before you commit** — every step shows exactly what will happen first
- 📁 **Clean separation** — matched files go to `found/`, unmatched to `missing_json/`
- 🕐 **Correct timestamps** — reads the Unix epoch `timestamp` field from JSON (locale-independent, works on non-English Takeout exports)
- 🏷️ **Smart renaming** — detects EXIF date, falls back to file timestamps; handles collisions with `_1`, `_2`, etc.
- 🪟 **Windoza creation-time fix** — on Windows, also sets the file creation date (not just modified date) using the Win32 API
- 🍎🐧 **Cross-platform** — works on Windoza, macOS, and Linux; Windows-only code is properly guarded
- 📊 **Progress bar** — tracks progress across all long-running steps
- 📝 **Export log** — save the full results log to a `.txt` file

---

## Requirements

- Python 3.10+
- [Pillow](https://pypi.org/project/Pillow/) *(optional — enables EXIF date reading from images)*

```bash
pip install pillow
```

Tkinter comes with standard Python on most systems. If it's missing on Linux:

```bash
# Debian/Ubuntu
sudo apt install python3-tk

# Arch
sudo pacman -S tk
```

---

## How to use

### 1. Get your Google Takeout

Go to [takeout.google.com](https://takeout.google.com), deselect everything, and select only **Google Photos**. Download and unzip all the parts, merging them into one folder if there are multiple.

### 2. Run the script

```bash
python Google_Photos_Fixer.py
```

### 3. Follow the four steps in order

| Step | What it does |
|------|-------------|
| **1. Search Matches** | Scans your Source folder for media files and finds their matching `.json` sidecars in the JSON folder |
| **2. Preview Organise → Confirm** | Moves matched files + their JSONs into `found/`, unmatched files into `missing_json/` |
| **3. Preview Dates → Confirm** | Stamps the correct original photo timestamp on each file in `found/` using its JSON |
| **4. Preview Rename → Confirm** | Renames files in `found/` to `YYYYMMDD_HHMMSS.ext` format |

> **Tip:** Your Source folder and JSON folder can be the same folder — that's fine. The script will figure it out.

---

## Output structure

After running, inside your Source folder you'll find:

```
source/
├── found/
│   ├── IMG_1234.jpg
│   ├── IMG_1234.jpg.json
│   ├── VID_5678.mp4
│   └── VID_5678.mp4.json
│       ...
└── missing_json/
    ├── IMG_9999.jpg
    └── screenshot_whatever.png
        ...
```

After Apply Dates + Smart Rename, `found/` will look like:

```
found/
├── 20220105_143022.jpg
├── 20220105_143022.json
├── 20220106_091500.mp4
└── 20220106_091500.json
    ...
```

---

## Notes

- Files are **moved**, not copied. Keep your original Takeout zips until you're happy with the result.
- Re-running Search after already organising is safe — `found/` and `missing_json/` are excluded from re-scanning.
- The `missing_json/` files won't have their dates fixed automatically (there's no JSON to read from), but you can still run Smart Rename on them manually by pointing the Source folder directly at `missing_json/`.

---

## Acknowledgements

Again, huge thanks to [**@TheLastGimbus**](https://github.com/TheLastGimbus) for [GooglePhotosTakeoutHelper](https://github.com/TheLastGimbus/GooglePhotosTakeoutHelper). If you want a battle-tested, fully-featured CLI/interactive tool that handles way more edge cases, go use that. This script scratches a different itch.

---

## License

MIT — do whatever you want with it.
