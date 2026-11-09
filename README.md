# Models as a Service Validated Pattern

This pattern is based on the RHDPS demo catalog item defined in the [private-maas-automation repo.](https://github.com/rhpds/private-maas-automation)

## AWS GPU worker

On regular AWS OpenShift clusters, the production variant automatically runs the
Validated Patterns `rhvp.cluster_utils.create_machineset` playbook to provision
one `g6e.2xlarge` GPU worker. It clones an existing worker MachineSet's AMI and
network placement, creates a separate `nvidia-gpu` MachineSet with one replica,
and sets this node taint:

```yaml
key: nvidia.com/gpu
value: l40-gpu
effect: NoSchedule
```

The model and AI hardware profile already tolerate this taint. The existing
`nvidia-gpu-enablement` application configures GPU support. Provisioning uses the
cluster's Machine API AWS credentials and requires an existing AWS worker
MachineSet. The imperative job reconciles on the framework's ten-minute schedule.
Its settings are in `variants/prod/overrides/values-AWS-gpu.yaml`, selected
automatically when `global.clusterPlatform` is `AWS`.

## Test tenant

The production variant provisions one complete tenant, `user1`, with premium
MaaS access, a DevSpaces workspace, a Rocket.Chat account, and a GenAI playground
backed by pgvector. It uses local charts adapted from the source tenant; see
[the source revision and adaptations](charts/tenant-workspace/UPSTREAM.md).

`global.tenant` in `values-global.yaml` controls the username, MaaS group, and
workspace namespace prefix. The default workspace namespace is `wksp-user1`.
The framework supplies `global.localClusterDomain` at installation. Chart values
also allow changing the exercise repository, workspace image, and playground
model. The model must match the single deployed MaaS model.

### Load credentials and deploy

Merge the entries in `values-secret.yaml.template` into your local
`~/values-secret-maas.yaml`. For a new installation, copy the template there.
Keep that file outside Git. The loader generates the tenant login and database
passwords without prompting. Tenant setup waits for Rocket.Chat and discovers its
administrator credentials from the deployed container environment, including
referenced Secrets. No administrator password needs to be supplied before
installation. The OpenShift and Rocket.Chat tenant logins share the generated
password.

```bash
export KUBECONFIG=/path/to/deployed-cluster/auth/kubeconfig
./pattern.sh make load-secrets
```

Commit and push the pattern changes to the branch tracked by the installed
Pattern. Its clustergroup will create `user-config` and `tenant-workspace`.
For a new installation, use the normal `./pattern.sh make install` flow.
If the cluster already has identity providers, include them in the user-config
chart's `additionalIdentityProviders` values before syncing; this chart manages
the OAuth provider list.

Monitor setup without displaying credentials:

```bash
oc get externalsecret -n openshift-config
oc get externalsecret,jobs,cronjobs -n tenant-provisioning
oc get devworkspace,ogxserver,pods -n wksp-user1
oc logs -n tenant-provisioning -l job-name --all-containers --tail=30
```

The setup Job runs as an Argo CD `Sync` hook alongside tenant resources. It waits for secrets and
platform readiness, adds the user to `premium-users`, reconciles the Rocket.Chat
account, and logs in as the tenant to mint a one-hour premium MaaS key. Initial
setup allows 90 minutes for each readiness stage, with a two-hour overall Job
deadline, to cover cold GPU provisioning, large model image downloads, and model
loading. These limits are configurable through
`provisioner.startupReadinessTimeoutSeconds` and `provisioner.startupDeadlineSeconds`.
Argo's sync retry policy recreates a failed hook; successful hooks are deleted.
The hook verifies the workspace and playground even if a scheduled run already
created the API key. It runs during a full application sync, not a selective
resource sync, and uses `Sync` rather than `PostSync` because consumers need its
key before they can become healthy.

A CronJob
runs every ten minutes and renews keys with twenty minutes or less remaining.
Scheduled runs retain the 20-minute readiness budget and 24-minute Job deadline.
It validates a new key before publishing it, waits for consumers, and revokes
the superseded key. Failed runs retain state for the next reconciliation.
Expired keys do not recover until provisioning and its dependencies are healthy.

Administrative Rocket.Chat credentials stay with its deployment and referenced
Secrets; provisioning reads them only at runtime. Tenant login credentials and
key lifecycle state stay in `tenant-provisioning`. The tenant has `edit` in its
own workspace namespace.
The workspace and playground receive the tenant MaaS key and its issuing CA;
TLS verification stays enabled. The playground runs an internal HTTP service,
matching the source deployment.

### Open the workspace and use MaaS

Retrieve only the tenant password with an administrator kubeconfig:

```bash
oc get secret tenant-credentials -n tenant-provisioning \
  -o jsonpath='{.data.password}' | base64 -d
```

Open the cluster's DevSpaces URL in a separate browser session. Select
`htpasswd_provider` and log in as `user1` using that password. Open `exercises`
and verify the coding-exercises repository is available. In its terminal, run:

```bash
curl --fail-with-body --cacert "$MAAS_CA_FILE" \
  -H "Authorization: Bearer $(cat "$MAAS_API_KEY_FILE")" \
  -H 'Content-Type: application/json' \
  "$MAAS_BASE_URL/completions" \
  -d "{\"model\":\"$MAAS_MODEL\",\"prompt\":\"Say hello\",\"max_tokens\":16,\"stream\":false}"
```

Open the AI dashboard's `Workspace user1` project and use its GenAI playground.
Log into Rocket.Chat as `user1` with the same password. These checks should work
without the kubeadmin browser session.

DevSpaces mounts `maas-api-key` at `/etc/maas` using its
[documented Secret mounting mechanism](https://docs.redhat.com/en/documentation/red_hat_openshift_dev_spaces/3.28/html-single/user_guide/index).
Both the mount and watch labels are required for the workspace to see the Secret.
Secret updates can restart running workspaces; the scheduled renewal therefore
can briefly interrupt the IDE roughly every forty minutes. Stopped workspaces
remain stopped. The playground also rolls over to the new key.

### Password changes and troubleshooting

HTPasswd uses an ExternalSecret with `OnChange` refresh because bcrypt generates
a different salted hash on every render. This avoids repeated OAuth rollouts
when the underlying password has not changed. After updating the tenant password
in Vault through `make load-secrets`, explicitly refresh the HTPasswd Secret:

```bash
oc annotate externalsecret htpasswd-secret-es -n openshift-config \
  force-sync="$(date +%s)" --overwrite
```

The next tenant reconciliation updates the Rocket.Chat password. Changing the
username also requires changing its field name in your local secrets file.
The database uses a generated persistent password; changing it requires
restarting the database and playground to reload their Secret references.

If setup fails, inspect the latest provisioning Job logs and ExternalSecret
conditions first. Then check the authentication operator, DevSpaces, model,
Rocket.Chat, and pgvector Pod readiness. The provisioner logs only safe error
summaries. A failed replacement key remains pending until consumers are ready;
the previous key is not revoked early. Both the initial Job and CronJob use a
shared Lease to avoid simultaneous reconciliations. The provisioner creates this
lock at runtime because Argo CD excludes Lease resources from synchronization.

When upgrading from the original standalone setup Job, push the chart changes,
refresh `tenant-workspace`, and perform a **full Sync with Prune enabled**. This
removes the old failed `tenant-setup-*` Job and runs the new setup hook. A refresh
alone does not retry a completed or failed standalone Kubernetes Job.

After deployment, run the cluster integration suite:

```bash
VP_HUBCONFIG="$KUBECONFIG" ./pattern.sh make run-ci-tests
```

The tenant tests require `exercises` to be running. They verify the actual login,
permissions, premium inference, inference inside DevSpaces, playground health,
and Rocket.Chat login. Existing free-tier and anonymous tests are unchanged.
