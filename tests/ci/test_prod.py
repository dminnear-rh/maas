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
