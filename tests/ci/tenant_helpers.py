"""Tenant checks share the provisioner's TLS-verified OAuth login implementation."""
import importlib.util
import json
from pathlib import Path

import pytest
from kubernetes import client

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('tenant_provision', ROOT / 'charts/tenant-workspace/files/provision.py')
provision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision)


class BootstrapKube(provision.Kube):
    def __init__(self, api_client):
        self.api_client = api_client

    def call(self, method, path, body=None, allowed=(200,)):
        __tracebackhide__ = True
        try:
            data, status, _ = self.api_client.call_api(
                path, method, body=body, response_type='object',
                auth_settings=['BearerToken'], _return_http_data_only=False,
                header_params={'Content-Type': 'application/json'},
            )
        except client.ApiException as error:
            if error.status in allowed:
                return {}
            pytest.fail(f'Tenant bootstrap API returned HTTP {error.status}', pytrace=False)
        if status not in allowed:
            pytest.fail(f'Tenant bootstrap API returned HTTP {status}', pytrace=False)
        return data or {}


def runtime(openshift_dyn_client):
    kube = BootstrapKube(openshift_dyn_client.client)
    config = json.loads(kube.get('/api/v1/namespaces/tenant-provisioning/configmaps/tenant-provisioner')['data']['config.json'])
    return kube, config


def allowed(http, host, namespace, verb, resource, group=''):
    __tracebackhide__ = True
    result = provision.request(http, 'POST', host + '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews',
                               allowed=(201,), json={
        'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
        'spec': {'resourceAttributes': {'namespace': namespace, 'verb': verb, 'resource': resource, 'group': group}},
    }).json()
    return result['status']['allowed']
