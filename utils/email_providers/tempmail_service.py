import re
import time
from typing import Optional, Dict, Any, List
from curl_cffi import requests
from utils import config as cfg

EMAIL_ADDRESS_PATTERN = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

class TempmailService:
    """
    Tempmail.lol 邮箱服务
    """
    def __init__(self, proxies: Optional[Dict[str, str]] = None):
        self.base_url = "https://api.tempmail.lol/v2"
        self.proxies = proxies
        self.timeout = 15

    def create_email(self) -> tuple[Optional[str], Optional[str]]:
        try:
            payload = None

            headers = {"Accept": "application/json"}
            if payload:
                headers["Content-Type"] = "application/json"

            response = requests.post(
                f"{self.base_url}/inbox/create",
                headers=headers,
                json=payload,
                proxies=self.proxies,
                timeout=self.timeout,
                impersonate="chrome110"
            )

            if response.status_code in (200, 201):
                data = response.json()
                email = str(data.get("address", "")).strip()
                token = str(data.get("token", "")).strip()
                if email and token:
                    return email, token
                else:
                    print(f"[{cfg.ts()}] [ERROR] HTTP状态正常，但找不到邮箱字段。返回: {response.text}")
            else:
                print(f"[{cfg.ts()}] [ERROR] 创建邮箱被拒！HTTP状态码: {response.status_code}, 详情: {response.text}")

            return None, None

        except Exception as e:
            print(f"[{cfg.ts()}] [ERROR] 请求发送彻底失败: {e}")
            return None, None

    def get_inbox(self, token: str) -> List[Dict[str, Any]]:
        """
        获取邮箱收件箱内容
        返回: 邮件列表
        """
        try:
            response = requests.get(
                f"{self.base_url}/inbox",
                params={"token": token},
                headers={"Accept": "application/json"},
                proxies=self.proxies,
                timeout=self.timeout,
                impersonate="chrome110"
            )

            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    return data.get("emails", [])
            return []
        except Exception as e:
            return []