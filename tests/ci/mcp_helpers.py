"""Small synchronous client for the deployed HTTP MCP integration tests."""

import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager

import requests


class MCPClient:
    def __init__(self, url):
        self.url = url
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
        })
        self.sequence = 0

    def initialize(self):
        result = self.rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "maas-ci", "version": "1"},
        })
        assert result.get("protocolVersion") == "2024-11-05", "Unsupported MCP version"
        assert "tools" in result.get("capabilities", {}), "MCP server has no tools"
        self.rpc("notifications/initialized", notification=True)

    def rpc(self, method, params=None, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.sequence
        if params is not None:
            body["params"] = params
        with self.session.post(self.url, json=body, stream=True,
                               timeout=(10, 30), allow_redirects=False) as response:
            assert response.status_code in (200, 202, 204), (
                f"MCP {method}: HTTP {response.status_code}"
            )
            sid = response.headers.get("Mcp-Session-Id")
            if sid:
                self.session.headers["Mcp-Session-Id"] = sid
            if notification:
                return None
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                message = None
                data = []
                started = time.monotonic()
                # Stop at the matching event: a server may keep the stream open.
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    assert time.monotonic() - started < 30, f"MCP {method}: stream timed out"
                    if isinstance(line, bytes):
                        line = line.decode("utf-8")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        event = json.loads("\n".join(data))
                        data = []
                        if event.get("id") == body["id"]:
                            message = event
                            break
                assert message is not None, f"MCP {method}: no matching SSE response"
            else:
                message = response.json()
            assert message.get("id") == body["id"], f"MCP {method}: mismatched response ID"
            assert "error" not in message, f"MCP {method}: JSON-RPC error"
            assert "result" in message, f"MCP {method}: missing result"
            return message["result"]

    def tools(self):
        tools, cursor, seen = {}, None, set()
        for _ in range(100):
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            for tool in result["tools"]:
                assert tool["name"] not in tools, "Duplicate MCP tool"
                tools[tool["name"]] = tool
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            assert cursor not in seen, "MCP pagination cursor repeated"
            seen.add(cursor)
        raise AssertionError("MCP tool pagination exceeded limit")

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), f"MCP tool {name} failed"
        return result

    def close(self):
        try:
            if "Mcp-Session-Id" in self.session.headers:
                with self.session.delete(self.url, timeout=(10, 10),
                                         allow_redirects=False) as response:
                    assert response.status_code in (200, 202, 204, 404, 405), (
                        f"MCP session termination: HTTP {response.status_code}"
                    )
        finally:
            self.session.close()


def tool_json(result):
    """Decode the JSON text returned by the deployed tools."""
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    text = "\n".join(c["text"] for c in result.get("content", []) if c.get("type") == "text")
    return json.loads(text)


@contextmanager
def forwarded_service(service, port, namespace="lls-demo"):
    # oc selects and reports an available port; do not reserve/release one ourselves.
    with tempfile.TemporaryFile(mode="w+t") as output:
        process = subprocess.Popen([
            "oc", "--kubeconfig", os.environ["VP_HUBCONFIG"], "port-forward",
            "--address=127.0.0.1", "-n", namespace, f"service/{service}", f":{port}",
        ], stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                output.seek(0)
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", output.read())
                if match:
                    yield f"http://127.0.0.1:{match[1]}"
                    return
                assert process.poll() is None, f"Port-forward for {service} exited"
                time.sleep(0.1)
            raise AssertionError(f"Port-forward for {service} did not start within 30s")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@contextmanager
def forwarded_mcp(service, port):
    with forwarded_service(service, port) as url:
        mcp = MCPClient(f"{url}/mcp")
        try:
            mcp.initialize()
            yield mcp
        finally:
            mcp.close()


def model_tool_roundtrip(session, endpoint, mcp, tool, arguments, prompt, marker):
    """Require automatic tool use and an answer grounded in a withheld marker."""
    __tracebackhide__ = True
    messages = [
        {"role": "system", "content": "Use the provided tool to read the requested data. "
         "Then answer with the exact marker value. Do not invent data."},
        {"role": "user", "content": prompt},
    ]
    definition = {"type": "function", "function": {
        "name": tool["name"], "description": tool.get("description", ""),
        "parameters": tool["inputSchema"],
    }}
    executions = 0
    for _ in range(3):
        # The free subscription includes a 1000-token/second window. Separate
        # requests instead of retrying inference (which can hide model failures).
        time.sleep(1.1)
        response = session.post(
            f"{endpoint['url']}/v1/chat/completions",
            json={"model": endpoint["model"], "messages": messages, "tools": [definition],
                  "tool_choice": "auto", "temperature": 0, "max_tokens": 4096,
                  "stream": False},
            timeout=(10, 120), allow_redirects=False,
        )
        assert response.status_code == 200, f"Tool-calling inference: HTTP {response.status_code}"
        result = response.json()
        assert result.get("model") == endpoint["model"], "Unexpected inference model"
        choice = result["choices"][0]
        assert choice.get("finish_reason") != "length", "Tool-calling response truncated"
        answer = choice["message"]
        calls = answer.get("tool_calls") or []
        if not calls:
            assert executions, "Model answered without executing a tool"
            assert marker in (answer.get("content") or ""), "Model omitted the backend marker"
            return
        messages.append({"role": "assistant", "content": answer.get("content"), "tool_calls": calls})
        ids = set()
        for call in calls:
            executions += 1
            assert executions <= 4, "Model exceeded four tool executions"
            assert call.get("type") == "function" and call.get("id"), "Malformed tool call"
            assert call["id"] not in ids, "Duplicate tool-call ID"
            ids.add(call["id"])
            function = call["function"]
            assert function["name"] == tool["name"], "Model requested an unexpected tool"
            supplied = json.loads(function["arguments"])
            assert supplied == arguments, "Model requested unexpected tool arguments"
            observed = mcp.call(tool["name"], supplied)
            assert marker in json.dumps(observed), "MCP result omitted the backend marker"
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(observed)})
    raise AssertionError("Model did not finish within three inference requests")
