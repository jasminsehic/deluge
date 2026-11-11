"""WSS tracker integration for Deluge's WebTorrent-enabled libtorrent session."""

import asyncio
import hashlib
import json
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

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


class RelayWssTracker(LoopbackWssTracker):
    """Minimal real WebTorrent tracker that relays RTC offers and answers."""

    def __init__(self, directory):
        super().__init__(directory)
        self.offers_relayed = 0
        self.answers_relayed = 0
        self.announces = []

    def run(self, context):
        from websockets.legacy.server import serve

        peers = {}
        pending_offers = {}

        async def handler(websocket):
            owned = set()
            try:
                async for payload in websocket:
                    request = json.loads(payload)
                    if request.get('action') != 'announce':
                        continue

                    info_hash = request['info_hash']
                    peer_id = request['peer_id']

                    if 'answer' in request:
                        target = peers.get((info_hash, request['to_peer_id']))
                        if target is not None:
                            await target.send(
                                json.dumps(
                                    {
                                        'action': 'announce',
                                        'info_hash': info_hash,
                                        'peer_id': peer_id,
                                        'offer_id': request['offer_id'],
                                        'answer': request['answer'],
                                    }
                                )
                            )
                            self.answers_relayed += 1
                        continue

                    self.announces.append(
                        (len(request.get('offers', [])), request.get('left'))
                    )
                    key = (info_hash, peer_id)
                    peers[key] = websocket
                    owned.add(key)
                    self.announced.set()
                    await websocket.send(
                        json.dumps(
                            {
                                'action': 'announce',
                                'info_hash': info_hash,
                                'interval': 120,
                                'min_interval': 60,
                            }
                        )
                    )

                    for offer in pending_offers.get(info_hash, []):
                        if offer['peer_id'] != peer_id:
                            await websocket.send(json.dumps(offer))
                            self.offers_relayed += 1

                    # One offer per announce is sufficient for this two-peer
                    # test; limiting it avoids hundreds of redundant ICE peers.
                    for offer in request.get('offers', [])[:1]:
                        forwarded = {
                            'action': 'announce',
                            'info_hash': info_hash,
                            'peer_id': peer_id,
                            'offer_id': offer['offer_id'],
                            'offer': offer['offer'],
                        }
                        pending_offers.setdefault(info_hash, []).append(forwarded)
                        for (other_hash, other_id), other in list(peers.items()):
                            if other_hash == info_hash and other_id != peer_id:
                                await other.send(json.dumps(forwarded))
                                self.offers_relayed += 1
            finally:
                for key in owned:
                    if peers.get(key) is websocket:
                        peers.pop(key)

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

    def test_current_metadata_writer_preserves_info_hash_and_wss_tracker(self):
        """Saving received metadata must preserve its original hash and tracker."""
        from deluge._libtorrent import lt
        from deluge.core.torrent import Torrent
        from deluge.tests import common

        params = lt.load_torrent_buffer(
            Path(common.get_test_data_file('test.torrent')).read_bytes()
        )
        info_hash = str(params.ti.info_hash())
        tracker = 'wss://tracker.example.com/announce'
        fake_handle = SimpleNamespace(
            trackers=lambda: [{'url': tracker, 'tier': 0}],
            url_seeds=lambda: [],
        )
        torrent = SimpleNamespace(
            torrent_id=info_hash,
            torrent_info=params.ti,
            handle=fake_handle,
            config={'copy_torrent_file': False},
        )
        state = Path(self.config_dir) / 'state'
        state.mkdir(exist_ok=True)
        Torrent.write_torrentfile(torrent)

        saved = lt.load_torrent_buffer(
            (state / f'{info_hash}.torrent').read_bytes()
        )
        assert str(saved.ti.info_hash()) == info_hash
        assert saved.trackers == [tracker]

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

    def test_webtorrent_stun_preference(self):
        prefs = component.get('PreferencesManager')
        key = 'webtorrent_stun_server'
        initial = self.core.session.get_settings()
        assert key in initial

        value = 'stun.example.com:3478'
        try:
            prefs.do_config_set_func(key, value)
            assert self.core.session.get_settings()[key] == value
        finally:
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
            'webtorrent_stun_server',
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

    @pytest_twisted.inlineCallbacks
    def test_wss_discovers_rtc_peer_and_transfers_verified_pieces(self):
        """Download payload from a WSS-discovered RTC seeder into Deluge."""
        from deluge._libtorrent import lt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed_dir = root / 'seed'
            download_dir = root / 'download'
            seed_dir.mkdir()
            download_dir.mkdir()
            filename = 'rtc-payload.bin'
            payload = bytes(range(256)) * 192
            (seed_dir / filename).write_bytes(payload)

            tracker = RelayWssTracker(directory)
            tracker.start()
            seeder = None
            try:
                tracker_url = f'wss://127.0.0.1:{tracker.port}/announce'
                piece_size = 16384
                creator = lt.create_torrent(
                    [lt.create_file_entry(filename, len(payload))],
                    piece_size,
                    lt.create_torrent.v1_only,
                )
                for index in range(creator.num_pieces()):
                    start = index * piece_size
                    creator.set_hash(
                        index,
                        hashlib.sha1(payload[start : start + piece_size]).digest(),
                    )
                creator.add_tracker(tracker_url)
                torrent_bytes = lt.bencode(creator.generate())

                settings = {
                    'enable_dht': False,
                    'enable_lsd': False,
                    'enable_upnp': False,
                    'enable_natpmp': False,
                    'validate_https_trackers': False,
                    'webtorrent_stun_server': '',
                }
                self.core.apply_session_settings(settings)

                seeder = lt.session(
                    {
                        **settings,
                        'listen_interfaces': '127.0.0.1:0',
                    }
                )
                seed_params = lt.load_torrent_buffer(torrent_bytes)
                seed_params.save_path = str(seed_dir)
                seed_params.flags &= ~lt.torrent_flags.paused
                seed_params.flags &= ~lt.torrent_flags.auto_managed
                seed_handle = seeder.add_torrent(seed_params)

                deadline = reactor.seconds() + 15
                while (
                    not seed_handle.status().is_seeding and reactor.seconds() < deadline
                ):
                    yield task.deferLater(reactor, 0.2, lambda: None)
                assert seed_handle.status().is_seeding, (
                    'RTC seeder did not hash its file'
                )

                # This exercises both RTC metadata exchange and piece
                # transfer, with WSS as the only peer discovery mechanism.
                magnet = (
                    f'magnet:?xt=urn:btih:{seed_params.ti.info_hash()}'
                    f'&tr=wss%3A%2F%2F127.0.0.1%3A{tracker.port}%2Fannounce'
                )
                torrent_id = yield self.core.add_torrent_magnet(
                    magnet,
                    {
                        'download_location': str(download_dir),
                        'add_paused': False,
                        'auto_managed': False,
                    },
                )
                assert torrent_id
                download_handle = self.core.torrentmanager.torrents[torrent_id].handle
                assert any(
                    entry['url'] == tracker_url for entry in download_handle.trackers()
                ), 'Deluge did not retain the WSS tracker from the torrent file'
                download_handle.resume()
                download_handle.force_reannounce()

                deadline = reactor.seconds() + 60
                while reactor.seconds() < deadline:
                    if download_handle.status().is_seeding:
                        break
                    yield task.deferLater(reactor, 0.2, lambda: None)

                target = download_dir / filename
                assert tracker.offers_relayed > 0, (
                    'No offer crossed the WSS tracker '
                    f'(announces={tracker.announces}, '
                    f'answers={tracker.answers_relayed}, '
                    f'seeder={seed_handle.status().is_seeding}, '
                    f'download_status={download_handle.status().state}, '
                    f'download_paused={bool(download_handle.status().flags & lt.torrent_flags.paused)}, '
                    f'download_error={download_handle.status().errc.message()}, '
                    f'trackers={download_handle.trackers()})'
                )
                assert tracker.answers_relayed > 0, 'No answer crossed the WSS tracker'
                assert download_handle.status().is_seeding, (
                    'No completed RTC payload transfer '
                    f'(offers={tracker.offers_relayed}, '
                    f'answers={tracker.answers_relayed}, '
                    f'progress={download_handle.status().progress}, '
                    f'download_peers={download_handle.status().num_peers}, '
                    f'seed_peers={seed_handle.status().num_peers}, '
                    f'download_state={download_handle.status().state}, '
                    f'download_error={download_handle.status().errc.message()})'
                )
                assert target.read_bytes() == payload
                saved = Path(self.config_dir) / 'state' / f'{torrent_id}.torrent'
                assert saved.is_file(), (
                    'Received metadata was not saved as a torrent file'
                )
                saved_params = lt.load_torrent_buffer(saved.read_bytes())
                assert str(saved_params.ti.info_hash()) == torrent_id
                assert tracker_url in saved_params.trackers
            finally:
                if seeder is not None:
                    seeder.pause()
                tracker.stop()
