"""One fixed synthetic GCS object; no application imports or execution entrypoint."""
import json

BUCKET = 'qsl-runtime-logs-shared'
OBJECT = 'notification-markers/ibkr/preflight/3c5b1830-9184-4966-9232-f8f1e290fd18.json'
PRINCIPAL = 'ibkr-platform-runtime@interactivebrokersquant.iam.gserviceaccount.com'
NONCE = '3c5b1830-9184-4966-9232-f8f1e290fd18'


class Stop(Exception):
    def __init__(self, reason, status=None):
        self.reason, self.status = reason, status


def phase(number):
    return {'schema_version': 1, 'probe_id': NONCE, 'phase': number}


def generation(value):
    result = value.get('generation') if isinstance(value, dict) else None
    if not isinstance(result, str) or not result.isdecimal() or int(result) <= 0:
        raise Stop('generation_unknown')
    return result


def qualify(client):
    result = {'schema_version': 1, 'status': 'stop', 'cleanup_attempted': False,
              'residual_generations': [], 'write_outcome_unknown': False, 'production_marker_touched': False}
    owned = []
    def read_own(g, number):
        meta = client.request('GET', OBJECT, query={'generation': g, 'fields': 'generation'})
        if generation(meta) != g:
            raise Stop('generation_mismatch')
        value = client.request('GET', OBJECT, query={'generation': g, 'alt': 'media'})
        if value != phase(number):
            raise Stop('own_payload_mismatch')
    def write(number, match):
        result['write_outcome_unknown'] = True
        try:
            value = client.request('POST', OBJECT, json.dumps(phase(number), separators=(',', ':')).encode(),
                                   {'ifGenerationMatch': str(match), 'fields': 'generation'})
        except Stop as error:
            if error.reason in {'permission_denied', 'precondition_failed'}:
                result['write_outcome_unknown'] = False
            raise
        g = generation(value)
        owned.append(g)
        result['write_outcome_unknown'] = False
        return g
    def negative(number, match):
        try:
            write(number, match)
        except Stop as error:
            if error.reason == 'precondition_failed' and error.status == 412:
                return
            raise
        raise Stop('precondition_unexpected_success')
    try:
        try:
            client.request('GET', OBJECT, query={'fields': 'generation'})
        except Stop as error:
            if error.reason != 'not_found' or error.status != 404:
                raise
        else:
            raise Stop('object_already_exists')
        g1 = write(1, 0)
        read_own(g1, 1)
        negative(1, 0)
        read_own(g1, 1)
        g2 = write(2, g1)
        if g2 == g1:
            raise Stop('generation_not_advanced')
        read_own(g2, 2)
        negative(2, g1)
        read_own(g2, 2)
        if generation(client.request('GET', OBJECT, query={'fields': 'generation'})) != g2:
            raise Stop('live_generation_changed')
        result['CAS_checks_passed'] = True
        result.update(status='pass_retained', reason='qualified_retained_without_cleanup',
                      observed_generations=list(owned), retained_live_generation=g2, old_generation_retention='unknown', probe_id=NONCE)
    except Stop as error:
        # No network after denial/unknown: exact remaining generations are reported.
        result.update(reason=error.reason, http_status=error.status)
    except Exception:
        result.update(reason='local_unknown', http_status=None)
    result['residual_generations'] = owned
    return result


class RuntimeClient:
    def __init__(self):
        import time
        import urllib.request
        import urllib.error
        import urllib.parse
        import ssl
        self.time, self.parse = time, urllib.parse
        self.deadline = time.monotonic() + 45
        self.count = 0
        self.token = None
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.request_type, self.http_error = urllib.request.Request, urllib.error.HTTPError
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        email = self._http('GET', 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email', metadata=True)
        if email.decode().strip() != PRINCIPAL:
            raise Stop('runtime_principal_mismatch')
        token = json.loads(self._http('GET', 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token', metadata=True))
        if not isinstance(token.get('access_token'), str) or not token['access_token']:
            raise Stop('runtime_token_unavailable')
        self.token = token['access_token']

    def _http(self, method, url, body=None, metadata=False):
        parts = self.parse.urlsplit(url)
        if (parts.scheme, parts.hostname) not in {('http', 'metadata.google.internal'), ('https', 'storage.googleapis.com')}:
            raise Stop('endpoint_rejected')
        if metadata != (parts.hostname == 'metadata.google.internal') or parts.username or parts.password or parts.fragment:
            raise Stop('endpoint_rejected')
        if metadata and (parts.path not in {
                '/computeMetadata/v1/instance/service-accounts/default/email',
                '/computeMetadata/v1/instance/service-accounts/default/token'} or parts.query or parts.port):
            raise Stop('endpoint_rejected')
        remaining = self.deadline - self.time.monotonic()
        self.count += 1
        if remaining <= 0 or self.count > 24:
            raise Stop('request_budget_exhausted')
        headers = {'Metadata-Flavor': 'Google'} if metadata else {'Authorization': 'Bearer ' + self.token}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            with self.opener.open(self.request_type(url, data=body, headers=headers, method=method), timeout=min(3, remaining)) as response:
                payload = response.read(8193)
                if len(payload) > 8192:
                    raise Stop('response_size_unknown')
                return payload
        except self.http_error as error:
            if error.code in (401, 403):
                raise Stop('permission_denied', error.code) from None
            if error.code in (404, 412):
                try:
                    details = json.loads(error.read(8192))['error']
                    reasons = {x.get('reason') for x in details.get('errors', []) if isinstance(x, dict)}
                    expected = 'notFound' if error.code == 404 else 'conditionNotMet'
                    if details.get('code') == error.code and expected in reasons:
                        raise Stop('not_found' if error.code == 404 else 'precondition_failed', error.code)
                except (ValueError, KeyError, TypeError):
                    pass
            raise Stop('http_status_unknown', error.code) from None
        except Stop:
            raise
        except Exception:
            raise Stop('transport_unknown') from None

    def request(self, method, path, body=None, query=None):
        if path != OBJECT or method not in {'GET', 'POST'}:
            raise Stop('object_or_method_rejected')
        params = dict(query or {})
        if method == 'POST':
            params.update(uploadType='media', name=OBJECT)
            url = 'https://storage.googleapis.com/upload/storage/v1/b/' + BUCKET + '/o'
        else:
            url = 'https://storage.googleapis.com/storage/v1/b/' + BUCKET + '/o/' + self.parse.quote(OBJECT, safe='')
        payload = self._http(method, url + '?' + self.parse.urlencode(params), body)
        try:
            return json.loads(payload)
        except (ValueError, TypeError):
            raise Stop('response_json_unknown') from None


def main():
    import os
    os.environ.clear()  # Probe process only; no credential/proxy/emulator/CA file overrides.
    try:
        result = qualify(RuntimeClient())
        result['runtime_principal_matches'] = True
    except Stop as error:
        result = {'schema_version': 1, 'status': 'stop', 'reason': error.reason, 'http_status': error.status}
    except Exception:
        result = {'schema_version': 1, 'status': 'stop', 'reason': 'local_unknown'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'pass_retained' else 1


if __name__ == '__main__':
    raise SystemExit(main())
