#!/usr/bin/env python3
"""Private, fixed-purpose health probe inside the VPN namespace. No shell API."""
import hmac
import ipaddress
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import uuid

KEY = Path('/run/secrets/control.key').read_text().strip()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOCK = threading.Lock()
CACHE = {}
SPEED_LOCK = threading.Lock()
SPEED_JOB = None
SPEED_FINISHED = 0


def speed_update(**fields):
    global SPEED_FINISHED
    with SPEED_LOCK:
        if fields.get('state') in ('succeeded', 'failed'):
            SPEED_FINISHED = time.monotonic()
        SPEED_JOB.update(fields, updated=time.time())


def run_speedtest():
    try:
        before = status(fresh=True)
        if not all(before.get(field) for field in ('running', 'healthy', 'endpoint', 'public_ip')):
            raise RuntimeError('The Proton tunnel must be connected before testing')
        results = {'download_mbps': [], 'upload_mbps': [], 'download_bytes': 50_000_000,
                   'upload_bytes': 20_000_000, 'samples': 3, 'endpoint': before['endpoint'],
                   'public_ip': before['public_ip'], 'provider': 'Cloudflare', 'cloudflare_locations': []}
        deadline = time.monotonic() + 150
        for direction, size in (('download', 50_000_000), ('upload', 20_000_000)):
            for sample in range(3):
                speed_update(stage=f'Testing Proton {direction} {sample + 1}/3')
                url = 'https://speed.cloudflare.com/' + (
                    '__down?bytes=' + str(size) if direction == 'download' else '__up')
                request = urllib.request.Request(url, data=b'\0' * size if direction == 'upload' else None,
                    headers={'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-cache',
                             'User-Agent': 'ProtonNode-speedtest/1'})
                started = time.perf_counter()
                received = 0
                with OPENER.open(request, timeout=15) as response:
                    if response.status != 200:
                        raise RuntimeError('Cloudflare did not accept the speed test')
                    location = response.headers.get('CF-Ray', '').rsplit('-', 1)[-1]
                    while chunk := response.read(1024 * 1024):
                        received += len(chunk)
                        if time.monotonic() > deadline:
                            raise RuntimeError('Speed test exceeded its time limit')
                        if received > (size if direction == 'download' else 65536):
                            raise RuntimeError('Unexpected speed-test response size')
                elapsed = time.perf_counter() - started
                if direction == 'download' and received != size:
                    raise RuntimeError('The speed-test download was incomplete')
                results[direction + '_mbps'].append(round(size * 8 / elapsed / 1_000_000, 2))
                results['cloudflare_locations'].append(location)
        after = status(fresh=True)
        if not after['running'] or not after['healthy'] or before['endpoint'] != after['endpoint'] or before['public_ip'] != after['public_ip']:
            raise RuntimeError('The Proton connection changed during the test; discard these results')
        results.update(download_median_mbps=statistics.median(results['download_mbps']),
                       upload_median_mbps=statistics.median(results['upload_mbps']), measured_at=time.time())
        speed_update(state='succeeded', stage='Proton speed test complete', result=results)
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else 'The speed test could not complete; the VPN configuration was not changed'
        speed_update(state='failed', stage='Proton speed test failed', error=message)


def start_speedtest(identity):
    global SPEED_JOB
    # The controller's operation ID makes an accidental duplicate POST harmless.
    if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
        raise ValueError('Invalid operation ID')
    with SPEED_LOCK:
        if SPEED_JOB and SPEED_JOB['id'] == identity:
            return dict(SPEED_JOB)
        if SPEED_JOB and SPEED_JOB['state'] == 'running':
            raise RuntimeError('A Proton speed test is already running')
        if SPEED_FINISHED and time.monotonic() - SPEED_FINISHED < 30:
            raise RuntimeError('Wait 30 seconds between Proton speed tests')
        SPEED_JOB = dict(id=identity, state='running', stage='Checking the Proton connection',
                         created=time.time(), updated=time.time(), result=None, error=None)
        threading.Thread(target=run_speedtest, daemon=True).start()
        return dict(SPEED_JOB)


def get(url, headers=None, timeout=5):
    with OPENER.open(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return r.read(16384).decode()


def status(fresh=False):
    with LOCK:
        result = {'running': False, 'healthy': False, 'endpoint': None, 'public_ip': None,
                  'checked_at': None, 'carrier_up': False}
        try:
            result['running'] = json.loads(get('http://127.0.0.1:8000/v1/vpn/status',
                                               {'X-API-Key': KEY}))['status'] == 'running'
            get('http://127.0.0.1:9999/')
            result['healthy'] = True
        except Exception:
            pass
        try:
            result['carrier_up'] = bool(int(Path('/sys/class/net/pn-data/flags').read_text(), 16) & 1)
            output = subprocess.check_output(['wg', 'show', 'tun0', 'endpoints'], timeout=3).decode()
            result['endpoint'] = output.split()[1].rsplit(':', 1)[0].strip('[]')
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
        if not result['running'] or not result['endpoint']:
            CACHE.clear()
            return result
        if fresh or CACHE.get('endpoint') != result['endpoint'] or time.time() - CACHE.get('checked_at', 0) > 45:
            CACHE.clear()
            try:
                trace = dict(line.split('=', 1) for line in get('https://1.1.1.1/cdn-cgi/trace', timeout=8).splitlines() if '=' in line)
                address = str(ipaddress.ip_address(trace['ip']))
                CACHE.update(endpoint=result['endpoint'], public_ip=address, checked_at=time.time())
            except Exception:
                pass
        result.update(public_ip=CACHE.get('public_ip'), checked_at=CACHE.get('checked_at'))
        return result


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        pass

    def send_json(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not hmac.compare_digest(self.headers.get('X-API-Key', '').encode(), KEY.encode()):
            self.send_error(401)
            return
        if self.path.startswith('/speedtest/'):
            with SPEED_LOCK:
                job = dict(SPEED_JOB) if SPEED_JOB and SPEED_JOB['id'] == self.path.rsplit('/', 1)[1] else None
            self.send_json(200 if job else 404, job or {'error': 'Speed test not found'})
            return
        if self.path not in ('/status', '/status?fresh=1'):
            self.send_error(404)
            return
        self.send_json(200, status(self.path.endswith('=1')))

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('X-API-Key', '').encode(), KEY.encode()):
            self.send_error(401)
            return
        if self.path == '/speedtest':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 256 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {'id'}:
                    raise ValueError('Expected operation ID only')
                job = start_speedtest(body['id'])
                self.send_json(202, job)
            except (ValueError, TypeError):
                self.send_json(400, {'error': 'Invalid speed-test request'})
            except RuntimeError as error:
                self.send_json(409, {'error': str(error)})
            return
        if self.path != '/restart':
            self.send_error(404)
            return
        self.send_response(202)
        self.send_header('Content-Length', '0')
        self.end_headers()
        # Docker restarts PID 1, clearing a stuck Gluetun health-check context.
        threading.Timer(0.5, lambda: os.kill(1, signal.SIGTERM)).start()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8001), Handler).serve_forever()
