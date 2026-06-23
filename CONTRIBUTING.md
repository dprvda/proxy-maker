# Contributing

Thanks for your interest in Proxy Maker! It's a small, focused tool, and contributions are welcome.

## Reporting a problem

The fastest way: run the app, reproduce the issue, then click **Copy for AI** (or **Copy log**) at the bottom of the window and paste it into a new [issue](../../issues). That text already includes your OS, Python/ffmpeg versions, your settings, the file statuses, and the exact error — everything needed to diagnose it.

## Suggesting a feature

Open an issue describing the use case. Please keep in mind the tool deliberately aims to stay **simple and one‑click** for non‑technical users — features that add a lot of UI or configuration are a hard sell.

## Development

There's no build step. You only need:

- Python 3.8+
- ffmpeg + ffprobe on your PATH

Run it directly:

```bash
python make_proxies.py
```

Everything lives in a single file — `make_proxies.py` (the Tkinter GUI and the ffmpeg engine). Please keep it **dependency‑free** (standard library only) so users never have to touch `pip`.

A good way to test the crash‑safe path without waiting on hour‑long files: temporarily set `CHUNK_SECS` small (e.g. `5`), point it at a short clip, and kill it mid‑run — then re‑run and confirm it resumes from the last saved chunk.

## Pull requests

- Keep changes focused and match the existing style.
- Test on **real footage**, especially long files — the checkpoint/resume path is the heart of the tool.
- If you change behavior or options, update `README.md` and `README.txt` in the same PR.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
