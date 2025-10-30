import asyncio
from typing import Any, Callable
from client import ChargeClient, ChargeClientController
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class _StationRecord:
    data: Any
    time: float
# 监听充电桩状态，允许注册回调函数订阅充电桩状态
class ChargeListener:
    POLL_INTERVAL = 15  # 轮询间隔，单位秒
    EXPIRE_TIME = 30  # 数据过期时间，单位秒

    HOOK_CALLBACK_TYPE = (
        Callable[[list], asyncio.Future] | Callable[[list, list], asyncio.Future]
    )

    def __init__(
        self,
        client_controller: ChargeClientController,
        stations: dict[str, int],
        longitude: str,
        latitude: str,
        *,
        on_error: Callable[[Exception, str], None] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ):
        self.client_controller = client_controller
        self.stations: dict[str, int] = stations
        self._refresh_task: asyncio.Task | None = None
        self.hooks: dict[int, list[ChargeListener.HOOK_CALLBACK_TYPE]] = {
            station_id: [] for station_id in stations.values()
        }
        self.on_error = on_error
        self.on_warning = on_warning
        self.locks: dict[int, asyncio.Lock] = {
            station_id: asyncio.Lock() for station_id in stations.values()
        }
        self.station_status: _StationRecord | None = None
        self.longitude = longitude
        self.latitude = latitude

    @classmethod
    async def create(
        cls,
        *,
        longitude: str,
        latitude: str,
        client_controller: ChargeClientController | None = None,
        host: str | None = None,
        openid: str | None = None,
        phonenumber: str | None = None,
        **kwargs,
    ) -> "ChargeListener":
        if client_controller is None:
            client = ChargeClient(host, openid, phonenumber)
            client_controller = ChargeClientController(client)
        stations = await client_controller.get_stations(longitude, latitude)
        station_ids = {name: info["id"] for name, info in stations.items()}
        listener = cls(client_controller, station_ids, longitude, latitude, **kwargs)
        listener.station_status = _StationRecord(
            {info["id"]: info for info in stations.values()},
            asyncio.get_event_loop().time(),
        )
        return listener

    async def get_station_status(self) -> dict:
        if self.station_status and (
            asyncio.get_event_loop().time() - self.station_status.time
            < self.EXPIRE_TIME
        ):
            return self.station_status.data
        data = await self._request_station_status()
        self.station_status = _StationRecord(data, asyncio.get_event_loop().time())
        return data

    async def _request_station_status(self) -> dict:
        data = await self.client_controller.get_stations(self.longitude, self.latitude)
        data = {info["id"]: info for info in data.values()}
        return data

    async def _call_hook(
        self,
        hook: HOOK_CALLBACK_TYPE,
        data: list,
        prev_data: list | None = None,
    ):
        try:
            if hook.__code__.co_argcount == 1:
                return bool(await hook(data))
            else:
                return bool(await hook(data, prev_data))
        except Exception as e:
            err_msg = f"Error calling hook: {e}"
            logger.error(err_msg)
            if self.on_error:
                self.on_error(e, err_msg)
            return False

    async def _handle_hook(
        self,
        station_id: int,
        hook: HOOK_CALLBACK_TYPE,
        data: list,
        prev_data: list | None = None,
    ):
        if station_id not in self.locks:
            raise ValueError(f"Station ID '{station_id}' not found.")
        async with self.locks[station_id]:
            if hook not in self.hooks[station_id]:
                return
            finished = await self._call_hook(hook, data, prev_data)
            if finished:
                self.hooks[station_id].remove(hook)

    async def _add_hook(self, station_id: int, hook: HOOK_CALLBACK_TYPE):
        async with self.locks[station_id]:
            if hook not in self.hooks[station_id]:
                self.hooks[station_id].append(hook)

    async def _remove_hook(self, station_id: int, hook: HOOK_CALLBACK_TYPE):
        async with self.locks[station_id]:
            if hook in self.hooks[station_id]:
                self.hooks[station_id].remove(hook)

    async def _refresh_station_status_task(self):
        try:
            while True:
                if not any(self.hooks.values()):
                    break
                try:
                    data = await self._request_station_status()
                except Exception as e:
                    err_msg = f"Error refreshing station status: {e}"
                    logger.error(err_msg)
                    if self.on_error:
                        self.on_error(e, err_msg)
                    await asyncio.sleep(self.POLL_INTERVAL)
                    continue
                current_time = asyncio.get_event_loop().time()
                prev_record = self.station_status
                self.station_status = _StationRecord(data, current_time)

                # 并发调用所有回调函数
                for station_id, hooks in self.hooks.items():
                    if station_id not in data:
                        warn_msg = (
                            f"Station ID {station_id} not found in refreshed data."
                        )
                        logger.warning(warn_msg)
                        if self.on_warning:
                            self.on_warning(warn_msg)
                        continue
                    for hook in hooks:
                        asyncio.create_task(
                            self._handle_hook(
                                station_id,
                                hook,
                                data[station_id],
                                prev_record.data[station_id] if prev_record else None,
                            )
                        )
                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            pass

        # 清理任务引用
        self._refresh_task = None

    def register_hook(self, station_name: str, hook: HOOK_CALLBACK_TYPE):
        if station_name not in self.stations:
            raise ValueError(f"Station '{station_name}' not found.")
        station_id = self.stations[station_name]
        asyncio.create_task(self._add_hook(station_id, hook))
        # 如果有最新数据且未过期则立即调用一次
        if self.station_status and station_id in self.station_status.data:
            if (
                asyncio.get_event_loop().time() - self.station_status.time
                < self.EXPIRE_TIME
            ):
                asyncio.create_task(
                    self._handle_hook(
                        station_id,
                        hook,
                        self.station_status.data[station_id],
                        None,
                    )
                )
        # 启动轮询任务
        if not self._refresh_task:
            self._refresh_task = asyncio.create_task(
                self._refresh_station_status_task()
            )

    def unregister_hook(self, station_name: str, hook: HOOK_CALLBACK_TYPE):
        if station_name not in self.stations:
            raise ValueError(f"Station '{station_name}' not found.")
        station_id = self.stations[station_name]
        if hook in self.hooks[station_id]:
            asyncio.create_task(self._remove_hook(station_id, hook))
        # 如果没有回调函数则取消轮询任务
        if not any(self.hooks.values()):
            if self._refresh_task:
                self._refresh_task.cancel()
                self._refresh_task = None

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await self.client_controller.close()
