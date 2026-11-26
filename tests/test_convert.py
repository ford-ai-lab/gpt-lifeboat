"""Run with:  python3 -m unittest discover -s tests -v

The converter tests need only the standard library. The tests that start the generated
MCP server need the MCP Python SDK version 2 (pip install "mcp>=2,<3") and are skipped without it.
"""

import asyncio
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gpt_lifeboat.cli import main as cli_main  # noqa: E402
from gpt_lifeboat.convert import DefinitionError, convert  # noqa: E402

SAMPLES = ROOT / "samples"
EXPECTED_TOOLS = {
    "brand-voice-editor": [],
    "parcel-desk": ["getShipment", "searchShipments", "redirectShipment", "listPickupPoints"],
    "studio-booking-concierge": ["list-classes", "listMyBookings", "createBooking", "cancelBooking", "updateBooking"],
}

try:
    import mcp  # noqa: F401
    from mcp import Client, StdioServerParameters

    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False
try:
    import yaml

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def load(name):
    return json.loads((SAMPLES / (name + ".json")).read_text(encoding="utf-8"))


def frontmatter(text):
    """Minimal reader for the frontmatter this tool writes; PyYAML is used as a second opinion if present."""
    assert text.startswith("---\n"), "frontmatter must start on the first line"
    block = text[4:].split("\n---\n", 1)[0]
    fields = {}
    for line in block.splitlines():
        if line and not line.startswith(" "):
            key, _, value = line.partition(":")
            value = value.strip()
            fields[key] = json.loads(value) if value.startswith('"') else value
    if HAVE_YAML:
        parsed = yaml.safe_load(block)
        assert parsed["name"] == fields["name"] and parsed["description"] == fields["description"]
    return fields


class RpcServer:
    """Speak MCP's newline-delimited JSON-RPC to a server subprocess over stdio."""

    def __init__(self, server_py, env=None):
        self.proc = subprocess.Popen(
            [sys.executable, str(server_py)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, **(env or {})},
        )
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited: " + self.proc.stderr.read())
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                return reply

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def handshake(self):
        reply = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        self.notify("notifications/initialized")
        return reply

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()


class StubApi:
    """A local HTTP server standing in for the real API; records what it receives."""

    def __init__(self):
        self.seen = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.seen.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(length).decode()})
                if "missing" in self.path:
                    self.send_response(404)
                    payload = b'{"error":"not found"}'
                else:
                    self.send_response(200)
                    payload = b'{"status":"in_transit","eta":"2026-12-01"}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v2" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_samples_produce_valid_skill_prompt_and_report(self):
        for name, tools in EXPECTED_TOOLS.items():
            with self.subTest(sample=name):
                out = self.out / name
                result = convert(load(name), out)
                self.assertEqual(result["tools"], tools)
                skill_dir = out / "skill" / result["slug"]
                text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
                fields = frontmatter(text)
                self.assertRegex(fields["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(fields["name"]), 64)
                self.assertEqual(fields["name"], skill_dir.name, "name must match the folder (Agent Skills spec)")
                self.assertTrue(0 < len(fields["description"]) <= 1024)
                self.assertLess(len(text.splitlines()), 500)
                self.assertIn(load(name)["instructions"], text)
                for link in re.findall(r"\]\((references/[^)]+)\)", text):
                    self.assertTrue((skill_dir / link).is_file(), link)
                prompt = (out / "system-prompt.txt").read_text(encoding="utf-8")
                self.assertTrue(prompt.startswith(load(name)["instructions"]))
                report = (out / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
                self.assertIn("Knowledge files are listed but their contents were not available", report)
                self.assertEqual((out / "mcp-server").exists(), bool(tools))

    def test_report_lists_what_was_not_converted(self):
        convert(load("studio-booking-concierge"), self.out)
        report = (self.out / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
        for needle in ("OAuth", "multipart/form-data", "Code Interpreter", "{tenant}", "NOT converted"):
            self.assertIn(needle, report)
        convert(load("brand-voice-editor"), self.out / "b")
        report = (self.out / "b" / "MIGRATION_REPORT.md").read_text(encoding="utf-8")
        for needle in ("Image generation", "Canvas", "Web search", "No MCP server"):
            self.assertIn(needle, report)

    def test_refs_are_inlined_and_nullable_converted(self):
        convert(load("parcel-desk"), self.out)
        config = json.loads((self.out / "mcp-server" / "operations.json").read_text())
        self.assertNotIn("$ref", json.dumps(config))
        redirect = next(op for op in config["operations"] if op["tool"] == "redirectShipment")
        address = redirect["input_schema"]["properties"]["body"]["properties"]["address"]
        self.assertEqual(address["properties"]["line2"]["type"], ["string", "null"])
        self.assertEqual(redirect["input_schema"]["required"], ["tracking_number", "body"])
        self.assertEqual(redirect["arg_map"]["Idempotency-Key"], {"in": "header", "name": "Idempotency-Key"})

    def test_secrets_in_the_input_are_never_written_out(self):
        gpt = load("parcel-desk")
        gpt["actions"][0]["auth"]["api_key"] = "sk-live-DO-NOT-LEAK-12345"
        convert(gpt, self.out)
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn("DO-NOT-LEAK", path.read_text(encoding="utf-8"), path)
        self.assertIn("secret value", (self.out / "MIGRATION_REPORT.md").read_text())

    def test_knowledge_dir_is_copied(self):
        source = self.out / "knowledge"
        source.mkdir()
        (source / "returns-policy.md").write_text("Returns within 30 days.\n")
        convert(load("parcel-desk"), self.out / "o", knowledge_dir=source)
        copied = self.out / "o" / "skill" / "parcel-desk" / "references" / "knowledge" / "returns-policy.md"
        self.assertEqual(copied.read_text(), "Returns within 30 days.\n")
        self.assertNotIn("contents were not available", (self.out / "o" / "MIGRATION_REPORT.md").read_text())

    def test_bad_input_gives_a_clear_error(self):
        with self.assertRaises(DefinitionError):
            convert({"name": "x"}, self.out)
        result = convert({"name": "X", "instructions": "Do x.", "actions": [{"schema": "{\"openapi\": \"3.1.0\"}"}]}, self.out)
        self.assertEqual(result["tools"], [])
        self.assertIn("was NOT converted", (self.out / "MIGRATION_REPORT.md").read_text())

    def test_cli_with_pasted_pieces(self):
        (self.out / "i.txt").write_text("Answer questions about parcels using getShipment.\n")
        schema = load("parcel-desk")["actions"][0]["schema"]
        (self.out / "s.json").write_text(json.dumps(schema))
        code = cli_main(["convert", "--name", "My Parcel GPT!", "--instructions", str(self.out / "i.txt"),
                         "--schema", str(self.out / "s.json"), "--auth", "custom:X-ParcelDesk-Key", "-o", str(self.out / "o")])
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "o" / "skill" / "my-parcel-gpt" / "SKILL.md").is_file())
        config = json.loads((self.out / "o" / "mcp-server" / "operations.json").read_text())
        self.assertEqual(config["actions"][0]["auth"], {"type": "api_key", "auth_type": "custom", "custom_header_name": "X-ParcelDesk-Key"})

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_yaml_schema(self):
        gpt = load("parcel-desk")
        gpt["actions"][0]["schema"] = yaml.safe_dump(gpt["actions"][0]["schema"], sort_keys=False)
        self.assertEqual(convert(gpt, self.out)["tools"], EXPECTED_TOOLS["parcel-desk"])


class GeneratedServerRequestTests(unittest.TestCase):
    """build_request in the generated server, without any network or SDK."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        convert(load("parcel-desk"), self.tmp.name)
        server_dir = Path(self.tmp.name) / "mcp-server"
        import importlib.util

        spec = importlib.util.spec_from_file_location("generated_server", server_dir / "server.py")
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.config = self.server.load_config(server_dir / "operations.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_is_built_with_auth_from_env(self):
        env = {"ACTION1_API_KEY": "k123"}
        method, url, headers, data = self.server.build_request(
            self.config, "redirectShipment",
            {"tracking_number": "PD/1 2", "Idempotency-Key": "abc", "body": {"pickup_point_id": "pp9"}}, env)
        self.assertEqual((method, url), ("POST", "https://api.parceldesk.example/v2/shipments/PD%2F1%202/redirect"))
        self.assertEqual(headers["X-ParcelDesk-Key"], "k123")
        self.assertEqual(headers["Idempotency-Key"], "abc")
        self.assertEqual(json.loads(data), {"pickup_point_id": "pp9"})
        _, url, _, _ = self.server.build_request(
            self.config, "searchShipments", {"email": "a@b.co", "status": ["in_transit", "delivered"], "limit": 5}, env)
        self.assertEqual(url, "https://api.parceldesk.example/v2/shipments?email=a%40b.co&status=in_transit&status=delivered&limit=5")

    def test_missing_key_or_argument_is_refused(self):
        with self.assertRaisesRegex(self.server.ToolInputError, "ACTION1_API_KEY"):
            self.server.build_request(self.config, "getShipment", {"tracking_number": "PD-1"}, {})
        with self.assertRaisesRegex(self.server.ToolInputError, "tracking_number"):
            self.server.build_request(self.config, "getShipment", {}, {"ACTION1_API_KEY": "k"})


@unittest.skipUnless(HAVE_MCP, 'MCP SDK not installed (pip install "mcp>=2,<3")')
class GeneratedServerLiveTests(unittest.TestCase):
    """Start each generated server as a real process and talk MCP to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stdio_server_lists_its_tools(self):
        for name, tools in EXPECTED_TOOLS.items():
            if not tools:
                continue
            with self.subTest(sample=name):
                convert(load(name), self.out / name)
                rpc = RpcServer(self.out / name / "mcp-server" / "server.py")
                try:
                    init = rpc.handshake()
                    self.assertIn("tools", init["result"]["capabilities"])
                    listed = rpc.request("tools/list")["result"]["tools"]
                finally:
                    rpc.close()
                self.assertEqual([t["name"] for t in listed], tools)
                for tool in listed:
                    self.assertEqual(tool["inputSchema"]["type"], "object")
                    self.assertTrue(tool["description"])

    def test_tool_call_reaches_the_api_with_the_key_from_env(self):
        convert(load("parcel-desk"), self.out)
        api = StubApi()
        rpc = RpcServer(self.out / "mcp-server" / "server.py", {"ACTION1_BASE_URL": api.url, "ACTION1_API_KEY": "test-key-1"})
        try:
            rpc.handshake()
            ok = rpc.request("tools/call", {"name": "getShipment", "arguments": {"tracking_number": "PD-48213377", "include_events": True}})
            bad = rpc.request("tools/call", {"name": "getShipment", "arguments": {"tracking_number": "missing"}})
            invalid = rpc.request("tools/call", {"name": "getShipment", "arguments": {}})
        finally:
            rpc.close()
            api.close()
        self.assertEqual(api.seen[0]["path"], "/v2/shipments/PD-48213377?include_events=true")
        self.assertEqual(api.seen[0]["headers"]["x-parceldesk-key"], "test-key-1")
        self.assertFalse(ok["result"].get("isError"))
        self.assertIn("in_transit", ok["result"]["content"][0]["text"])
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("404", bad["result"]["content"][0]["text"])
        self.assertTrue("error" in invalid or invalid["result"].get("isError"))
        self.assertEqual(len(api.seen), 2)

    def test_official_sdk_client_over_stdio(self):
        convert(load("studio-booking-concierge"), self.out)

        async def run():
            params = StdioServerParameters(command=sys.executable, args=[str(self.out / "mcp-server" / "server.py")])
            async with Client(params) as client:
                return [tool.name for tool in (await client.list_tools()).tools]

        self.assertEqual(asyncio.run(run()), EXPECTED_TOOLS["studio-booking-concierge"])

    def test_http_mode_lists_its_tools(self):
        convert(load("parcel-desk"), self.out)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        proc = subprocess.Popen([sys.executable, str(self.out / "mcp-server" / "server.py"), "--http", "--port", str(port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        async def run():
            async with Client("http://127.0.0.1:%d/mcp" % port) as client:
                return [tool.name for tool in (await client.list_tools()).tools]

        try:
            for _ in range(100):
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.1)
            self.assertEqual(asyncio.run(run()), EXPECTED_TOOLS["parcel-desk"])
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
