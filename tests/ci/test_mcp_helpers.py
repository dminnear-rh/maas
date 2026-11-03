"""Protocol and tool-loop regression tests; no cluster or model required."""

import json

import pytest

from .mcp_helpers import MCPClient, model_tool_roundtrip


class Response:
    status_code = 200

    def __init__(self, body=None, headers=None, events=()):
        self.body = body
        self.headers = headers or {}
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        return self.body

    def iter_lines(self, **kwargs):
        yield from self.events


def test_mcp_json_session_and_tool_error(monkeypatch):
    mcp = MCPClient('http://localhost/mcp')
    seen = []

    def post(url, **kwargs):
        seen.append(kwargs['json'])
        return Response({'id': len(seen), 'result': {'isError': len(seen) == 2}},
                        {'Mcp-Session-Id': 'session'})

    monkeypatch.setattr(mcp.session, 'post', post)
    assert mcp.rpc('tools/list') == {'isError': False}
    assert mcp.session.headers['Mcp-Session-Id'] == 'session'
    with pytest.raises(AssertionError, match='tool broken failed'):
        mcp.call('broken', {})
    mcp.session.close()


def test_mcp_sse_skips_notifications_and_stops_at_response(monkeypatch):
    mcp = MCPClient('http://localhost/mcp')

    def events():
        yield ': keepalive'
        yield 'data: {"method":"notifications/message"}'
        yield ''
        yield 'data: {"id":1,'
        yield 'data: "result":{"tools":[]}}'
        yield ''
        raise AssertionError('Client continued reading a persistent stream')

    monkeypatch.setattr(mcp.session, 'post', lambda *a, **k: Response(
        headers={'Content-Type': 'text/event-stream'}, events=events()))
    assert mcp.rpc('tools/list') == {'tools': []}
    mcp.session.close()


@pytest.mark.parametrize('reply', [{'id': 1, 'error': {'code': -1}},
                                   {'id': 7, 'result': {}}])
def test_mcp_rejects_protocol_errors(monkeypatch, reply):
    mcp = MCPClient('http://localhost/mcp')
    monkeypatch.setattr(mcp.session, 'post', lambda *a, **k: Response(reply))
    with pytest.raises(AssertionError):
        mcp.rpc('tools/list')
    mcp.session.close()


def test_tool_pagination(monkeypatch):
    mcp = MCPClient('http://localhost/mcp')
    pages = iter([{'tools': [{'name': 'one'}], 'nextCursor': 'next'},
                  {'tools': [{'name': 'two'}]}])
    calls = []

    def rpc(method, params):
        calls.append(params)
        return next(pages)

    monkeypatch.setattr(mcp, 'rpc', rpc)
    assert set(mcp.tools()) == {'one', 'two'}
    assert calls == [{}, {'cursor': 'next'}]
    mcp.session.close()


class ModelSession:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs['json'])))
        return Response({'model': 'model', 'choices': [
            {'message': next(self.answers), 'finish_reason': 'stop'}]})


def tool_call(name='read', arguments=None):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': 'call1', 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments or {'name': 'fixture'})},
    }]}


def run_model(session, mcp):
    model_tool_roundtrip(session, {'url': 'https://inference', 'model': 'model'}, mcp,
                         {'name': 'read', 'inputSchema': {'type': 'object'}},
                         {'name': 'fixture'}, 'Read the fixture', 'withheld-marker')


def test_model_uses_real_result_and_preserves_call_id():
    class MCP:
        def call(self, name, arguments):
            return {'content': [{'type': 'text', 'text': 'withheld-marker'}]}

    session = ModelSession([tool_call(), {'content': 'withheld-marker'}])
    run_model(session, MCP())
    assert 'withheld-marker' not in json.dumps(session.requests[0])
    assert session.requests[1]['messages'][-1]['tool_call_id'] == 'call1'
    assert 'withheld-marker' in session.requests[1]['messages'][-1]['content']


@pytest.mark.parametrize('answer', [tool_call('delete'), tool_call(arguments={'name': 'other'}),
                                     {'content': 'withheld-marker'}])
def test_model_rejects_unapproved_calls_and_answers_without_tools(answer):
    run = ModelSession([answer])
    with pytest.raises(AssertionError):
        run_model(run, None)  # Any attempt to execute would itself fail.


def test_model_conversation_is_bounded():
    class MCP:
        def call(self, *args):
            return {'content': [{'type': 'text', 'text': 'withheld-marker'}]}

    session = ModelSession([tool_call(), tool_call(), tool_call()])
    with pytest.raises(AssertionError, match='three inference requests'):
        run_model(session, MCP())
    assert len(session.requests) == 3


def test_model_tool_execution_budget():
    class MCP:
        calls = 0

        def call(self, *args):
            self.calls += 1
            return {'content': [{'type': 'text', 'text': 'withheld-marker'}]}

    answer = tool_call()
    answer['tool_calls'] = [dict(answer['tool_calls'][0], id=f'call{i}') for i in range(5)]
    mcp = MCP()
    with pytest.raises(AssertionError, match='four tool executions'):
        run_model(ModelSession([answer]), mcp)
    assert mcp.calls == 4


def test_model_requires_marker_in_actual_tool_result():
    class MCP:
        def call(self, *args):
            return {'content': [{'type': 'text', 'text': 'unrelated result'}]}

    with pytest.raises(AssertionError, match='MCP result omitted'):
        run_model(ModelSession([tool_call()]), MCP())


def test_tool_pagination_rejects_repeated_cursor(monkeypatch):
    mcp = MCPClient('http://localhost/mcp')
    monkeypatch.setattr(mcp, 'rpc', lambda *args: {'tools': [], 'nextCursor': 'same'})
    with pytest.raises(AssertionError, match='cursor repeated'):
        mcp.tools()
    mcp.session.close()


@pytest.mark.parametrize('secret_backed', [False, True])
def test_admin_cleanup_discovers_credentials_and_logs_out(monkeypatch, secret_backed):
    import base64
    from contextlib import contextmanager
    from types import SimpleNamespace as NS

    from . import conftest

    def env(name, value):
        return NS(name=name, value=None if secret_backed else value,
                  value_from=NS(secret_key_ref=NS(name='admin', key=name)) if secret_backed else None)

    deployment = NS(spec=NS(template=NS(spec=NS(containers=[NS(env=[
        env('ADMIN_USERNAME', 'admin'), env('ADMIN_PASS', 'password'),
    ])]))))
    monkeypatch.setattr(conftest.client, 'AppsV1Api', lambda *args: NS(
        read_namespaced_deployment=lambda *args: deployment))
    monkeypatch.setattr(conftest.client, 'CoreV1Api', lambda *args: NS(
        read_namespaced_secret=lambda *args: NS(data={
            'ADMIN_USERNAME': base64.b64encode(b'admin'),
            'ADMIN_PASS': base64.b64encode(b'password'),
        })))

    @contextmanager
    def forward(*args):
        yield 'http://localhost'

    monkeypatch.setattr(conftest, 'forwarded_service', forward)
    requests = []

    class AdminSession:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith('/login'):
                assert kwargs['json'] == {'user': 'admin', 'password': 'password'}
                return Response({'data': {'authToken': 'admin-token', 'userId': 'admin-id'}})
            assert self.headers == {'X-Auth-Token': 'admin-token', 'X-User-Id': 'admin-id'}
            return Response({'success': True})

    monkeypatch.setattr(conftest.requests, 'Session', AdminSession)
    fixture = conftest.rocket_admin.__wrapped__(NS(client=None))
    cleanup = next(fixture)
    cleanup('room', 'message')
    with pytest.raises(StopIteration):
        next(fixture)
    assert [url.rsplit('/', 1)[1] for url, _ in requests] == ['login', 'chat.delete', 'logout']
    assert requests[1][1]['json'] == {'roomId': 'room', 'msgId': 'message'}


def test_admin_cleanup_without_credentials_warns_and_leaves_messages(monkeypatch):
    from types import SimpleNamespace as NS

    from . import conftest

    deployment = NS(spec=NS(template=NS(spec=NS(containers=[NS(env=[])]))))
    monkeypatch.setattr(conftest.client, 'AppsV1Api', lambda *args: NS(
        read_namespaced_deployment=lambda *args: deployment))
    monkeypatch.setattr(conftest.client, 'CoreV1Api', lambda *args: None)
    fixture = conftest.rocket_admin.__wrapped__(NS(client=None))
    with pytest.warns(UserWarning, match='messages will remain'):
        assert next(fixture) is None
    with pytest.raises(StopIteration):
        next(fixture)
