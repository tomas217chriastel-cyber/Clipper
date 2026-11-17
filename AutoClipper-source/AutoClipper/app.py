"""AutoClipper launcher: starts the engine + local UI server and opens the app window."""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import autoclipper as ac
import server


def _existing_instance(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == "autoclipper"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="AutoClipper")
    ap.add_argument("urls", nargs="*", help="optional channel links to add on start")
    ap.add_argument("--headless", action="store_true", help="no window; run as a background service")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if sys.stdout is None or sys.stderr is None:  # windowed build: no console
        sys.stdout = sys.stderr = open(os.path.join(ac.data_dir(), "app.log"), "a", buffering=1, encoding="utf8")

    if _existing_instance(args.port):  # already running -> just show it
        webbrowser.open(f"http://127.0.0.1:{args.port}/")
        return

    engine = server.Engine()
    for u in args.urls:
        try:
            engine.add_channel(u)
        except ValueError as e:
            ac.log("main", str(e))
    done = threading.Event()
    srv, port = server.serve(engine, args.port, quit_hook=done.set)
    engine.start_all()
    url = f"http://127.0.0.1:{port}/"
    ac.log("main", f"AutoClipper running at {url}")

    if not args.headless:
        try:
            import webview  # native window (WebView2 / WKWebView)

            webview.create_window("AutoClipper", url, width=1240, height=820, min_size=(900, 600))
            webview.start()  # returns when the window is closed
            done.set()
        except Exception as e:
            ac.log("main", f"native window unavailable ({e}); opening browser instead")
            webbrowser.open(url)
    try:
        while not done.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    engine.stop_all()
    time.sleep(0.5)
    os._exit(0)


if __name__ == "__main__":
    main()
