"""One-shot standard-library TCP probe; no application or broker protocol."""
import json
import os
import socket


def probe(host, port):
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            return False, "name_resolution_failed"
        family, kind, protocol, _, address = addresses[0]
        with socket.socket(family, kind, protocol) as connection:
            connection.settimeout(5)
            connection.connect(address)
        return True, None
    except socket.gaierror:
        return False, "name_resolution_failed"
    except TimeoutError:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "connection_refused"
    except OSError:
        return False, "network_error"


if __name__ == "__main__":
    connected, failure = probe(os.environ["PILOT_TCP_HOST"], int(os.environ["PILOT_TCP_PORT"]))
    print(json.dumps(dict(schema_version="ibkr.cloud_tcp_probe.v1", tcp_connected=connected,
        failure_class=failure, broker_protocol_sent=False, broker_called=False,
        strategy_imported=False)), flush=True)
