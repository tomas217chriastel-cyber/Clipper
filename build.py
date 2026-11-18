#!/usr/bin/env python3
"""
Builds a SELF-CONTAINED AutoClipper app with PyInstaller.
The result needs nothing installed: Python, ffmpeg, streamlink, yt-dlp, chat clients and the
Whisper subtitle model are all bundled.   (Run this on the OS you want the app for.)

    python build.py                 # bundles the 'small' subtitle model (~460 MB)
    python build.py --model base    # smaller app (~150 MB model)
    python build.py --model none    # no bundled model (downloads on first use)
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(*cmd):
    print(">", " ".join(cmd), flush=True)
    subprocess.check_call(list(cmd), cwd=HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small", help="tiny|base|small|medium|none")
    ap.add_argument("--skip-install", action="store_true")
    a = ap.parse_args()
    py, system = sys.executable, platform.system()

    if not a.skip_install:
        run(py, "-m", "pip", "install", "--upgrade", "pip")
        run(py, "-m", "pip", "install", "-r", "requirements.txt", "pyinstaller")

    models = os.path.join(HERE, "bundle", "models")
    os.makedirs(models, exist_ok=True)
    if a.model != "none":
        target = os.path.join(models, a.model)
        if not os.path.isfile(os.path.join(target, "model.bin")):
            print(f"Downloading Whisper '{a.model}' model for bundling...", flush=True)
            from faster_whisper import download_model
            download_model(a.model, output_dir=target)
        shutil.rmtree(os.path.join(target, ".cache"), ignore_errors=True)

    sep = os.pathsep
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "AutoClipper",
           "--add-data", f"ui.html{sep}.", "--add-data", f"fonts{sep}fonts",
           "--add-data", f"bundle/models{sep}models"]
    if system in ("Windows", "Darwin"):
        cmd.append("--windowed")
    for pkg in ["streamlink", "yt_dlp", "faster_whisper", "ctranslate2", "onnxruntime", "av", "tokenizers",
                "pytchat", "curl_cffi", "imageio_ffmpeg", "websockets"] + (["webview"] if system != "Linux" else []):
        cmd += ["--collect-all", pkg]
    cmd += ["app.py"]
    run(*cmd)

    dist = os.path.join(HERE, "dist")
    tag = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[system] + "-" + platform.machine().lower()
    if system == "Windows":
        shutil.make_archive(os.path.join(dist, f"AutoClipper-{tag}"), "zip", dist, "AutoClipper")
    elif system == "Darwin":
        run("ditto", "-c", "-k", "--keepParent", "dist/AutoClipper.app", f"dist/AutoClipper-{tag}.zip")
    else:
        run("tar", "-czf", f"dist/AutoClipper-{tag}.tar.gz", "-C", "dist", "AutoClipper")
    print("\nDONE. Your app is in the 'dist' folder.")


if __name__ == "__main__":
    main()
