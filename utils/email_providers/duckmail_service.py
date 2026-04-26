import random
import string
import time
import re
import logging
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from curl_cffi import requests
from utils import config as cfg

logger = logging.getLogger(__name__)


class DuckMailService:

    def __init__(self, proxies=None):
        self.proxies = proxies
        self.mode = str(getattr(cfg, 'DUCKMAIL_MODE', 'custom_api')).strip().lower()

        self.duck_api_base_url = str(getattr(cfg, 'DUCK_OFFICIAL_API_BASE', 'https://quack.duckduckgo.com')).rstrip("/")
        self.base_url = str(getattr(cfg, 'DUCKMAIL_API_URL', 'https://api.duckmail.com')).rstrip("/")

        self.api_token = str(getattr(cfg, 'DUCK_API_TOKEN', '')).strip()
        self.cookie = str(getattr(cfg, 'DUCK_COOKIE', '')).strip()
        self.domain = str(getattr(cfg, 'DUCKMAIL_DOMAIN', 'duck.com')).strip()

        self.timeout = 30

    def _make_request(self, method: str, url: str, headers: Dict[str, str] = None, **kwargs) -> Dict[str, Any]:
        try:
            impersonate = "chrome110" if "duckduckgo" in url else None

            resp = requests.request(
                method, url,
                headers=headers,
                proxies=self.proxies,
                timeout=self.timeout,
                impersonate=impersonate,
                verify=False,
                **kwargs
            )

            if resp.status_code >= 400:
                return {"error": True, "status": resp.status_code, "text": resp.text}

            try:
                return resp.json()
            except:
                return {"raw_response": resp.text}
        except Exception as e:
            return {"error": True, "msg": str(e)}

    def _resolve_duck_official_token(self) -> str:
        token = self.api_token
        cookie = self.cookie

        if token and not cookie:
            return token

        if token and cookie:
            headers = {
                "Authorization": f"Bearer {token}",
                "Cookie": cookie,
                "Accept": "application/json"
            }
            res = self._make_request("GET", f"{self.duck_api_base_url}/api/email/dashboard", headers=headers)
            if not res.get("error"):
                return token


            logger.info("Duck token 失效，尝试通过 cookie 刷新 token")

        if not cookie:
            logger.warning("Duck 官方模式缺少有效 Token 或 Cookie")
            return token

        headers = {"Cookie": cookie, "Accept": "application/json"}
        dashboard = self._make_request("GET", f"{self.duck_api_base_url}/api/email/dashboard", headers=headers)

        refreshed = dashboard.get("access_token") or \
                    dashboard.get("token") or \
                    (dashboard.get("data") or {}).get("access_token")

        if refreshed:
            self.api_token = str(refreshed).strip()
            return self.api_token

        return token

    def create_email(self) -> Tuple[Optional[str], Optional[str]]:

        if self.mode == "duck_official":
            token = self._resolve_duck_official_token()
            if not token:
                return None, None

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            if self.cookie:
                headers["Cookie"] = self.cookie

            res = self._make_request("POST", f"{self.duck_api_base_url}/api/email/addresses", headers=headers)

            address = res.get("address") or (res.get("data") or {}).get("address")
            if address:
                address = str(address).strip()
                if "@" not in address:
                    full_addr = f"{address}@duck.com"
                elif address.endswith("@"):
                    full_addr = f"{address}duck.com"
                else:
                    full_addr = address

                return full_addr, token
        else:
            user = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            address = f"{user}@{self.domain}"
            password = "".join(random.choices(string.ascii_letters + string.digits, k=12))


            self._make_request("POST", f"{self.base_url}/accounts", json={"address": address, "password": password})

            tk_res = self._make_request("POST", f"{self.base_url}/token",
                                        json={"address": address, "password": password})
            return address, tk_res.get("token")

        return None, None

    def get_inbox(self, token: str) -> list:
        if self.mode == "duck_official":
            return "__DELEGATE_TO_IMAP__"

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        res = self._make_request("GET", f"{self.base_url}/messages", headers=headers, params={"page": 1})
        messages = res.get("hydra:member", [])
        details = []
        for m in messages:
            msg_id = m.get("id")
            if msg_id:
                d = self._make_request("GET", f"{self.base_url}/messages/{msg_id}", headers=headers)
                if not d.get("error"): details.append(d)
        return details

    def strip_html(self, html: str) -> str:
        return unescape(re.sub(r"<[^>]+>", " ", str(html or "")))