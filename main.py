import json
import websockets
import asyncio
import dotenv
import os
import logging

from bot import ChargeRobot
from listener import ChargeListener

logger = logging.getLogger(__name__)
dotenv.load_dotenv()


async def qq_bot_server():
    async with websockets.connect(
        uri=os.getenv("ROBOT_WS_URL"),
        additional_headers={"Authorization": f"Bearer {os.getenv('QQ_TOKEN')}"},
    ) as ws:
        def send_private_msg(user_id: int, msg: str):
            asyncio.create_task(
                ws.send(
                    json.dumps(
                        {
                            "action": "send_private_msg",
                            "params": {
                                "user_id": user_id,
                                "message": [{"type": "text", "data": {"text": msg}}],
                            },
                        }
                    )
                )
            )

        def send_group_private_msg(user_id: int, msg: str):
            asyncio.create_task(
                ws.send(
                    json.dumps(
                        {
                            "action": "send_group_msg",
                            "params": {
                                "group_id": int(os.getenv("WORK_GROUP")),
                                "message": [
                                    {
                                        "type": "at",
                                        "data": {"qq": user_id},
                                    },
                                    {"type": "text", "data": {"text": "\n" + msg}},
                                ],
                            },
                        }
                    )
                )
            )

        def on_error(e: Exception, msg: str):
            send_private_msg(
                int(os.getenv("MASTER_QQ")),
                f"ChargeListener错误: {msg}\n--------\n{e}",
            )

        def on_warning(msg: str):
            send_private_msg(int(os.getenv("MASTER_QQ")), f"ChargeListener警告: {msg}")

        charge_listener = await ChargeListener.create(
            host=os.getenv("CQT_HOST"),
            openid=os.getenv("OPEN_ID"),
            phonenumber=os.getenv("PHONENUMBER"),
            longitude=os.getenv("LONGITUDE"),
            latitude=os.getenv("LATITUDE"),
            on_error=on_error,
            on_warning=on_warning,
        )

        robot = ChargeRobot(charge_listener, send_group_private_msg)
        logger.info("ChargeRobot started")
        send_private_msg(
            int(os.getenv("MASTER_QQ")),
            "ChargeRobot已启动！\n输入 'charge help' 获取帮助信息。",
        )

        async for ws_message in ws:
            message_data = json.loads(ws_message)
            if message_data.get("message_type") == "group":
                group_id: int = message_data["group_id"]
                if group_id != int(os.getenv("WORK_GROUP")):
                    continue
                user_id: int = message_data["user_id"]
                message: str = message_data["raw_message"]
                robot_id = message_data["self_id"]
                if message.strip() == f"[CQ:at,qq={robot_id}]":
                    robot.use_preference_shortcut(user_id)
                    continue
                robot.handle_message(user_id, message)

        # 保存用户数据
        robot.save_user_data()
        logger.info("ChargeRobot stopped")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    while True:
        try:
            asyncio.run(qq_bot_server())
        except Exception as e:
            logger.error(f"qq_bot_server error: {e}")
        logger.info("Reconnecting to qq_bot_server")


if __name__ == "__main__":
    main()
