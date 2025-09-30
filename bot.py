from typing import Callable
import asyncio
import logging

from listener import ChargeListener

logger = logging.getLogger(__name__)


class ChargeRobot:
    CMD_PREFIX = "charge "
    LIST_CMD = "list"
    PS_CMD = "ps"
    SUB_CMD = "sub"
    UNSUB_CMD = "stop"
    HELP_CMD = "help"

    MAX_THRESHOLD = 4  # 最大空闲数量阈值
    MAX_EXPIRE_MINUTES = 60 * 24  # 最大订阅时间，单位分钟

    def __init__(
        self, listener: ChargeListener, send_message: Callable[[int, str], None]
    ):
        self.user_hooks: dict[int, dict[str, ChargeListener.HOOK_CALLBACK_TYPE]] = {}
        self.listener = listener
        self.send_message = send_message

    def add_subscriber(
        self,
        user_id: int,
        station_name: str,
        *,
        expire_in_minutes: int,
        threshold: int,
    ):
        if station_name not in self.listener.stations:
            self.send_message(
                user_id,
                f"未找到充电桩 '{station_name}'，输入 '{self.CMD_PREFIX}{self.LIST_CMD}' 查看可用充电桩列表",
            )
            return

        if self.user_hooks.setdefault(user_id, {}).get(station_name):
            self.send_message(
                user_id,
                f"您当前已订阅充电桩 '{station_name}' ，请勿重复订阅！\n如需取消订阅请输入 '{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}'",
            )
            return

        start_time = asyncio.get_event_loop().time()
        triggered = False

        async def hook(data: list, prev_data: list | None = None):
            nonlocal triggered
            current_free_counter = len(
                list(filter(lambda x: x["showStatusString"] == "空闲", data))
            )
            prev_free_counter = len(
                list(filter(lambda x: x["showStatusString"] == "空闲", prev_data or []))
            )
            if not triggered:
                if current_free_counter >= threshold:
                    triggered = True
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
            if asyncio.get_event_loop().time() >= start_time + expire_in_minutes * 60:
                self.send_message(
                    user_id,
                    f"充电桩 '{station_name}' 订阅时长已到期，本次订阅结束！\n如需继续订阅请重新添加",
                )
                return True
            return False

        self.user_hooks[user_id][station_name] = hook
        self.listener.register_hook(station_name, hook)

        self.send_message(
            user_id,
            f"已为您添加充电桩 '{station_name}' 的订阅！\n当空闲充电位数量达到 {threshold} 个时会通知您，并在空闲充电位数量变化时再次通知您直到空闲充电位数量变为0。\n在 {expire_in_minutes} 分钟后订阅将自动取消。\n输入 '{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}' 可手动取消订阅",
        )

    def remove_subscriber(self, user_id: int, station_name: str, echo: bool = True):
        if user_id not in self.user_hooks:
            if echo:
                self.send_message(
                    user_id,
                    "您当前没有任何充电桩订阅！",
                )
            return
        if station_name in self.user_hooks[user_id]:
            self.listener.unregister_hook(
                station_name, self.user_hooks[user_id][station_name]
            )
            del self.user_hooks[user_id][station_name]
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
        if not self.user_hooks[user_id]:
            del self.user_hooks[user_id]

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
        if user_id not in self.user_hooks or not self.user_hooks[user_id]:
            self.send_message(
                user_id,
                "您当前没有任何充电桩订阅！",
            )
            return
        stations = list(self.user_hooks[user_id].keys())
        msg = "您当前订阅的充电桩列表：\n" + "\n".join(f"- {name}" for name in stations)
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
                    station_name,
                    expire_in_minutes=expire_in_minutes,
                    threshold=threshold,
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
