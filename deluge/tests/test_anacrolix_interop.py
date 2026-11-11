"""Cross-implementation WebTorrent transfers: Deluge/libtorrent vs Anacrolix/Pion.

Run with the pinned anacrolix-interop-peer binary built in CI. The normal
Deluge test suite skips this module when the independent peer is unavailable.
"""

import base64
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import pytest_twisted
from twisted.internet import reactor, task

import deluge.component as component
from deluge.conftest import BaseTestCase
from deluge.core.core import Core
from deluge.core.rpcserver import RPCServer
from deluge.tests.test_webtorrent import RelayWssTracker

TRANSPORT_SETTINGS = {
    'enable_dht': False,
    'enable_lsd': False,
    'enable_upnp': False,
    'enable_natpmp': False,
    'enable_incoming_tcp': False,
    'enable_outgoing_tcp': False,
    'enable_incoming_utp': False,
    'enable_outgoing_utp': False,
    'validate_https_trackers': False,
    'webtorrent_stun_server': '',
}


@pytest.mark.internet
@pytest.mark.webtorrent
class TestAnacrolixInterop(BaseTestCase):
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

    @pytest.mark.parametrize('direction', ['anacrolix-to-deluge', 'deluge-to-anacrolix'])
    @pytest_twisted.inlineCallbacks
    def test_wss_rtc_transfer_with_anacrolix(self, direction):
        """Both directions must complete verified payload transfer via RTC only."""
        from deluge._libtorrent import lt

        binary = os.environ.get('ANACROLIX_INTEROP_PEER')
        if not binary:
            pytest.skip('Independent Anacrolix test binary not configured')
        if not Path(binary).is_file():
            pytest.fail(f'Independent Anacrolix peer binary not found: {binary}')

        self.core.apply_session_settings(TRANSPORT_SETTINGS)
        active = self.core.session.get_settings()
        for key in (
            'enable_dht',
            'enable_lsd',
            'enable_incoming_tcp',
            'enable_outgoing_tcp',
            'enable_incoming_utp',
            'enable_outgoing_utp',
        ):
            assert active[key] is False, f'RTC-only guard failed: {key}={active[key]}'

        log_root = Path(os.environ.get('ANACROLIX_INTEROP_LOG_DIR', tempfile.gettempdir()))
        log_root.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix=f'{direction}-', dir=str(log_root)))
        go_dir = root / 'anacrolix'
        deluge_dir = root / 'deluge'
        go_dir.mkdir()
        deluge_dir.mkdir()

        filename = 'interop.bin'
        payload = bytes(range(256)) * 1024  # 256 KiB, sixteen 16-KiB pieces.
        source = go_dir if direction == 'anacrolix-to-deluge' else deluge_dir
        (source / filename).write_bytes(payload)

        tracker = RelayWssTracker(root)
        tracker.start()
        process = None
        stdout = None
        torrent_id = None

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
            torrent_path = root / 'interop.torrent'
            torrent_path.write_bytes(torrent_bytes)
            info_hash = str(lt.load_torrent_buffer(torrent_bytes).ti.info_hash())

            stdout_path = root / 'anacrolix.log'

            def start_anacrolix(mode):
                nonlocal process, stdout
                stdout = stdout_path.open('w', encoding='utf-8')
                env = os.environ.copy()
                # Only the loopback self-signed test tracker is trusted.
                env['SSL_CERT_FILE'] = str(root / 'cert.pem')
                process = subprocess.Popen(
                    [
                        binary,
                        '-mode', mode,
                        '-torrent', str(torrent_path),
                        '-data', str(go_dir),
                        '-size', str(len(payload)),
                    ],
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    env=env,
                )

            def go_log():
                if stdout is not None:
                    stdout.flush()
                return stdout_path.read_text(errors='replace') if stdout_path.exists() else ''

            def deluge_handle():
                return self.core.torrentmanager.torrents[torrent_id].handle

            if direction == 'anacrolix-to-deluge':
                start_anacrolix('seed')
                deadline = reactor.seconds() + 45
                while 'READY seed' not in go_log() and reactor.seconds() < deadline:
                    assert process.poll() is None, f'Anacrolix exited: {go_log()}'
                    yield task.deferLater(reactor, 0.2, lambda: None)
                assert 'READY seed' in go_log(), f'Anacrolix did not seed: {go_log()}'

                magnet = (
                    f'magnet:?xt=urn:btih:{info_hash}'
                    f'&tr=wss%3A%2F%2F127.0.0.1%3A{tracker.port}%2Fannounce'
                )
                torrent_id = yield self.core.add_torrent_magnet(
                    magnet,
                    {
                        'download_location': str(deluge_dir),
                        'add_paused': False,
                        'auto_managed': False,
                    },
                )
                assert torrent_id == info_hash
                deluge_handle().resume()
                deluge_handle().force_reannounce()
            else:
                torrent_id = self.core.add_torrent_file(
                    'interop.torrent',
                    base64.b64encode(torrent_bytes),
                    {
                        'download_location': str(deluge_dir),
                        'add_paused': False,
                        'auto_managed': False,
                    },
                )
                assert torrent_id == info_hash
                deadline = reactor.seconds() + 45
                while not deluge_handle().status().is_seeding and reactor.seconds() < deadline:
                    yield task.deferLater(reactor, 0.2, lambda: None)
                assert deluge_handle().status().is_seeding, 'Deluge seed file failed verification'
                start_anacrolix('download')

            deadline = reactor.seconds() + 120
            dest = deluge_dir / filename if direction == 'anacrolix-to-deluge' else go_dir / filename
            while reactor.seconds() < deadline:
                if direction == 'anacrolix-to-deluge':
                    finished = deluge_handle().status().is_seeding
                else:
                    finished = process.poll() == 0 and dest.is_file()
                if finished:
                    break
                if process.poll() not in (None, 0):
                    raise AssertionError(f'Anacrolix exited with {process.returncode}: {go_log()}')
                yield task.deferLater(reactor, 0.2, lambda: None)

            assert tracker.offers_relayed > 0, 'No RTC offer traversed the WSS tracker'
            assert tracker.answers_relayed > 0, 'No RTC answer traversed the WSS tracker'
            assert dest.is_file(), f'Downloaded file missing: {dest}'
            assert hashlib.sha256(dest.read_bytes()).digest() == hashlib.sha256(payload).digest(), (
                'Transferred bytes did not match the seed payload'
            )
            if direction == 'anacrolix-to-deluge':
                assert deluge_handle().status().is_seeding, 'Deluge torrent not complete'
            else:
                assert process.poll() == 0, f'Anacrolix download not complete: {go_log()}'
            print(
                f'PASS {direction}: {len(payload)} bytes; '
                f'{tracker.offers_relayed} WSS offers / {tracker.answers_relayed} answers',
                flush=True,
            )
        except Exception as exc:
            raise AssertionError(
                f'{direction}: {exc}; offers={tracker.offers_relayed}; '
                f'answers={tracker.answers_relayed}; '
                f'deluge_status={deluge_handle().status() if torrent_id else None}; '
                f'anacrolix_log={go_log() if stdout else None}; logs={root}'
            ) from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if stdout is not None:
                stdout.close()
            if torrent_id is not None:
                self.core.remove_torrent(torrent_id, remove_data=False)
            tracker.stop()
