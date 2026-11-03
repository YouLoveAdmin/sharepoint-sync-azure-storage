import io
from unittest.mock import Mock

import pytest

from app import container, cron_tick


def test_cron_tick_calls_loopback_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    response = io.StringIO('{"status":"not_due"}')
    open_request = Mock(return_value=response)
    monkeypatch.setattr(cron_tick.urllib.request, "urlopen", open_request)
    assert cron_tick.main() == 0
    request = open_request.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:8000/internal/sync-due"
    assert request.get_method() == "POST"
    assert open_request.call_args.kwargs["timeout"] == 30


def test_cron_tick_reports_api_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cron_tick.urllib.request, "urlopen",
        Mock(side_effect=cron_tick.urllib.error.URLError("private-detail")),
    )
    assert cron_tick.main() == 1
    assert "local API unavailable" in capsys.readouterr().err


def test_supervisor_stops_api_if_cron_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    cron = Mock()
    cron.poll.return_value = 1
    cron.returncode = 1
    api = Mock()
    api.poll.return_value = None
    start = Mock(side_effect=[cron, api])
    monkeypatch.setattr(container.subprocess, "Popen", start)
    monkeypatch.setattr(container.signal, "signal", Mock())
    event = Mock()
    event.wait.return_value = False
    monkeypatch.setattr(container.threading, "Event", Mock(return_value=event))

    assert container.main() == 1
    assert start.call_args_list[0].args[0] == ["/usr/sbin/cron", "-f"]
    assert start.call_args_list[1].kwargs["user"] == "appuser"
    assert "--no-proxy-headers" in start.call_args_list[1].args[0]
    api.terminate.assert_called_once()
    api.wait.assert_called_once_with(timeout=25)


def test_supervisor_stops_cron_if_api_fails_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    cron = Mock()
    cron.poll.return_value = None
    monkeypatch.setattr(container.subprocess, "Popen", Mock(side_effect=[cron, OSError("failure")]))
    monkeypatch.setattr(container.signal, "signal", Mock())
    with pytest.raises(OSError):
        container.main()
    cron.terminate.assert_called_once()
    cron.wait.assert_called_once_with(timeout=25)
