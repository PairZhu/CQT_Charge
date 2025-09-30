import asyncio
from typing import Callable
from client import ChargeClient, ChargeClientController
from dataclasses import dataclass
import logging
import traceback

logger = logging.getLogger(__name__)


# 监听充电桩状态，允许注册回调函数订阅充电桩状态
class ChargeListener:
    @dataclass
    class __StationRecord:
        data: list
        time: float

    POLL_INTERVAL = 20  # 轮询间隔，单位秒
    EXPIRE_TIME = 60  # 数据过期时间，单位秒

    HOOK_CALLBACK_TYPE = (
        Callable[[list], asyncio.Future] | Callable[[list, list], asyncio.Future]
    )

    def __init__(
        self,
        client_controller: ChargeClientController,
        stations: dict[str, int],
        *,
        on_error: Callable[[Exception, str], None] | None = None,
        on_warning: Callable[[str], None] | None = None,
    ):
        self.client_controller = client_controller
        self.latest_station_record: dict[int, ChargeListener.__StationRecord] = {}
        self.stations: dict[str, int] = stations
        self.tasks: dict[int, asyncio.Task] = {}
        self.hooks: dict[int, list[ChargeListener.HOOK_CALLBACK_TYPE]] = {
            station_id: [] for station_id in stations.values()
        }
        self.locks: dict[int, asyncio.Lock] = {
            station_id: asyncio.Lock() for station_id in stations.values()
        }
        self.on_error = on_error
        self.on_warning = on_warning

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
        listener = cls(client_controller, stations, **kwargs)
        return listener

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

    async def _poll_station(self, station_id: int):
        try:
            while True:
                if not self.hooks[station_id]:
                    break
                try:
                    data = await self.client_controller.get_station_info(station_id)
                except Exception as e:
                    err_msg = f"Error polling station {station_id}: {e}\n{traceback.format_exc()}"
                    logger.error(err_msg)
                    if self.on_error:
                        self.on_error(e, err_msg)
                    await asyncio.sleep(self.POLL_INTERVAL)
                    continue

                current_time = asyncio.get_event_loop().time()
                prev_record = self.latest_station_record.get(station_id)
                self.latest_station_record[station_id] = self.__StationRecord(
                    data=data, time=current_time
                )

                # 并发调用所有回调函数
                for hook in self.hooks[station_id]:
                    asyncio.create_task(
                        self._handle_hook(
                            station_id,
                            hook,
                            data,
                            prev_record.data if prev_record else None,
                        )
                    )

                await asyncio.sleep(self.POLL_INTERVAL)
        except asyncio.CancelledError:
            pass

        # 清理任务
        if station_id in self.tasks:
            del self.tasks[station_id]

    def register_hook(self, station_name: str, hook: HOOK_CALLBACK_TYPE):
        if station_name not in self.stations:
            raise ValueError(f"Station '{station_name}' not found.")
        station_id = self.stations[station_name]
        asyncio.create_task(self._add_hook(station_id, hook))
        # 如果有最新数据且未过期则立即调用一次
        if station_id in self.latest_station_record:
            if (
                asyncio.get_event_loop().time()
                - self.latest_station_record[station_id].time
                < self.EXPIRE_TIME
            ):
                asyncio.create_task(
                    self._handle_hook(
                        station_id,
                        hook,
                        self.latest_station_record[station_id].data,
                        None,
                    )
                )
        # 启动轮询任务
        if station_id not in self.tasks:
            self.tasks[station_id] = asyncio.create_task(self._poll_station(station_id))

    def unregister_hook(self, station_name: str, hook: HOOK_CALLBACK_TYPE):
        if station_name not in self.stations:
            raise ValueError(f"Station '{station_name}' not found.")
        station_id = self.stations[station_name]
        if hook in self.hooks[station_id]:
            asyncio.create_task(self._remove_hook(station_id, hook))
        # 如果没有回调函数则取消轮询任务
        if not self.hooks[station_id]:
            if station_id in self.tasks:
                self.tasks[station_id].cancel()
                del self.tasks[station_id]

    async def wait_all(self):
        await asyncio.gather(*self.tasks.values())

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await self.client_controller.close()
