#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy Maker — crash-safe Premiere Pro proxy generator.

- Converts heavy videos to small, fast-scrubbing proxies (DNxHR LB by default).
- Audio is COPIED from the source untouched (identical channels/rate/duration)
  so Premiere will attach the proxy. Re-encodes only if the source audio codec
  cannot be copied into .mov.
- Frame rate is preserved (required for proxy attach).
- Crash-safe: each file is encoded to a ".part" file and only renamed to its
  final name when fully finished and verified. Finished proxies are permanent
  restore points -- just run again to continue where it stopped.
- Parallel: converts several files at once.
- Simple 1-window UI for non-technical users.

Needs: Python 3.8+ (tkinter, included) and ffmpeg + ffprobe on PATH.
"""

import os
import sys
import json
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "Proxy Maker  —  crash-safe Premiere proxies"

VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".ts"}
COPY_OK_AUDIO = {"aac", "ac3", "eac3", "alac", "mp3",
                 "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_s24be"}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PRORES_ARGS = ["-c:v", "prores_ks", "-profile:v", "0", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
CODECS = {
    "ProRes Proxy (recommended)":        PRORES_ARGS,
    "DNxHR LB (smaller; auto-fallback)": ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"],
}
HEIGHTS = ["360", "540", "720"]
CHUNK_SECS = 120   # each ~2-minute chunk is an atomic checkpoint (a crash loses <= 1 chunk)


def human(n):
    n = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dur(secs):
    """Friendly duration: '1h 23m', '12m 30s', '45s'."""
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def run_ffprobe(path):
    """Return (duration_seconds, has_video, audio_codec_or_None, audio_channels_or_None)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_entries",
           "format=duration:stream=index,codec_type,codec_name,channels",
           path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=CREATE_NO_WINDOW)
        data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return 0.0, False, None, None
    try:
        dur = float(data.get("format", {}).get("duration") or 0.0)
    except Exception:
        dur = 0.0
    has_video = False
    acodec = None
    ach = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            has_video = True
        elif s.get("codec_type") == "audio" and acodec is None:
            acodec = s.get("codec_name")
            ach = s.get("channels")
    return dur, has_video, acodec, ach


def tools_ok():
    for t in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run([t, "-version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except Exception:
            return False
    return True


class App:
    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.items = {}          # iid (full path) -> state dict
        self.procs = {}          # iid -> Popen
        self.cancel = threading.Event()
        self.running = False
        self.logfile = None
        self.loglines = []       # full in-memory log (for the box + copy buttons)
        self._log_index = 0      # how many lines already drawn into the log box
        self.job_start = None    # wall-clock when the current run began (for ETA)
        self.run_iids = set()    # files being processed in the current run (for ETA)
        self.last_ckpt = None    # wall-clock of the last finished file (restore point)
        self.folder = os.path.dirname(os.path.abspath(__file__))

        self._build_ui()

        if not tools_ok():
            messagebox.showerror(
                APP_TITLE,
                "ffmpeg / ffprobe were not found on this PC.\n\n"
                "Install them, then reopen this program:\n\n"
                "    winget install --id Gyan.FFmpeg -e\n\n"
                "(then close and reopen PowerShell once)")
        self.refresh_list()
        self._tick()

    # ---------- UI ----------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry("900x650")
        self.root.minsize(780, 540)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=8)
        style.configure("Treeview", rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=(10, 2))
        ttk.Label(header, text="Proxy Maker",
                  font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(header, text="   light, smooth-playing copies for Premiere / After Effects",
                  foreground="#666666").pack(side="left", pady=(7, 0))

        pad = dict(padx=12, pady=4)

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Video folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.folder)
        ttk.Entry(top, textvariable=self.folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self.on_browse).pack(side="left")

        opt = ttk.Frame(self.root)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="Quality:").pack(side="left")
        self.height_var = tk.StringVar(value="540")
        hcb = ttk.Combobox(opt, textvariable=self.height_var, values=HEIGHTS,
                           width=6, state="readonly")
        hcb.pack(side="left", padx=(4, 12))
        hcb.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        ttk.Label(opt, text="(lower = lighter playback)").pack(side="left")

        ttk.Label(opt, text="   Codec:").pack(side="left")
        self.codec_var = tk.StringVar(value=list(CODECS)[0])
        ttk.Combobox(opt, textvariable=self.codec_var, values=list(CODECS),
                     width=30, state="readonly").pack(side="left", padx=4)

        ttk.Label(opt, text="   At once:").pack(side="left")
        default_par = max(1, min(3, (os.cpu_count() or 4) // 2))
        self.parallel = tk.IntVar(value=default_par)
        ttk.Spinbox(opt, from_=1, to=8, textvariable=self.parallel, width=4).pack(side="left", padx=4)

        self.recurse = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="include subfolders", variable=self.recurse,
                        command=self.refresh_list).pack(side="left", padx=12)

        opt2 = ttk.Frame(self.root)
        opt2.pack(fill="x", padx=8, pady=2)
        ttk.Label(opt2, text="Frame rate:").pack(side="left")
        self.fps_var = tk.StringVar(value="Keep original")
        fcb = ttk.Combobox(opt2, textvariable=self.fps_var,
                           values=["Keep original", "30", "15"], width=14,
                           state="readonly")
        fcb.pack(side="left", padx=4)
        fcb.bind("<<ComboboxSelected>>", lambda e: self.refresh_list())
        ttk.Label(opt2,
                  text="30/15 = lighter to PLAY in After Effects, but will NOT attach in Premiere",
                  foreground="#b00000").pack(side="left", padx=6)
        self.hwaccel = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt2, text="GPU decode", variable=self.hwaccel).pack(side="right")

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶  START", command=self.on_start,
                                    style="Big.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■  Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Open proxies folder", command=self.on_open).pack(side="left", padx=6)
        ttk.Button(btns, text="Rescan", command=self.refresh_list).pack(side="left")

        cols = ("size", "status", "prog")
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=12)
        self.tree.heading("#0", text="File")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.heading("prog", text="Progress")
        self.tree.column("#0", width=300, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("status", width=150, anchor="w")
        self.tree.column("prog", width=250, anchor="w")
        self.tree.tag_configure("done", foreground="#127a12")
        self.tree.tag_configure("fail", foreground="#b00000")
        self.tree.tag_configure("enc", foreground="#0a52a8")
        self.tree.pack(fill="both", expand=True, padx=12, pady=4)

        logbar = ttk.Frame(self.root)
        logbar.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Label(logbar, text="Log:").pack(side="left")
        ttk.Button(logbar, text="Copy for AI  (ChatGPT / Claude)",
                   command=self.copy_for_ai).pack(side="right")
        ttk.Button(logbar, text="Copy log", command=self.copy_log).pack(side="right", padx=6)

        logwrap = ttk.Frame(self.root)
        logwrap.pack(fill="x", padx=12, pady=(0, 4))
        self.logbox = tk.Text(logwrap, height=8, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(logwrap, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.overall = ttk.Progressbar(bottom, maximum=100, value=0)
        self.overall.pack(fill="x", side="top", pady=(0, 5))
        self.info_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.info_var,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.status_var = tk.StringVar(value="Ready.  Pick a folder and press START.")
        ttk.Label(bottom, textvariable=self.status_var,
                  foreground="#666666").pack(anchor="w")

    # ---------- helpers ----------
    def outdir(self):
        d = os.path.join(self.folder, "proxies")
        os.makedirs(d, exist_ok=True)
        return d

    def out_path_for(self, path, height, fps=None):
        base = os.path.splitext(os.path.basename(path))[0]
        tag = f"_{fps}fps" if fps else ""
        return os.path.join(self.outdir(), f"{base}_proxy_{height}p{tag}.mov")

    def _fps_from_var(self):
        v = self.fps_var.get()
        return None if v.startswith("Keep") else int(v)

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        with self.lock:
            self.loglines.append(line)
            try:
                if self.logfile is None:
                    self.logfile = open(os.path.join(self.outdir(), "proxy_log.txt"),
                                        "a", encoding="utf-8")
                self.logfile.write(line + "\n")
                self.logfile.flush()
            except Exception:
                pass

    def scan_files(self):
        folder = self.folder
        found = []
        if self.recurse.get():
            for root, dirs, names in os.walk(folder):
                dirs[:] = [d for d in dirs if d.lower() != "proxies"]
                for fn in names:
                    self._maybe_add(root, fn, found)
        else:
            try:
                names = os.listdir(folder)
            except Exception:
                names = []
            for fn in names:
                self._maybe_add(folder, fn, found)
        return sorted(found)

    @staticmethod
    def _maybe_add(root, fn, found):
        low = fn.lower()
        if low.endswith(".part"):
            return
        ext = os.path.splitext(low)[1]
        if ext in VIDEO_EXTS:
            found.append(os.path.join(root, fn))

    def verify_duration(self, path, dur):
        """True if path exists and its duration is close to dur (catches truncation)."""
        if not os.path.exists(path):
            return False
        odur, _, _, _ = run_ffprobe(path)
        if dur and dur > 0:
            return abs(odur - dur) <= max(1.5, dur * 0.02)
        return odur > 0

    # ---------- list ----------
    def refresh_list(self):
        if self.running:
            return
        self.folder = self.folder_var.get().strip() or self.folder
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items.clear()
        height = self.height_var.get()
        fps = self._fps_from_var()
        for path in self.scan_files():
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
            out = self.out_path_for(path, height, fps)
            status = "done" if os.path.exists(out) else "pending"
            it = {"path": path, "name": os.path.basename(path), "size": size,
                  "dur": 0.0, "out": out, "audio": "copy",
                  "status": status, "pct": 100 if status == "done" else 0,
                  "speed": "", "err": ""}
            self.items[path] = it
            self.tree.insert("", "end", iid=path, text=it["name"],
                             values=(human(size), status, ""))
        n = len(self.items)
        self.status_var.set(f"{n} video file(s) found in this folder."
                            if n else "No video files in this folder. Click Browse…")

    # ---------- run ----------
    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.folder)
        if d:
            self.folder_var.set(d)
            self.folder = d
            self.refresh_list()

    def on_open(self):
        try:
            os.startfile(self.outdir())  # Windows
        except Exception:
            messagebox.showinfo(APP_TITLE, self.outdir())

    # ---------- clipboard / AI help ----------
    def _to_clip(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()  # keep clipboard contents after the window changes
        except Exception:
            pass

    def copy_log(self):
        with self.lock:
            txt = "\n".join(self.loglines)
        self._to_clip(txt or "(log is empty)")
        self.status_var.set("Log copied to clipboard.")

    def copy_for_ai(self):
        self._to_clip(self.build_ai_context())
        self.status_var.set("AI help text copied — paste it into ChatGPT or Claude.")
        messagebox.showinfo(
            APP_TITLE,
            "Copied!\n\n"
            "1) Open ChatGPT or Claude in your web browser.\n"
            "2) Click the message box and paste with  Ctrl + V\n"
            "3) Press Enter.\n\n"
            "It already contains everything the AI needs to help you fix this.")

    def _ffmpeg_version(self):
        try:
            p = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            return p.stdout.decode("utf-8", "replace").splitlines()[0]
        except Exception:
            return "ffmpeg: (version unknown)"

    def build_ai_context(self):
        """A self-contained help request, written TO an AI, for a non-technical user."""
        import platform
        with self.lock:
            items = [(it["name"], human(it["size"]), it["status"], it.get("err", ""))
                     for it in self.items.values()]
            log = "\n".join(self.loglines[-80:])
        failed = [x for x in items if x[2] == "FAILED"]

        L = []
        L.append("Hi! Please help me fix a problem. I am NOT technical, so please explain")
        L.append("in simple, plain-language steps. If I need to type a command, give me the")
        L.append("exact command and tell me where to paste it.")
        L.append("")
        L.append("Please answer in this order:")
        L.append("  1) In ONE sentence, what went wrong.")
        L.append("  2) Exact step-by-step instructions to fix it (simple words).")
        L.append("  3) One short tip to avoid it next time.")
        L.append("")
        L.append("Here is everything about the program and what happened:")
        L.append("============================================================")
        L.append("PROGRAM: 'Proxy Maker' - a small Windows app (Python + ffmpeg).")
        L.append("WHAT IT DOES: makes small, low-resolution PROXY copies of heavy video files")
        L.append("so they play smoothly while editing in Adobe Premiere Pro / After Effects.")
        L.append("")
        L.append("HOW IT WORKS (so you can understand any error):")
        L.append("- For each video it runs ffmpeg to shrink the picture and re-encode it as")
        L.append("  ProRes Proxy (default) or DNxHR LB, inside a .mov file.")
        L.append("- The AUDIO is COPIED from the original unchanged and the frame rate is kept")
        L.append("  the same; both MUST match the original or Premiere won't attach the proxy.")
        L.append("- Crash-safe with CHECKPOINTS: each file is encoded in ~2-minute chunks saved")
        L.append("  under proxies/.chunks/. A crash loses at most one chunk; re-running skips the")
        L.append("  finished chunks. At the end the chunks are stitched together losslessly and")
        L.append("  the ORIGINAL audio is copied in, then renamed to the final .mov.")
        L.append("- It can convert several files at once (parallel).")
        L.append("- If DNxHR LB fails on a frame, that file is AUTOMATICALLY re-tried with")
        L.append("  ProRes Proxy, so one hard frame never blocks the whole run. This means")
        L.append("  the proxies folder may contain a mix of DNxHR and ProRes files (both fine).")
        L.append("")
        L.append("THE ffmpeg COMMAND IT RUNS PER FILE (template):")
        L.append("  ffmpeg -hide_banner -loglevel error -nostats -progress pipe:1 [-hwaccel auto]")
        L.append('    -i "INPUT" -map 0:v:0 -map 0:a:0?')
        L.append("    -vf scale=-2:HEIGHT:flags=bilinear[,fps=N]")
        L.append("    -c:v prores_ks -profile:v 0 -pix_fmt yuv422p10le   (or DNxHR: -c:v dnxhd -profile:v dnxhr_lb)")
        L.append('    -c:a copy  (or: -c:a aac -b:a 192k)  -f mov -y "OUTPUT.mov.part"')
        L.append("")
        L.append("MY COMPUTER:")
        L.append(f"  OS: {platform.platform()}")
        L.append(f"  Python: {sys.version.split()[0]}")
        L.append(f"  {self._ffmpeg_version()}")
        L.append("")
        L.append("SETTINGS I USED THIS TIME:")
        L.append(f"  folder = {self.folder_var.get()}")
        L.append(f"  quality(height) = {self.height_var.get()}    codec = {self.codec_var.get()}")
        L.append(f"  convert at once = {self.parallel.get()}    frame rate = {self.fps_var.get()}")
        L.append(f"  GPU decode = {bool(self.hwaccel.get())}    include subfolders = {bool(self.recurse.get())}")
        L.append("")
        if failed:
            L.append(f"WHAT FAILED ({len(failed)} file(s)):")
            for n, sz, st, er in failed:
                L.append(f"  {n}  ({sz})")
                if er:
                    L.append(f"      ERROR: {er}")
        else:
            L.append("FILES AND STATUS:")
            for n, sz, st, er in (items or [("(none found)", "", "", "")]):
                L.append(f"  {n}  ({sz})  -> {st}")
        L.append("")
        L.append("FULL RECENT LOG:")
        L.append(log or "  (the log is empty)")
        L.append("============================================================")
        L.append("Thank you!")
        return "\n".join(L)

    def on_start(self):
        if self.running:
            return
        self.folder = self.folder_var.get().strip() or self.folder
        if not os.path.isdir(self.folder):
            messagebox.showerror(APP_TITLE, "That folder does not exist.")
            return
        if not self.items:
            self.refresh_list()
            if not self.items:
                return
        self.running = True
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        height = self.height_var.get()
        vargs = CODECS[self.codec_var.get()]
        fps = self._fps_from_var()
        hw = bool(self.hwaccel.get())
        self.log(f"START folder={self.folder} height={height} codec={self.codec_var.get()} "
                 f"parallel={self.parallel.get()} fps={fps} gpu={hw}")
        threading.Thread(target=self.controller, args=(height, vargs, fps, hw), daemon=True).start()

    def on_stop(self):
        if not self.running:
            return
        self.cancel.set()
        self.status_var.set("Stopping… (finishing current frames, then safe to close)")
        with self.lock:
            for proc in list(self.procs.values()):
                try:
                    proc.kill()
                except Exception:
                    pass

    def controller(self, height, vargs, fps, hwaccel):
        pending = []
        for iid, it in list(self.items.items()):
            if self.cancel.is_set():
                break
            with self.lock:
                it["status"] = "checking…"
            dur, has_video, acodec, ach = run_ffprobe(it["path"])
            out = self.out_path_for(it["path"], height, fps)
            if acodec is None:
                audio = "none"
            elif acodec in COPY_OK_AUDIO:
                audio = "copy"
            else:
                audio = "aac"
            with self.lock:
                it["dur"] = dur
                it["out"] = out
                it["audio"] = audio
            if not has_video:
                with self.lock:
                    it["status"] = "no video"
                continue
            if self.verify_duration(out, dur):
                with self.lock:
                    it["status"] = "done"
                    it["pct"] = 100
                continue
            with self.lock:
                it["status"] = "queued"
                it["pct"] = 0
            pending.append(iid)

        if not pending and not self.cancel.is_set():
            self._finish("All proxies already exist — nothing to do.")
            return

        with self.lock:
            self.job_start = time.time()
            self.run_iids = set(pending)

        try:
            with ThreadPoolExecutor(max_workers=max(1, self.parallel.get())) as ex:
                for iid in pending:
                    ex.submit(self.encode, iid, height, vargs, fps, hwaccel)
        except Exception as e:
            self.log(f"pool error: {e}")

        if self.cancel.is_set():
            self._finish("Stopped. Run again any time to continue where it left off.")
        else:
            fails = [it for it in self.items.values() if it["status"] == "FAILED"]
            if fails:
                self._finish(f"Done, but {len(fails)} file(s) failed — see proxy_log.txt.")
            else:
                self._finish("All done. Proxies are in the 'proxies' subfolder.")

    def encode(self, iid, height, vargs, fps, hwaccel):
        it = self.items[iid]
        if self.cancel.is_set():
            with self.lock:
                it["status"] = "stopped"
            return
        out = it["out"]
        part = out + ".part"
        dur = it["dur"] or 0.0

        # audio is taken from the ORIGINAL (input #1 during the final stitch)
        if it["audio"] == "none":
            amap, aargs = [], []
        elif it["audio"] == "copy":
            amap, aargs = ["-map", "1:a:0?"], ["-c:a", "copy"]
        else:
            amap, aargs = ["-map", "1:a:0?"], ["-c:a", "aac", "-b:a", "192k"]

        vf = f"scale=-2:{height}:flags=bilinear"
        if fps:
            vf += f",fps={fps}"
        pre = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
        if hwaccel:
            pre += ["-hwaccel", "auto"]

        key = os.path.splitext(os.path.basename(out))[0]
        workdir = os.path.join(self.outdir(), ".chunks", key)
        try:
            os.makedirs(workdir, exist_ok=True)
        except Exception:
            pass

        n_chunks = max(1, int((dur + CHUNK_SECS - 1) // CHUNK_SECS)) if dur > 0 else 1

        codec_args, codec_label = vargs, ""
        with self.lock:
            it["status"] = "encoding"
            it["pct"] = 0
            it["speed"] = ""
            it["t0"] = time.time()

        # ---- encode each chunk; an existing chunk file means "already done" (resume) ----
        chunk_files = []
        while True:                       # may restart once if DNxHR must fall back to ProRes
            chunk_files = []
            restart = False
            for i in range(n_chunks):
                if self.cancel.is_set():
                    with self.lock:
                        it["status"] = "stopped"
                    return
                start = i * CHUNK_SECS
                length = CHUNK_SECS if (dur <= 0 or start + CHUNK_SECS <= dur) else (dur - start)
                cfinal = os.path.join(workdir, f"v{i:04d}.mov")
                cpart = cfinal + ".part"
                chunk_files.append(cfinal)
                if os.path.exists(cfinal):
                    continue              # checkpoint already saved -> skip
                self._safe_remove(cpart)
                if dur > 0:
                    ccmd = (pre + ["-ss", f"{start:.3f}", "-i", it["path"], "-t", f"{length:.3f}",
                            "-map", "0:v:0", "-vf", vf] + codec_args + ["-an", "-f", "mov", "-y", cpart])
                else:
                    ccmd = (pre + ["-i", it["path"], "-map", "0:v:0", "-vf", vf]
                            + codec_args + ["-an", "-f", "mov", "-y", cpart])
                rc, err_tail = self._run_ffmpeg(iid, it, ccmd, base=start, total=dur)
                if self.cancel.is_set():
                    self._safe_remove(cpart)
                    with self.lock:
                        it["status"] = "stopped"
                    return
                if rc == 0 and os.path.exists(cpart):
                    os.replace(cpart, cfinal)          # CHECKPOINT (atomic, survives a crash)
                    with self.lock:
                        self.last_ckpt = time.time()
                    self.log(f"{it['name']}: checkpoint {i + 1}/{n_chunks} saved")
                else:
                    self._safe_remove(cpart)
                    # DNxHR encoder bug on a chunk -> switch this whole file to ProRes and restart
                    if codec_args is vargs and any(a == "dnxhd" for a in vargs):
                        self.log(f"{it['name']}: DNxHR failed on a chunk - switching this file to ProRes Proxy")
                        codec_args, codec_label = PRORES_ARGS, "ProRes Proxy"
                        for cf in chunk_files:
                            self._safe_remove(cf)
                        with self.lock:
                            it["status"] = "encoding (ProRes Proxy)"
                            it["pct"] = 0
                        restart = True
                        break
                    reason = " | ".join(err_tail[-3:]) if err_tail else f"exit {rc}"
                    with self.lock:
                        it["status"] = "FAILED"
                        it["err"] = reason
                    self.log(f"FAILED {it['name']}: {reason}")
                    return
            if restart:
                continue
            break

        # ---- stitch chunks (lossless) + copy the ORIGINAL audio in one pass ----
        if self.cancel.is_set():
            with self.lock:
                it["status"] = "stopped"
            return
        with self.lock:
            it["status"] = "finishing"
            it["pct"] = 99
        listpath = os.path.join(workdir, "list.txt")
        try:
            with open(listpath, "w", encoding="utf-8") as lf:
                for cf in chunk_files:
                    p = os.path.abspath(cf).replace("\\", "/").replace("'", "'\\''")
                    lf.write(f"file '{p}'\n")
        except Exception as e:
            with self.lock:
                it["status"] = "FAILED"
                it["err"] = f"list: {e}"
            self.log(f"FAILED {it['name']}: cannot write stitch list: {e}")
            return

        self._safe_remove(part)
        ccat = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                 "-f", "concat", "-safe", "0", "-i", listpath, "-i", it["path"],
                 "-map", "0:v:0"] + amap + ["-c:v", "copy"] + aargs + ["-f", "mov", "-y", part])
        rc, err_tail = self._run_ffmpeg(iid, it, ccat, base=0, total=dur)
        if self.cancel.is_set():
            self._safe_remove(part)
            with self.lock:
                it["status"] = "stopped"
            return
        if rc == 0 and os.path.exists(part) and self.verify_duration(part, dur):
            try:
                os.replace(part, out)
                with self.lock:
                    it["status"] = "done"
                    it["pct"] = 100
                    self.last_ckpt = time.time()
                self.log(f"DONE {it['name']}" + (f" [{codec_label}]" if codec_label else ""))
                for cf in chunk_files:
                    self._safe_remove(cf)
                self._safe_remove(listpath)
                try:
                    os.rmdir(workdir)
                    os.rmdir(os.path.dirname(workdir))   # remove .chunks if now empty
                except Exception:
                    pass
                return
            except Exception as e:
                self._safe_remove(part)
                err_tail = [f"rename: {e}"]
        self._safe_remove(part)
        reason = " | ".join(err_tail[-3:]) if err_tail else f"stitch exit {rc}"
        with self.lock:
            it["status"] = "FAILED"
            it["err"] = reason
        self.log(f"FAILED {it['name']} (stitch): {reason}")

    def _run_ffmpeg(self, iid, it, cmd, base, total):
        """Run one ffmpeg command. Maps its progress into the file's overall
        percent as (base + chunk_seconds)/total. Returns (returncode, err_tail)."""
        err_tail = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            return 1, [str(e)]
        with self.lock:
            self.procs[iid] = proc
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us="):
                try:
                    secs = float(line.split("=", 1)[1]) / 1_000_000.0
                    pct = 0 if total <= 0 else max(0, min(99, int((base + secs) / total * 100)))
                    with self.lock:
                        it["pct"] = pct
                except Exception:
                    pass
            elif line.startswith("speed="):
                with self.lock:
                    it["speed"] = line.split("=", 1)[1]
            elif line and "=" not in line:
                err_tail.append(line)
                del err_tail[:-15]
            if self.cancel.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                break
        proc.wait()
        rc = proc.returncode
        with self.lock:
            self.procs.pop(iid, None)
        return rc, err_tail

    @staticmethod
    def _safe_remove(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _finish(self, msg):
        def done():
            self.running = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.status_var.set(msg)
        self.root.after(0, done)

    # ---------- periodic UI refresh ----------
    def _tick(self):
        now = time.time()
        rows = []
        with self.lock:
            total = len(self.items)
            done = 0
            sum_pct = 0
            run_total = 0.0   # total seconds of video to process this run
            run_proc = 0.0    # seconds processed so far this run
            for iid, it in self.items.items():
                st = it["status"]
                pct = it["pct"]
                dur = it.get("dur", 0) or 0
                if st.startswith("encoding"):
                    eta = ""
                    t0 = it.get("t0")
                    if t0 and pct > 0:
                        rem = (now - t0) * (100 - pct) / pct
                        eta = "   ~" + fmt_dur(rem) + " left"
                    extra = " (ProRes)" if "(" in st else ""
                    friendly = "Converting" + extra
                    detail = (f"{pct}%   {it['speed']}{eta}").strip()
                    tag = "enc"
                elif st == "finishing":
                    friendly, detail, tag = "Finishing (stitching)", f"{pct}%", "enc"
                elif st == "done":
                    friendly, detail, tag = "Done", "", "done"
                    done += 1
                elif st == "FAILED":
                    friendly, detail, tag = "Failed", it.get("err", "")[:60], "fail"
                elif st in ("queued", "pending"):
                    friendly, detail, tag = "Waiting", "", ""
                elif st == "checking…":
                    friendly, detail, tag = "Checking", "", ""
                elif st == "no video":
                    friendly, detail, tag = "No video (skipped)", "", ""
                elif st == "stopped":
                    friendly, detail, tag = "Stopped", "", ""
                else:
                    friendly, detail, tag = st, "", ""
                rows.append((iid, friendly, detail, tag))
                sum_pct += pct
                if iid in self.run_iids:
                    run_total += dur
                    run_proc += dur * pct / 100.0
            overall_val = (sum_pct / total) if total else 0
            new_logs = self.loglines[self._log_index:]
            self._log_index = len(self.loglines)
            job_start = self.job_start
            last_ckpt = self.last_ckpt
            running = self.running

        for iid, friendly, detail, tag in rows:
            if self.tree.exists(iid):
                self.tree.set(iid, "status", friendly)
                self.tree.set(iid, "prog", detail)
                self.tree.item(iid, tags=(tag,) if tag else ())
        self.overall["value"] = overall_val
        for ln in new_logs:
            self.logbox.insert("end", ln + "\n")
        if new_logs:
            self.logbox.see("end")

        info = f"Files done:  {done} / {total}"
        if running and job_start and run_total > 0:
            elapsed = now - job_start
            if run_proc > 0 and elapsed > 1.5:
                thru = run_proc / elapsed
                rem_wall = (run_total - run_proc) / thru if thru > 0 else 0
                clock = time.strftime("%H:%M", time.localtime(now + rem_wall))
                info += f"      •      Estimated finish:  ~{fmt_dur(rem_wall)}  (around {clock})"
            else:
                info += "      •      Estimated finish:  calculating…"
        if last_ckpt:
            saved = time.strftime("%H:%M:%S", time.localtime(last_ckpt))
            info += f"      •      Last saved:  {saved}  ({fmt_dur(now - last_ckpt)} ago)"
        else:
            info += "      •      Last saved:  (nothing finished yet)"
        self.info_var.set(info)

        if running:
            self.status_var.set("Converting…   you can close this window any time and "
                                "re-run later — finished files are kept.")
        self.root.after(500, self._tick)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
