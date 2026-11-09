"""Reconcile one tenant. Bootstrap API access and tenant HTTP access stay separate."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import requests


class ProvisionError(RuntimeError):
    pass


class LeaseBusy(ProvisionError):
    pass


def session(verify=True):
    result = requests.Session()
    result.trust_env = False
    result.verify = verify
    return result


def request(http, method, url, *, allowed=(200,), **kwargs):
    __tracebackhide__ = True
    # Never include URLs, response bodies, headers or credentials in errors.
    response = http.request(method, url, timeout=(10, 120), allow_redirects=False, **kwargs)
    if response.status_code not in allowed:
        raise ProvisionError(f"{method} returned HTTP {response.status_code}")
    return response


def retry(check, timeout, interval=10):
    end = time.monotonic() + timeout
    while True:
        try:
            result = check()
            if result:
                return result
        except (ProvisionError, requests.RequestException, KeyError):
            pass
        if time.monotonic() >= end:
            raise ProvisionError("Readiness deadline exceeded")
        time.sleep(interval)


class Kube:
    def __init__(self):
        directory = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        self.http = session(str(directory / 'ca.crt'))
        self.token_file = directory / 'token'

    def call(self, method, path, body=None, allowed=(200,)):
        # Kubelet rotates the projected token during long cold-start waits.
        self.http.headers['Authorization'] = 'Bearer ' + self.token_file.read_text().strip()
        headers = {'Content-Type': 'application/merge-patch+json'} if method == 'PATCH' else {}
        response = request(self.http, method, 'https://kubernetes.default.svc' + path,
                           allowed=allowed, json=body, headers=headers)
        if response.status_code == 404:
            return {}
        return response.json() if response.content else {}

    def get(self, path):
        return self.call('GET', path)

    def secret(self, namespace, name):
        return self.get(f'/api/v1/namespaces/{namespace}/secrets/{name}')

    def save_secret(self, namespace, name, values, metadata=None):
        # All output/state Secrets are pre-created by Helm. Never replace metadata
        # or ownership when updating runtime data.
        body = {'data': {key: base64.b64encode(value.encode()).decode()
                         for key, value in values.items()}}
        if metadata:
            body['metadata'] = metadata
        return self.call('PATCH', f'/api/v1/namespaces/{namespace}/secrets/{name}', body)


def decode(secret):
    return {key: base64.b64decode(value).decode() for key, value in secret.get('data', {}).items()}


@contextlib.contextmanager
def lease(kube, namespace, duration=1500):
    path = f'/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/tenant-provisioner'
    holder = uuid4().hex
    current = kube.call('GET', path, allowed=(200, 404))
    if not current:
        current = kube.call('POST', path.rsplit('/', 1)[0], {
            'apiVersion': 'coordination.k8s.io/v1', 'kind': 'Lease',
            'metadata': {'name': 'tenant-provisioner'}, 'spec': {},
        }, allowed=(201, 409))
        if current.get('kind') == 'Status':
            raise LeaseBusy('Another provisioner created the lease')
    spec = current.get('spec', {})
    now = dt.datetime.now(dt.timezone.utc)
    renewed = dt.datetime.fromisoformat(spec.get('renewTime', '1970-01-01T00:00:00Z').replace('Z', '+00:00'))
    if spec.get('holderIdentity') and (now - renewed).total_seconds() < spec.get('leaseDurationSeconds', 1500):
        raise LeaseBusy('Another provisioner holds the lease')
    current['spec'] = {'holderIdentity': holder, 'renewTime': now.isoformat(), 'leaseDurationSeconds': duration}
    kube.call('PUT', path, current)
    try:
        yield
    finally:
        # Resource-version checking avoids releasing a lease acquired by another job.
        current = kube.get(path)
        if current.get('spec', {}).get('holderIdentity') == holder:
            current['spec']['holderIdentity'] = ''
            kube.call('PUT', path, current)


@contextlib.contextmanager
def tenant_login(kube, username, password, timeout):
    __tracebackhide__ = True
    bundle = kube.get('/api/v1/namespaces/openshift-config-managed/configmaps/default-ingress-cert')['data']['ca-bundle.crt']
    route = kube.get('/apis/route.openshift.io/v1/namespaces/openshift-authentication/routes/oauth-openshift')
    with tempfile.TemporaryDirectory() as directory:
        ca = Path(directory) / 'ca.crt'
        ca.write_text(bundle)
        with session(str(ca)) as http:
            def login():
                __tracebackhide__ = True
                response = request(http, 'GET', 'https://' + route['spec']['host'] + '/oauth/authorize',
                                   allowed=(302,), params={'client_id': 'openshift-challenging-client',
                                   'response_type': 'token'}, auth=(username, password),
                                   headers={'X-CSRF-Token': 'tenant-provisioner'})
                token = parse_qs(urlsplit(response.headers.get('Location', '')).fragment).get('access_token', [''])[0]
                if not token:
                    raise ProvisionError('OAuth response contained no token')
                return token
            token = retry(login, timeout)
            try:
                yield token
            finally:
                # OpenShift names OAuthAccessToken resources by SHA-256 of the token.
                digest = base64.urlsafe_b64encode(hashlib.sha256(token.encode()).digest()).decode().rstrip('=')
                kube.call('DELETE', '/apis/oauth.openshift.io/v1/oauthaccesstokens/' + quote('sha256~' + digest, safe=''),
                          allowed=(200, 202, 404))


def group_membership(kube, config):
    path = '/apis/user.openshift.io/v1/groups/' + config['group']
    group = kube.get(path)  # Platform MaaS chart owns and creates this group.
    if config['username'] not in group.get('users', []):
        group['users'] = group.get('users', []) + [config['username']]
        kube.call('PUT', path, group)


def rocket_admin_credentials(kube, config):
    __tracebackhide__ = True
    namespace = config['rocketchatNamespace']
    deployment = kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{config['rocketchatDeployment']}")
    containers = deployment['spec']['template']['spec']['containers']
    container = next((item for item in containers if item['name'] == config['rocketchatContainer']), None)
    if container is None:
        raise ProvisionError('Rocket.Chat container not available')

    def values(kind, name):
        __tracebackhide__ = True
        if kind == 'secret':
            return decode(kube.secret(namespace, name))
        return kube.get(f'/api/v1/namespaces/{namespace}/configmaps/{name}').get('data', {})

    env = {}
    for source in container.get('envFrom', []):
        for kind in ('secret', 'configMap'):
            ref = source.get(kind + 'Ref')
            if ref:
                env.update({source.get('prefix', '') + key: value for key, value in values(kind, ref['name']).items()})
    for entry in container.get('env', []):
        if entry['name'] not in ('ADMIN_USERNAME', 'ADMIN_PASS'):
            continue
        if 'value' in entry:
            env[entry['name']] = entry['value']
            continue
        for kind in ('secret', 'configMap'):
            ref = entry.get('valueFrom', {}).get(kind + 'KeyRef')
            if ref:
                env[entry['name']] = values(kind, ref['name'])[ref['key']]
                break
        else:
            raise ProvisionError('Rocket.Chat admin credential source is unsupported')
    if not env.get('ADMIN_USERNAME') or not env.get('ADMIN_PASS'):
        raise ProvisionError('Rocket.Chat admin credentials not available')
    return {'username': env['ADMIN_USERNAME'], 'password': env['ADMIN_PASS']}


def rocket_setup(kube, config, credentials, state):
    __tracebackhide__ = True
    if not config['rocketchatEnabled']:
        return
    fingerprint = hashlib.sha256(credentials['password'].encode()).hexdigest()
    output = decode(kube.secret(config['workspaceNamespace'], 'rocketchat-user-credentials'))
    if state.get('rocketPasswordHash') == fingerprint and output.get('auth-token'):
        return
    url = config['rocketchatUrl'].rstrip('/')
    with session() as http:
        retry(lambda: request(http, 'GET', url + '/api/info'), config['timeout'])
        def admin_login():
            __tracebackhide__ = True
            admin = rocket_admin_credentials(kube, config)
            result = request(http, 'POST', url + '/api/v1/login', json={
                'user': admin['username'], 'password': admin['password']}).json()
            if result.get('status') != 'success':
                raise ProvisionError('Rocket.Chat admin login failed')
            return result
        login = retry(admin_login, config['timeout'])
        http.headers.update({'X-Auth-Token': login['data']['authToken'], 'X-User-Id': login['data']['userId']})
        try:
            found = request(http, 'GET', url + '/api/v1/users.info', allowed=(200, 400),
                            params={'username': config['username']}).json()
            user = found.get('user')
            data = {'password': credentials['password'], 'roles': ['user'], 'requirePasswordChange': False}
            if user:
                result = request(http, 'POST', url + '/api/v1/users.update', json={
                    'userId': user['_id'], 'data': data}).json()
            else:
                data.update({'username': config['username'], 'name': config['username'],
                             'email': config['username'] + '@' + config['domain'],
                             'verified': True, 'joinDefaultChannels': True, 'sendWelcomeEmail': False})
                result = request(http, 'POST', url + '/api/v1/users.create', json=data).json()
            if not result.get('success'):
                raise ProvisionError('Rocket.Chat user reconciliation failed')
        finally:
            request(http, 'POST', url + '/api/v1/logout')
    with session() as http:
        result = request(http, 'POST', url + '/api/v1/login', json={
            'user': config['username'], 'password': credentials['password']}).json()
        if result.get('status') != 'success':
            raise ProvisionError('Rocket.Chat tenant login failed')
        kube.save_secret(config['workspaceNamespace'], 'rocketchat-user-credentials', {
            'auth-token': result['data']['authToken'], 'user-id': result['data']['userId'],
            'username': config['username'], 'rocketchat-url': url})
    state['rocketPasswordHash'] = fingerprint


def endpoint(kube):
    models = kube.get('/apis/serving.kserve.io/v1alpha1/namespaces/llm/llminferenceservices')['items']
    if len(models) != 1:
        raise ProvisionError('Expected one MaaS model')
    model = models[0]
    if not any(c['type'] == 'Ready' and c['status'] == 'True'
               for c in model.get('status', {}).get('conditions', [])):
        raise ProvisionError('MaaS model not ready')
    url = model['status']['url'].rstrip('/')
    if urlsplit(url).scheme != 'https':
        raise ProvisionError('Expected HTTPS MaaS URL')
    gateway = kube.get('/apis/gateway.networking.k8s.io/v1/namespaces/openshift-ingress/gateways/maas-default-gateway')
    listener = next(item for item in gateway['spec']['listeners'] if item.get('hostname') == urlsplit(url).hostname)
    ref = listener['tls']['certificateRefs'][0]
    ca = decode(kube.secret(ref.get('namespace', 'openshift-ingress'), ref['name']))['ca.crt']
    return {'url': url, 'management': 'https://' + urlsplit(url).netloc + '/maas-api/v1/api-keys',
            'model': model['spec']['model']['name'], 'ca': ca}


def completion(http, ep):
    result = request(http, 'POST', ep['url'] + '/v1/completions', json={
        'model': ep['model'], 'prompt': 'The capital of France is', 'max_tokens': 8,
        'temperature': 0, 'stream': False}).json()
    if not result.get('choices', [{}])[0].get('text', '').strip():
        raise ProvisionError('MaaS returned no generated text')
    return True


def renew_needed(state, now, renew_before):
    return not state.get('key') or float(state.get('expiresAt', 0)) - now <= renew_before


def rotate(kube, config, management, inference, ep, state):
    __tracebackhide__ = True
    namespace = config['workspaceNamespace']
    public = decode(kube.secret(namespace, 'maas-api-key'))
    # Persist pending key before publishing, so an interrupted run can finish it.
    if not state.get('pending') and (renew_needed(state, time.time(), config['renewBefore'])
                                    or public.get('api-key') != state.get('key') or public.get('ca.crt') != ep['ca']):
        created = request(management, 'POST', ep['management'], allowed=(201,), json={
            'name': 'tenant-' + config['username'] + '-' + uuid4().hex[:12],
            'expiresIn': '1h', 'subscription': config['subscription']}).json()
        if not created.get('id') or not created.get('key'):
            raise ProvisionError('MaaS key response incomplete')
        state['pending'] = {'id': created['id'], 'key': created['key'], 'expiresAt': time.time() + 3600}
        save_state(kube, config, state)
    pending = state.get('pending')
    if pending:
        inference.headers['Authorization'] = 'Bearer ' + pending['key']
        try:
            completion(inference, ep)  # Check before publishing or restarting anything.
        except Exception:
            if public.get('api-key') == pending['key'] and state.get('key'):
                kube.save_secret(namespace, 'maas-api-key', {'api-key': state['key'], 'ca.crt': ep['ca']})
                restart_playground(kube, config, state['id'])
            request(management, 'DELETE', ep['management'] + '/' + quote(pending['id'], safe=''), allowed=(200, 204, 404))
            state.pop('pending')
            save_state(kube, config, state)
            raise
        kube.save_secret(namespace, 'maas-api-key', {'api-key': pending['key'], 'ca.crt': ep['ca']}, {
            'labels': {'controller.devfile.io/mount-to-devworkspace': 'true',
                       'controller.devfile.io/watch-secret': 'true'},
            'annotations': {'controller.devfile.io/mount-as': 'file', 'controller.devfile.io/mount-path': '/etc/maas'}})
        restart_playground(kube, config, pending['id'])
        # Check consumers before retiring the old key. A retry resumes pending state.
        wait_consumers(kube, config, pending['id'])
        if state.get('id') and state['id'] != pending['id']:
            request(management, 'DELETE', ep['management'] + '/' + quote(state['id'], safe=''), allowed=(200, 204, 404))
        state.update(state.pop('pending'))
        save_state(kube, config, state)
    else:
        inference.headers['Authorization'] = 'Bearer ' + state['key']
        completion(inference, ep)
        if config.get('startup'):
            # A sync must validate the current consumers even if the scheduled
            # provisioner has already issued a working key.
            restart_playground(kube, config, state['id'])
            wait_consumers(kube, config, state['id'])


def restart_playground(kube, config, generation):
    if not config['playgroundEnabled']:
        return
    path = f"/apis/ogx.io/v1beta1/namespaces/{config['workspaceNamespace']}/ogxservers/{config['playground']}"
    obj = kube.get(path)
    env = obj['spec']['workload']['overrides']['env']
    desired = {'name': 'MAAS_KEY_GENERATION', 'value': generation}
    updated = [item for item in env if item['name'] != 'MAAS_KEY_GENERATION'] + [desired]
    if env != updated:
        kube.call('PATCH', path, {'spec': {'workload': {'overrides': {'env': updated}}}})


def playground_completion(http, url, model):
    result = request(http, 'POST', url.rstrip('/') + '/v1/chat/completions', json={
        'model': 'maas-vllm-inference-1/publishers/llm/models/' + model,
        'messages': [{'role': 'user', 'content': 'Say hello.'}],
        'max_tokens': 256, 'temperature': 0, 'stream': False,
    }).json()
    choices = result.get('choices', [])
    if not choices or not (choices[0].get('message', {}).get('content') or '').strip():
        raise ProvisionError('Playground inference returned no text')
    return True


def wait_consumers(kube, config, generation):
    def ready():
        ns = config['workspaceNamespace']
        dw = kube.get(f"/apis/workspace.devfile.io/v1alpha2/namespaces/{ns}/devworkspaces/{config['workspace']}")
        if dw['spec'].get('started') and dw.get('status', {}).get('phase') != 'Running':
            return False
        if config['playgroundEnabled']:
            ogx = kube.get(f"/apis/ogx.io/v1beta1/namespaces/{ns}/ogxservers/{config['playground']}")
            if ogx.get('status', {}).get('phase') != 'Ready':
                return False
            deployments = kube.get(f'/apis/apps/v1/namespaces/{ns}/deployments')['items']
            owned = [d for d in deployments if any(
                ref.get('uid') == ogx['metadata']['uid'] for ref in d['metadata'].get('ownerReferences', []))]
            if not owned:
                return False
            for deployment in owned:
                status = deployment.get('status', {})
                if status.get('observedGeneration', 0) < deployment['metadata']['generation']:
                    return False
                if status.get('updatedReplicas', 0) < 1 or status.get('availableReplicas', 0) < 1:
                    return False
                if not any(env.get('name') == 'MAAS_KEY_GENERATION' and env.get('value') == generation
                           for container in deployment['spec']['template']['spec']['containers']
                           for env in container.get('env', [])):
                    return False
            # Inference through Llama Stack confirms its provider credentials and DB startup.
            url = ogx.get('status', {}).get('serviceURL', '').rstrip('/')
            if not url:
                return False
            with session() as http:
                playground_completion(http, url, config['model'])
        return True
    retry(ready, config['timeout'])


def save_state(kube, config, state):
    kube.save_secret(config['controlNamespace'], 'tenant-provisioner-state', {'state.json': json.dumps(state)})


def reconcile(kube, config):
    __tracebackhide__ = True
    with lease(kube, config['controlNamespace'], config.get('leaseDuration', 1500)):
        credentials = retry(lambda: decode(kube.secret(config['controlNamespace'], 'tenant-credentials')),
                            config['timeout'])
        state = json.loads(decode(kube.secret(config['controlNamespace'], 'tenant-provisioner-state')).get('state.json', '{}'))
        retry(lambda: (group_membership(kube, config), True)[1], config['timeout'])
        print('Reconciling Rocket.Chat account', flush=True)
        rocket_setup(kube, config, credentials, state)
        save_state(kube, config, state)
        print('Waiting for deployed MaaS model', flush=True)
        ep = retry(lambda: endpoint(kube), config['timeout'])
        if ep['model'] != config['model']:
            raise ProvisionError('Configured tenant model differs from deployed model')
        print('Waiting for tenant OAuth login', flush=True)
        with tenant_login(kube, config['username'], credentials['password'], config['timeout']) as token:
            with tempfile.TemporaryDirectory() as directory:
                ca = Path(directory) / 'ca.crt'
                ca.write_text(ep['ca'])
                with session(str(ca)) as management, session(str(ca)) as inference:
                    management.headers['Authorization'] = 'Bearer ' + token
                    print('Reconciling tenant MaaS key', flush=True)
                    rotate(kube, config, management, inference, ep, state)
        print('Tenant reconciliation complete', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--startup', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path('/config/config.json').read_text())
        if args.startup:
            config['startup'] = True
            config['timeout'] = config['startupTimeout']
            # Keep the lock longer than the entire Job; otherwise a scheduled
            # run could take it while cold-start provisioning is still active.
            config['leaseDuration'] = config['startupDeadline'] + 60
        reconcile(Kube(), config)
    except LeaseBusy:
        print('Another tenant reconciliation is already running', flush=True)
        # A startup hook must actually verify readiness before completing. Argo
        # can retry the hook; a scheduled run can safely skip an overlapping run.
        return 1 if args.startup else 0
    except Exception as error:
        # requests exceptions and tracebacks can contain tokens or passwords.
        reason = str(error) if isinstance(error, ProvisionError) else type(error).__name__
        print('Tenant reconciliation failed: ' + reason, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
