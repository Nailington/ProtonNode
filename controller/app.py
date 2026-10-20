#!/usr/bin/env python3
"""ProtonNode controller: tailnet identity, durable operations, verified rollback.

Only Python's standard library. No Docker socket, arbitrary proxy, or shell API.
"""
import argparse
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC = Path(__file__).with_name('static')
TERMINAL = ('succeeded', 'failed', 'interrupted')


class Problem(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get('TS_SOCKET', '/var/run/tailscale/tailscaled.sock'))


def local_api(path):
    connection = UnixConnection('local-tailscaled.sock', timeout=3)
    try:
        connection.request('GET', '/localapi/v0/' + path)
        response = connection.getresponse()
        if response.status != 200:
            raise OSError('Tailscale identity unavailable')
        return json.loads(response.read(2_000_000))
    finally:
        connection.close()


def payload(server, mode):
    return {'provider': {'server_selection': {
        'countries': [server['country']], 'cities': [server['city']], 'hostnames': [server['hostname']],
        'regions': [], 'names': [server['name']], 'numbers': [], 'categories': [], 'isps': [],
        'free_only': False, 'stream_only': False, 'secure_core_only': False,
        'tor_only': mode == 'tor', 'port_forward_only': mode == 'p2p',
    }}}


class Backend:
    def __init__(self, key):
        self.key = key
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, probe=False):
        # Destinations are fixed and cannot be supplied through the API.
        base = 'http://gluetun:8001' if probe else 'http://gluetun:8000'
        request = urllib.request.Request(base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-API-Key': self.key, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=18) as response:
            data = response.read(65536)
            # Gluetun's settings PATCH is exposed as PUT and returns plain text,
            # while its status endpoints return JSON.
            if not probe and method == 'PUT' and path == '/v1/vpn/settings':
                return {'outcome': data.decode()}
            return json.loads(data) if data else {}

    def probe(self, fresh=False):
        return self.request('GET', '/status' + ('?fresh=1' if fresh else ''), probe=True)

    def stop(self):
        self.request('PUT', '/v1/vpn/status', {'status': 'stopped'})
        deadline = time.monotonic() + 25
        while self.request('GET', '/v1/vpn/status').get('status') != 'stopped':
            if time.monotonic() > deadline:
                raise Problem('VPN did not stop in time', 502)
            time.sleep(0.3)
        time.sleep(1)  # Drain Gluetun's cancelled health checks before changing settings.

    def apply(self, selection):
        self.stop()
        self.request('PUT', '/v1/vpn/settings', payload(selection['server'], selection['mode']))
        self.request('PUT', '/v1/vpn/status', {'status': 'running'})

    def restart(self):
        return self.request('POST', '/restart', probe=True)

    def speedtest(self, identity, progress, stopping):
        try:
            job = self.request('POST', '/speedtest', {'id': identity}, probe=True)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 409:
                raise Problem('A Proton test is running or cooling down. Wait 30 seconds and try again.', 409)
            raise Problem('The VPN speed-test service is unavailable', 502)
        deadline = time.monotonic() + 200
        while job['state'] == 'running':
            progress(job['stage'])
            if stopping.wait(1) or time.monotonic() > deadline:
                raise Problem('The speed test was interrupted or exceeded its time limit', 502)
            job = self.request('GET', '/speedtest/' + identity, probe=True)
        if job['state'] != 'succeeded':
            raise Problem(job.get('error') or 'Proton speed test did not complete', 502)
        return job['result']


class DemoBackend:
    def __init__(self):
        self.current = '198.51.100.20'
        self.running = True

    def probe(self, fresh=False):
        return dict(running=self.running, healthy=self.running, endpoint=self.current if self.running else None,
                    public_ip='203.0.113.42' if self.running else None, checked_at=time.time(), carrier_up=True)

    def stop(self):
        self.running = False
        time.sleep(0.3)

    def apply(self, selection):
        self.stop()
        time.sleep(1)
        self.current = selection['server']['ips'][0]
        self.running = True

    def restart(self):
        self.running = True

    def speedtest(self, identity, progress, stopping):
        progress('Preview: synthetic Proton speed test')
        stopping.wait(1)
        return dict(download_mbps=[150, 160, 155], upload_mbps=[30, 32, 31],
                    download_median_mbps=155, upload_median_mbps=31,
                    endpoint=self.current, public_ip='203.0.113.42', measured_at=time.time(), samples=3,
                    download_bytes=50_000_000, upload_bytes=20_000_000, provider='Synthetic preview')


class Controller:
    def __init__(self, directory, catalog, key, backend=None, demo=False):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.catalog_path = Path(catalog)
        self.key = key
        self.backend = backend or Backend(key)
        self.demo = demo
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.catalog_cache = (0, [])
        self.probe_cache = (0, {})
        self.probe_lock = threading.Lock()
        self.db = sqlite3.connect(self.directory / 'operations.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request_key TEXT UNIQUE, '
                        'request TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL, created REAL NOT NULL, '
                        'updated REAL NOT NULL, result TEXT, error TEXT)')
        self.db.execute("UPDATE operations SET state='interrupted', stage='Controller restarted; restoring saved state', "
                        "error='Interrupted by a controller restart', updated=? WHERE state NOT IN ('succeeded','failed','interrupted')", (time.time(),))
        self.db.commit()
        self.worker = None

    def saved(self):
        try:
            value = json.loads((self.directory / 'selection.json').read_text())
            if value.get('version') != 1 or value.get('desired') not in ('running', 'stopped'):
                raise ValueError('Invalid saved selection')
            return value
        except FileNotFoundError:
            return None

    def persist(self, value):
        value = dict(value, version=1, saved_at=time.time())
        target = self.directory / 'selection.json'
        temporary = target.with_suffix('.tmp')
        with temporary.open('w') as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, target)
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def catalog(self):
        if self.demo:
            return [dict(hostname=f'node-{i}.protonvpn.net', name=name, country=country, city=city,
                         p2p=p2p, tor=tor, ips=[f'198.51.100.{20+i}'])
                    for i, (name, country, city, p2p, tor) in enumerate([
                        ('US-FL#353', 'United States', 'Miami', True, False),
                        ('US-FL#354', 'United States', 'Miami', True, False),
                        ('US-NY#101', 'United States', 'New York', True, False),
                        ('CH#14-TOR', 'Switzerland', 'Zurich', False, True),
                        ('NL#145', 'Netherlands', 'Amsterdam', True, False),
                        ('DE#27-TOR', 'Germany', 'Frankfurt', False, True)])]
        try:
            modified = self.catalog_path.stat().st_mtime
        except FileNotFoundError:
            return self.catalog_cache[1]
        if modified != self.catalog_cache[0]:
            source = json.loads(self.catalog_path.read_text())['protonvpn']['servers']
            normalized = {}
            for server in source:
                # Secure Core entries reuse exit hostnames but have different entry IPs.
                # They are a separate route type and must not overwrite ordinary servers.
                if server.get('vpn') != 'wireguard' or server.get('secure_core'):
                    continue
                hostname = server.get('hostname')
                if not hostname:
                    continue
                normalized[hostname] = dict(hostname=hostname, name=server.get('server_name', hostname),
                    country=server.get('country', ''), city=server.get('city', ''),
                    p2p=bool(server.get('port_forward')), tor=bool(server.get('tor')), ips=server.get('ips', []))
            self.catalog_cache = modified, sorted(normalized.values(), key=lambda s: (s['country'], s['city'], s['name']))
        return self.catalog_cache[1]

    def resolve(self, criteria):
        if not isinstance(criteria, dict) or set(criteria) - {'country', 'city', 'hostname', 'mode'}:
            raise Problem('Selection accepts country, city, hostname, and mode only')
        for field, value in criteria.items():
            if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
                raise Problem(f'Invalid {field}')
        mode = criteria.get('mode', 'p2p')
        if mode not in ('p2p', 'tor', 'any'):
            raise Problem('Mode must be p2p, tor, or any')
        matches = self.catalog()
        for field in ('country', 'city', 'hostname'):
            if criteria.get(field):
                matches = [s for s in matches if s[field].casefold() == criteria[field].casefold()]
        if mode != 'any':
            matches = [s for s in matches if s[mode]]
        if not matches:
            raise Problem('No WireGuard server matches this selection. Change the filters and try again.')
        return {'server': random.choice(matches), 'mode': mode, 'criteria': dict(criteria)}

    def probe(self, fresh=False):
        with self.probe_lock:
            if fresh or time.time() - self.probe_cache[0] > 5:
                try:
                    value = self.backend.probe(fresh)
                    value['available'] = True
                except Exception:
                    value = dict(available=False, running=False, healthy=False, public_ip=None,
                                 endpoint=None, checked_at=None, carrier_up=False)
                self.probe_cache = time.time(), value
            return dict(self.probe_cache[1])

    def adopted(self):
        probe = self.probe(True)
        if not self.connected(probe):
            return None
        server = next((s for s in self.catalog() if probe.get('endpoint') in s['ips']), None)
        if server:
            return dict(desired='running', server=server, mode='tor' if server['tor'] else 'p2p' if server['p2p'] else 'any',
                        criteria={'hostname': server['hostname']})
        return None

    @staticmethod
    def connected(probe):
        return bool(probe.get('available', True) and probe.get('running') and probe.get('healthy') and probe.get('public_ip'))

    def verify(self, selection, timeout=75):
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set():
            probe = self.probe(True)
            if self.connected(probe) and probe.get('endpoint') in selection['server']['ips']:
                return probe
            if time.monotonic() >= deadline:
                raise Problem('Selected server did not pass the tunnel and fresh internet checks', 502)
            self.stop_event.wait(2)
        raise Problem('Controller is shutting down', 503)

    def operation(self, identity):
        with self.lock:
            row = self.db.execute('SELECT * FROM operations WHERE id=?', (identity,)).fetchone()
        if not row:
            raise Problem('Operation not found', 404)
        item = dict(row)
        item.pop('request_key')
        for field in ('request', 'result'):
            item[field] = json.loads(item[field]) if item[field] else None
        return item

    def history(self):
        with self.lock:
            rows = self.db.execute('SELECT id FROM operations ORDER BY created DESC LIMIT 20').fetchall()
        return [self.operation(r['id']) for r in rows]

    def last_speedtest(self):
        with self.lock:
            row = self.db.execute('SELECT id FROM operations WHERE request=? AND state=? ORDER BY created DESC LIMIT 1',
                (json.dumps({'action': 'speedtest'}, sort_keys=True), 'succeeded')).fetchone()
        return self.operation(row['id']) if row else None

    def update(self, identity, state, stage, result=None, error=None):
        with self.lock:
            self.db.execute('UPDATE operations SET state=?,stage=?,updated=?,result=?,error=? WHERE id=?',
                (state, stage, time.time(), json.dumps(result) if result else None, error, identity))
            self.db.commit()

    def submit(self, command, request_key=None):
        if not isinstance(command, dict) or set(command) - {'action', 'selection'}:
            raise Problem('Expected action and optional selection')
        action = command.get('action')
        if action not in ('switch', 'connect', 'disconnect', 'reconnect', 'speedtest'):
            raise Problem('Action must be switch, connect, disconnect, reconnect, or speedtest')
        if action != 'switch' and 'selection' in command:
            raise Problem('Only switch accepts selection')
        if request_key and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', request_key):
            raise Problem('Invalid Idempotency-Key')
        serialized = json.dumps(command, sort_keys=True)
        with self.lock:
            if request_key:
                row = self.db.execute('SELECT id,request FROM operations WHERE request_key=?', (request_key,)).fetchone()
                if row:
                    if row['request'] != serialized:
                        raise Problem('Idempotency-Key was already used for another request', 409)
                    return self.operation(row['id'])
            if self.db.execute("SELECT 1 FROM operations WHERE state IN ('queued','running')").fetchone():
                raise Problem('A connection change is already in progress', 409)
            if action == 'switch':
                resolved = self.resolve(command.get('selection', {}))  # Validate before stopping anything.
            else:
                resolved = None
            identity = str(uuid.uuid4())
            now = time.time()
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)',
                (identity, request_key, serialized, 'queued', 'Waiting to start', now, now, None, None))
            self.db.commit()
            self.pending = (identity, command, resolved)
        return self.operation(identity)

    def run_operation(self, identity, command, resolved):
        if command['action'] == 'speedtest':
            self.run_speedtest(identity)
            return
        previous = self.saved()
        if previous is None:
            previous = self.adopted()
            if previous:
                self.persist(previous)
        action = command['action']
        try:
            self.update(identity, 'running', 'Stopping the VPN' if action == 'disconnect' else 'Connecting to Proton')
            if action == 'disconnect':
                if previous is None:
                    raise Problem('Cannot save a disconnected state until a working server has been selected')
                # Drop the data carrier before stopping; this persists across gateway restarts.
                self.persist(dict(previous, desired='stopped'))
                self.backend.stop()
                self.probe_cache = (0, {})
                self.update(identity, 'succeeded', 'VPN disconnected; tailnet access stays available')
                return
            target = resolved or previous
            if not target:
                raise Problem('Choose a server first; no verified selection has been saved')
            self.backend.apply(target)
            self.update(identity, 'running', 'Verifying the server and internet connection')
            probe = self.verify(target)
            self.persist(dict(target, desired='running'))
            # A saved disconnect keeps the independent data carrier down. Wait for
            # its gateway watcher to reopen it before reporting success.
            deadline = time.monotonic() + 12
            while not self.probe(True).get('carrier_up'):
                if time.monotonic() >= deadline:
                    raise Problem('VPN connected but the exit data link did not reopen', 502)
                self.stop_event.wait(0.5)
            self.update(identity, 'succeeded', 'Connected and saved', {'public_ip': probe['public_ip'], 'server': target['server']})
        except Exception as error:
            print(f'Connection change failed ({type(error).__name__})', flush=True)
            # Never expose backend exception bodies or credentials to a client.
            message = str(error) if isinstance(error, Problem) else 'VPN backend request failed'
            rollback = 'unavailable'
            if previous:
                self.update(identity, 'running', 'Restoring the previous connection', error=message)
                try:
                    self.persist(previous)
                    if previous['desired'] == 'stopped':
                        self.backend.stop()
                    else:
                        try:
                            self.backend.apply(previous)
                            self.verify(previous, 40)
                        except Exception:
                            # State is already saved. Restart only the VPN container, never Docker/host.
                            self.backend.restart()
                            self.stop_event.wait(4)
                            self.verify(previous, 60)
                    rollback = 'succeeded'
                except Exception:
                    rollback = 'failed'
            self.update(identity, 'failed', 'Change failed; previous state restored' if rollback == 'succeeded' else 'Change failed; check VPN status',
                        {'rollback': rollback}, message)

    def run_speedtest(self, identity):
        # Explicit operation only: startup, status polling, and server changes
        # never invoke this path. Measurement failures never restart the VPN.
        try:
            self.update(identity, 'running', 'Preparing the Proton speed test')
            before = self.probe(True)
            if not self.connected(before) or not before.get('carrier_up'):
                raise Problem('Connect the Proton tunnel before running a speed test', 409)
            server = next((s for s in self.catalog() if before['endpoint'] in s['ips']), None)
            if not server:
                raise Problem('The current Proton server could not be identified', 409)
            result = self.backend.speedtest(identity,
                lambda stage: self.update(identity, 'running', stage), self.stop_event)
            after = self.probe(True)
            if (not self.connected(after) or result['endpoint'] != before['endpoint'] or
                    before['endpoint'] != after['endpoint'] or before['public_ip'] != result['public_ip'] or
                    result['public_ip'] != after['public_ip']):
                raise Problem('The Proton connection changed during the test; discard these results', 502)
            self.update(identity, 'succeeded', 'Proton speed test complete', dict(result, server=server))
        except Exception as error:
            message = str(error) if isinstance(error, Problem) else 'The speed test could not complete; VPN settings were not changed'
            self.update(identity, 'failed', 'Proton speed test failed', error=message)

    def run(self):
        # Reconcile persisted selection after controller/host restart before accepting writes.
        saved = self.saved()
        try:
            if saved:
                if saved['desired'] == 'stopped':
                    self.backend.stop()
                else:
                    current = self.probe(True)
                    if not (self.connected(current) and current.get('endpoint') in saved['server']['ips']):
                        self.backend.apply(saved)
                        self.verify(saved)
            else:
                adopted = self.adopted()
                if adopted:
                    self.persist(adopted)
        except Exception:
            print('Saved connection could not be restored; dashboard remains available', flush=True)
        self.ready = True
        while not self.stop_event.wait(0.2):
            with self.lock:
                pending = getattr(self, 'pending', None)
                self.pending = None
            if pending:
                self.run_operation(*pending)

    def start(self):
        self.ready = False
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def status(self):
        probe = self.probe()
        saved = self.saved()
        server = next((s for s in self.catalog() if probe.get('endpoint') in s['ips']), None)
        return dict(demo=self.demo, ready=getattr(self, 'ready', False),
            connection='connected' if self.connected(probe) and probe.get('carrier_up') else
                       ('disconnected' if saved and saved['desired'] == 'stopped' else 'unavailable'),
            probe=probe, server=server, saved=saved, operations=self.history(), last_speedtest=self.last_speedtest())


class Handler(BaseHTTPRequestHandler):
    server_version = 'ProtonNode'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Do not log auth headers, URLs, query strings, or user identity.
        pass

    def send(self, code, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def validate_host(self):
        host = self.headers.get('Host', '')
        try:
            parsed = urllib.parse.urlsplit('http://' + host)
            if parsed.hostname not in self.server.allowed_hosts or parsed.port != self.server.server_port:
                raise ValueError()
            if parsed.username or parsed.password or parsed.path:
                raise ValueError()
        except ValueError:
            raise Problem('Invalid Host header', 403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            raise Problem('Cross-origin requests are not allowed', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise Problem('Cross-site requests are not allowed', 403)

    def authenticate(self):
        supplied = self.headers.get('X-API-Key', '')
        if self.headers.get('Authorization', '').startswith('Bearer '):
            supplied = self.headers['Authorization'][7:]
        if supplied:
            if hmac.compare_digest(supplied.encode(), self.app.key.encode()):
                return 'API key'
            raise Problem('Invalid API key', 401)
        if self.app.demo:
            return 'Preview'
        try:
            peer = ipaddress.ip_address(self.client_address[0])
            if peer.version == 6 and peer.ipv4_mapped:
                peer = peer.ipv4_mapped
            if not (peer in ipaddress.ip_network('100.64.0.0/10') or peer in ipaddress.ip_network('fd7a:115c:a1e0::/48')):
                raise OSError()
            address = f'[{peer}]:1' if peer.version == 6 else f'{peer}:1'
            who = local_api('whois?addr=' + urllib.parse.quote(address))
            user = who.get('UserProfile') or {}
            login = user.get('LoginName', '')
            allowed = [x.strip().casefold() for x in os.environ.get('CONTROLLER_ALLOWED_LOGINS', '').split(',') if x.strip()]
            # Explicitly delegate authorization to tailnet ACLs/grants, while
            # still requiring a node verified by the local Tailscale daemon.
            if '*' in allowed and who.get('Node', {}).get('ID'):
                return user.get('DisplayName') or login or 'Tailnet device'
            own = self.server.ts_self
            owner = own.get('Self', {}).get('UserID')
            tagged = bool(own.get('Self', {}).get('Tags'))
            if who.get('Node', {}).get('Tags'):
                raise OSError()
            if (allowed and login.casefold() in allowed) or (not allowed and not tagged and owner and str(user.get('ID')) == str(owner)):
                return user.get('DisplayName') or login or 'Tailnet owner'
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            pass
        raise Problem('This Tailscale device is not authorized. Ask the administrator for access or enter an API key.', 401)

    def dispatch(self, method):
        try:
            self.validate_host()
            path = urllib.parse.urlsplit(self.path).path
            if method == 'GET' and path in ('/', '/app.js', '/style.css'):
                filename, kind = {'/': ('index.html', 'text/html; charset=utf-8'),
                    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                    '/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
                self.send(200, (STATIC / filename).read_bytes(), kind)
                return
            identity = self.authenticate()
            if method == 'GET':
                if path == '/api/v1/status':
                    self.send(200, dict(self.app.status(), identity=identity))
                elif path == '/api/v1/servers':
                    # Catalog contains public metadata only; never WG keys.
                    self.send(200, {'servers': [{k: v for k, v in s.items() if k != 'ips'} for s in self.app.catalog()]})
                elif path.startswith('/api/v1/operations/'):
                    self.send(200, self.app.operation(path.rsplit('/', 1)[1]))
                elif path == '/api/v1/operations':
                    self.send(200, {'operations': self.app.history()})
                elif path == '/v1/vpn/status':
                    self.send(200, {'status': 'running' if self.app.probe()['running'] else 'stopped'})
                elif path == '/v1/publicip/ip':
                    self.send(200, {'public_ip': self.app.probe()['public_ip']})
                else:
                    raise Problem('Endpoint not found', 404)
            elif method == 'POST' and path == '/api/v1/operations':
                if not getattr(self.app, 'ready', False):
                    raise Problem('Restoring saved state; try again shortly', 503)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise Problem('Content-Type must be application/json', 415)
                if self.headers.get('X-ProtonNode-Request') != '1':
                    raise Problem('X-ProtonNode-Request: 1 is required', 403)
                if self.headers.get('Transfer-Encoding'):
                    raise Problem('Chunked request bodies are not accepted')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    raise Problem('JSON body must be between 1 and 8192 bytes', 413)
                body = json.loads(self.rfile.read(length))
                self.send(202, self.app.submit(body, self.headers.get('Idempotency-Key')))
            else:
                raise Problem('Use POST /api/v1/operations for connection changes', 405)
        except Problem as error:
            self.send(error.code, {'error': str(error)})
        except (ValueError, UnicodeError):
            self.send(400, {'error': 'Invalid request'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.send(503, {'error': 'Controller data or VPN is temporarily unavailable'})

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, app, allowed_hosts, ts_self):
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.ts_self = ts_self
        self.slots = threading.BoundedSemaphore(24)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Server6(Server):
    address_family = socket.AF_INET6


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Loopback-only preview with synthetic servers; no VPN control')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--state-dir', default='/var/lib/protonnode')
    args = parser.parse_args()
    key = 'demo-key' if args.demo else Path('/run/secrets/control.key').read_text().strip()
    if len(key) < 32 and not args.demo:
        raise SystemExit('Control key must contain at least 32 characters')
    own = {}
    if args.demo:
        addresses, hosts = ['127.0.0.1'], {'localhost', '127.0.0.1'}
    else:
        while True:
            try:
                own = local_api('status')
                addresses = own.get('TailscaleIPs', [])
                if own.get('BackendState') == 'Running' and addresses:
                    break
            except Exception:
                pass
            print('Waiting for the management Tailscale node to sign in', flush=True)
            time.sleep(3)
        dns = own.get('Self', {}).get('DNSName', '').rstrip('.')
        hosts = set(addresses) | {dns, dns.split('.')[0], os.environ.get('CONTROL_HOSTNAME', 'proton-control')}
    app = Controller(args.state_dir, '/catalog/servers.json', key,
                     DemoBackend() if args.demo else None, args.demo)
    app.start()
    servers = []
    for address in addresses:
        server = (Server6 if ':' in address else Server)((address, args.port), app, hosts, own)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f'ProtonNode listening on {address}:{args.port}', flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.stop_event.set()
        for server in servers:
            server.shutdown()


if __name__ == '__main__':
    main()
