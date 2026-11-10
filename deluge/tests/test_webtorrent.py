"""WSS tracker integration for Deluge's WebTorrent-enabled libtorrent session."""

import asyncio
import json
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest
import pytest_twisted
from twisted.internet import reactor, task

import deluge.component as component
from deluge.conftest import BaseTestCase
from deluge.core.core import Core
from deluge.core.rpcserver import RPCServer


class LoopbackWssTracker:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.ready = threading.Event()
        self.announced = threading.Event()
        self.request = None
        self.port = None
        self.error = None

    def start(self):
        cert = self.directory / 'cert.pem'
        key = self.directory / 'key.pem'
        subprocess.run(
            [
                'openssl',
                'req',
                '-x509',
                '-newkey',
                'rsa:2048',
                '-nodes',
                '-days',
                '1',
                '-subj',
                '/CN=localhost',
                '-keyout',
                str(key),
                '-out',
                str(cert),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.thread = threading.Thread(target=self.run, args=(context,), daemon=True)
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError('WSS server startup timeout')
        if self.error:
            raise self.error

    def run(self, context):
        from websockets.legacy.server import serve

        async def handler(websocket):
            async for payload in websocket:
                request = json.loads(payload)
                if request.get('action') == 'announce':
                    self.request = request
                    self.announced.set()
                    await websocket.send(
                        json.dumps(
                            {
                                'action': 'announce',
                                'info_hash': request['info_hash'],
                                'interval': 120,
                                'min_interval': 60,
                            }
                        )
                    )

        async def start_server():
            self.server = await serve(handler, '127.0.0.1', 0, ssl=context)
            self.port = self.server.sockets[0].getsockname()[1]

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(start_server())
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()
        if self.error:
            self.loop.close()
            return
        self.loop.run_forever()
        self.loop.close()

    def stop(self):
        if not self.error:

            async def close():
                self.server.close()
                await self.server.wait_closed()

            asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


@pytest.mark.internet
@pytest.mark.webtorrent
class TestWebTorrent(BaseTestCase):
    def set_up(self):
        self.rpcserver = RPCServer(listen=False)
        self.core = Core()
        self.core.config.config['lsd'] = False
        return component.start()

    def tear_down(self):
        def cleanup(result):
            del self.rpcserver
            del self.core
            return result

        return component.shutdown().addCallback(cleanup)

    def test_current_session_state_roundtrip(self):
        from deluge._libtorrent import lt

        if getattr(lt, 'api_version', 1) < 3:
            pytest.skip('Requires session_params API')

        self.core.apply_session_setting('connections_limit', 137)
        self.core._save_session_state()
        params = self.core._load_session_state()
        assert isinstance(params, lt.session_params)
        assert params.settings['connections_limit'] == 137

        restored = lt.session(params)
        assert restored.get_settings()['connections_limit'] == 137

    def test_webtorrent_stun_turn_and_relay_only_preferences(self):
        prefs = component.get('PreferencesManager')
        initial = self.core.session.get_settings()
        values = {
            'webtorrent_turn_servers': 'turn:127.0.0.1:3478?transport=udp',
            'webtorrent_stun_server': 'stun:stun.l.google.com:19302',
            'webtorrent_relay_only': True,
        }
        for key in values:
            assert key in initial

        try:
            for key, value in values.items():
                prefs.do_config_set_func(key, value)
            updated = self.core.session.get_settings()
            for key, value in values.items():
                assert updated[key] == value
        finally:
            for key in values:
                prefs.do_config_set_func(key, initial[key])

    def test_session_preferences_with_libtorrent_2_1(self):
        prefs = component.get('PreferencesManager')
        session_pref_keys = (
            'outgoing_interface',
            'listen_ports',
            'random_port',
            'outgoing_ports',
            'random_outgoing_ports',
            'peer_tos',
            'dht',
            'upnp',
            'natpmp',
            'lsd',
            'enc_level',
            'max_connections_global',
            'max_upload_speed',
            'max_download_speed',
            'max_upload_slots_global',
            'max_half_open_connections',
            'max_connections_per_second',
            'ignore_limits_on_local_network',
            'share_ratio_limit',
            'seed_time_ratio_limit',
            'seed_time_limit',
            'max_active_downloading',
            'max_active_seeding',
            'max_active_limit',
            'dont_count_slow_torrents',
            'announce_to_all_tiers',
            'proxy',
            'rate_limit_ip_overhead',
            'cache_size',
            'cache_expiry',
            'auto_manage_prefer_seeds',
            'webtorrent_relay_only',
            'webtorrent_stun_server',
            'webtorrent_turn_servers',
        )
        for key in session_pref_keys:
            prefs.do_config_set_func(key, prefs.config[key])

    @pytest_twisted.inlineCallbacks
    def test_wss_tracker_announces_rtc_offers_from_deluge(self):
        settings = self.core.session.get_settings()
        assert 'webtorrent_stun_server' in settings, (
            'WebTorrent support is missing in the native libtorrent build'
        )
        self.core.apply_session_settings({'validate_https_trackers': False})
        with tempfile.TemporaryDirectory() as directory:
            tracker = LoopbackWssTracker(directory)
            tracker.start()
            try:
                info_hash = 'ab570cdd5a17ea1b61e970bb72047de141bce173'
                uri = (
                    f'magnet:?xt=urn:btih:{info_hash}'
                    f'&tr=wss%3A%2F%2F127.0.0.1%3A{tracker.port}%2Fannounce'
                )
                torrent_id = yield self.core.add_torrent_magnet(uri, {})
                assert torrent_id == info_hash
                deadline = reactor.seconds() + 30
                while not tracker.announced.is_set() and reactor.seconds() < deadline:
                    yield task.deferLater(reactor, 0.2, lambda: None)
                assert tracker.announced.is_set(), 'No WSS announce received'
                assert tracker.request['action'] == 'announce'
                assert tracker.request.get('offers'), 'No WebRTC offers announced'
            finally:
                tracker.stop()
