import base64
import warnings
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
from kubernetes import client

from validatedpatterns_tests.interop.conftest_openshift import *  # noqa: F401,F403,RUF100

from .mcp_helpers import forwarded_mcp, forwarded_service, tool_json


@pytest.fixture
def maas_endpoint(openshift_dyn_client, tmp_path):
    """Discover the single deployed model and trust its gateway's issuing CA."""
    __tracebackhide__ = True  # Gateway Secret also contains a private key.
    resources = openshift_dyn_client.resources
    models = resources.get(
        api_version="serving.kserve.io/v1alpha1", kind="LLMInferenceService"
    ).get(namespace="llm").to_dict()["items"]
    assert len(models) == 1, "Expected exactly one deployed LLMInferenceService in llm"
    model = models[0]
    status = model.get("status", {})
    assert any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in status.get("conditions", [])
    ), f"Model {model['metadata']['name']} is not Ready"
    url = status.get("url", "").rstrip("/")
    assert url.startswith("https://"), "Model has no external HTTPS URL"
    gateway = resources.get(api_version="gateway.networking.k8s.io/v1", kind="Gateway").get(
        namespace="openshift-ingress", name="maas-default-gateway"
    ).to_dict()
    listener = next(
        listener for listener in gateway["spec"]["listeners"]
        if listener.get("hostname") == urlsplit(url).hostname
        and listener.get("protocol") == "HTTPS"
    )
    certificate = listener["tls"]["certificateRefs"][0]
    secret = resources.get(api_version="v1", kind="Secret").get(
        namespace=certificate.get("namespace", "openshift-ingress"),
        name=certificate["name"],
    ).to_dict()
    assert secret.get("data", {}).get("ca.crt"), "Gateway secret has no issuing CA"
    ca_path = tmp_path / "maas-ca.crt"
    ca_path.write_bytes(base64.b64decode(secret["data"]["ca.crt"]))
    return {
        "url": url,
        "api_url": f"https://{urlsplit(url).netloc}/maas-api/v1/api-keys",
        "model": model["spec"]["model"]["name"],
        "verify": str(ca_path),
    }


@pytest.fixture
def maas_api_session(openshift_dyn_client, maas_endpoint):
    """Mint a MaaS key as an unprivileged identity; revoke it before removing it."""
    __tracebackhide__ = True  # The framework uses --showlocals; keep credentials out of failures.
    core = client.CoreV1Api(openshift_dyn_client.client)
    name = f"maas-ci-{uuid4().hex[:12]}"
    core.create_namespaced_service_account(
        "llm", client.V1ServiceAccount(metadata=client.V1ObjectMeta(name=name))
    )
    try:
        token = core.create_namespaced_service_account_token(
            name, "llm", client.AuthenticationV1TokenRequest(
                spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=3600)
            )
        ).status.token
        with requests.Session() as management, requests.Session() as inference:
            management.trust_env = inference.trust_env = False
            management.verify = inference.verify = maas_endpoint["verify"]
            management.headers["Authorization"] = f"Bearer {token}"
            response = management.post(
                maas_endpoint["api_url"],
                json={"name": name, "expiresIn": "1h", "subscription": "maas-free-subscription"},
                timeout=(10, 30), allow_redirects=False,
            )
            # Never include the creation response body: it contains the plaintext key.
            assert response.status_code == 201, f"MaaS key creation returned HTTP {response.status_code}"
            created = response.json()
            key_id = created.get("id")
            assert key_id, "MaaS key creation returned no key ID"
            try:
                if not created.get("key"):
                    pytest.fail("MaaS key creation returned no key", pytrace=False)
                inference.headers["Authorization"] = f"Bearer {created['key']}"
                yield inference
            finally:
                revoked = management.delete(
                    f"{maas_endpoint['api_url']}/{key_id}",
                    timeout=(10, 30), allow_redirects=False,
                )
                assert revoked.status_code in (200, 204), (
                    f"MaaS key revocation returned HTTP {revoked.status_code}"
                )
    finally:
        core.delete_namespaced_service_account(name, "llm")


@pytest.fixture
def maas_completion(maas_endpoint):
    return {
        "model": maas_endpoint["model"],
        "prompt": "The capital of France is",
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }


@pytest.fixture
def kubernetes_mcp():
    with forwarded_mcp("ocp-mcp-server", 8080) as mcp:
        yield mcp


@pytest.fixture
def rocketchat_mcp():
    with forwarded_mcp("rocketchat-mcp", 80) as mcp:
        yield mcp


@pytest.fixture
def mcp_configmap(openshift_dyn_client):
    core = client.CoreV1Api(openshift_dyn_client.client)
    name, marker = f"mcp-ci-{uuid4().hex[:12]}", uuid4().hex
    core.create_namespaced_config_map("lls-demo", client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name), data={"marker": marker},
    ))
    try:
        yield {"name": name, "marker": marker, "arguments": {
            "apiVersion": "v1", "kind": "ConfigMap", "name": name, "namespace": "lls-demo",
        }}
    finally:
        core.delete_namespaced_config_map(name, "lls-demo")


@pytest.fixture
def rocket_admin(openshift_dyn_client):
    """Use installed admin credentials only for fixture cleanup, never model calls."""
    __tracebackhide__ = True
    core = client.CoreV1Api(openshift_dyn_client.client)
    deployment = client.AppsV1Api(openshift_dyn_client.client).read_namespaced_deployment(
        "rocketchat-rocketchat", "rocketchat",
    )
    env = {entry.name: entry for container in deployment.spec.template.spec.containers
           for entry in container.env or []}

    def value(name):
        __tracebackhide__ = True
        entry = env.get(name)
        if entry is None:
            return None
        if entry.value is not None:
            return entry.value
        ref = entry.value_from.secret_key_ref if entry.value_from else None
        if ref:
            secret = core.read_namespaced_secret(ref.name, "rocketchat")
            return base64.b64decode(secret.data[ref.key]).decode()
        return None

    username, password = value("ADMIN_USERNAME"), value("ADMIN_PASS")
    if not username or not password:
        warnings.warn("Rocket.Chat admin credentials unavailable; test messages will remain", stacklevel=2)
        yield None
        return
    with forwarded_service("rocketchat-rocketchat", 80, "rocketchat") as url:
        with requests.Session() as session:
            session.trust_env = False
            login = session.post(f"{url}/api/v1/login", json={"user": username, "password": password},
                                 timeout=(10, 30), allow_redirects=False)
            assert login.status_code == 200, f"Rocket.Chat admin login: HTTP {login.status_code}"
            credentials = login.json()["data"]
            session.headers.update({"X-Auth-Token": credentials["authToken"],
                                    "X-User-Id": credentials["userId"]})

            def delete(room_id, message_id):
                __tracebackhide__ = True
                response = session.post(f"{url}/api/v1/chat.delete",
                                        json={"roomId": room_id, "msgId": message_id},
                                        timeout=(10, 30), allow_redirects=False)
                assert response.status_code == 200 and response.json().get("success"), (
                    f"Rocket.Chat admin cleanup: HTTP {response.status_code}"
                )

            try:
                yield delete
            finally:
                response = session.post(f"{url}/api/v1/logout", timeout=(10, 30), allow_redirects=False)
                assert response.status_code == 200, "Rocket.Chat admin logout failed"


@pytest.fixture
def rocket_message(rocketchat_mcp, rocket_admin):
    """Post and remove a unique message, even if the POST response is lost."""
    mcp = rocketchat_mcp
    tools = mcp.tools()
    for name in ("get_channel_info", "send_message", "get_channel_messages"):
        assert name in tools, f"Rocket.Chat MCP is missing {name}"
    channel = tool_json(mcp.call("get_channel_info", {"channel": "workshop"}))
    room_id = channel["id"]
    tag, marker = f"mcp-ci-{uuid4().hex}", uuid4().hex
    text = f"{tag} marker={marker}"

    def history():
        # Bound the scan, but page beyond the first result to tolerate workshop traffic.
        messages = []
        for offset in range(0, 1000, 100):
            page = tool_json(mcp.call("get_channel_messages", {
                "channel": room_id, "count": 100, "offset": offset, "group": False,
            }))["messages"]
            messages.extend(page)
            if len(page) < 100:
                break
        return messages

    posted = None
    try:
        posted = tool_json(mcp.call("send_message", {"channel": room_id, "text": text}))
        yield {"room_id": room_id, "tag": tag, "marker": marker, "text": text,
               "posted": posted, "history": history, "tools": tools}
    finally:
        if posted and posted.get("id"):
            # Delete the known ID even if subsequent history reads have failed.
            ids = [posted["id"]]
        else:
            ids = [message["id"] for message in history() if message.get("text") == text]
        if rocket_admin:
            for message_id in ids:
                rocket_admin(room_id, message_id)
            assert not any(tag in message.get("text", "") for message in history()), (
                "Rocket.Chat test message remains after cleanup"
            )
