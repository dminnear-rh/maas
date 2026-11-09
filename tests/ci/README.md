# CI Tests

The requirements.txt file is just a placeholder to show the installed packages in the utility container:
[utility-container requirements.txt](https://github.com/validatedpatterns/utility-container/blob/main/requirements.txt)

## The ci will run pytest based on file pattern

pytest -lv test\_\<TARGET_CLUSTERGROUP>.py --junit-xml .results/test\_\<TARGET_CLUSTERGROUP>.xml

## To run upstream tests locally

Set the env variables pointing to the clusters needed to run the tests on:\
`VP_HUBCONFIG` (`VP_SPOKECONFIG` if applicable) pointing to the kubconfig of hub (and spoke) clusters\
(all VP\_\* env var will be available inside the container)\
`TARGET_CLUSTERGROUP` if its different from the default (values-global.yaml main.clusterGroupName)\
Test logs and junit-xml will be saved in ci/.results/\
Run from root repository:

```bash
./pattern.sh make run-ci-tests
```

## Writing additional tests

The openshift_dyn_client fixture will return a DynamicClient which can be used inside test functions.\
It requires only an env variable param to use it as a kubeconfig.

```python
@pytest.mark.parametrize(
    "openshift_dyn_client",
    ["VP_HUBCONFIG"],
    indirect=True,
)
```

If your tests requires additional python packages, you might want to either run them fully locally in a venv or similar.\
Or if you want to use the pattern framework (make/Ansible wrappers) you need to use your own util container

```bash
PATTERN_UTILITY_CONTAINER="your_utility container" ./pattern.sh make run-ci-tests
```

## MaaS integration tests

The production suite also tests the single deployed `LLMInferenceService` in `llm`:

- A temporary service account requests a one-hour Kubernetes token and creates a
  one-hour MaaS API key bound to `maas-free-subscription`. The account has no role
  bindings; model access comes from the existing `system:authenticated` policy.
- A small non-streaming completion using that key must return HTTP 200, the
  expected model name, and nonempty generated text.
- The same completion without an Authorization header must return HTTP 401.
  Redirects and server errors do not count as authentication rejection.

The tests discover the model URL from its status and trust `ca.crt` from the
certificate Secret referenced by the MaaS gateway's HTTPS listener. TLS hostname
verification stays enabled. Requests use a 10-second connection timeout,
30-second management timeout, and 120-second inference timeout. HTTP sessions
ignore environment credentials and proxies, so the gateway must be directly
reachable from the runner.

The bootstrap kubeconfig needs permission to list model resources, read the
gateway and its certificate Secret, create/delete service accounts in `llm`, and
create `serviceaccounts/token` there. MaaS HTTP calls use the temporary identity,
not the bootstrap administrator. The fixture revokes the key before deleting the
service account, including after test failures; cleanup failures fail the run.
Keys expire after one hour if the runner is forcibly terminated. Revocation can
leave an inactive audit record in MaaS. Fixture tracebacks containing credentials
are hidden because the framework runs pytest with `--showlocals`.

Run with the installed pattern's kubeconfig:

```bash
KUBECONFIG="/path/to/kubeconfig" \
VP_HUBCONFIG="/path/to/kubeconfig" \
./pattern.sh make run-ci-tests
```

### Persistent tenant

The production suite also includes `test_tenant_*` checks for the pattern-managed
`user1` tenant. Deploy the tenant applications and load secrets first. These
checks fail if the tenant is missing, the workspace is stopped, or any component
is unhealthy. Existing temporary service-account tests continue to exercise the
free subscription independently.

Tenant checks log in through OpenShift OAuth, verify namespace edit permissions
and denial of administrative access, mint and revoke a temporary premium MaaS
key, and execute inference **inside the DevWorkspace using the tenant's API
client**. They also call the playground and log into Rocket.Chat as the tenant.
Bootstrap access reads configuration and credentials and creates loopback service
tunnels. The tenant token performs its own permission checks and workspace exec.
Temporary OAuth tokens, MaaS keys, and Rocket.Chat test sessions are revoked.

Run these checks with the normal CI command above. Run the offline lifecycle and
Helm checks separately (requires Python `requests`, PyYAML, and Helm):

```bash
python3 -m unittest discover -s tests/unit -v
```

API lifecycle reference:
[MaaS API key management](https://opendatahub-io.github.io/models-as-a-service/v2.0.1/user-guide/api-key-management/).

## MCP integration tests

The production suite includes four MCP tests, without provisioning a login user
or identity provider:

- `test_kubernetes_mcp_direct` creates a temporary ConfigMap in `lls-demo` and
  retrieves its unique marker through the Kubernetes MCP `resources_get` tool.
- `test_rocketchat_mcp_direct` posts a unique bot message to `workshop` and
  verifies its ID and text through MCP channel history.
- `test_kubernetes_mcp_model` asks the deployed model to retrieve a marker from
  a separate temporary ConfigMap using MCP.
- `test_rocketchat_mcp_model` asks the model to read the marker from a seeded
  message in `workshop` using MCP.

Model tests use `/v1/chat/completions` and the existing temporary MaaS API-key
fixture. The marker is withheld from the prompt: each test requires an actual
MCP call and a final answer containing the backend marker. Only the scenario's
read tool is exposed to the model, and arguments must match the test resource.
Tool calls are executed by the test runner, not by the inference endpoint.
Kubernetes operations use the MCP server's existing service account; Rocket.Chat
operations use the existing bot. The MaaS API key is never sent to MCP.

Run the complete suite through the normal framework command:

```bash
KUBECONFIG="/path/to/kubeconfig" \
VP_HUBCONFIG="/path/to/kubeconfig" \
./pattern.sh make run-ci-tests
```

The utility container must include `oc` and the existing Python test dependencies.
No additional Python packages are needed. The bootstrap kubeconfig also needs
permission to manage ConfigMaps in `lls-demo`, discover the MCP services and pods,
and open pod port-forwards. Rocket.Chat cleanup also reads its deployment and any
Secrets referenced by `ADMIN_USERNAME` and `ADMIN_PASS`. MCP services remain internal: fixtures open temporary
loopback tunnels on automatically allocated ports and terminate them on teardown.

MCP startup is limited to 30 seconds. Calls have 10-second connection and
30-second read timeouts; model requests retain the 120-second inference timeout.
Model conversations allow at most three requests and four tool executions, with
4096 output tokens per request and temperature zero. Inference requests are
spaced by 1.1 seconds to accommodate the free subscription's one-second rate
window. Model failures are not retried. Under concurrent load, MaaS can still
return HTTP 429, which fails the test.

Missing tools, channels, backend permissions, protocol errors, and tool-calling
failures fail the suite rather than skipping coverage. Rocket.Chat history is
read with grouping disabled. Teardown removes test ConfigMaps and only the
messages belonging to that test. Cleanup uses the existing Rocket.Chat admin
credentials discovered from deployment environment values or Secret references;
credentials are neither logged nor passed to the model. The temporary admin
session is logged out after cleanup. If admin credentials are not configured,
tests warn and leave their messages, which is acceptable for disposable CI
clusters. When credentials are present, login or cleanup failures fail the run.
Forced runner
termination can leave test artifacts, identifiable by their `mcp-ci-` prefix.

## MaaS token rate-limit test

`test_maas_token_rate_limit` checks the free subscription's 1000-token/second
and 20000-token/minute configuration with a new temporary MaaS identity. It first
requires a small completion to succeed, then sends a prompt of 1200 repeated
tokens with only one output token requested. It requires HTTP 429 either for the
oversized request or an immediate follow-up, accommodating post-inference token
accounting. Successful oversized requests must report more than 1000 prompt
tokens. Up to three bursts account for requests straddling a rate window; this
is a bounded rate-limit exercise, not an inference retry policy. The total burst
budget stays below the minute limit. A small request must succeed again after
waiting 1.1 seconds. Authentication rejection and server errors do not count as
rate limiting. This test runs as part of the normal framework command above.

## Helper regression tests

For protocol/helper regression tests without cluster access:

```bash
./pattern.sh python3 -m pytest -q tests/ci/test_mcp_helpers.py
```

These cover JSON/SSE parsing, MCP sessions, tool pagination and errors, restricted
model calls, tool-call IDs, withheld markers, and execution/conversation budgets.
They supplement the production suite rather than replacing its cluster tests.
