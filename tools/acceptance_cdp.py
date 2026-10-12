"""Small CDP bridge for recording acceptance interactions through the UI."""

from __future__ import annotations

import argparse
import base64
import json

import requests
import websocket


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9307)
    parser.add_argument("--url-prefix", default="http://127.0.0.1:8000/")
    parser.add_argument("--expr", required=True)
    parser.add_argument("--screenshot")
    args = parser.parse_args()

    targets = requests.get(f"http://127.0.0.1:{args.port}/json/list", timeout=5).json()
    page = next(item for item in targets if item.get("type") == "page" and item.get("url", "").startswith(args.url_prefix))
    socket = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True, timeout=15)
    socket.send(json.dumps({
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {"expression": args.expr, "returnByValue": True, "awaitPromise": True},
    }))
    result = json.loads(socket.recv())
    if "exceptionDetails" in result.get("result", {}).get("result", {}):
        raise RuntimeError(json.dumps(result["result"]["result"]["exceptionDetails"], ensure_ascii=False))
    value = result.get("result", {}).get("result", {}).get("value")
    print(json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value.encode("unicode_escape").decode("ascii"))
    if args.screenshot:
        socket.send(json.dumps({"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png", "captureBeyondViewport": True}}))
        shot = json.loads(socket.recv())
        with open(args.screenshot, "wb") as output:
            output.write(base64.b64decode(shot["result"]["data"]))
    socket.close()


if __name__ == "__main__":
    main()
