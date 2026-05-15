"""
Google Photos Fixer
====================
Workflow
--------
1. Search   — scan Source for media files; match each against a JSON sidecar
              in the JSON folder.
2. Organise — MOVE matched media + their JSONs  →  <source>/found/
              MOVE unmatched media              →  <source>/missing_json/
3. Apply Dates — read each JSON in found/ and stamp the correct timestamp
                 onto its media file.
4. Smart Rename — rename every media file in found/ to YYYYMMDD_HHMMSS.<ext>.

Steps 3 & 4 always operate on <source>/found/.
"""

import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext, messagebox
from pathlib import Path
import shutil
import threading
import json
import os
import re
from datetime import datetime

# --- Windows-only imports (guarded) ---
if os.name == "nt":
    from ctypes import windll, wintypes, byref
    _WINDOWS = True
else:
    _WINDOWS = False

# --- Optional Pillow import for EXIF data ---
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
NAMED_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})', re.ASCII)


def parse_dt_from_name(filename: str):
    """Return a datetime if the filename starts with YYYYMMDD_HHMMSS, else None."""
    m = NAMED_RE.match(Path(filename).stem)
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]),
                        int(m[4]), int(m[5]), int(m[6]))
    except ValueError:
        return None


def exif_datetime(filepath):
    """Return the earliest EXIF datetime from an image, or None."""
    if not PILLOW_OK:
        return None
    try:
        img = Image.open(filepath)
        exif_data = img._getexif()
        if not exif_data:
            return None
        tag_map = {TAGS.get(k, k): v for k, v in exif_data.items()}
        candidates = []
        for tag in ('DateTimeOriginal', 'DateTimeDigitized', 'DateTime'):
            val = tag_map.get(tag)
            if val:
                try:
                    candidates.append(datetime.strptime(val, '%Y:%m:%d %H:%M:%S'))
                except ValueError:
                    pass
        return min(candidates) if candidates else None
    except Exception:
        return None


def earliest_datetime(filepath):
    """
    Return the earliest plausible datetime for a file.
    Uses EXIF (if available) and mtime.
    On Windows also considers ctime (genuine creation time).
    On Linux/macOS ctime is inode-change time, so it is excluded there.
    """
    candidates = []
    ex = exif_datetime(filepath)
    if ex:
        candidates.append(ex)
    stat = os.stat(filepath)
    candidates.append(datetime.fromtimestamp(stat.st_mtime))
    if _WINDOWS:
        candidates.append(datetime.fromtimestamp(stat.st_ctime))
    return min(candidates)


def build_target_name(filepath, taken: set) -> str:
    """
    Build a YYYYMMDD_HHMMSS<ext> name not already in `taken`.
    Appends _1, _2, … to resolve collisions.
    """
    dt = earliest_datetime(filepath)
    base = dt.strftime('%Y%m%d_%H%M%S')
    ext = Path(filepath).suffix.lower()
    candidate = base + ext
    if candidate not in taken:
        return candidate
    counter = 1
    while True:
        candidate = f"{base}_{counter}{ext}"
        if candidate not in taken:
            return candidate
        counter += 1


def set_file_timestamps(filepath, dt: datetime):
    """Set atime/mtime (and on Windows, ctime) to dt."""
    ts = dt.timestamp()
    os.utime(filepath, (ts, ts))
    if _WINDOWS:
        try:
            GENERIC_WRITE    = 0x40000000
            FILE_SHARE_WRITE = 0x2
            OPEN_EXISTING    = 3
            FILE_ATTR_NORMAL = 0x80
            handle = windll.kernel32.CreateFileW(
                str(filepath), GENERIC_WRITE, FILE_SHARE_WRITE,
                None, OPEN_EXISTING, FILE_ATTR_NORMAL, None
            )
            if handle != wintypes.HANDLE(-1).value:
                win_ts = int((ts + 11644473600) * 10_000_000)
                ft = wintypes.FILETIME(win_ts & 0xFFFFFFFF, win_ts >> 32)
                windll.kernel32.SetFileTime(handle, byref(ft), byref(ft), byref(ft))
                windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def ts_matches(filepath, dt: datetime, tolerance_sec: int = 2) -> bool:
    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
    return abs((mtime - dt).total_seconds()) <= tolerance_sec


def parse_photo_taken_time(data: dict):
    """
    Extract photo-taken datetime from a Google Takeout JSON blob.
    Prefers the Unix epoch `timestamp` field (locale-independent).
    Falls back to the human-readable `formatted` string.
    """
    ptt = data.get("photoTakenTime", {})

    ts_str = ptt.get("timestamp")
    if ts_str:
        try:
            return datetime.fromtimestamp(int(ts_str))
        except (ValueError, OSError):
            pass

    formatted = ptt.get("formatted")
    if formatted:
        for fmt in (
            "%b %d, %Y, %I:%M:%S %p %Z",
            "%d %b %Y, %H:%M:%S %Z",
        ):
            try:
                return datetime.strptime(formatted, fmt)
            except ValueError:
                continue

    return None


def safe_move(src: Path, dst: Path) -> Path:
    """
    Move src to dst, creating parent dirs as needed.
    If dst already exists, appends _1, _2, … before the extension.
    Returns the actual destination path used.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stem, ext = dst.stem, dst.suffix
        counter = 1
        while dst.exists():
            dst = dst.parent / f"{stem}_{counter}{ext}"
            counter += 1
    shutil.move(str(src), str(dst))
    return dst


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class JsonMatcherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Google Photos Fixer — Organise, Date Fix & Smart Rename")
        self.root.geometry("860x680")
        self.root.minsize(650, 500)

        self.source_dir = tk.StringVar()
        self.target_dir = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")

        # Results of the search step
        self.matched: list = []   # [(media_path, json_path), ...]
        self.missing: list = []   # [media_path, ...]  (no JSON found)

        # Pending actions for preview → confirm pattern
        self.pending_organise: list = []
        self.pending_dates:    list = []
        self.pending_rename:   list = []

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Source folder
        src_frame = ttk.LabelFrame(
            main_frame, text="Source Folder (Images / Videos)", padding="8")
        src_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Entry(src_frame, textvariable=self.source_dir, width=60).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(src_frame, text="Browse…",
                   command=self._browse_source).pack(side=tk.RIGHT)

        # JSON folder
        tgt_frame = ttk.LabelFrame(
            main_frame, text="JSON Folder (Where the .json sidecars live)", padding="8")
        tgt_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Entry(tgt_frame, textvariable=self.target_dir, width=60).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(tgt_frame, text="Browse…",
                   command=self._browse_target).pack(side=tk.RIGHT)

        # Action buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        self.search_btn = ttk.Button(
            btn_frame, text="1. Search Matches", command=self._start_search)
        self.search_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        self.organise_btn = ttk.Button(
            btn_frame, text="2. Preview Organise",
            command=self._preview_organise, state=tk.DISABLED)
        self.organise_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        self.date_btn = ttk.Button(
            btn_frame, text="3. Preview Dates",
            command=self._preview_dates, state=tk.DISABLED)
        self.date_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        self.rename_btn = ttk.Button(
            btn_frame, text="4. Preview Rename",
            command=self._preview_rename, state=tk.DISABLED)
        self.rename_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        self.export_btn = ttk.Button(
            btn_frame, text="Export Log", command=self._export_log)
        self.export_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Progress bar
        self.progress = ttk.Progressbar(main_frame, mode='determinate', maximum=100)
        self.progress.pack(fill=tk.X, pady=(0, 4))

        # Results log
        res_frame = ttk.LabelFrame(
            main_frame, text="Results (Preview before executing)", padding="8")
        res_frame.pack(fill=tk.BOTH, expand=True)

        self.results_text = scrolledtext.ScrolledText(
            res_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.results_text.pack(fill=tk.BOTH, expand=True)

        self.results_text.tag_config('match',   foreground="green")
        self.results_text.tag_config('missing', foreground="red")
        self.results_text.tag_config('error',   foreground="orange")
        self.results_text.tag_config('moved',   foreground="blue")
        self.results_text.tag_config('skipped', foreground="gray")
        self.results_text.tag_config('updated', foreground="dark violet")
        self.results_text.tag_config('renamed', foreground="teal")
        self.results_text.tag_config('preview', foreground="deep sky blue")
        self.results_text.tag_config('warn',    foreground="orange red")
        self.results_text.tag_config('default', foreground="")

        # Status bar
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor=tk.W, padding=(5, 2)).pack(
            fill=tk.X, side=tk.BOTTOM)

    # ------------------------------------------------------------------
    # Browse
    # ------------------------------------------------------------------
    def _browse_source(self):
        folder = filedialog.askdirectory(title="Select Source Folder (Images/Videos)")
        if folder:
            self.source_dir.set(folder)

    def _browse_target(self):
        folder = filedialog.askdirectory(title="Select JSON Folder")
        if folder:
            self.target_dir.set(folder)

    # ------------------------------------------------------------------
    # Export log
    # ------------------------------------------------------------------
    def _export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Save Log As")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.results_text.get("1.0", tk.END))
            self._update_status(f"Log exported to {path}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    # ------------------------------------------------------------------
    # STEP 1: SEARCH
    # ------------------------------------------------------------------
    def _start_search(self):
        src, tgt = self.source_dir.get(), self.target_dir.get()
        if not src or not tgt:
            self._update_status("Please select both Source and JSON folders.")
            return
        if not Path(src).is_dir() or not Path(tgt).is_dir():
            self._update_status("Invalid directory path(s) provided.")
            return

        self._disable_buttons()
        self.results_text.delete("1.0", tk.END)
        self.matched.clear()
        self.missing.clear()
        self._update_status("Searching…")
        self._set_progress(0)
        threading.Thread(target=self._run_search, daemon=True).start()

    def _run_search(self):
        src_path = Path(self.source_dir.get())
        tgt_path = Path(self.target_dir.get())

        # Index all JSON sidecars in the JSON folder
        self._update_status("Indexing JSON files…")
        target_media_map: dict[str, Path] = {}
        try:
            for json_file in tgt_path.rglob("*.json"):
                stem = json_file.name.lower()[:-5]       # strip ".json"
                last_dot = stem.rfind('.')
                base = stem[:last_dot] if last_dot > 0 else stem

                # Strip .supplemental-(N) suffix e.g. "photo.jpg.supplemental-(1)" -> "photo.jpg"
                base = re.sub(r'\.supplemental-\(\d+\)$', '', base)

                # Also register a version with (N) appended to match the image side
                # e.g. base "videocapture_20240614-223829.jpg" -> also map "(1)" variant
                m = re.search(r'-\((\d+)\)$', json_file.name.lower()[:-5].split('.supplemental')[0].rsplit('.', 1)[0] if '.supplemental' in json_file.name.lower() else '')
                numbered_base = None
                if '.supplemental-(' in json_file.name.lower():
                    n = re.search(r'supplemental-\((\d+)\)', json_file.name.lower())
                    if n:
                        # insert (N) before the extension in base
                        b_stem, _, b_ext = base.rpartition('.')
                        numbered_base = f"{b_stem}({n.group(1)}).{b_ext}" if b_stem else f"{base}({n.group(1)})"

                target_media_map[base] = json_file
                if numbered_base:
                    target_media_map[numbered_base] = json_file
        except Exception as e:
            self._append_result(f"[ERROR] Could not read JSON folder: {e}\n", 'error')
            self._schedule_enable_buttons()
            return

        # Match each media file against a JSON
        self._update_status("Matching files…")
        try:
            # Exclude files already inside found/ or missing_json/
            # so re-running search after organise doesn't re-process them.
            reserved = {src_path / "found", src_path / "missing_json"}
            src_files = [
                f for f in src_path.rglob("*")
                if f.is_file()
                and f.suffix.lower() != ".json"
                and not any(r in f.parents for r in reserved)
            ]
            total = len(src_files)

            for idx, src_file in enumerate(src_files, 1):
                self._set_progress(int(idx / total * 100) if total else 100)
                name_lower = src_file.name.lower()
                stem_lower = src_file.stem.lower()
                json_match = (target_media_map.get(name_lower)
                              or target_media_map.get(stem_lower))

                # Fallback: fuzzy prefix match for truncated filenames
                if not json_match:
                    for key, path in target_media_map.items():
                        min_len = min(len(stem_lower), len(key))
                        if min_len >= 20 and stem_lower[:min_len] == key[:min_len]:
                            json_match = path
                            break
                if json_match:
                    self._append_result(
                        f"[MATCH]    {src_file.name}  →  {json_match.name}\n", 'match')
                    self.matched.append((src_file, json_match))
                else:
                    self._append_result(
                        f"[NO JSON]  {src_file.name}\n", 'missing')
                    self.missing.append(src_file)

        except Exception as e:
            self._append_result(
                f"[ERROR] Could not read source folder: {e}\n", 'error')

        self._append_result(
            f"\n--- Search Complete ---\n"
            f"  With JSON : {len(self.matched)}\n"
            f"  No JSON   : {len(self.missing)}\n\n"
            f"Click '2. Preview Organise' to review the planned moves.\n", 'default')
        self._update_status(
            f"Search done — {len(self.matched)} matched, {len(self.missing)} unmatched.")

        self.root.after(0, lambda: self.organise_btn.config(
            text="2. Preview Organise", command=self._preview_organise))
        self.root.after(0, lambda: self.date_btn.config(
            text="3. Preview Dates", command=self._preview_dates))
        self.root.after(0, lambda: self.rename_btn.config(
            text="4. Preview Rename", command=self._preview_rename))
        self._schedule_enable_buttons()

    # ------------------------------------------------------------------
    # STEP 2: ORGANISE (PREVIEW & EXECUTE)
    #   matched  →  <source>/found/         (media + JSON together)
    #   missing  →  <source>/missing_json/  (media only, no JSON)
    # ------------------------------------------------------------------
    def _preview_organise(self):
        if not self.matched and not self.missing:
            self._update_status("No search results yet — run Search first.")
            return
        self._disable_buttons()
        self.pending_organise.clear()
        self._update_status("Generating Organise Preview…")
        self._set_progress(0)
        self._append_result(
            "\n--- ORGANISE PREVIEW (nothing moved yet) ---\n", 'preview')
        threading.Thread(target=self._run_preview_organise, daemon=True).start()

    def _run_preview_organise(self):
        src_path  = Path(self.source_dir.get())
        found_dir = src_path / "found"
        miss_dir  = src_path / "missing_json"

        self._append_result(
            f"  Matched files  →  {found_dir}\n"
            f"  No-JSON files  →  {miss_dir}\n\n", 'preview')

        total = len(self.matched) * 2 + len(self.missing)
        idx   = 0

        # Matched: move media file + JSON sidecar into found/
        for media_file, json_file in self.matched:
            media_dst = found_dir / media_file.name
            json_dst  = found_dir / json_file.name
            self._append_result(
                f"[PREVIEW]  {media_file.name}  →  found/\n", 'preview')
            self._append_result(
                f"[PREVIEW]  {json_file.name}  →  found/\n", 'preview')
            self.pending_organise.append(('move', media_file, media_dst))
            self.pending_organise.append(('move', json_file,  json_dst))
            idx += 2
            self._set_progress(int(idx / total * 100) if total else 100)

        # Unmatched: move media into missing_json/
        for media_file in self.missing:
            media_dst = miss_dir / media_file.name
            self._append_result(
                f"[PREVIEW]  {media_file.name}  →  missing_json/\n", 'missing')
            self.pending_organise.append(('move', media_file, media_dst))
            idx += 1
            self._set_progress(int(idx / total * 100) if total else 100)

        if self.pending_organise:
            self._append_result(
                f"\n{len(self.pending_organise)} move actions queued. "
                f"Click CONFIRM to execute.\n", 'preview')
            self.root.after(0, lambda: self.organise_btn.config(
                text="2. CONFIRM ORGANISE", command=self._execute_organise))

        self._schedule_enable_buttons()

    def _execute_organise(self):
        if not messagebox.askyesno(
                "Confirm Organise",
                f"Move {len(self.pending_organise)} files into found/ and missing_json/?\n\n"
                "Files will be MOVED (not copied) from their current locations."):
            return
        self._disable_buttons()
        self._update_status("Organising files…")
        self._set_progress(0)
        threading.Thread(target=self._run_execute_organise, daemon=True).start()

    def _run_execute_organise(self):
        moved_count = error_count = 0
        total = len(self.pending_organise)
        src_path  = Path(self.source_dir.get())
        found_dir = src_path / "found"

        self._append_result("\n--- EXECUTING ORGANISE ---\n", 'moved')

        for idx, (action, src_file, dst_file) in enumerate(self.pending_organise, 1):
            self._set_progress(int(idx / total * 100) if total else 100)
            try:
                actual_dst = safe_move(src_file, dst_file)
                self._append_result(
                    f"[MOVED]    {src_file.name}  →  {actual_dst.parent.name}/\n", 'moved')
                moved_count += 1
            except Exception as e:
                self._append_result(
                    f"[ERROR]    {src_file.name}: {e}\n", 'error')
                error_count += 1

        # Update self.matched to point at the new locations inside found/
        # so that steps 3 & 4 work on the right paths.
        updated_matches = []
        for media_file, json_file in self.matched:
            new_media = found_dir / media_file.name
            new_json  = found_dir / json_file.name
            if new_media.exists():
                updated_matches.append((new_media, new_json))
        self.matched = updated_matches

        self.pending_organise.clear()
        self._append_result(
            f"\n--- Organise Complete: {moved_count} moved, {error_count} errors ---\n"
            f"Steps 3 and 4 will now work on: {found_dir}\n", 'default')
        self._update_status(
            f"Organise done — {moved_count} moved, {error_count} errors. "
            f"found/ is ready.")

        self.root.after(0, lambda: self.organise_btn.config(
            text="2. Preview Organise", command=self._preview_organise))
        self._schedule_enable_buttons()

    # ------------------------------------------------------------------
    # STEP 3: APPLY DATES  (operates on found/)
    # ------------------------------------------------------------------
    def _preview_dates(self):
        if not self.matched:
            self._update_status(
                "No matched files available — run Search & Organise first.")
            return
        self._disable_buttons()
        self.pending_dates.clear()
        self._update_status("Generating Date Preview…")
        self._set_progress(0)
        self._append_result(
            "\n--- DATE APPLY PREVIEW (no files changed yet) ---\n", 'preview')
        threading.Thread(target=self._run_preview_dates, daemon=True).start()

    def _run_preview_dates(self):
        total = len(self.matched)
        for idx, (media_file, json_file) in enumerate(self.matched, 1):
            self._set_progress(int(idx / total * 100) if total else 100)
            if not media_file.exists():
                self._append_result(
                    f"[MISSING]  {media_file.name} not found\n", 'warn')
                continue
            if not json_file.exists():
                self._append_result(
                    f"[SKIPPED]  {media_file.name} — JSON missing\n", 'skipped')
                continue
            try:
                with open(json_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                dt = parse_photo_taken_time(data)
                if not dt:
                    self._append_result(
                        f"[SKIPPED]  {media_file.name} (no usable date in JSON)\n",
                        'skipped')
                    continue
                self._append_result(
                    f"[PREVIEW]  {media_file.name}  →  "
                    f"{dt.strftime('%Y-%m-%d %H:%M:%S')}\n", 'preview')
                self.pending_dates.append((media_file, dt))
            except Exception as e:
                self._append_result(f"[ERROR]    {json_file.name}: {e}\n", 'error')

        if self.pending_dates:
            self._append_result(
                f"\n{len(self.pending_dates)} timestamps queued. "
                f"Click CONFIRM to apply.\n", 'preview')
            self.root.after(0, lambda: self.date_btn.config(
                text="3. CONFIRM DATES", command=self._execute_dates))
        self._schedule_enable_buttons()

    def _execute_dates(self):
        if not messagebox.askyesno(
                "Confirm Dates",
                f"Stamp timestamps on {len(self.pending_dates)} file(s)?"):
            return
        self._disable_buttons()
        self._update_status("Applying dates…")
        self._set_progress(0)
        threading.Thread(target=self._run_execute_dates, daemon=True).start()

    def _run_execute_dates(self):
        updated_count = error_count = 0
        total = len(self.pending_dates)
        self._append_result("\n--- EXECUTING DATE APPLY ---\n", 'updated')

        for idx, (media_file, dt) in enumerate(self.pending_dates, 1):
            self._set_progress(int(idx / total * 100) if total else 100)
            try:
                set_file_timestamps(media_file, dt)
                self._append_result(
                    f"[UPDATED]  {media_file.name}  →  "
                    f"{dt.strftime('%Y%m%d_%H%M%S')}\n", 'updated')
                updated_count += 1
            except Exception as e:
                self._append_result(
                    f"[ERROR]    {media_file.name}: {e}\n", 'error')
                error_count += 1

        self.pending_dates.clear()
        self._append_result(
            f"\n--- Date Apply Complete: {updated_count} updated, "
            f"{error_count} errors ---\n", 'default')
        self._update_status(
            f"Dates done — Updated: {updated_count}, Errors: {error_count}")
        self.root.after(0, lambda: self.date_btn.config(
            text="3. Preview Dates", command=self._preview_dates))
        self._schedule_enable_buttons()

    # ------------------------------------------------------------------
    # STEP 4: SMART RENAME  (operates on found/)
    # ------------------------------------------------------------------
    def _preview_rename(self):
        src_str  = self.source_dir.get()
        src_path = Path(src_str) if src_str else None
        # Always prefer found/ if it exists; fall back to source root
        work_dir = (src_path / "found") if (
            src_path and (src_path / "found").is_dir()) else src_path

        if not work_dir or not work_dir.is_dir():
            self._update_status(
                "No folder to rename — run Organise first or select a Source folder.")
            return

        self._disable_buttons()
        self.pending_rename.clear()
        self._update_status(f"Generating Rename Preview for {work_dir.name}/…")
        self._set_progress(0)
        self._append_result(
            f"\n--- SMART RENAME PREVIEW  (folder: {work_dir}) ---\n", 'preview')
        threading.Thread(
            target=self._run_preview_rename, args=(work_dir,), daemon=True).start()

    def _run_preview_rename(self, work_dir: Path):
        if not PILLOW_OK:
            self._append_result(
                "[INFO] Pillow not installed — EXIF skipped; "
                "using file timestamps only.\n", 'skipped')

        files = sorted([
            f for f in work_dir.rglob("*")
            if f.is_file() and f.suffix.lower() != '.json'
        ])
        total = len(files)
        taken: set[str] = {f.name for f in files}

        for idx, src_file in enumerate(files, 1):
            self._set_progress(int(idx / total * 100) if total else 100)
            try:
                parsed_dt = parse_dt_from_name(src_file.name)

                if parsed_dt is not None:
                    if not ts_matches(src_file, parsed_dt):
                        self._append_result(
                            f"[PREVIEW]  Fix timestamp  {src_file.name}\n", 'preview')
                        self.pending_rename.append(('fix_ts', src_file, parsed_dt))
                    else:
                        self._append_result(
                            f"[SKIP]     {src_file.name} (already correct)\n", 'skipped')
                else:
                    new_name = build_target_name(src_file, taken)
                    new_path = src_file.parent / new_name
                    if new_path.exists():
                        self._append_result(
                            f"[WARNING]  {src_file.name} → {new_name} "
                            f"(target already exists)\n", 'warn')
                    else:
                        taken.discard(src_file.name)
                        taken.add(new_name)
                        self._append_result(
                            f"[PREVIEW]  {src_file.name}  →  {new_name}\n", 'preview')
                        self.pending_rename.append(('rename', src_file, new_path))

            except Exception as e:
                self._append_result(f"[ERROR]    {src_file.name}: {e}\n", 'error')

        if self.pending_rename:
            self._append_result(
                f"\n{len(self.pending_rename)} actions queued. "
                f"Click CONFIRM to execute.\n", 'preview')
            self.root.after(0, lambda: self.rename_btn.config(
                text="4. CONFIRM RENAME", command=self._execute_rename))
        self._schedule_enable_buttons()

    def _execute_rename(self):
        if not messagebox.askyesno(
                "Confirm Rename",
                f"Execute {len(self.pending_rename)} rename/timestamp action(s)?"):
            return
        self._disable_buttons()
        self._update_status("Renaming…")
        self._set_progress(0)
        threading.Thread(target=self._run_execute_rename, daemon=True).start()

    def _run_execute_rename(self):
        renamed_count = ts_fixed_count = error_count = 0
        total = len(self.pending_rename)
        self._append_result("\n--- EXECUTING SMART RENAME ---\n", 'renamed')

        for idx, (action_type, src_path, target) in enumerate(self.pending_rename, 1):
            self._set_progress(int(idx / total * 100) if total else 100)
            try:
                if action_type == 'fix_ts':
                    set_file_timestamps(src_path, target)
                    self._append_result(
                        f"[TS FIX]   {src_path.name}\n", 'updated')
                    ts_fixed_count += 1
                elif action_type == 'rename':
                    src_path.rename(target)
                    new_parsed_dt = parse_dt_from_name(target.name)
                    if new_parsed_dt:
                        set_file_timestamps(target, new_parsed_dt)
                    self._append_result(
                        f"[RENAMED]  {src_path.name}  →  {target.name}\n", 'renamed')
                    renamed_count += 1
            except Exception as e:
                self._append_result(
                    f"[ERROR]    {src_path.name}: {e}\n", 'error')
                error_count += 1

        self.pending_rename.clear()
        self._append_result(
            f"\n--- Rename Complete: {renamed_count} renamed, "
            f"{ts_fixed_count} timestamps fixed, {error_count} errors ---\n", 'default')
        self._update_status(
            f"Rename done — Renamed: {renamed_count}, "
            f"TS fixed: {ts_fixed_count}, Errors: {error_count}")
        self.root.after(0, lambda: self.rename_btn.config(
            text="4. Preview Rename", command=self._preview_rename))
        self._schedule_enable_buttons()

    # ------------------------------------------------------------------
    # GUI helpers
    # ------------------------------------------------------------------
    def _disable_buttons(self):
        for btn in (self.search_btn, self.organise_btn,
                    self.date_btn, self.rename_btn):
            self.root.after(0, lambda b=btn: b.config(state=tk.DISABLED))

    def _schedule_enable_buttons(self):
        self.root.after(0, self._enable_buttons)

    def _enable_buttons(self):
        """Must only be called on the main thread."""
        self.search_btn.config(state=tk.NORMAL)
        self.rename_btn.config(state=tk.NORMAL)
        if self.matched or self.missing:
            self.organise_btn.config(state=tk.NORMAL)
        if self.matched:
            self.date_btn.config(state=tk.NORMAL)

    def _set_progress(self, value: int):
        self.root.after(0, lambda: self.progress.config(value=value))

    def _append_result(self, text: str, tag: str = 'default'):
        self.root.after(0, lambda: self.results_text.insert(tk.END, text, tag))
        self.root.after(0, lambda: self.results_text.see(tk.END))

    def _update_status(self, text: str):
        self.root.after(0, lambda: self.status_var.set(text))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    root = tk.Tk()
    app = JsonMatcherApp(root)
    root.mainloop()
