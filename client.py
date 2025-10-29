import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

class ChargeClient:
    def __init__(self, host, openid, phonenumber):
        self.host = host
        self.openid = openid
        self.phonenumber = phonenumber
        self.session = None
        self.token = None

    async def login(self):
        if self.session:
            await self.session.close()
        self.session = aiohttp.ClientSession()
        url = f"{self.host}/api/MiniAccount/Login"
        data = {"openid": self.openid, "phonenumber": self.phonenumber}
        async with self.session.post(url, json=data) as response:
            response_data = (await response.json())["data"]
            self.token = response_data["access_token"]
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})
            return response_data

    async def get_station_info(self, station_id):
        if not self.session or not self.token:
            raise Exception("Not logged in")
        url = f"{self.host}/api/ChargeStation/boxpiles?stationId={station_id}"
        async with self.session.get(url) as response:
            if response.status != 200:
                raise Exception(f"Error fetching station info: {response.status}")
            resp = await response.json()
        data = resp["data"]
        if not data:
            raise ValueError(f"No data fund in json response: {resp}")
        # 合并为一个列表
        result = [pile for box in data for pile in box.get("piles", [])]
        return result

    async def get_stations(self, longitude, latitude):
        if not self.session or not self.token:
            raise Exception("Not logged in")
        url = f"{self.host}/api/ChargeStation/list?longitude={longitude}&latitude={latitude}"
        async with self.session.get(url) as response:
            resp = await response.json()

        data = resp.get("data", [])
        return {item["stationName"]: item for item in data}

    async def close(self):
        if self.session:
            await self.session.close()


# 控制请求频率、控制登录时机
class ChargeClientController:
    RELOGIN_INTERVAL = 24 * 60 * 60  # 24小时重新登录一次
    MIN_REQUEST_INTERVAL = 5  # 最小请求间隔，单位秒
    MAX_REQUESTS_PER_MINUTE = 15  # 每分钟最大请求数

    def __init__(self, client: ChargeClient):
        self.client = client
        self.login_time = -self.RELOGIN_INTERVAL
        self.request_times = []  # 滑动窗口记录请求时间
        self.error_count = 0
        self.lock = asyncio.Lock()

    async def ensure_login(self):
        if not self.client.token:
            await self.client.login()
            self.login_time = asyncio.get_event_loop().time()
            return
        # 每隔24小时重新登录一次
        if asyncio.get_event_loop().time() - self.login_time > self.RELOGIN_INTERVAL:
            await self.client.login()
            self.login_time = asyncio.get_event_loop().time()
            return

    async def ensure_rate_limit(self):
        async with self.lock:
            current_time = asyncio.get_event_loop().time()
            # 移除一分钟前的请求时间
            self.request_times = [
                t for t in self.request_times if current_time - t < 60
            ]
            if len(self.request_times) >= self.MAX_REQUESTS_PER_MINUTE:
                wait_time = 60 - (current_time - self.request_times[0])
                await asyncio.sleep(wait_time)
            # 确保最小请求间隔
            if self.request_times:
                latest_request_time = self.request_times[-1]
                elapsed = current_time - latest_request_time
                if elapsed < self.MIN_REQUEST_INTERVAL:
                    await asyncio.sleep(self.MIN_REQUEST_INTERVAL - elapsed)
            current_time = asyncio.get_event_loop().time()
            self.request_times.append(current_time)

    async def get_station_info(self, station_id):
        await self.ensure_rate_limit()
        await self.ensure_login()
        try:
            logger.info(f"Fetching station info for station_id: {station_id}")
            data = await self.client.get_station_info(station_id)
            self.error_count = 0
            return data
        except Exception:
            self.error_count += 1
            if self.error_count >= 5:
                logger.error("Multiple consecutive errors, forcing re-login")
                self.client.token = None  # 强制重新登录
            raise

    async def get_stations(self, longitude, latitude):
        await self.ensure_login()
        await self.ensure_rate_limit()
        return await self.client.get_stations(longitude, latitude)

    async def close(self):
        await self.client.close()
