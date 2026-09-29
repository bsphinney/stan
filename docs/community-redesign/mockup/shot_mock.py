"""Screenshot the community mockup in headless Chrome over CDP.

Wraps community_mockup.html in the same skeleton the Artifact host adds
(doctype + charset + viewport), loads it, runs optional JS, reports console
errors and horizontal overflow, and writes the page as vertical slices.

usage: python shot_mock.py WIDTH OUTPREFIX [JS_TO_RUN] [MAX_SLICES] [SLICE_H]
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = Path(__file__).resolve().parent
PORT = 9341


def main() -> None:
    width = int(sys.argv[1])
    prefix = sys.argv[2]
    js = sys.argv[3] if len(sys.argv) > 3 else ""
    max_slices = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    slice_h = int(sys.argv[5]) if len(sys.argv) > 5 else 1800
    wrapped = HERE / "_preview.html"
    wrapped.write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"></head><body>'
                       + (HERE / "community_mockup.html").read_text() + "</body></html>")
    prof = HERE / "_chrome_prof"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                             f"--remote-debugging-port={PORT}", f"--remote-allow-origins=http://127.0.0.1:{PORT}",
                             f"--user-data-dir={prof}", f"--window-size={width},1000", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.25)
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        mid = [0]
        logs: list[str] = []

        def call(method, **params):
            mid[0] += 1
            ws.send(json.dumps({"id": mid[0], "method": method, "params": params}))
            while True:
                m = json.loads(ws.recv())
                if m.get("method") == "Runtime.consoleAPICalled":
                    logs.append(m["params"]["type"] + ": " + " ".join(str(x.get("value", x.get("description", ""))) for x in m["params"]["args"]))
                elif m.get("method") == "Runtime.exceptionThrown":
                    logs.append("EXCEPTION: " + json.dumps(m["params"]["exceptionDetails"])[:700])
                elif m.get("method") == "Log.entryAdded":
                    e = m["params"]["entry"]
                    if e.get("level") in ("error", "warning"):
                        logs.append(f"LOG {e.get('level')}: {e.get('text', '')[:300]}")
                if m.get("id") == mid[0]:
                    return m.get("result", {})

        call("Runtime.enable")
        call("Log.enable")
        call("Page.enable")
        call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=width < 600)
        call("Page.navigate", url=wrapped.as_uri())
        for _ in range(60):
            r = call("Runtime.evaluate", expression="document.readyState === 'complete'", returnByValue=True)
            if r.get("result", {}).get("value") is True:
                break
            time.sleep(0.25)
        time.sleep(1.0)
        if js:
            r = call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
            if "exceptionDetails" in r:
                logs.append("JS-ARG EXCEPTION: " + json.dumps(r["exceptionDetails"])[:500])
            else:
                print("JS result:", r.get("result", {}).get("value"))
            time.sleep(1.0)
        info = call("Runtime.evaluate", returnByValue=True, expression="""(() => {
            const wide = [...document.querySelectorAll('body *')].filter(e => { const r = e.getBoundingClientRect(); return r.right > window.innerWidth + 1 && !e.closest('.scrollx') && getComputedStyle(e).position !== 'fixed'; }).slice(0, 8).map(e => e.tagName + '.' + e.className + '#' + e.id + ' right=' + Math.round(e.getBoundingClientRect().right));
            return {docH: document.documentElement.scrollHeight, scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth, overflowX: document.documentElement.scrollWidth > window.innerWidth, wide};
        })()""")["result"]["value"]
        print(json.dumps(info))
        h = info["docH"]
        y, i = 0, 0
        while y < h and i < max_slices:
            ch = min(slice_h, h - y)
            shot = call("Page.captureScreenshot", format="png", clip={"x": 0, "y": y, "width": width, "height": ch, "scale": 1}, captureBeyondViewport=True)
            out = HERE / f"{prefix}_{i:02d}.png"
            out.write_bytes(base64.b64decode(shot["data"]))
            print("wrote", out.name, y, ch)
            y += ch
            i += 1
        print("console:", "(none)" if not logs else "")
        for line in logs:
            print("  ", line[:600])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
