"""Cloud-only entry point. Never print exception text or configuration."""
import json
import logging
import os
import sys
import time

from application.flex_daily.runner import Fetcher, collect, read_token
from application.flex_daily.storage import Store
from application.flex_daily.watch import send, watch


def main():
    logging.disable(logging.CRITICAL)
    store = Store(os.environ['FLEX_BUCKET'])
    mode = sys.argv[1]
    if mode == 'pull':
        config, _ = store.json('state/config.json')
        if not config or config.get('ip_restriction') != 'none':
            _, generation = store.json('status/latest.json', {})
            store.put('status/latest.json', {'schema': 1, 'blocked': 'ip_restriction_unconfirmed_or_present',
                                            'reconciliation_status': 'partial'}, generation)
            print(json.dumps({'status': 'configuration_or_ip_blocked'}))
            return 2
        token = read_token('/secrets/flex/token')
        result = collect(store, config, Fetcher(token, config['query_id'], time.monotonic() + 1500))
        print(json.dumps({'status': 'blocked' if result.get('blocked') else 'finished'}))
    elif mode == 'watch':
        config, _ = store.json('status/notification-config.json')
        if not config or not config.get('chat_id') or config.get('chat_type') != 'private':
            print(json.dumps({'status': 'notification_configuration_missing'}))
            return 2
        token = read_token('/secrets/telegram/token')
        result = watch(store, lambda text: send(token, config['chat_id'], text))
        print(json.dumps({'status': result['delivery']}))
    else:
        raise ValueError('invalid_mode')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({'status': 'failed', 'detail': 'private_diagnostics_required'}))
        sys.exit(1)
