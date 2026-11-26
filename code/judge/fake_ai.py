import argparse
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(judge_url):
    class Handler(BaseHTTPRequestHandler):
        def _post_judge(self, path, data):
            request = urllib.request.Request(
                judge_url.rstrip("/") + path,
                data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.loads(response.read(65536))
            if result.get("ok") is not True:
                raise ValueError("judge rejected request")

        def _json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/desk/event":
                self._json(404, {"ok": False})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if length < 0 or length > 65536:
                    raise ValueError("invalid length")
                event = json.loads(self.rfile.read(length))
                required = {"schema_version", "kind", "event_id", "ts", "game", "event",
                            "importance", "desc", "board"}
                if not isinstance(event, dict) or not required <= set(event):
                    raise ValueError("invalid event")
                if event["kind"] not in {"desk.event", "desk.direct"}:
                    raise ValueError("invalid kind")
                if not isinstance(event["desc"], str):
                    raise ValueError("invalid desc")
                if event["kind"] == "desk.direct" and "image_path" not in event:  # auto may carry image_path too
                    raise ValueError("invalid image_path")
                reply = {"event_id": event["event_id"], "text": "收到：" + event["desc"]}
                self._post_judge("/v1/status", {"idle": False})
                self._post_judge("/v1/reply", reply)
                self._post_judge("/v1/status", {"idle": True})
                self._json(200, {"ok": True})
            except (ValueError, OSError, json.JSONDecodeError):
                self._json(400, {"ok": False})

        def log_message(self, format, *args):
            pass

    return Handler


def make_server(judge_url, host="127.0.0.1", port=8798):
    if host != "127.0.0.1":
        raise ValueError("fake AI must bind 127.0.0.1")
    return ThreadingHTTPServer((host, port), make_handler(judge_url))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge-url", default="http://127.0.0.1:8833")
    parser.add_argument("--port", type=int, default=8798)
    args = parser.parse_args()
    server = make_server(args.judge_url, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
