# libtorrent 2.1 session-setting compatibility

Deluge's configuration includes options originally used with libtorrent 1.2.
The settings bridge now checks the actual installed libtorrent session
capabilities, rather than assuming a setting exists from a version string.

| Deluge setting | Modern libtorrent treatment |
| --- | --- |
| `peer_tos` | Forward as `peer_dscp` when the old name is unavailable |
| `ignore_resume_timestamps` | Only set for the legacy ABI that supports it |
| `max_half_open_connections` (`half_open_limit`) | Not available; keep saved preference for legacy builds and log warning |
| `ignore_limits_on_local_network` | Not available; no one-to-one session-setting replacement; log warning |
| `cache_size`, `cache_expiry` | Removed old disk-cache knobs; keep saved preferences for older builds and log warning |
| `proxy.force_proxy` | Not available; a true value is rejected rather than silently ignored. A saved true value prevents network-session creation, and an attempted live change pauses the session |
| All other settings emitted by Deluge preferences | Apply unchanged if present in the installed libtorrent session; otherwise fail explicitly |

The `force_proxy` safeguard does **not** add equivalent proxy isolation to
libtorrent 2.1. Users requiring guaranteed forced-proxy isolation should not
start transfers with a newer build until they have an alternative protection
mechanism. Other supported proxy flags are still passed through.

The active compatibility tests verify this mapping, unknown-setting rejection,
and the actual startup application of all session-related preferences using
the WebTorrent-enabled native Python bindings.

Magnet additions use the modern `add_torrent_params` binding for ABI 3 and
newer, including the `info_hashes` field, while retaining the legacy
dictionary path for older ABI builds.

The RTC native transfer test and Deluge loopback WSS announce test verify
different portions of the integration. A complete Deluge-to-WebRTC peer
piece transfer via live WebSocket signaling is not yet part of this suite.
