"""审核页后端 — 简单 HTTP Server"""
import json
import io
import sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Windows UTF-8 修复
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"


class ReviewHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/history"):
            files = sorted(OUTPUT_DIR.glob("*.json"), reverse=True)
            history = []
            for f in files[:30]:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    history.append({
                        "date": data.get("date", ""),
                        "title": data.get("post", {}).get("title", ""),
                        "status": data.get("status", "pending_review")
                    })
                except:
                    pass
            self._send_json({"history": history})
        elif self.path == "/" or self.path.startswith("/review"):
            # /review/20260615 或 / → 最新
            date = self.path.replace("/review/", "").replace("/review", "").lstrip("/")
            if not date:
                # 找最新的 JSON
                files = sorted(OUTPUT_DIR.glob("*.json"), reverse=True)
                if files:
                    with open(files[0], encoding="utf-8") as f:
                        date = json.load(f).get("date", "")
            html_path = OUTPUT_DIR / f"{date}.html"
            if html_path.exists():
                content = html_path.read_text(encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"未找到 {date} 的草稿".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        if self.path == "/api/save":
            self._handle_save(body)
        elif self.path == "/api/approve":
            self._handle_status(body, "approved")
        elif self.path == "/api/delete":
            self._handle_status(body, "deleted")
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_save(self, body):
        date = body.get("date", "")
        json_path = OUTPUT_DIR / f"{date}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            data["post"]["title"] = body.get("title", "")
            data["post"]["content"] = body.get("content", "")
            data["post"]["tags"] = body.get("tags", [])
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json({"status": "saved"})
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_status(self, body, status):
        date = body.get("date", "")
        json_path = OUTPUT_DIR / f"{date}.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            data["status"] = status
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._send_json({"status": status})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        pass  # 静默


if __name__ == "__main__":
    port = 8776
    server = HTTPServer(("127.0.0.1", port), ReviewHandler)
    print(f"📡 审核服务运行在 http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 关闭")
