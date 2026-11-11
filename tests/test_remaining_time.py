"""Offline regression tests for charge ps; no credentials or QQ connection needed."""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Callable
import unittest
from unittest.mock import Mock, patch


def load_bot():
    # The display method doesn't use the network listener. Isolate that dependency
    # so these tests can run using only the standard library.
    listener = ModuleType('listener')
    listener.ChargeListener = SimpleNamespace(HOOK_CALLBACK_TYPE=Callable)
    spec = importlib.util.spec_from_file_location('remaining_time_bot', Path(__file__).parents[1]/'bot.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'listener': listener, spec.name: module}):
        spec.loader.exec_module(module)
    return module


bot = load_bot()


class RemainingTimeTests(unittest.TestCase):
    def test_remaining_minutes_use_subscription_timestamp(self):
        created = 1_800_000_000
        subscription = SimpleNamespace(station_name='测试站点', threshold=1,
                                       created_at=created, expire_in_minutes=60)
        robot = bot.ChargeRobot.__new__(bot.ChargeRobot)
        robot.user_data = {123: {'测试站点': subscription}}
        robot.send_message = Mock()
        loop = asyncio.new_event_loop()
        try:
            with patch.object(bot.asyncio, 'get_event_loop', return_value=loop):
                for elapsed, expected in ((0, 60), (1800, 30), (3599, 0), (3600, 0), (3660, 0)):
                    with self.subTest(elapsed=elapsed):
                        robot.send_message.reset_mock()
                        with patch.object(bot.time, 'time', return_value=created+elapsed):
                            robot.list_subscriptions(123)
                        robot.send_message.assert_called_once()
                        self.assertIn(f'剩余：{expected} 分钟', robot.send_message.call_args.args[1])
        finally:
            loop.close()


if __name__ == '__main__':
    unittest.main()
