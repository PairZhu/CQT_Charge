# 常青藤充电桩订阅
一个用于订阅常青藤充电桩状态变化的Python程序。用户可以通过QQ机器人订阅特定充电桩，当充电桩的空闲充电位数量达到设定的阈值时，机器人会发送通知。
## 快速开始
1. 克隆仓库
   ```bash
   git clone <repository-url>
   ```
2. 安装依赖
   ```bash
   uv sync
   ```
3. 设置环境变量(可使用`.env`文件)
   - `CQT_HOST`: 常青藤充电桩API的主机地址
   - `OPEN_ID`: 用户的OpenID
   - `PHONENUMBER`: 用户的手机号
   - `LONGITUDE`: 用户所在位置的经度
   - `LATITUDE`: 用户所在位置的纬度
   - `QQ_TOKEN`: QQ机器人的API令牌
   - `ROBOT_WS_URL`: QQ机器人的WebSocket URL
   - `MASTER_QQ`: 管理员QQ号
   - `WORK_GROUP`: 机器人工作的QQ群号
4. 运行程序
   ```bash
   uv run main.py
   ```