from contextlib import closing
import time

import pytest
import requests
from validatedpatterns_tests.interop import components, subscription

from .mcp_helpers import model_tool_roundtrip, tool_json


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_subscription_status(openshift_dyn_client):
    expected_subs = {
        "openshift-gitops-operator": ["openshift-gitops-operator"],
    }

    subscription.assert_subscription_status(openshift_dyn_client, expected_subs)


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_site_reachable(openshift_dyn_client):
    components.assert_site_reachable(openshift_dyn_client)


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_argocd_reachable(openshift_dyn_client):
    components.assert_argocd_reachable(openshift_dyn_client)


@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
def test_pod_status(openshift_dyn_client):
    projects = [
        "patterns-operator",
        "vp-gitops",
        "lls-demo",
        "rocketchat",
        "llm",
    ]
    components.assert_pod_status(openshift_dyn_client, projects, skip_check=[])


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_maas_inference_with_api_key(maas_endpoint, maas_api_session, maas_completion):
    response = maas_api_session.post(
        f"{maas_endpoint['url']}/v1/completions",
        json=maas_completion,
        timeout=(10, 120),
        allow_redirects=False,
    )
    assert (
        response.status_code == 200
    ), f"Inference returned HTTP {response.status_code}"
    result = response.json()
    assert result.get("model") == maas_endpoint["model"]
    assert result.get("choices"), "Inference returned no choices"
    assert (
        result["choices"][0].get("text", "").strip()
    ), "Inference returned no generated text"


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_maas_inference_without_api_key(maas_endpoint, maas_completion):
    # A separate session ensures the positive test's Authorization header cannot leak here.
    with requests.Session() as anonymous:
        anonymous.trust_env = False  # Do not load credentials from ~/.netrc.
        response = anonymous.post(
            f"{maas_endpoint['url']}/v1/completions",
            json=maas_completion,
            verify=maas_endpoint["verify"],
            timeout=(10, 120),
            allow_redirects=False,
        )
    assert (
        response.status_code == 401
    ), f"Expected authentication rejection (401), got HTTP {response.status_code}"


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_kubernetes_mcp_direct(kubernetes_mcp, mcp_configmap):
    assert "resources_get" in kubernetes_mcp.tools()
    result = tool_json(kubernetes_mcp.call("resources_get", mcp_configmap["arguments"]))
    assert result["metadata"]["name"] == mcp_configmap["name"]
    assert result["data"]["marker"] == mcp_configmap["marker"]


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_rocketchat_mcp_direct(rocket_message):
    posted = rocket_message["posted"]
    assert posted["id"]
    assert any(message["id"] == posted["id"] and message["text"] == rocket_message["text"]
               for message in rocket_message["history"]())


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_kubernetes_mcp_model(kubernetes_mcp, mcp_configmap, maas_api_session, maas_endpoint):
    model_tool_roundtrip(
        maas_api_session, maas_endpoint, kubernetes_mcp,
        kubernetes_mcp.tools()["resources_get"], mcp_configmap["arguments"],
        f"Read the marker data field from ConfigMap {mcp_configmap['name']} in namespace "
        "lls-demo, apiVersion v1, using resources_get. Return the exact marker.",
        mcp_configmap["marker"],
    )


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_rocketchat_mcp_model(rocketchat_mcp, rocket_message, maas_api_session, maas_endpoint):
    arguments = {"channel": rocket_message["room_id"], "count": 100, "group": False}
    model_tool_roundtrip(
        maas_api_session, maas_endpoint, rocketchat_mcp,
        rocket_message["tools"]["get_channel_messages"], arguments,
        f"Read messages from channel ID {rocket_message['room_id']} using get_channel_messages "
        "with count=100 and group=false (omit other optional arguments). "
        f"Find the message containing {rocket_message['tag']} and return its exact marker value.",
        rocket_message["marker"],
    )



@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_maas_token_rate_limit(openshift_dyn_client, maas_api_session, maas_endpoint, maas_completion):
    subscription = openshift_dyn_client.resources.get(
        api_version="maas.opendatahub.io/v1alpha1", kind="MaaSSubscription",
    ).get(namespace="models-as-a-service", name="maas-free-subscription").to_dict()
    model = next(ref for ref in subscription["spec"]["modelRefs"]
                 if ref["name"] == maas_endpoint["model"] and ref["namespace"] == "llm")
    limits = {limit["window"]: limit["limit"] for limit in model["tokenRateLimits"]}
    assert limits["1s"] == 1000 and limits["1m"] == 20000, "Unexpected free subscription token limits"

    def complete(body):
        return maas_api_session.post(f"{maas_endpoint['url']}/v1/completions", json=body,
                                     timeout=(10, 120), allow_redirects=False)

    # Establish valid credentials and a working backend before testing rejection.
    small = dict(maas_completion, max_tokens=1)
    assert complete(small).status_code == 200, "Rate-limit positive control failed"
    large = dict(small, prompt=" hello" * 1200)
    limited = False
    for _ in range(3):
        time.sleep(1.1)
        response = complete(large)
        if response.status_code == 429:
            limited = True
            break
        assert response.status_code == 200, f"Token burst: HTTP {response.status_code}"
        assert response.json()["usage"]["prompt_tokens"] > limits["1s"], "Burst did not exceed token limit"
        # Token accounting may occur after inference. Probe immediately after the
        # oversized request; repeat the bounded burst if it straddled a window.
        probe = complete(small)
        assert probe.status_code in (200, 429), f"Rate-limit probe: HTTP {probe.status_code}"
        if probe.status_code == 429:
            limited = True
            break
    assert limited, "Exceeding 1000 tokens/second was not rate-limited (expected HTTP 429)"
    time.sleep(1.1)
    assert complete(small).status_code == 200, "Inference did not recover after the one-second rate window"


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_tenant_login_and_permissions(tenant_identity):
    __tracebackhide__ = True
    from .tenant_helpers import allowed, provision

    tenant = tenant_identity
    config = tenant['config']
    identity = provision.request(tenant['http'], 'GET', tenant['host'] + '/apis/user.openshift.io/v1/users/~').json()
    assert identity['metadata']['name'] == config['username']
    group = tenant['kube'].get('/apis/user.openshift.io/v1/groups/' + config['group'])
    assert config['username'] in group['users']
    assert allowed(tenant['http'], tenant['host'], config['workspaceNamespace'], 'create', 'pods')
    assert not allowed(tenant['http'], tenant['host'], config['controlNamespace'], 'get', 'secrets')
    assert not allowed(tenant['http'], tenant['host'], '', 'create', 'clusterrolebindings', 'rbac.authorization.k8s.io')


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_tenant_premium_maas(tenant_identity, maas_endpoint, maas_completion):
    __tracebackhide__ = True
    from uuid import uuid4
    from .tenant_helpers import provision

    with provision.session(maas_endpoint['verify']) as management, provision.session(maas_endpoint['verify']) as inference:
        management.headers.update(tenant_identity['http'].headers)
        created = provision.request(management, 'POST', maas_endpoint['api_url'], allowed=(201,), json={
            'name': 'tenant-ci-' + uuid4().hex[:12], 'expiresIn': '1h',
            'subscription': tenant_identity['config']['subscription'],
        }).json()
        assert created.get('id'), 'MaaS did not return a key ID'
        try:
            assert created.get('key'), 'MaaS did not return a key'
            inference.headers['Authorization'] = 'Bearer ' + created['key']
            result = provision.request(inference, 'POST', maas_endpoint['url'] + '/v1/completions', json=maas_completion).json()
            assert result['choices'][0]['text'].strip()
        finally:
            provision.request(management, 'DELETE', maas_endpoint['api_url'] + '/' + created['id'], allowed=(200, 204))


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_tenant_devspaces_inference(tenant_identity):
    __tracebackhide__ = True
    from kubernetes import client
    from kubernetes.stream import stream
    from .tenant_helpers import provision

    config = tenant_identity['config']
    core = client.CoreV1Api(tenant_identity['api_client'])
    path = f"/apis/workspace.devfile.io/v1alpha2/namespaces/{config['workspaceNamespace']}/devworkspaces/{config['workspace']}"
    workspace = provision.request(tenant_identity['http'], 'GET', tenant_identity['host'] + path).json()
    assert workspace['spec']['started'], 'Start the tenant workspace before running its integration test'
    assert workspace.get('status', {}).get('phase') == 'Running', 'Tenant workspace is not Running'
    pods = core.list_namespaced_pod(config['workspaceNamespace'], label_selector=(
        'controller.devfile.io/devworkspace_id=' + workspace['status']['devworkspaceId'])).items
    assert len(pods) == 1, 'Expected one tenant workspace Pod'
    pod = pods[0]
    container = next(c.name for c in pod.spec.containers if any(
        e.name == 'MAAS_API_KEY_FILE' for e in c.env or []))
    # Only filenames are sent in the exec command. The tenant reads its own key.
    script = '''import json, os, pathlib, ssl, urllib.request
body = json.dumps({"model": os.environ["MAAS_MODEL"], "prompt": "Say hello", "max_tokens": 8, "stream": False}).encode()
key = pathlib.Path(os.environ["MAAS_API_KEY_FILE"]).read_text().strip()
req = urllib.request.Request(os.environ["MAAS_BASE_URL"].rstrip("/") + "/completions", data=body,
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
with urllib.request.urlopen(req, context=ssl.create_default_context(cafile=os.environ["MAAS_CA_FILE"]), timeout=120) as response:
    result = json.load(response)
assert result["choices"][0]["text"].strip()
print("tenant-workspace-inference-ok")
'''
    with closing(stream(core.connect_get_namespaced_pod_exec, pod.metadata.name, config['workspaceNamespace'],
                container=container, command=['python3', '-c', script], stderr=True, stdout=True,
                stdin=False, tty=False, _preload_content=False)) as websocket:
        websocket.run_forever(timeout=150)
        output = websocket.read_stdout()
        assert websocket.returncode == 0, 'Inference inside the tenant workspace failed'
        assert 'tenant-workspace-inference-ok' in output


@pytest.mark.parametrize("openshift_dyn_client", ["VP_HUBCONFIG"], indirect=True)
def test_tenant_playground_and_rocketchat(tenant_identity):
    __tracebackhide__ = True
    from kubernetes import client
    from urllib.parse import urlsplit
    from .mcp_helpers import forwarded_service
    from .tenant_helpers import provision

    tenant = tenant_identity
    config = tenant['config']
    if config['playgroundEnabled']:
        ns = config['workspaceNamespace']
        ogx = tenant['kube'].get(f"/apis/ogx.io/v1beta1/namespaces/{ns}/ogxservers/{config['playground']}")
        assert ogx.get('status', {}).get('phase') == 'Ready', 'Tenant playground is not Ready'
        endpoint = urlsplit(ogx['status']['serviceURL'])
        with forwarded_service(endpoint.hostname.split('.')[0], endpoint.port or 8321, namespace=ns) as base:
            with provision.session() as http:
                provision.playground_completion(http, base, config['model'])
        # The database Pod checks the extension explicitly rather than just accepting TCP.
        pods = client.CoreV1Api(tenant['api_client']).list_namespaced_pod(
            ns, label_selector='app=genai-pgvector').items
        assert len(pods) == 1
        statuses = pods[0].status.container_statuses
        assert statuses and all(status.ready for status in statuses)
    if config['rocketchatEnabled']:
        credentials = provision.decode(tenant['kube'].secret(config['controlNamespace'], 'tenant-credentials'))
        endpoint = urlsplit(config['rocketchatUrl'])
        parts = endpoint.hostname.split('.')
        with forwarded_service(parts[0], endpoint.port or 80, namespace=parts[1]) as base:
            with provision.session() as http:
                result = provision.request(http, 'POST', base + '/api/v1/login', json={
                    'user': config['username'], 'password': credentials['password'],
                }).json()
                assert result.get('status') == 'success', 'Rocket.Chat tenant login failed'
                http.headers.update({'X-Auth-Token': result['data']['authToken'], 'X-User-Id': result['data']['userId']})
                provision.request(http, 'POST', base + '/api/v1/logout')
