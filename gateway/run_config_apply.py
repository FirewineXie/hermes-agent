"""Apply a changed primary messaging channel without replacing the gateway process."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from gateway.config import Platform

logger = logging.getLogger("gateway.run")


class GatewayConfigApplyMixin:
    async def apply_primary_channel_config(self, platform_name: str) -> dict:
        """Rebuild one adapter from the launch profile's persisted config.

        The old adapter is retained as a rollback candidate until the new one connects.
        A failed apply never reports the saved credentials as active.
        """
        try:
            platform = Platform(platform_name)
        except ValueError:
            return {"status": "failed", "reason": "unknown_platform"}
        if platform not in {Platform.FEISHU, Platform.WEIXIN}:
            return {"status": "gateway_restart_required", "reason": "channel_reload_unsupported"}

        lock = self.__dict__.get("_primary_channel_apply_lock")
        if lock is None:
            lock = self._primary_channel_apply_lock = asyncio.Lock()
        async with lock:
            if not self._running or getattr(self, "_draining", False):
                return {"status": "gateway_restart_required", "reason": "gateway_not_ready"}

            from gateway.run import load_gateway_config_for_runner

            updated = await asyncio.to_thread(load_gateway_config_for_runner)
            new_config = updated.platforms.get(platform)
            old_config = self.config.platforms.get(platform)
            generations = self.__dict__.setdefault("_channel_apply_generations", {})
            generations[platform] = generations.get(platform, 0) + 1
            old_adapter = self.adapters.pop(platform, None)
            if old_adapter is not None:
                old_adapter._config_replaced = True
            self.delivery_router.adapters = self.adapters
            queued = self._failed_platforms.pop(platform, None)
            predecessor = old_adapter or (queued or {}).get("predecessor")
            if old_adapter is not None:
                await self._bounded_adapter_teardown(old_adapter, platform)

            if new_config is None or not new_config.enabled:
                if new_config is not None:
                    self.config.platforms[platform] = new_config
                else:
                    self.config.platforms.pop(platform, None)
                self._update_platform_runtime_status(platform.value, platform_state="disabled",
                                                     error_code=None, error_message=None)
                with suppress(Exception):
                    from gateway.channel_directory import build_channel_directory

                    await build_channel_directory(self.adapters)
                return {"status": "applied", "component": platform.value}

            try:
                adapter = self._create_adapter(platform, new_config)
            except Exception:
                logger.exception("Channel adapter creation failed for %s", platform.value)
                adapter = None
            if adapter is not None:
                from gateway.platforms.helpers import carry_inbound_dedup

                try:
                    if old_adapter is not None:
                        carry_inbound_dedup(old_adapter, adapter)
                    self._wire_adapter_handlers(adapter)
                    connected = await self._connect_adapter_with_timeout(adapter, platform, is_reconnect=True)
                except Exception:
                    logger.exception("Channel config apply failed for %s", platform.value)
                    connected = False
                if connected:
                    self.config.platforms[platform] = new_config
                    await self._install_reconnected_adapter(platform, adapter, predecessor=predecessor)
                    return {"status": "component_restarted", "component": platform.value}
                await self._safe_adapter_disconnect(adapter, platform)

            if old_adapter is not None and old_config is not None:
                try:
                    rollback = self._create_adapter(platform, old_config)
                except Exception:
                    logger.exception("Channel rollback adapter creation failed for %s", platform.value)
                    rollback = None
                if rollback is not None:
                    try:
                        self._wire_adapter_handlers(rollback)
                        restored = await self._connect_adapter_with_timeout(rollback, platform, is_reconnect=True)
                    except Exception:
                        logger.exception("Channel config rollback failed for %s", platform.value)
                        restored = False
                    if restored:
                        await self._install_reconnected_adapter(platform, rollback, predecessor=predecessor)
                        return {"status": "failed", "component": platform.value,
                                "reason": "new_channel_unavailable_old_connection_restored"}
                    await self._safe_adapter_disconnect(rollback, platform)

            self._update_platform_runtime_status(platform.value, platform_state="fatal",
                                                 error_code="config_apply_failed")
            return {"status": "failed", "component": platform.value,
                    "reason": "channel_unavailable"}
