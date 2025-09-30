from dataclasses import dataclass
import json
import os
from typing import Callable
import asyncio
import logging
import time

from listener import ChargeListener

logger = logging.getLogger(__name__)


class ChargeRobot:
    CMD_PREFIX = "charge "
    LIST_CMD = "list"
    PS_CMD = "ps"
    SUB_CMD = "sub"
    UNSUB_CMD = "stop"
    HELP_CMD = "help"

    MAX_THRESHOLD = 5  # 最大空闲数量阈值
    MAX_EXPIRE_MINUTES = 60 * 24  # 最大订阅时间，单位分钟

    DATA_SAVE_INTERVAL = 60  # 用户数据保存间隔，单位秒
    DATA_FILE = "user_config.json"

    @dataclass
    class SubscriberData:
        station_name: str
        created_at: float
        expire_in_minutes: int
        threshold: int
        triggered: bool = False
        hook: ChargeListener.HOOK_CALLBACK_TYPE | None = None

        def __dict__(self):
            return {
                "station_name": self.station_name,
                "created_at": self.created_at,
                "expire_in_minutes": self.expire_in_minutes,
                "threshold": self.threshold,
                "triggered": self.triggered,
            }

    def __init__(
        self, listener: ChargeListener, send_message: Callable[[int, str], None]
    ):
        self.user_data: dict[int, dict[str, ChargeRobot.SubscriberData]] = {}
        self.listener = listener
        self.send_message = send_message
        self.load_user_data()
        asyncio.create_task(self.save_user_data_periodically())

    async def save_user_data_periodically(self):
        old_data = self.get_user_data_snapshot()
        while True:
            await asyncio.sleep(self.DATA_SAVE_INTERVAL)
            new_data = self.get_user_data_snapshot()
            if new_data != old_data:
                self.save_user_data()
                old_data = new_data
                logger.info("用户数据已更新并保存")

    def get_user_data_snapshot(self):
        return {
            user_id: {
                station_name: sub_data.__dict__()
                for station_name, sub_data in subscriber_dict.items()
            }
            for user_id, subscriber_dict in self.user_data.items()
        }

    def save_user_data(self):
        data = self.get_user_data_snapshot()
        with open(self.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        logger.debug(f"已保存共 {len(self.user_data)} 位用户的订阅数据")

    def load_user_data(self):
        if not os.path.exists(self.DATA_FILE):
            return
        with open(self.DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for user_id, subscriber_dict in data.items():
                for sub_data in subscriber_dict.values():
                    sub_data_obj = ChargeRobot.SubscriberData(
                        station_name=sub_data["station_name"],
                        created_at=sub_data["created_at"],
                        expire_in_minutes=sub_data["expire_in_minutes"],
                        threshold=sub_data["threshold"],
                        triggered=sub_data["triggered"],
                    )
                    self.add_subscriber(int(user_id), sub_data_obj, echo=False)
        logger.info(f"已加载共 {len(self.user_data)} 位用户的订阅数据")

    def add_subscriber(
        self,
        user_id: int,
        subscriber_data: SubscriberData,
        echo: bool = True,
    ):
        station_name = subscriber_data.station_name
        if station_name not in self.listener.stations:
            self.send_message(
                user_id,
                f"未找到充电桩 '{station_name}'，输入 '{self.CMD_PREFIX}{self.LIST_CMD}' 查看可用充电桩列表",
            )
            return

        if self.user_data.setdefault(user_id, {}).get(station_name):
            self.remove_subscriber(user_id, station_name, echo=False)
            if echo:
                self.send_message(
                    user_id,
                    f"检测到您已订阅充电桩 '{station_name}'，已为您取消之前的订阅，正在为您重新添加新的订阅...",
                )

        async def hook(data: list, prev_data: list | None = None):
            nonlocal subscriber_data
            station_name = subscriber_data.station_name
            current_free_counter = len(
                list(filter(lambda x: x["showStatusString"] == "空闲", data))
            )
            prev_free_counter = len(
                list(filter(lambda x: x["showStatusString"] == "空闲", prev_data or []))
            )

            if not subscriber_data.triggered:
                if current_free_counter >= subscriber_data.threshold:
                    subscriber_data.triggered = True
                    self.send_message(
                        user_id,
                        f"充电桩 '{station_name}' 已有足够的空闲充电位！\n当前空闲充电位数量：{current_free_counter}",
                    )
            else:
                if current_free_counter != 0:
                    if current_free_counter != prev_free_counter:
                        self.send_message(
                            user_id,
                            f"充电桩 '{station_name}' 空闲充电位数量发生变化！\n当前空闲充电位数量：{current_free_counter}\n输入 '{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}' 结束订阅",
                        )
                else:
                    self.send_message(
                        user_id,
                        f"充电桩 '{station_name}' 充电位已满，本次订阅结束！\n如需继续订阅请重新添加",
                    )
                    self.remove_subscriber(user_id, station_name, echo=False)
                    return True  # 结束订阅
            if (
                time.time()
                >= subscriber_data.created_at + subscriber_data.expire_in_minutes * 60
            ):
                self.send_message(
                    user_id,
                    f"充电桩 '{station_name}' 订阅时长已到期，本次订阅结束！\n如需继续订阅请重新添加",
                )
                self.remove_subscriber(user_id, station_name, echo=False)
                return True
            return False

        subscriber_data.hook = hook
        self.user_data[user_id][station_name] = subscriber_data
        self.listener.register_hook(station_name, hook)

        if echo:
            self.send_message(
                user_id,
                f"已为您添加充电桩 '{station_name}' 的订阅！\n当空闲充电位数量达到 {subscriber_data.threshold} 个时会通知您，并在空闲充电位数量变化时再次通知您，直到空闲充电位数量变为0。\n在 {subscriber_data.expire_in_minutes} 分钟后订阅将自动取消。\n输入 '{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}' 可手动取消订阅",
            )

    def remove_subscriber(self, user_id: int, station_name: str, echo: bool = True):
        if user_id not in self.user_data:
            if echo:
                self.send_message(
                    user_id,
                    "您当前没有任何充电桩订阅！",
                )
            return
        if station_name in self.user_data[user_id]:
            self.listener.unregister_hook(
                station_name, self.user_data[user_id][station_name].hook
            )
            del self.user_data[user_id][station_name]
            if echo:
                self.send_message(
                    user_id,
                    f"已为您取消充电桩 '{station_name}' 的订阅！",
                )
        elif echo:
            self.send_message(
                user_id,
                f"您当前没有订阅充电桩 '{station_name}' ！",
            )
        if not self.user_data[user_id]:
            del self.user_data[user_id]

    def list_stations(self, user_id: int):
        stations = list(self.listener.stations.keys())
        if not stations:
            self.send_message(
                user_id,
                "当前没有可用的充电桩！可能是网络问题或接口变更，请联系管理员。",
            )
            return
        msg = "当前可用的充电桩列表：\n" + "\n".join(f"- {name}" for name in stations)
        self.send_message(user_id, msg)

    def list_subscriptions(self, user_id: int):
        if user_id not in self.user_data or not self.user_data[user_id]:
            self.send_message(
                user_id,
                "您当前没有任何充电桩订阅！",
            )
            return
        msg = "您当前订阅的充电桩列表：\n" + "\n".join(
            f"- {data.station_name} (阈值: {data.threshold}, 剩余: {max(0, int((data.created_at + data.expire_in_minutes * 60 - asyncio.get_event_loop().time()) / 60))} 分钟)"
            for data in self.user_data[user_id].values()
        )
        self.send_message(user_id, msg)

    def help(self, user_id: int):
        msg = (
            "充电桩订阅机器人使用帮助：\n"
            f"- 输入 '{self.CMD_PREFIX}{self.LIST_CMD}' 查看可用充电桩列表\n"
            f"- 输入 '{self.CMD_PREFIX}{self.PS_CMD}' 查看当前已订阅的充电桩列表\n"
            f"- 输入 '{self.CMD_PREFIX}{self.SUB_CMD} <充电桩名称> [持续时间(单位：分钟，默认24小时)] [空闲数量阈值(默认1)]' 添加充电桩订阅，例如：'{self.CMD_PREFIX}{self.SUB_CMD} 充电桩A 60 2' 表示订阅'充电桩A'，当空闲数量达到2个时通知我，订阅持续时间为60分钟\n"
            f"- 输入 '{self.CMD_PREFIX}{self.UNSUB_CMD} <充电桩名称>' 取消充电桩订阅，例如：'{self.CMD_PREFIX}{self.UNSUB_CMD} 充电桩A'\n"
            f"- 输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看本帮助信息\n"
        )
        self.send_message(user_id, msg)

    def handle_message(self, user_id: int, message: str):
        if not message.startswith(self.CMD_PREFIX):
            return
        parts = message[len(self.CMD_PREFIX) :].strip().split()
        if not parts:
            self.help(user_id)
            return
        cmd = parts[0]
        args = parts[1:]
        match cmd:
            case self.LIST_CMD:
                self.list_stations(user_id)
            case self.PS_CMD:
                self.list_subscriptions(user_id)
            case self.SUB_CMD:
                station_name = args.pop(0) if args else ""
                if not station_name:
                    self.send_message(
                        user_id,
                        f"请提供充电桩名称！\n输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看使用帮助",
                    )
                    return
                try:
                    expire_in_minutes = (
                        int(args.pop(0)) if args else self.MAX_EXPIRE_MINUTES
                    )
                except ValueError:
                    self.send_message(
                        user_id,
                        "持续时间参数必须是整数，单位为分钟！\n输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看使用帮助",
                    )
                    return
                if not (1 <= expire_in_minutes <= self.MAX_EXPIRE_MINUTES):
                    self.send_message(
                        user_id,
                        f"持续时间必须在 1 到 {self.MAX_EXPIRE_MINUTES} 分钟之间！",
                    )
                    return
                try:
                    threshold = int(args.pop(0)) if args else 1
                except ValueError:
                    self.send_message(
                        user_id,
                        "空闲数量阈值参数必须是整数！\n输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看使用帮助",
                    )
                    return
                if not (1 <= threshold <= self.MAX_THRESHOLD):
                    self.send_message(
                        user_id,
                        f"空闲数量阈值必须在 1 到 {self.MAX_THRESHOLD} 之间！",
                    )
                    return
                self.add_subscriber(
                    user_id,
                    subscriber_data=self.SubscriberData(
                        station_name=station_name,
                        created_at=time.time(),
                        expire_in_minutes=expire_in_minutes,
                        threshold=threshold,
                    ),
                )
            case self.UNSUB_CMD:
                station_name = args.pop(0) if args else ""
                if not station_name:
                    self.send_message(
                        user_id,
                        f"请提供充电桩名称！\n输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看使用帮助",
                    )
                    return
                self.remove_subscriber(user_id, station_name)
            case self.HELP_CMD:
                self.help(user_id)
            case _:
                self.send_message(
                    user_id,
                    f"未知命令 '{cmd}'！\n输入 '{self.CMD_PREFIX}{self.HELP_CMD}' 查看使用帮助",
                )
