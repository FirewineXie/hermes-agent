"""A channel credential change replaces only its adapter and reports the actual outcome."""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run_config_apply import GatewayConfigApplyMixin


class _Adapter:
    send_path_degraded = False


class _Runner(GatewayConfigApplyMixin):
    def __init__(self, old_config, old_adapter=None, *, accepts_new=True):
        self.config = SimpleNamespace(platforms={Platform.WEIXIN: old_config})
        self.adapters = {Platform.WEIXIN: old_adapter} if old_adapter else {}
        self.delivery_router = SimpleNamespace(adapters=self.adapters)
        self._failed_platforms = {}
        self._running = True
        self._draining = False
        self.accepts_new = accepts_new
        self.events = []

    async def _bounded_adapter_teardown(self, adapter, platform):
        self.events.append(("stop", adapter))

    def _create_adapter(self, platform, config):
        adapter = _Adapter()
        adapter.config = config
        return adapter

    def _wire_adapter_handlers(self, adapter):
        pass

    async def _connect_adapter_with_timeout(self, adapter, platform, *, is_reconnect):
        self.events.append(("connect", adapter.config))
        return self.accepts_new or adapter.config == self.config.platforms[platform]

    async def _safe_adapter_disconnect(self, adapter, platform):
        self.events.append(("dispose", adapter.config))

    async def _install_reconnected_adapter(self, platform, adapter, *, predecessor):
        self.adapters[platform] = adapter
        self.events.append(("installed", adapter.config))

    def _update_platform_runtime_status(self, platform, **status):
        self.events.append(("status", status["platform_state"]))


def test_changed_weixin_credentials_apply_without_gateway_restart(monkeypatch):
    old, new = SimpleNamespace(enabled=True, token="old"), SimpleNamespace(enabled=True, token="new")
    previous = _Adapter()
    runner = _Runner(old, previous)
    monkeypatch.setattr("gateway.run.load_gateway_config_for_runner",
                        lambda: SimpleNamespace(platforms={Platform.WEIXIN: new}))

    result = asyncio.run(runner.apply_primary_channel_config("weixin"))

    assert result == {"status": "component_restarted", "component": "weixin"}
    assert runner.adapters[Platform.WEIXIN].config is new
    assert ("stop", previous) in runner.events
    assert runner._running is True


def test_failed_new_connection_restores_previous_channel(monkeypatch):
    old, new = SimpleNamespace(enabled=True, token="old"), SimpleNamespace(enabled=True, token="new")
    previous = _Adapter()
    runner = _Runner(old, previous, accepts_new=False)
    monkeypatch.setattr("gateway.run.load_gateway_config_for_runner",
                        lambda: SimpleNamespace(platforms={Platform.WEIXIN: new}))

    result = asyncio.run(runner.apply_primary_channel_config("weixin"))

    assert result["status"] == "failed"
    assert result["reason"] == "new_channel_unavailable_old_connection_restored"
    assert runner.adapters[Platform.WEIXIN].config is old
    assert ("dispose", new) in runner.events
