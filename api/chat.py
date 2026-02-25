"""Vercel serverless function — Trenchee chat proxy."""
import os, json
from http.server import BaseHTTPRequestHandler
import urllib.request

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SYSTEM = "You are Trenchee — the ultimate memecoin trenching companion built by someone who traded pump.fun full-time for 1.5 years profitably. You know: pump.fun meta, bonding curves, dev wallet patterns, bundle detection, KOL manipulation, chart reading, social signals, rug detection, entry/exit strategies. Speak like a seasoned degen — direct, no fluff, actionable. Keep responses concise (under 200 words). If given a Solana CA, tell them to use the scanner on trenchee.fun or message @trencheeagent_bot on Telegram for full analysis with real on-chain data."

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        messages = body.get("messages", [])[-10:]  # last 10 msgs

        if not ANTHROPIC_KEY:
            self._respond(500, {"error": "no key"})
            return

        payload = json.dumps({
            "model": "claude-3-haiku-20240307",
            "max_tokens": 600,
            "system": SYSTEM,
            "messages": messages,
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                reply = data.get("content", [{}])[0].get("text", "brain glitched")
                self._respond(200, {"reply": reply})
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _respond(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
