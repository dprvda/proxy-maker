============================================================
  PROXY MAKER  —  crash-safe video proxies for Premiere Pro
============================================================

WHAT IT DOES
  Turns big, heavy video files into small "proxy" copies that
  play back smoothly while editing. The audio is copied from the
  original EXACTLY, and the frame rate is kept the same, so the
  proxies attach correctly in Premiere Pro.

  It is CRASH-SAFE with CHECKPOINTS: each video is converted in short
  (~2 minute) chunks that are saved as it goes. If the PC crashes, the
  power goes out, or you close it early, just run it again — it keeps every
  finished chunk and continues from where it stopped, so you barely lose
  any work. Finished proxies are never lost.


------------------------------------------------------------
ONE-TIME SETUP  (only needed once per computer)
------------------------------------------------------------
This program needs two free tools installed:

  1) Python      https://www.python.org/downloads/
                 During install, TICK the box "Add python.exe to PATH".

  2) ffmpeg      Open PowerShell and run:
                     winget install --id Gyan.FFmpeg -e
                 Then close and reopen PowerShell once.

If either is missing, START.bat will tell you when you run it.


------------------------------------------------------------
HOW TO USE  (the 1 button)
------------------------------------------------------------
  1) Double-click  START.bat
  2) Click "Browse…" and pick the folder with your videos
  3) Click  ▶ START

That's it. Proxies appear in a "proxies" subfolder inside the
folder you picked. You can close the window any time and run it
again later to continue.


------------------------------------------------------------
THE OPTIONS (sensible defaults are already set)
------------------------------------------------------------
  Quality       360 / 540 / 720 / Original  — the proxy's height in pixels.
                Lower = smaller files + lighter playback. 540 is a
                good default. (To play smoother, lower THIS — not
                the frame rate.)  Original = keep the full source
                resolution — a full-quality transcode, not a small proxy.

  Codec         ProRes Proxy = recommended default; works on all files.
                ProRes 422 LT = higher quality than Proxy, larger files.
                DNxHR LB = a bit smaller + faster, but can fail on rare complex
                frames. If it does, THAT file is redone automatically in ProRes
                Proxy so the run still finishes (the folder may then hold a mix
                of both, which is perfectly fine).
                (The codec is part of the output filename, so different
                codecs/qualities never overwrite each other.)

  At once       How many files to convert simultaneously. 2-3 is
                usually best on a laptop.

  Frame rate    "Keep original"  = required for Premiere (default).
                30 / 15          = fewer frames, lighter to PLAY in
                                   After Effects, BUT these will NOT
                                   attach as proxies in Premiere Pro.
                                   Only use for AE or offline editing.

  GPU decode    Optional. May speed things up on some laptops by
                using the graphics chip to read the video. Harmless
                to leave off; try it if conversion feels slow.

  include subfolders   Also convert videos in folders inside the
                       chosen folder.


------------------------------------------------------------
IF SOMETHING GOES WRONG  (get instant help)
------------------------------------------------------------
  At the bottom of the window there is a Log, and two buttons:

    "Copy log"      Copies the log text so you can paste it anywhere.

    "Copy for AI"   Copies a ready-made help message that already explains
                    the whole program, your settings, and the exact error.
                    Then:
                      1) Open ChatGPT or Claude in your web browser
                      2) Click the message box and paste with  Ctrl + V
                      3) Press Enter
                    The AI will tell you, in simple steps, how to fix it.

  Nothing is lost when something fails: fix the cause, press START again,
  and it continues from where it stopped (already-finished files are skipped).


------------------------------------------------------------
USING THE PROXIES IN PREMIERE PRO
------------------------------------------------------------
  - In the Project panel, select your clips.
  - Right-click -> Proxy -> Attach Proxies -> Add, point to the
    matching file in the "proxies" folder.
  - Toggle the proxy on/off with the Toggle Proxies button in the
    Program monitor (add it via the "+" button editor if needed).
  - On export, Premiere automatically uses the full-resolution
    originals — proxies are for playback only.

  If playback is still not smooth: set the Program monitor's
  Playback Resolution dropdown to 1/2 or 1/4, and make sure
  Project Settings -> General -> Renderer is the GPU option.


------------------------------------------------------------
GOOD TO KNOW
------------------------------------------------------------
  - While converting, temporary chunks live in  proxies\.chunks\  and are
    deleted automatically as each file finishes. A file briefly needs about
    2x its final size on disk during the final stitch step.
  - Output files are named  <name>_proxy_<quality>_<codec>[_<fps>fps].mov
    e.g.  CAM A_proxy_540p_prores.mov  or  CAM A_proxy_full_lt_30fps.mov
  - Incomplete downloads (".part" files) are ignored automatically.
  - A log of any failures is written to  proxies\proxy_log.txt
  - To re-make proxies at a different quality, just change Quality
    and run again (different settings get different file names, so
    they won't overwrite each other).
============================================================
