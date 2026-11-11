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
    CLEAR_CMD = "clear"
    PREF_CMD = "pref"  # 偏好设置指令

    MAX_THRESHOLD = 5  # 最大空闲数量阈值
    MAX_EXPIRE_MINUTES = 60 * 24  # 最大订阅时间，单位分钟
    DEFAULT_PREF_THRESHOLD = 2  # 偏好设置默认阈值
    DEFAULT_PREF_EXPIRE_MINUTES = 60 * 2  # 偏好设置默认时间，单位分钟

    DATA_SAVE_INTERVAL = 10  # 用户数据保存间隔，单位秒
    DATA_FILE = "user_config.json"
    CURRENT_DATA_VERSION = 1  # 当前数据文件版本

    @dataclass
    class SubscriberData:
        station_name: str
        created_at: float
        expire_in_minutes: int
        threshold: int
        latest_free_count: int = 0
        triggered: bool = False
        hook: ChargeListener.HOOK_CALLBACK_TYPE | None = None

        def __dict__(self):
            return {
                "station_name": self.station_name,
                "created_at": self.created_at,
                "expire_in_minutes": self.expire_in_minutes,
                "threshold": self.threshold,
                "latest_free_count": self.latest_free_count,
                "triggered": self.triggered,
            }

    @dataclass
    class UserPreference:
        station_names: list[str]
        threshold: int
        expire_in_minutes: int

        def __dict__(self):
            return {
                "station_names": self.station_names,
                "threshold": self.threshold,
                "expire_in_minutes": self.expire_in_minutes,
            }

    def __init__(
        self, listener: ChargeListener, send_message: Callable[[int, str], None]
    ):
        self.user_data: dict[int, dict[str, ChargeRobot.SubscriberData]] = {}
        self.user_preferences: dict[int, ChargeRobot.UserPreference] = {}
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
        data_snapshot = {
            user_id: {
                station_name: sub_data.__dict__()
                for station_name, sub_data in subscriber_dict.items()
            }
            for user_id, subscriber_dict in self.user_data.items()
        }
        preferences_snapshot = {
            user_id: pref.__dict__() for user_id, pref in self.user_preferences.items()
        }
        return {
            "version": self.CURRENT_DATA_VERSION,
            "data": data_snapshot,
            "preferences": preferences_snapshot,
        }

    def save_user_data(self):
        snapshot = self.get_user_data_snapshot()
        with open(self.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=4)
        logger.debug(
            f"已保存共 {len(self.user_data)} 位用户的订阅数据和 {len(self.user_preferences)} 位用户的偏好设置"
        )

    def upgrade_data_v0_to_v1(self, file_content: dict) -> dict:
        """将v0格式数据升级到v1格式"""
        logger.info("检测到v0格式数据，正在升级到v1格式...")

        # v0格式直接是用户数据，没有version和preferences字段
        upgraded_data = {
            "version": 1,
            "data": file_content,  # 原数据作为data字段
            "preferences": {},  # 新增空的preferences字段
        }

        logger.info("数据升级完成：v0 -> v1")
        return upgraded_data

    def upgrade_data_if_needed(self, file_content: dict) -> dict:
        """自动升级数据到当前版本"""
        current_version = file_content.get("version", 0)  # 无version字段视为v0

        if current_version == self.CURRENT_DATA_VERSION:
            return file_content  # 已是最新版本

        logger.info(
            f"检测到数据版本 v{current_version}，当前版本 v{self.CURRENT_DATA_VERSION}，开始升级..."
        )

        # 定义升级路径
        upgrade_functions = {
            0: self.upgrade_data_v0_to_v1,
            # 未来版本可以在这里添加：
            # 1: self.upgrade_data_v1_to_v2,
        }

        # 逐步升级到目标版本
        upgraded_data = file_content
        for version in range(current_version, self.CURRENT_DATA_VERSION):
            if version in upgrade_functions:
                upgraded_data = upgrade_functions[version](upgraded_data)
            else:
                logger.error(f"缺少 v{version} 到 v{version+1} 的升级函数")
                raise ValueError(f"无法从版本 v{version} 升级到 v{version+1}")

        # 升级完成后立即保存
        self.save_user_data()
        logger.info(
            f"数据升级完成并已保存：v{current_version} -> v{self.CURRENT_DATA_VERSION}"
        )

        return upgraded_data

    def load_user_data(self):
        if not os.path.exists(self.DATA_FILE):
            return

        with open(self.DATA_FILE, "r", encoding="utf-8") as f:
            file_content = json.load(f)

        # 自动升级数据到当前版本
        file_content = self.upgrade_data_if_needed(file_content)

        # 现在所有数据都应该是最新版本格式
        data = file_content["data"]
        preferences = file_content["preferences"]

        # 加载订阅数据
        for user_id, subscriber_dict in data.items():
            for sub_data in subscriber_dict.values():
                sub_data_obj = ChargeRobot.SubscriberData(
                    station_name=sub_data["station_name"],
                    created_at=sub_data["created_at"],
                    expire_in_minutes=sub_data["expire_in_minutes"],
                    threshold=sub_data["threshold"],
                    triggered=sub_data.get("triggered", False),
                    latest_free_count=sub_data.get("latest_free_count", 0),
                )
                self.add_subscriber(int(user_id), sub_data_obj, echo=False)

        # 加载偏好设置
        for user_id, pref_data in preferences.items():
            pref_obj = ChargeRobot.UserPreference(
                station_names=pref_data["station_names"],
                threshold=pref_data["threshold"],
                expire_in_minutes=pref_data["expire_in_minutes"],
            )
            self.user_preferences[int(user_id)] = pref_obj

        logger.info(
            f"已加载共 {len(self.user_data)} 位用户的订阅数据和 {len(self.user_preferences)} 位用户的偏好设置"
        )

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
                f"未找到充电桩 🚫『{station_name}』\n输入『{self.CMD_PREFIX}{self.LIST_CMD}』查看可用充电桩列表 ⚡",
            )
            return

        if self.user_data.setdefault(user_id, {}).get(station_name):
            self.remove_subscriber(user_id, station_name, echo=False)
            if echo:
                self.send_message(
                    user_id,
                    f"您已订阅过充电桩 🔁『{station_name}』\n已自动为您取消旧订阅并重新添加 ✅",
                )

        async def hook(data: list):
            nonlocal subscriber_data
            station_name = subscriber_data.station_name
            current_free_counter = data["freePileCount"]
            prev_free_counter = subscriber_data.latest_free_count
            subscriber_data.latest_free_count = current_free_counter

            if not subscriber_data.triggered:
                if current_free_counter >= subscriber_data.threshold:
                    subscriber_data.triggered = True
                    self.send_message(
                        user_id,
                        f"🔔 充电桩 『{station_name}』 已有足够的空闲充电位！\n当前空闲充电位数量：{current_free_counter} 🟢",
                    )
            else:
                if current_free_counter != 0:
                    if current_free_counter != prev_free_counter:
                        self.send_message(
                            user_id,
                            f"📊 充电桩 『{station_name}』 空闲充电位数量发生变化！\n当前空闲充电位数量：{current_free_counter} 🟢\n输入『{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}』可结束订阅 ❌",
                        )
                else:
                    self.send_message(
                        user_id,
                        f"🔕 充电桩 『{station_name}』 已满，订阅结束！\n如需继续订阅请重新添加 🔁",
                    )
                    self.remove_subscriber(user_id, station_name, echo=False)
                    return True  # 结束订阅
            if (
                time.time()
                >= subscriber_data.created_at + subscriber_data.expire_in_minutes * 60
            ):
                self.send_message(
                    user_id,
                    f"⏰ 充电桩 『{station_name}』 订阅时长已到期，本次订阅结束！\n如需继续订阅请重新添加 🔁",
                )
                self.remove_subscriber(user_id, station_name, echo=False)
                return True
            return False

        subscriber_data.hook = hook
        self.user_data.setdefault(user_id, {})[station_name] = subscriber_data
        self.listener.register_hook(station_name, hook)

        if echo:
            self.send_message(
                user_id,
                f"✅ 已成功订阅充电桩『{station_name}』！\n\n"
                f"🔔 当空闲充电位 ≥ {subscriber_data.threshold} 时会通知您\n"
                f"📊 若空闲数量变化也会再次提醒\n"
                f"⏰ 订阅将在 {subscriber_data.expire_in_minutes} 分钟后自动失效\n"
                f"如需取消，请输入『{self.CMD_PREFIX}{self.UNSUB_CMD} {station_name}』 ❌",
            )

    def remove_subscriber(self, user_id: int, station_name: str, echo: bool = True):
        if user_id not in self.user_data:
            if echo:
                self.send_message(
                    user_id,
                    "⚠️ 您当前没有任何充电桩订阅",
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
                    f"✅ 已取消充电桩『{station_name}』的订阅",
                )
        elif echo:
            self.send_message(
                user_id,
                f"⚠️ 您当前未订阅充电桩『{station_name}』",
            )
        if not self.user_data[user_id]:
            del self.user_data[user_id]

    def clear_subscribers(self, user_id: int):
        if user_id not in self.user_data:
            self.send_message(
                user_id,
                "⚠️ 您当前没有任何充电桩订阅",
            )
            return
        user_listening_stations = list(self.user_data[user_id].keys())
        for station_name in user_listening_stations:
            self.remove_subscriber(user_id, station_name, echo=False)
        self.send_message(
            user_id,
            "🧹 已取消以下所有充电桩订阅：\n"
            + "\n".join(f"- {name}" for name in user_listening_stations),
        )

    def list_stations(self, user_id: int):
        stations = list(self.listener.stations.keys())
        if not stations:
            self.send_message(
                user_id,
                "🚨 当前没有可用的充电桩！可能是网络问题或接口变更，请联系管理员。",
            )
            return

        async def _get_notify_station_status():
            station_status = await self.listener.get_station_status()
            msg = "⚡ 当前可用的充电桩列表：\n——————————\n"
            for station_info in station_status.values():
                if station_info["freePileCount"] > 0:
                    status_emoji = "🟢"
                else:
                    status_emoji = "🔴"
                # 1代表充电柜，2代表充电桩
                if station_info["stationDeviceType"] == 1:
                    logo_emoji = "🔋"
                else:
                    logo_emoji = "🔌"
                msg += f"{status_emoji} {logo_emoji} {station_info['stationName']} (空闲 {station_info['freePileCount']})\n"
            msg += "——————————\n⚙️ 提示： 🔋 代表充电柜，🔌 代表充电桩；🟢 代表有空闲，🔴 代表无空闲。"
            self.send_message(user_id, msg)

        asyncio.create_task(_get_notify_station_status())

    def list_subscriptions(self, user_id: int):
        if user_id not in self.user_data or not self.user_data[user_id]:
            self.send_message(
                user_id,
                "⚠️ 您当前没有任何充电桩订阅！",
            )
            return
        msg = "📋 您当前订阅的充电桩列表：\n" + "\n".join(
            f"• {data.station_name} ｜阈值：{data.threshold} ｜剩余：{max(0, int((data.created_at + data.expire_in_minutes * 60 - time.time()) / 60))} 分钟"
            for data in self.user_data[user_id].values()
        )
        self.send_message(user_id, msg)

    def set_user_preference(
        self,
        user_id: int,
        station_names: list[str],
        threshold: int,
        expire_in_minutes: int,
    ):
        """设置用户偏好"""
        # 验证充电桩名称
        invalid_stations = [
            name for name in station_names if name not in self.listener.stations
        ]
        if invalid_stations:
            self.send_message(
                user_id,
                f"未找到以下充电桩 🚫：{', '.join(f'『{name}』' for name in invalid_stations)}\n输入『{self.CMD_PREFIX}{self.LIST_CMD}』查看可用充电桩列表 ⚡",
            )
            return

        # 创建或更新偏好设置
        self.user_preferences[user_id] = ChargeRobot.UserPreference(
            station_names=station_names,
            threshold=threshold,
            expire_in_minutes=expire_in_minutes,
        )

        station_list = "、".join(f"『{name}』" for name in station_names)
        self.send_message(
            user_id,
            f"✅ 偏好设置已保存！\n"
            f"📍 充电桩列表：{station_list}\n"
            f"🔔 空闲数量阈值：{threshold}\n"
            f"⏰ 订阅持续时间：{expire_in_minutes} 分钟",
        )

    def use_preference_shortcut(self, user_id: int):
        """使用偏好设置的快捷方式监听任务"""
        # 检查用户是否设置了偏好
        if user_id not in self.user_preferences:
            self.send_message(
                user_id,
                f"⚠️ 您还没有设置偏好！\n请先使用『{self.CMD_PREFIX}{self.PREF_CMD}』命令设置偏好",
            )
            return

        # 检查用户当前是否有订阅任务
        has_subscriptions = user_id in self.user_data and bool(self.user_data[user_id])

        if has_subscriptions:
            # 有任务时执行clear操作
            self.clear_subscribers(user_id)
        else:
            # 无任务时执行偏好任务
            pref = self.user_preferences[user_id]
            success_count = 0

            for station_name in pref.station_names:
                # 添加订阅
                subscriber_data = self.SubscriberData(
                    station_name=station_name,
                    created_at=time.time(),
                    expire_in_minutes=pref.expire_in_minutes,
                    threshold=pref.threshold,
                )
                # 检查充电桩是否存在
                if station_name in self.listener.stations:
                    self.add_subscriber(user_id, subscriber_data, echo=False)
                    success_count += 1
                else:
                    self.send_message(
                        user_id,
                        f"⚠️ 偏好中的充电桩『{station_name}』不存在，已跳过",
                    )

            if success_count > 0:
                station_list = "、".join(
                    f"『{name}』"
                    for name in pref.station_names
                    if name in self.listener.stations
                )
                self.send_message(
                    user_id,
                    f"✅ 已根据偏好设置订阅 {success_count} 个充电桩：{station_list}\n"
                    f"🔔 空闲数量阈值：{pref.threshold}\n"
                    f"⏰ 订阅持续时间：{pref.expire_in_minutes} 分钟",
                )
            else:
                self.send_message(
                    user_id,
                    "❌ 偏好中没有有效的充电桩，无法订阅",
                )

    def help(self, user_id: int):
        msg = (
            "🤖 充电桩订阅机器人使用指南：\n"
            "======================\n"
            f"⚡ 『{self.CMD_PREFIX}{self.LIST_CMD}』查看可用充电桩列表\n"
            f"📋 『{self.CMD_PREFIX}{self.PS_CMD}』查看当前已订阅的充电桩列表\n"
            f"➕ 『{self.CMD_PREFIX}{self.SUB_CMD} <充电桩名> [持续时间(分钟, 默认1440)] [空闲数量阈值(默认1)]』添加充电桩订阅\n"
            f"  例：『{self.CMD_PREFIX}{self.SUB_CMD} 充电桩A 60 2』表示订阅『充电桩A』，当空闲数量达到2个时通知我，订阅持续时间为60分钟\n"
            f"⚙️ 『{self.CMD_PREFIX}{self.PREF_CMD} <充电桩名1> [充电桩名2] ... [阈值(默认{self.DEFAULT_PREF_THRESHOLD})] [时间(分钟,默认{self.DEFAULT_PREF_EXPIRE_MINUTES})]』设置偏好\n"
            f"  例：『{self.CMD_PREFIX}{self.PREF_CMD} 充电桩A 充电桩B 3 45』设置偏好为充电桩A和B，阈值3，持续时间45分钟\n"
            f"➖ 『{self.CMD_PREFIX}{self.UNSUB_CMD} <充电桩名>』取消充电桩订阅\n"
            f"🧹 『{self.CMD_PREFIX}{self.CLEAR_CMD}』取消所有充电桩订阅\n"
            f"💡 『{self.CMD_PREFIX}{self.HELP_CMD}』查看帮助说明\n"
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
            case self.PREF_CMD:
                if not args:
                    self.send_message(
                        user_id,
                        f"⚠️ 请提供至少一个充电桩名称！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return

                # 解析参数：至少一个充电桩名称 + 可选的阈值 + 可选的时间参数
                threshold = self.DEFAULT_PREF_THRESHOLD
                expire_in_minutes = self.DEFAULT_PREF_EXPIRE_MINUTES
                station_names = []

                # 从后往前检查数字参数，最多检查两个
                args_copy = args.copy()
                numeric_args = []

                # 收集后面的数字参数（最多2个）
                while args_copy and args_copy[-1].isdigit() and len(numeric_args) < 2:
                    numeric_args.append(int(args_copy.pop()))

                # 根据数字参数的个数来分配
                if len(numeric_args) == 1:
                    # 只有一个数字参数，作为阈值
                    threshold = numeric_args[0]
                elif len(numeric_args) == 2:
                    # 两个数字参数，第一个是时间，第二个是阈值
                    expire_in_minutes = numeric_args[0]
                    threshold = numeric_args[1]

                # 验证参数范围
                if not (1 <= threshold <= self.MAX_THRESHOLD):
                    self.send_message(
                        user_id,
                        f"⚠️ 空闲数量阈值必须在 1 到 {self.MAX_THRESHOLD} 之间！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return

                if not (1 <= expire_in_minutes <= self.MAX_EXPIRE_MINUTES):
                    self.send_message(
                        user_id,
                        f"⚠️ 持续时间必须在 1 到 {self.MAX_EXPIRE_MINUTES} 分钟之间！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return

                # 剩下的都是充电桩名称
                station_names = args_copy

                if not station_names:
                    self.send_message(
                        user_id,
                        f"⚠️ 请提供至少一个充电桩名称！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return

                self.set_user_preference(
                    user_id, station_names, threshold, expire_in_minutes
                )
            case self.SUB_CMD:
                station_name = args.pop(0) if args else ""
                if not station_name:
                    self.send_message(
                        user_id,
                        f"⚠️ 请提供充电桩名称！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return
                try:
                    expire_in_minutes = (
                        int(args.pop(0)) if args else self.MAX_EXPIRE_MINUTES
                    )
                except ValueError:
                    self.send_message(
                        user_id,
                        f"⚠️ 持续时间参数必须是整数，单位为分钟！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return
                if not (1 <= expire_in_minutes <= self.MAX_EXPIRE_MINUTES):
                    self.send_message(
                        user_id,
                        f"⚠️ 持续时间必须在 1 到 {self.MAX_EXPIRE_MINUTES} 分钟之间！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return
                try:
                    threshold = int(args.pop(0)) if args else 1
                except ValueError:
                    self.send_message(
                        user_id,
                        f"⚠️ 空闲数量阈值参数必须是整数！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return
                if not (1 <= threshold <= self.MAX_THRESHOLD):
                    self.send_message(
                        user_id,
                        f"⚠️ 空闲数量阈值必须在 1 到 {self.MAX_THRESHOLD} 之间！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
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
                        f"⚠️ 请提供充电桩名称！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                    )
                    return
                self.remove_subscriber(user_id, station_name)
            case self.CLEAR_CMD:
                self.clear_subscribers(user_id)
            case self.HELP_CMD:
                self.help(user_id)
            case _:
                self.send_message(
                    user_id,
                    f"⚠️ 未知命令！\n输入『{self.CMD_PREFIX}{self.HELP_CMD}』查看使用帮助",
                )
