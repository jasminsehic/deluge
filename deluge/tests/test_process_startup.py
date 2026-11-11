"""Tests for asynchronous daemon startup diagnostics and timeouts."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from twisted.internet.defer import Deferred
from twisted.internet.task import Clock

from deluge.error import DelugeError
from deluge.tests.common import ProcessOutputHandler, add_watchdog


def test_startup_watchdog_fails_with_timeout():
    clock = Clock()
    startup = Deferred()
    failures = []
    startup.addErrback(lambda failure: failures.append(failure) or None)

    with patch('deluge.tests.common.reactor.callLater', clock.callLater):
        watchdog = add_watchdog(startup, timeout=5, message='Startup stalled')
        clock.advance(5)

    assert watchdog.called
    assert len(failures) == 1
    assert failures[0].check(TimeoutError)
    assert 'Startup stalled' in failures[0].getErrorMessage()


def test_successful_startup_cancels_watchdog():
    clock = Clock()
    startup = Deferred()

    with patch('deluge.tests.common.reactor.callLater', clock.callLater):
        watchdog = add_watchdog(startup, timeout=5)
        startup.callback('ready')
        clock.advance(6)

    assert watchdog.cancelled
    assert startup.result == 'ready'


def test_early_subprocess_exit_fails_startup_with_output():
    startup = Deferred()
    errors = []
    startup.addErrback(lambda failure: errors.append(failure) or None)
    process = ProcessOutputHandler(
        b'',
        lambda: None,
        [{'deferred': startup, 'types': ('stdout',), 'triggers': []}],
    )
    process.transport = Mock()
    process.log_output = 'Exception raised: missing session setting'

    process.processEnded(SimpleNamespace(value=SimpleNamespace(exitCode=1)))

    process.transport.loseConnection.assert_called_once()
    assert len(errors) == 1
    assert errors[0].check(DelugeError)
    assert 'missing session setting' in errors[0].getErrorMessage()
