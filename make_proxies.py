#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy Maker — crash-safe Premiere Pro proxy generator.

- Converts heavy videos to small, fast-scrubbing proxies (ProRes Proxy by default).
- Audio is COPIED from the source untouched (identical channels/rate/duration)
  so Premiere will attach the proxy. Re-encodes only if the source audio codec
  cannot be copied into .mov.
- Frame rate is preserved (required for proxy attach).
- Crash-safe: each file is encoded in ~2-minute chunks (checkpoints) and the
  final file is only renamed into place when fully finished and verified.
  Just run again to continue where it stopped.
- Parallel: converts several files at once.
- Simple one-screen UI in the browser, served by this script on 127.0.0.1.

Needs: Python 3.8+ (standard library only) and ffmpeg + ffprobe on PATH.
The UI is prebuilt into ./dist (source in ./src, see CONTRIBUTING.md).
"""

import os
import sys
import json
import time
import platform
import threading
import subprocess
import webbrowser
import mimetypes
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

APP_TITLE = "Proxy Maker  —  crash-safe Premiere proxies"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PROXY_MAKER_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.join(HERE, "dist")

VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".ts"}
COPY_OK_AUDIO = {"aac", "ac3", "eac3", "alac", "mp3",
                 "pcm_s16le", "pcm_s24le", "pcm_s16be", "pcm_s24be"}
COPY_OK_AUDIO_MP4 = {"aac", "ac3", "eac3", "alac", "mp3"}   # .mp4 cannot hold PCM

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PRORES_ARGS = ["-c:v", "prores_ks", "-profile:v", "0", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
PRORES_LT_ARGS = ["-c:v", "prores_ks", "-profile:v", "1", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
# codec id (also the short tag in the output filename, so codecs never collide) -> (label, ffmpeg args)
CODECS = {
    "prores": ("ProRes Proxy (recommended)", PRORES_ARGS),
    "lt":     ("ProRes 422 LT (higher quality)", PRORES_LT_ARGS),
    "dnxhr":  ("DNxHR LB (smaller; auto-fallback)",
               ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]),
    # short GOP (a keyframe every 15 frames, no B-frames): ~20x smaller than ProRes, still scrubs
    # well on any modern GPU, and plays in phone browsers. Written as .mp4.
    "h264":   ("H.264 short GOP (small; phones and web)",
               ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "15", "-keyint_min", "15",
                "-bf", "0", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]),
}
MP4_CODECS = {"h264"}
# With "GPU" on, H.264 is encoded on an NVIDIA card (NVENC); any failure falls back to the CPU encoder.
# ProRes and DNxHR have no GPU encoder, so for them "GPU" only speeds up decoding.
H264_NVENC_ARGS = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                   "-g", "15", "-bf", "0", "-pix_fmt", "yuv420p"]
# Quality is the SHORT side of the picture: 540 gives 960x540 for landscape and 540x960 for phone video.
HEIGHTS = ["360", "540", "720", "Original"]   # "Original" = keep full source resolution
FPS_CHOICES = ["keep", "30", "15"]
CHUNK_SECS = int(os.environ.get("PROXY_MAKER_CHUNK_SECS", "120"))   # each chunk is an atomic checkpoint


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


def default_settings():
    return {
        "folder": HERE,
        "height": "540",
        "codec": "prores",
        "fps": "keep",
        "parallel": max(1, min(3, (os.cpu_count() or 4) // 2)),
        "recurse": False,
        "hwaccel": False,
    }


def clean_settings(raw, base):
    """Merge user-sent settings into base, keeping only valid values."""
    s = dict(base)
    raw = raw or {}
    if isinstance(raw.get("folder"), str) and raw["folder"].strip():
        s["folder"] = os.path.expanduser(raw["folder"].strip())
    if raw.get("height") in HEIGHTS:
        s["height"] = raw["height"]
    if raw.get("codec") in CODECS:
        s["codec"] = raw["codec"]
    if str(raw.get("fps")) in FPS_CHOICES:
        s["fps"] = str(raw["fps"])
    try:
        s["parallel"] = max(1, min(8, int(raw.get("parallel", s["parallel"]))))
    except Exception:
        pass
    for k in ("recurse", "hwaccel"):
        if k in raw:
            s[k] = bool(raw[k])
    return s


class Engine:
    """Everything the old Tkinter window did, without the window: scan, convert, report."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}          # iid (full path) -> state dict
        self.procs = {}          # iid -> Popen
        self.cancel = threading.Event()
        self.running = False
        self.logfile = None
        self.logfile_dir = None
        self.loglines = []       # [(epoch, text)] full in-memory log
        self.job_start = None    # wall-clock when the current run began (for ETA)
        self.run_iids = set()    # files being processed in the current run (for ETA)
        self.last_ckpt = None    # wall-clock of the last saved checkpoint
        self.settings = default_settings()
        self.folder = self.settings["folder"]
        self.message = "Ready. Pick a folder and press Start."
        self.finished_at = None  # wall-clock of the last run's end (UI shows a toast)
        self.tools = tools_ok()
        self._ffver = None
        self.files = None        # explicit file list (command line); None = scan the folder
        self.out_override = None # explicit output folder (command line); None = <folder>/proxies

    # ---------- helpers ----------
    def outdir(self):
        d = self.out_override or os.path.join(self.folder, "proxies")
        os.makedirs(d, exist_ok=True)
        return d

    def out_path_for(self, path, height, fps=None, codec=None):
        base = os.path.splitext(os.path.basename(path))[0]
        res = "full" if height == "Original" else f"{height}p"
        ctag = f"_{codec}" if codec in CODECS else ""
        ftag = f"_{fps}fps" if fps else ""
        ext = ".mp4" if codec in MP4_CODECS else ".mov"
        return os.path.join(self.outdir(), f"{base}_proxy_{res}{ctag}{ftag}{ext}")

    def _fps(self):
        v = self.settings["fps"]
        return None if v == "keep" else int(v)

    def log(self, msg):
        now = time.time()
        line = time.strftime("%H:%M:%S ", time.localtime(now)) + msg
        with self.lock:
            self.loglines.append((now, msg))
            try:
                if self.logfile is None or self.logfile_dir != self.folder:
                    if self.logfile is not None:
                        self.logfile.close()
                    self.logfile = open(os.path.join(self.outdir(), "proxy_log.txt"),
                                        "a", encoding="utf-8")
                    self.logfile_dir = self.folder
                self.logfile.write(line + "\n")
                self.logfile.flush()
            except Exception:
                pass

    def log_text(self, last=None):
        with self.lock:
            lines = self.loglines[-last:] if last else list(self.loglines)
        return "\n".join(time.strftime("%H:%M:%S ", time.localtime(t)) + m for t, m in lines)

    def scan_files(self):
        if self.files is not None:
            return list(self.files)
        folder = self.folder
        found = []
        if self.settings["recurse"]:
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
    def refresh_list(self, raw_settings=None):
        """Apply settings and rescan. Returns an error string, or None."""
        if self.running:
            return "A conversion is running. Stop it before changing settings."
        s = clean_settings(raw_settings, self.settings)
        if not os.path.isdir(s["folder"]):
            with self.lock:
                self.settings = s
                self.items.clear()
            self.message = "That folder does not exist."
            return "That folder does not exist."
        with self.lock:
            self.settings = s
            self.folder = s["folder"]
        height, fps, codec = s["height"], self._fps(), s["codec"]
        items = {}
        for path in self.scan_files():
            try:
                size = os.path.getsize(path)
            except Exception:
                size = 0
            out = self.out_path_for(path, height, fps, codec)
            status = "done" if os.path.exists(out) else "pending"
            items[path] = {"path": path, "name": os.path.relpath(path, self.folder), "size": size,
                           "dur": 0.0, "out": out, "audio": "copy",
                           "status": status, "pct": 100 if status == "done" else 0,
                           "speed": "", "err": ""}
        with self.lock:
            self.items = items
        n = len(items)
        self.message = (f"{n} video file(s) found in this folder." if n
                        else "No video files in this folder.")
        return None

    # ---------- run ----------
    def start(self, raw_settings=None):
        if self.running:
            return "Already running."
        err = self.refresh_list(raw_settings)
        if err:
            return err
        if not self.items:
            return "No video files in this folder."
        if not self.tools:
            self.tools = tools_ok()
            if not self.tools:
                return "ffmpeg / ffprobe were not found on this PC."
        s = self.settings
        self.running = True
        self.finished_at = None
        self.cancel.clear()
        with self.lock:
            self.job_start = None
            self.run_iids = set()
        height, codec, fps, hw = s["height"], s["codec"], self._fps(), s["hwaccel"]
        vargs = H264_NVENC_ARGS if (codec == "h264" and hw) else CODECS[codec][1]
        self.message = "Checking files…"
        self.log(f"START folder={self.folder} height={height} codec={CODECS[codec][0]} "
                 f"parallel={s['parallel']} fps={fps} gpu={hw}")
        threading.Thread(target=self.controller, args=(height, vargs, fps, hw, codec),
                         daemon=True).start()
        return None

    def stop(self):
        if not self.running:
            return
        self.cancel.set()
        self.message = "Stopping… finishing current frames, then it is safe to close."
        with self.lock:
            for proc in list(self.procs.values()):
                try:
                    proc.kill()
                except Exception:
                    pass

    def controller(self, height, vargs, fps, hwaccel, codec):
        pending = []
        for iid, it in list(self.items.items()):
            if self.cancel.is_set():
                break
            with self.lock:
                it["status"] = "checking…"
            dur, has_video, acodec, ach = run_ffprobe(it["path"])
            out = self.out_path_for(it["path"], height, fps, codec)
            copy_ok = COPY_OK_AUDIO_MP4 if codec in MP4_CODECS else COPY_OK_AUDIO
            if acodec is None:
                audio = "none"
            elif acodec in copy_ok:
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
        self.message = ("Converting… you can close this page any time and re-run later "
                        "— finished files are kept.")

        try:
            with ThreadPoolExecutor(max_workers=max(1, self.settings["parallel"])) as ex:
                for iid in pending:
                    ex.submit(self.encode, iid, height, vargs, fps, hwaccel)
        except Exception as e:
            self.log(f"pool error: {e}")

        if self.cancel.is_set():
            with self.lock:
                for it in self.items.values():
                    if it["status"] in ("queued", "checking…", "pending"):
                        it["status"] = "stopped"
            self._finish("Stopped. Run again any time to continue where it left off.")
        else:
            fails = [it for it in self.items.values() if it["status"] == "FAILED"]
            if fails:
                self._finish(f"Done, but {len(fails)} file(s) failed — see the log.")
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

        filters = []
        if height != "Original":
            # the SHORT side becomes `height`, so phone video (portrait) gets 540x960, not 304x540
            filters.append(f"scale='if(gte(iw,ih),-2,{height})':'if(gte(iw,ih),{height},-2)':flags=bilinear")
        if fps:
            filters.append(f"fps={fps}")
        vf_args = ["-vf", ",".join(filters)] if filters else []
        mp4 = self.settings["codec"] in MP4_CODECS
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
        while True:                       # may restart once if DNxHR -> ProRes or GPU -> CPU
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
                            "-map", "0:v:0"] + vf_args + codec_args + ["-an", "-f", "mov", "-y", cpart])
                else:
                    ccmd = (pre + ["-i", it["path"], "-map", "0:v:0"] + vf_args
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
                    if codec_args is vargs and "h264_nvenc" in vargs:
                        self.log(f"{it['name']}: GPU encoder failed - switching this file to the CPU encoder")
                        codec_args, codec_label = CODECS["h264"][1], "H.264 on CPU"
                        for cf in chunk_files:
                            self._safe_remove(cf)
                        with self.lock:
                            it["status"] = "encoding (H.264 on CPU)"
                            it["pct"] = 0
                        restart = True
                        break
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
        fmt = ["-f", "mp4", "-movflags", "+faststart"] if mp4 else ["-f", "mov"]
        ccat = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                 "-f", "concat", "-safe", "0", "-i", listpath, "-i", it["path"],
                 "-map", "0:v:0"] + amap + ["-c:v", "copy"] + aargs + fmt + ["-y", part])
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
        self.log(msg)
        self.message = msg
        self.finished_at = time.time()
        self.running = False

    # ---------- what the UI shows (was the 500 ms _tick) ----------
    def snapshot(self, since=0):
        now = time.time()
        rows = []
        with self.lock:
            total = len(self.items)
            done = 0
            failed = 0
            sum_pct = 0
            run_total = 0.0   # total seconds of video to process this run
            run_proc = 0.0    # seconds processed so far this run
            for iid, it in self.items.items():
                st = it["status"]
                pct = it["pct"]
                dur = it.get("dur", 0) or 0
                eta = None
                if st.startswith("encoding"):
                    t0 = it.get("t0")
                    if t0 and pct > 0:
                        eta = (now - t0) * (100 - pct) / pct
                    key = "converting_fallback" if "(" in st else "converting"
                elif st == "finishing":
                    key = "finishing"
                elif st == "done":
                    key = "done"
                    done += 1
                elif st == "FAILED":
                    key = "failed"
                    failed += 1
                elif st == "queued":
                    key = "waiting"
                elif st == "pending":
                    key = "pending"
                elif st == "checking…":
                    key = "checking"
                elif st == "no video":
                    key = "no_video"
                elif st == "stopped":
                    key = "stopped"
                else:
                    key = st
                rows.append({"id": iid, "name": it["name"], "size": it["size"],
                             "status": key, "pct": pct, "speed": it.get("speed", ""),
                             "eta": eta, "error": it.get("err", ""),
                             "out": os.path.basename(it["out"])})
                sum_pct += pct
                if iid in self.run_iids:
                    run_total += dur
                    run_proc += dur * pct / 100.0
            overall = (sum_pct / total) if total else 0
            log = [{"id": i, "time": int(t * 1000), "message": m}
                   for i, (t, m) in enumerate(self.loglines[since:], start=since)]
            log_total = len(self.loglines)
            job_start = self.job_start
            last_ckpt = self.last_ckpt
            settings = dict(self.settings)

        finish_secs = None
        if self.running and job_start and run_total > 0:
            elapsed = now - job_start
            if run_proc > 0 and elapsed > 1.5:
                thru = run_proc / elapsed
                finish_secs = (run_total - run_proc) / thru if thru > 0 else 0
        return {
            "running": self.running,
            "stopping": self.running and self.cancel.is_set(),
            "tools": self.tools,
            "message": self.message,
            "settings": settings,
            "folderExists": os.path.isdir(settings["folder"]),
            "files": rows,
            "summary": {
                "total": total, "done": done, "failed": failed,
                "overall": round(overall, 1),
                "finishSecs": finish_secs,
                "finishAt": int((now + finish_secs) * 1000) if finish_secs is not None else None,
                "lastSaved": int(last_ckpt * 1000) if last_ckpt else None,
            },
            "finishedAt": int(self.finished_at * 1000) if self.finished_at else None,
            "log": log,
            "logTotal": log_total,
        }

    # ---------- AI help ----------
    def _ffmpeg_version(self):
        if self._ffver:
            return self._ffver
        try:
            p = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW)
            self._ffver = p.stdout.decode("utf-8", "replace").splitlines()[0]
        except Exception:
            return "ffmpeg: (version unknown)"
        return self._ffver

    def build_ai_context(self):
        """A self-contained help request, written TO an AI, for a non-technical user."""
        with self.lock:
            items = [(it["name"], human(it["size"]), it["status"], it.get("err", ""))
                     for it in self.items.values()]
            s = dict(self.settings)
        log = self.log_text(last=80)
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
        L.append("PROGRAM: 'Proxy Maker' - a small app (Python + ffmpeg) with a page in the web browser.")
        L.append("WHAT IT DOES: makes small, low-resolution PROXY copies of heavy video files")
        L.append("so they play smoothly while editing in Adobe Premiere Pro / After Effects.")
        L.append("")
        L.append("HOW IT WORKS (so you can understand any error):")
        L.append("- START.bat runs 'python make_proxies.py', which starts a small local web server")
        L.append(f"  on http://{HOST}:{PORT} and opens the page in the browser.")
        L.append("- For each video it runs ffmpeg to (optionally) shrink the picture and re-encode")
        L.append("  it as ProRes Proxy (default), ProRes 422 LT, or DNxHR LB, inside a .mov file,")
        L.append("  or as short-GOP H.264 inside an .mp4 (small; with GPU on, encoded by NVIDIA NVENC,")
        L.append("  falling back to the CPU encoder libx264 if NVENC fails).")
        L.append("  Quality is the SHORT side: 540 gives 960x540 for landscape and 540x960 for phone video.")
        L.append("  With the 'Original' quality it keeps the full source resolution (full-res transcode).")
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
        L.append("    [-vf scale='if(gte(iw,ih),-2,Q)':'if(gte(iw,ih),Q,-2)':flags=bilinear[,fps=N]]   (no scale when quality = Original)")
        L.append("    -c:v prores_ks -profile:v 0 -pix_fmt yuv422p10le   (Proxy=0, 422 LT=1; or DNxHR: -c:v dnxhd -profile:v dnxhr_lb;")
        L.append("     or H.264: -c:v libx264 -crf 23 -g 15 -bf 0 / -c:v h264_nvenc -cq 23 -g 15 -bf 0)")
        L.append('    -c:a copy  (or: -c:a aac -b:a 192k)  -f mov -y "OUTPUT.mov.part"')
        L.append("")
        L.append("MY COMPUTER:")
        L.append(f"  OS: {platform.platform()}")
        L.append(f"  Python: {sys.version.split()[0]}")
        L.append(f"  {self._ffmpeg_version()}")
        L.append("")
        L.append("SETTINGS I USED THIS TIME:")
        L.append(f"  folder = {s['folder']}")
        L.append(f"  quality(short side) = {s['height']}    codec = {CODECS[s['codec']][0]}")
        L.append(f"  convert at once = {s['parallel']}    frame rate = "
                 f"{'Keep original' if s['fps'] == 'keep' else s['fps']}")
        L.append(f"  GPU decode = {s['hwaccel']}    include subfolders = {s['recurse']}")
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

    # ---------- desktop helpers ----------
    def browse(self):
        """Native folder picker in a helper process (the browser cannot give us a real path)."""
        script = ("import sys, tkinter as tk\nfrom tkinter import filedialog\n"
                  "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
                  "d = filedialog.askdirectory(initialdir=sys.argv[1], title='Pick the folder with your videos')\n"
                  "sys.stdout.write(d or '')\n")
        try:
            p = subprocess.run([sys.executable, "-c", script, self.folder],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        except Exception as e:
            return None, f"The folder picker could not open ({e}). Type or paste the path instead."
        if p.returncode != 0:
            return None, "The folder picker is not available on this computer. Type or paste the path instead."
        return os.path.normpath(p.stdout.decode("utf-8", "replace").strip()) or None, None

    def open_outdir(self):
        try:
            d = self.outdir()
        except Exception as e:
            return f"Could not open the proxies folder: {e}"
        try:
            if os.name == "nt":
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", d])
            else:
                subprocess.Popen(["xdg-open", d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return f"Could not open the proxies folder ({e}). It is here: {d}"
        return None


ENGINE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ProxyMaker"

    def log_message(self, fmt, *args):   # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            try:
                since = int(parse_qs(u.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, ENGINE.snapshot(since))
        if u.path == "/api/help-text":
            return self._send(200, {"text": ENGINE.build_ai_context()})
        if u.path == "/api/log-text":
            return self._send(200, {"text": ENGINE.log_text() or "(log is empty)"})
        return self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/scan":
            err = ENGINE.refresh_list(body)
            return self._send(200, {"error": err, "state": ENGINE.snapshot(0)})
        if u.path == "/api/start":
            err = ENGINE.start(body)
            return self._send(200, {"error": err, "state": ENGINE.snapshot(0)})
        if u.path == "/api/stop":
            ENGINE.stop()
            return self._send(200, {"error": None})
        if u.path == "/api/browse":
            folder, err = ENGINE.browse()
            return self._send(200, {"folder": folder, "error": err})
        if u.path == "/api/open":
            return self._send(200, {"error": ENGINE.open_outdir()})
        return self._send(404, {"error": "not found"})

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEBUI, rel))
        if not full.startswith(WEBUI) or not os.path.isfile(full):
            full = os.path.join(WEBUI, "index.html")
        if not os.path.isfile(full):
            return self._send(500, b"The web UI is missing (dist/index.html). Re-download Proxy Maker.",
                              "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)


def already_running(url):
    try:
        with urllib.request.urlopen(url + "api/state", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def cli(argv):
    """Command line: make proxies without the page, e.g. from a server or a script.
    Progress goes to stderr; a JSON summary goes to stdout. Exit code 1 if any file failed."""
    import argparse
    ap = argparse.ArgumentParser(prog="make_proxies.py",
                                 description="Make proxies without the browser page.")
    ap.add_argument("paths", nargs="+", help="video files and/or folders")
    ap.add_argument("--quality", choices=HEIGHTS, default="540",
                    help="short side of the picture in pixels (540 = 960x540, or 540x960 for phone video)")
    ap.add_argument("--codec", choices=list(CODECS), default="prores")
    ap.add_argument("--fps", choices=FPS_CHOICES, default="keep")
    ap.add_argument("--parallel", type=int, default=default_settings()["parallel"])
    ap.add_argument("--gpu", action="store_true", help="GPU decoding; H.264 is also encoded on the GPU")
    ap.add_argument("--recurse", action="store_true", help="include subfolders of folder arguments")
    ap.add_argument("--out", help="output folder (default: a 'proxies' folder next to the videos)")
    a = ap.parse_args(argv)

    files = []
    for pth in a.paths:
        pth = os.path.abspath(os.path.expanduser(pth))
        if os.path.isdir(pth):
            for root, dirs, names in os.walk(pth):
                dirs[:] = [d for d in dirs if d.lower() != "proxies"] if a.recurse else []
                for fn in sorted(names):
                    Engine._maybe_add(root, fn, files)
        elif os.path.isfile(pth):
            files.append(pth)
        else:
            print(f"not found: {pth}", file=sys.stderr)
            return 2
    if not files:
        print("no video files found", file=sys.stderr)
        return 2
    if not tools_ok():
        print("ffmpeg / ffprobe were not found on PATH", file=sys.stderr)
        return 2

    e = Engine()
    e.files = files
    e.out_override = os.path.abspath(os.path.expanduser(a.out)) if a.out else None
    folder = os.path.commonpath([os.path.dirname(f) for f in files])
    t0 = time.time()
    err = e.start({"folder": folder, "height": a.quality, "codec": a.codec, "fps": a.fps,
                   "parallel": a.parallel, "hwaccel": a.gpu})
    if err:
        print(err, file=sys.stderr)
        return 2
    shown = 0
    while True:
        running = e.running
        with e.lock:
            lines = e.loglines[shown:]
        for t, m in lines:
            print(time.strftime("%H:%M:%S ", time.localtime(t)) + m, file=sys.stderr, flush=True)
        shown += len(lines)
        if not running:
            break
        time.sleep(0.25)
    with e.lock:
        result = [{"source": it["path"], "proxy": it["out"], "status": it["status"], "error": it.get("err", "")}
                  for it in e.items.values()]
    print(json.dumps({"seconds": round(time.time() - t0, 2), "files": result}, indent=1, ensure_ascii=False))
    return 1 if any(r["status"] == "FAILED" for r in result) else 0


def main():
    global ENGINE
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    url = f"http://{HOST}:{PORT}/"
    if already_running(url):
        print(f"Proxy Maker is already running — opening {url}")
        webbrowser.open(url)
        return
    ENGINE = Engine()
    if not ENGINE.tools:
        print("WARNING: ffmpeg / ffprobe were not found. Install them:  winget install --id Gyan.FFmpeg -e")
    ENGINE.refresh_list()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"Proxy Maker is running at {url}")
    print("Keep this window open while converting. Close it (or press Ctrl+C) to quit.")
    if not os.environ.get("PROXY_MAKER_NO_BROWSER"):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ENGINE.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
