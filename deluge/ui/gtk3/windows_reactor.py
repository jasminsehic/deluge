"""Windows compatibility for Twisted's portable GTK/GLib reactor.

Twisted's GlibWaker subclasses the POSIX-only _UnixWaker. On Windows its
base class calls fdesc, which is not imported on non-POSIX platforms.
Use the supported socket waker for Windows while keeping GLib's
simulation callback for cross-thread events.
"""

import os


def configure_windows_glib_waker():
    """Install a socket-backed waker before calling gtk3reactor.install()."""
    if os.name != 'nt':
        return

    from twisted.internet import gireactor
    from twisted.internet._signals import _SocketWaker

    class WindowsGlibWaker(_SocketWaker):
        def __init__(self, reactor):
            super().__init__()
            self._reactor = reactor

        def doRead(self):
            super().doRead()
            self._reactor._simulate()

    def make_windows_waker(reactor):
        return WindowsGlibWaker(reactor)

    gireactor.PortableGIReactor._wakerFactory = make_windows_waker
