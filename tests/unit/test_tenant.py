"""Offline checks for tenant isolation, Helm wiring, and key lifecycle."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('tenant_provision', ROOT / 'charts/tenant-workspace/files/provision.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class Kube:
    def __init__(self, public=None):
        self.values = {'maas-api-key': public or {}}

    def secret(self, namespace, name):
        return {'data': {k: base64.b64encode(v.encode()).decode() for k, v in self.values.get(name, {}).items()}}

    def save_secret(self, namespace, name, values, metadata=None):
        self.values[name] = values.copy()


class KeyLifecycle(unittest.TestCase):
    def setUp(self):
        self.config = {'workspaceNamespace': 'wksp-user1', 'controlNamespace': 'tenant-provisioning',
                       'username': 'user1', 'renewBefore': 1200, 'subscription': 'maas-premium-subscription'}
        self.ep = {'ca': 'ca', 'management': 'https://maas.example/api-keys'}
        self.state = {'id': 'old', 'key': 'old-key', 'expiresAt': 5000}
        self.kube = Kube({'api-key': 'old-key', 'ca.crt': 'ca'})
        self.management, self.inference = Mock(), Mock(headers={})
        self.patches = [patch.object(p.time, 'time', return_value=1000),
                        patch.object(p, 'completion', return_value=True),
                        patch.object(p, 'restart_playground'), patch.object(p, 'wait_consumers'),
                        patch.object(p, 'request')]
        self.now, self.complete, self.restart, self.ready, self.request = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)
        self.request.return_value.json.return_value = {'id': 'new', 'key': 'new-key'}

    def rotate(self):
        p.rotate(self.kube, self.config, self.management, self.inference, self.ep, self.state)

    def test_reuses_valid_key_without_restarting_consumers(self):
        self.rotate()
        self.request.assert_not_called()
        self.restart.assert_not_called()
        self.assertEqual(self.inference.headers['Authorization'], 'Bearer old-key')

    def test_startup_rechecks_consumers_when_reusing_a_scheduled_key(self):
        self.config['startup'] = True
        self.rotate()
        self.request.assert_not_called()
        self.restart.assert_called_once_with(self.kube, self.config, 'old')
        self.ready.assert_called_once_with(self.kube, self.config, 'old')

    def test_renews_at_threshold_and_retires_old_key_after_consumers(self):
        self.state['expiresAt'] = 2200
        events = []
        self.ready.side_effect = lambda *args: events.append('ready')
        self.request.side_effect = lambda http, method, *args, **kwargs: (events.append(method), Mock(json=lambda: {'id': 'new', 'key': 'new-key'}))[1]
        self.rotate()
        self.assertEqual(events, ['POST', 'ready', 'DELETE'])
        self.assertEqual(self.kube.values['maas-api-key']['api-key'], 'new-key')
        self.assertEqual(self.state['id'], 'new')
        self.assertNotIn('pending', self.state)

    def test_bad_replacement_preserves_old_key(self):
        self.state['expiresAt'] = 2000
        self.complete.side_effect = p.ProvisionError('backend unavailable')
        with self.assertRaises(p.ProvisionError):
            self.rotate()
        self.assertEqual(self.kube.values['maas-api-key']['api-key'], 'old-key')
        self.restart.assert_not_called()
        self.assertEqual(self.state['id'], 'old')
        self.assertNotIn('pending', self.state)
        self.assertTrue(self.request.call_args.args[2].endswith('/new'))

    def test_consumer_failure_retains_both_keys_and_resumes_pending(self):
        self.state['expiresAt'] = 2000
        self.ready.side_effect = p.ProvisionError('not ready')
        with self.assertRaises(p.ProvisionError):
            self.rotate()
        self.assertEqual(self.state['id'], 'old')
        self.assertEqual(self.state['pending']['id'], 'new')
        self.assertEqual([c.args[1] for c in self.request.call_args_list], ['POST'])
        self.ready.side_effect = None
        self.rotate()
        self.assertEqual([c.args[1] for c in self.request.call_args_list], ['POST', 'DELETE'])
        self.assertEqual(self.state['id'], 'new')

    def test_failed_pending_key_restores_previous_published_key(self):
        self.state['pending'] = {'id': 'new', 'key': 'new-key', 'expiresAt': 2000}
        self.kube.values['maas-api-key']['api-key'] = 'new-key'
        self.complete.side_effect = p.ProvisionError('expired')
        with self.assertRaises(p.ProvisionError):
            self.rotate()
        self.assertEqual(self.kube.values['maas-api-key']['api-key'], 'old-key')
        self.restart.assert_called_once_with(self.kube, self.config, 'old')

    def test_group_membership_is_additive_and_idempotent(self):
        kube = Mock()
        group = {'users': ['admin', 'another-user']}
        kube.get.return_value = group
        config = dict(self.config, group='premium-users')
        p.group_membership(kube, config)
        self.assertEqual(group['users'], ['admin', 'another-user', 'user1'])
        kube.call.reset_mock()
        p.group_membership(kube, config)
        kube.call.assert_not_called()


class Credentials(unittest.TestCase):
    def test_changed_rocketchat_password_updates_existing_account(self):
        config = {'rocketchatEnabled': True, 'rocketchatUrl': 'http://rocket',
                  'timeout': 1, 'username': 'user1',
                  'workspaceNamespace': 'wksp-user1', 'domain': 'apps.example.test'}
        credentials = {'password': 'new-password-with-"-and-$'}
        state = {'rocketPasswordHash': 'previous-hash'}
        kube = Kube()
        http = MagicMock(headers={})
        def response(_http, method, url, **kwargs):
            if url.endswith('/api/v1/login'):
                return Mock(json=lambda: {'status': 'success', 'data': {'authToken': 'token', 'userId': 'id'}})
            if url.endswith('/api/v1/users.info'):
                return Mock(json=lambda: {'user': {'_id': 'tenant-id'}})
            return Mock(json=lambda: {'success': True})
        admin = {'username': 'discovered-admin', 'password': 'discovered-password'}
        with patch.object(p, 'session') as make_http, patch.object(p, 'request', side_effect=response) as request, \
                patch.object(p, 'rocket_admin_credentials', side_effect=[p.ProvisionError('not ready'), admin]) as discover, \
                patch.object(p.time, 'sleep') as sleep:
            make_http.return_value.__enter__.return_value = http
            p.rocket_setup(kube, config, credentials, state)
        self.assertEqual(discover.call_count, 2)
        sleep.assert_called_once()
        logins = [c.kwargs['json'] for c in request.call_args_list if c.args[2].endswith('/api/v1/login')]
        self.assertEqual(logins[0], {'user': admin['username'], 'password': admin['password']})
        updates = [c for c in request.call_args_list if c.args[2].endswith('/api/v1/users.update')]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].kwargs['json']['data']['password'], credentials['password'])
        self.assertEqual(updates[0].kwargs['json']['data']['roles'], ['user'])
        self.assertNotEqual(state['rocketPasswordHash'], 'previous-hash')
        self.assertNotIn('password', kube.values['rocketchat-user-credentials'])

    def test_existing_rocketchat_account_is_not_recreated_on_rerun(self):
        password = 'generated-password'
        state = {'rocketPasswordHash': p.hashlib.sha256(password.encode()).hexdigest()}
        kube = Kube()
        kube.values['rocketchat-user-credentials'] = {'auth-token': 'existing'}
        config = {'rocketchatEnabled': True, 'workspaceNamespace': 'wksp-user1'}
        with patch.object(p, 'request') as request:
            p.rocket_setup(kube, config, {'password': password}, state)
        request.assert_not_called()

    def test_shared_lease_blocks_overlapping_initial_and_scheduled_jobs(self):
        kube = Mock()
        kube.call.return_value = {'spec': {'holderIdentity': 'another-job',
            'renewTime': p.dt.datetime.now(p.dt.timezone.utc).isoformat(), 'leaseDurationSeconds': 1500}}
        with self.assertRaises(p.ProvisionError):
            with p.lease(kube, 'tenant-provisioning'):
                self.fail('Must not acquire a held lease')
        self.assertEqual([call.args[0] for call in kube.call.call_args_list], ['GET'])

    def test_provisioner_creates_lease_when_argocd_excludes_leases(self):
        kube = Mock()
        kube.call.side_effect = [{}, {'metadata': {'resourceVersion': '1'}, 'spec': {}}, {}, {}]
        kube.get.side_effect = lambda path: {
            'metadata': {'resourceVersion': '2'},
            'spec': dict(kube.call.call_args_list[2].args[2]['spec']),
        }
        with p.lease(kube, 'tenant-provisioning'):
            pass
        self.assertEqual([call.args[0] for call in kube.call.call_args_list], ['GET', 'POST', 'PUT', 'PUT'])
        self.assertEqual(kube.call.call_args_list[1].args[2]['kind'], 'Lease')
        self.assertEqual(kube.call.call_args_list[-1].args[2]['spec']['holderIdentity'], '')

    def test_provisioner_handles_concurrent_lease_creation(self):
        kube = Mock()
        kube.call.side_effect = [{}, {'kind': 'Status', 'code': 409}]
        with self.assertRaises(p.LeaseBusy):
            with p.lease(kube, 'tenant-provisioning'):
                self.fail('Must not acquire a lease created concurrently')


class RocketAdminDiscovery(unittest.TestCase):
    def setUp(self):
        self.config = {'rocketchatNamespace': 'chat', 'rocketchatDeployment': 'chat-server',
                       'rocketchatContainer': 'rocketchat'}
        self.kube = Mock()

    def deployment(self, **environment):
        self.kube.get.return_value = {'spec': {'template': {'spec': {'containers': [
            {'name': 'sidecar', 'env': [{'name': 'ADMIN_PASS', 'value': 'wrong-container'}]},
            {'name': 'rocketchat', **environment},
        ]}}}}

    def test_literal_credentials_come_from_selected_deployment_container(self):
        self.deployment(env=[{'name': 'ADMIN_USERNAME', 'value': 'installed-admin'},
                             {'name': 'ADMIN_PASS', 'value': 'installed-password'}])
        self.assertEqual(p.rocket_admin_credentials(self.kube, self.config),
                         {'username': 'installed-admin', 'password': 'installed-password'})
        self.kube.get.assert_called_once_with('/apis/apps/v1/namespaces/chat/deployments/chat-server')
        self.kube.secret.assert_not_called()

    def test_secret_key_references_are_resolved_in_rocketchat_namespace(self):
        self.deployment(env=[{'name': 'ADMIN_USERNAME', 'value': 'admin'},
                             {'name': 'ADMIN_PASS', 'valueFrom': {
                                 'secretKeyRef': {'name': 'generated-admin', 'key': 'password'}}}])
        self.kube.secret.return_value = {'data': {'password': base64.b64encode(b'generated-password').decode()}}
        self.assertEqual(p.rocket_admin_credentials(self.kube, self.config)['password'], 'generated-password')
        self.kube.secret.assert_called_once_with('chat', 'generated-admin')

    def test_envfrom_secret_and_configmap_explicit_env_takes_precedence(self):
        self.deployment(envFrom=[{'secretRef': {'name': 'admin'}},
                                  {'prefix': 'ADMIN_', 'configMapRef': {'name': 'admin-config'}}],
                        env=[{'name': 'ADMIN_USERNAME', 'value': 'explicit-admin'}])
        deployment = self.kube.get.return_value
        self.kube.get.side_effect = [deployment, {'data': {'USERNAME': 'configmap-admin'}}]
        self.kube.secret.return_value = {'data': {
            'ADMIN_PASS': base64.b64encode(b'generated-password').decode(),
            'ADMIN_USERNAME': base64.b64encode(b'secret-admin').decode()}}
        self.assertEqual(p.rocket_admin_credentials(self.kube, self.config),
                         {'username': 'explicit-admin', 'password': 'generated-password'})

    def test_missing_credentials_fail_without_a_prompt_or_secret_in_error(self):
        self.deployment(env=[{'name': 'ADMIN_PASS', 'value': 'do-not-log-this'}])
        with self.assertRaisesRegex(p.ProvisionError, '^Rocket.Chat admin credentials not available$'):
            p.rocket_admin_credentials(self.kube, self.config)


class StartupLifecycle(unittest.TestCase):
    config = {'timeout': 1200, 'startupTimeout': 5400, 'startupDeadline': 7200}

    def invoke(self, argv, error=None):
        with patch.object(p.Path, 'read_text', return_value=json.dumps(self.config)), \
                patch.object(p, 'Kube') as kube, patch.object(p, 'reconcile', side_effect=error) as reconcile, \
                patch('builtins.print'):
            status = p.main(argv)
        return status, reconcile.call_args.args[1]

    def test_startup_has_cold_start_budget_and_lock_outlives_job(self):
        status, config = self.invoke(['--startup'])
        self.assertEqual(status, 0)
        self.assertEqual(config['timeout'], 5400)
        self.assertGreater(config['leaseDuration'], config['startupDeadline'])

    def test_scheduled_run_keeps_shorter_timeout_and_default_lock(self):
        status, config = self.invoke([])
        self.assertEqual(status, 0)
        self.assertEqual(config['timeout'], 1200)
        self.assertNotIn('leaseDuration', config)

    def test_busy_startup_fails_for_argocd_retry_instead_of_claiming_readiness(self):
        status, _ = self.invoke(['--startup'], p.LeaseBusy('busy'))
        self.assertEqual(status, 1)
        status, _ = self.invoke([], p.LeaseBusy('busy'))
        self.assertEqual(status, 0)

    def test_fifty_minute_model_cold_start_fits_startup_budget(self):
        elapsed = [0]
        def advance(seconds):
            elapsed[0] += seconds
        with patch.object(p.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(p.time, 'sleep', side_effect=advance):
            result = p.retry(lambda: elapsed[0] >= 3000, self.config['startupTimeout'], interval=600)
        self.assertTrue(result)
        self.assertGreater(elapsed[0], self.config['timeout'])

    def test_long_running_client_reloads_projected_serviceaccount_token(self):
        kube = object.__new__(p.Kube)
        kube.http = Mock(headers={})
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {}
        with tempfile.TemporaryDirectory() as directory, patch.object(p, 'request', return_value=response):
            kube.token_file = Path(directory) / 'token'
            kube.token_file.write_text('first-token')
            kube.get('/api/v1/namespaces')
            self.assertEqual(kube.http.headers['Authorization'], 'Bearer first-token')
            kube.token_file.write_text('rotated-token')
            kube.get('/api/v1/namespaces')
            self.assertEqual(kube.http.headers['Authorization'], 'Bearer rotated-token')


class PlaygroundInference(unittest.TestCase):
    def test_uses_openai_endpoint_and_provider_qualified_model(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'Hello!'}}]}
        with patch.object(p, 'request', return_value=response) as request:
            self.assertTrue(p.playground_completion('http-session', 'http://playground:8321/', 'model1'))
        request.assert_called_once_with('http-session', 'POST', 'http://playground:8321/v1/chat/completions', json={
            'model': 'maas-vllm-inference-1/publishers/llm/models/model1',
            'messages': [{'role': 'user', 'content': 'Say hello.'}],
            'max_tokens': 256, 'temperature': 0, 'stream': False,
        })

    def test_empty_generated_text_fails_readiness(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': '  '}}]}
        with patch.object(p, 'request', return_value=response):
            with self.assertRaisesRegex(p.ProvisionError, 'Playground inference returned no text'):
                p.playground_completion('http-session', 'http://playground:8321', 'model1')

    def test_reasoning_without_final_content_is_not_a_successful_completion(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': None, 'reasoning_content': 'Thinking'}}]}
        with patch.object(p, 'request', return_value=response):
            with self.assertRaisesRegex(p.ProvisionError, 'Playground inference returned no text'):
                p.playground_completion('http-session', 'http://playground:8321', 'model1')


class Charts(unittest.TestCase):
    @staticmethod
    def render(chart, *arguments):
        output = subprocess.check_output(['helm', 'template', chart, str(ROOT / 'charts' / chart),
                                         '--namespace', 'tenant-provisioning', *arguments], text=True)
        return list(yaml.safe_load_all(output))

    def test_default_tenant_keeps_setup_credentials_out_of_workspace(self):
        docs = self.render('tenant-workspace')
        config = json.loads(next(d for d in docs if d['kind'] == 'ConfigMap' and
                                 d['metadata']['name'] == 'tenant-provisioner')['data']['config.json'])
        self.assertEqual(config['workspaceNamespace'], 'wksp-user1')
        self.assertEqual(config['subscription'], 'maas-premium-subscription')
        maas_secret = next(d for d in docs if d['kind'] == 'Secret' and d['metadata']['name'] == 'maas-api-key')
        self.assertEqual(maas_secret['metadata']['labels']['controller.devfile.io/watch-secret'], 'true')
        credentials = next(d for d in docs if d['kind'] == 'ExternalSecret' and d['metadata']['name'] == 'tenant-credentials')
        self.assertNotEqual(credentials['metadata'].get('namespace'), 'wksp-user1')
        self.assertEqual({f['secretKey'] for f in credentials['spec']['data']}, {'password'})
        discovery = next(d for d in docs if d['kind'] == 'Role' and d['metadata']['name'] == 'tenant-rocketchat-discovery')
        self.assertEqual(discovery['metadata']['namespace'], 'rocketchat')
        self.assertTrue(all(rule['verbs'] == ['get'] for rule in discovery['rules']))
        jobs = [d for d in docs if d['kind'] in ('Job', 'CronJob')]
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(d['metadata'].get('namespace') != 'wksp-user1' for d in jobs))
        self.assertFalse(any(d['kind'] == 'ClusterRoleBinding' and
                             any(s['kind'] == 'User' for s in d.get('subjects', [])) for d in docs))

    def test_custom_username_domain_and_credential_mapping(self):
        args = ('--set', 'global.tenant.username=alice', '--set', 'global.tenant.namespacePrefix=demo',
                '--set', 'global.localClusterDomain=apps.example.test')
        docs = self.render('tenant-workspace', *args)
        namespace = next(d for d in docs if d['kind'] == 'Namespace')
        self.assertEqual(namespace['metadata']['name'], 'demo-alice')
        workspace = next(d for d in docs if d['kind'] == 'DevWorkspace')
        env = {e['name']: e['value'] for e in workspace['spec']['template']['components'][0]['container']['env']}
        self.assertEqual(env['MAAS_BASE_URL'], 'https://maas.apps.example.test/v1')
        self.assertEqual(env['MAAS_MODEL'], 'publishers/llm/models/nemotron-3-nano-30b-a3b')
        login = self.render('user-config', *args)
        self.assertEqual(login[0]['spec']['data'][0]['remoteRef']['property'], 'alice')
        self.assertIn('htpasswd "alice"', login[0]['spec']['target']['template']['data']['htpasswd'])

    def test_optional_components_and_argocd_runtime_fields(self):
        docs = self.render('tenant-workspace', '--set', 'playground.enabled=false', '--set', 'rocketchat.enabled=false')
        self.assertFalse(any(d['kind'] in ('OGXServer', 'Deployment', 'PersistentVolumeClaim') for d in docs))
        self.assertFalse(any(d['metadata']['name'] == 'tenant-rocketchat-discovery' for d in docs))
        values = yaml.safe_load((ROOT / 'variants/prod/values-prod.yaml').read_text())
        app = values['clusterGroup']['applications']['tenant-workspace']
        self.assertTrue(any(d['kind'] == 'DevWorkspace' and '/spec/started' in d['jsonPointers'] for d in app['ignoreDifferences']))

    def test_initial_setup_is_retriable_sync_hook_with_separate_startup_budget(self):
        docs = self.render('tenant-workspace')
        job = next(d for d in docs if d['kind'] == 'Job')
        annotations = job['metadata']['annotations']
        self.assertEqual(annotations['argocd.argoproj.io/hook'], 'Sync')
        self.assertEqual(set(annotations['argocd.argoproj.io/hook-delete-policy'].split(',')),
                         {'BeforeHookCreation', 'HookSucceeded'})
        self.assertEqual(job['spec']['activeDeadlineSeconds'], 7200)
        self.assertEqual(job['spec']['template']['spec']['containers'][0]['args'], ['--startup'])
        cron = next(d for d in docs if d['kind'] == 'CronJob')
        self.assertEqual(cron['spec']['jobTemplate']['spec']['activeDeadlineSeconds'], 1440)
        self.assertNotIn('args', cron['spec']['jobTemplate']['spec']['template']['spec']['containers'][0])

    def test_secret_template_requires_no_interactive_input(self):
        template = yaml.safe_load((ROOT / 'values-secret.yaml.template').read_text())
        fields = [field for secret in template['secrets'] for field in secret['fields']]
        self.assertTrue(fields)
        self.assertTrue(all(field['onMissingValue'] == 'generate' for field in fields))
        self.assertNotIn('rocketchat-admin-password', {field['name'] for field in fields})

    def test_playground_does_not_register_provider_discovered_maas_model_twice(self):
        docs = self.render('tenant-workspace')
        configmap = next(d for d in docs if d['kind'] == 'ConfigMap' and
                         d['metadata']['name'] == 'llama-stack-config')
        config = yaml.safe_load(configmap['data']['config.yaml'])
        providers = config['providers']['inference']
        self.assertTrue(any(provider['provider_id'] == 'maas-vllm-inference-1' for provider in providers))
        models = config['registered_resources']['models']
        self.assertFalse(any(model['provider_id'] == 'maas-vllm-inference-1' for model in models))
        self.assertTrue(any(model['provider_id'] == 'sentence-transformers' for model in models))


if __name__ == '__main__':
    unittest.main()
