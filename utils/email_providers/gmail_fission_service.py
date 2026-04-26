import os
import random
import string
import time
import socket
import base64
import httplib2
import socks
import urllib.parse
from typing import Optional, List, Dict, Any
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from utils import config as cfg
from utils import db_manager
from utils.email_providers.gmail_oauth_handler import GmailOAuthHandler


class GmailFissionService:
    """Gmail OAuth 专用服务类"""
    _MYSTIC_NAMES = [
        "leo", "nova", "kai", "luna", "milo", "iris", "axel", "zara"
    ]
    _MYSTIC_NOUNS = [
        "fox", "river", "comet", "lotus", "cloud", "ember", "aurora", "tiger"
    ]

    def __init__(self, proxies: Optional[Dict[str, str]] = None):
        self.proxies = proxies
        self.master_email = getattr(cfg, "GMAIL_OAUTH_MASTER_EMAIL", "").strip()

    def _resolve_suffix_mode(self) -> str:
        mode = str(getattr(cfg, "GMAIL_OAUTH_SUFFIX_MODE", "fixed") or "fixed").strip().lower()
        if mode not in {"fixed", "range", "mystic"}:
            return "fixed"
        return mode

    def _resolve_suffix_bounds(self, user_part: str) -> tuple[int, int]:
        try:
            min_len = int(getattr(cfg, "GMAIL_OAUTH_SUFFIX_LEN_MIN", 8) or 8)
        except Exception:
            min_len = 8
        try:
            max_len = int(getattr(cfg, "GMAIL_OAUTH_SUFFIX_LEN_MAX", min_len) or min_len)
        except Exception:
            max_len = min_len

        min_len = max(8, min(32, min_len))
        max_len = max(8, min(32, max_len))
        if max_len < min_len:
            max_len = min_len

        user_len = len(str(user_part or ""))
        available = 64 - user_len - 1
        if available <= 0:
            return 0, 0

        min_len = max(1, min(min_len, available))
        max_len = max(min_len, min(max_len, available))
        return min_len, max_len

    def _random_hex(self, length: int) -> str:
        return "".join(random.choices("0123456789abcdef", k=max(1, int(length))))

    def _build_mystic_seed(self) -> str:
        name = random.choice(self._MYSTIC_NAMES)
        noun = random.choice(self._MYSTIC_NOUNS)
        mmdd = f"{random.randint(1, 12):02d}{random.randint(1, 28):02d}"
        yyyy = str(random.randint(1990, 2012))
        return random.choice([
            f"{name}{noun}{mmdd}",
            f"{noun}{name}{mmdd}",
            f"{name}{mmdd}{noun}",
            f"{name}{noun}{yyyy}",
        ]).lower()

    def generate_suffix_v2(self, user_part: str = ""):
        mode = self._resolve_suffix_mode()
        min_len, max_len = self._resolve_suffix_bounds(user_part)
        if max_len <= 0:
            return ""
        target_len = min_len if mode == "fixed" else random.randint(min_len, max_len)

        if mode == "mystic":
            suffix = "".join(ch for ch in self._build_mystic_seed() if ch.isalnum())
            if len(suffix) < target_len:
                suffix += "".join(random.choices(string.ascii_lowercase + string.digits, k=target_len - len(suffix)))
            return suffix[:target_len]

        return self._random_hex(target_len)

    def generate_dot_variants(self, user_part: str) -> str:
        if len(user_part) <= 1:
            return user_part
        chars = list(user_part)
        slots = len(chars) - 1

        insert_count = random.randint(1, slots)
        insert_positions = random.sample(range(slots, 0, -1), insert_count)

        insert_positions.sort(reverse=True)
        for pos in insert_positions:
            chars.insert(pos, '.')
        return "".join(chars)

    def get_unused_mailbox(self) -> Optional[dict]:
        if not self.master_email or "@" not in self.master_email:
            print(f"[{cfg.ts()}] [ERROR] 未配置 Gmail 主号，请前往配置文件设置 `gmail_oauth_mode` 的 master_email。")
            return None

        user_part, domain_part = self.master_email.split("@", 1)

        if getattr(cfg, "GMAIL_OAUTH_FISSION_ENABLE", False):
            fission_mode = getattr(cfg, "GMAIL_OAUTH_FISSION_MODE", "suffix")

            for _ in range(20):
                if fission_mode == "dot":
                    new_user = self.generate_dot_variants(user_part)
                    target_email = f"{new_user}@{domain_part}"
                else:
                    random_suffix = self.generate_suffix_v2(user_part=user_part)
                    target_email = f"{user_part}+{random_suffix}@{domain_part}" if random_suffix else self.master_email

                if not db_manager.check_account_exists(target_email.lower()):
                    return {
                        "id": "gmail_fission",
                        "email": target_email,
                        "master_email": self.master_email,
                        "is_raw_trial": False,
                        "client_id": "",
                        "refresh_token": "",
                        "assigned_at": time.time()
                    }

            print(f"[{cfg.ts()}] [WARNING] 拦截：Gmail 裂变生成的别名重复多次，无可用账号！")
            return None

        if db_manager.check_account_exists(self.master_email.lower()):
            print(f"[{cfg.ts()}] [WARNING] 拦截：Gmail 主号 {self.master_email} 实际已存在账号库！无号可用。")
            return None

        return {
            "id": "gmail_master",
            "email": self.master_email,
            "master_email": self.master_email,
            "is_raw_trial": True,
            "client_id": "",
            "refresh_token": "",
            "assigned_at": time.time()
        }

    def fetch_openai_messages(self, mailbox: dict) -> List[Dict[str, Any]]:
        socket.setdefaulttimeout(15)
        config_dir = os.path.dirname(cfg.CONFIG_PATH)
        token_path = os.path.join(config_dir, "token.json")
        if not os.path.exists(token_path): return []

        proxy_url = self.proxies.get('https') or self.proxies.get('http') if self.proxies else None
        GmailOAuthHandler._set_proxy(proxy_url)

        try:
            creds = Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/gmail.modify'])
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, 'w') as f: f.write(creds.to_json())

            custom_http = None
            if proxy_url and proxy_url.startswith("socks5"):
                parsed = urllib.parse.urlparse(proxy_url)
                proxy_info = httplib2.ProxyInfo(proxy_type=socks.PROXY_TYPE_SOCKS5, proxy_host=parsed.hostname,
                                                proxy_port=parsed.port)
                custom_http = httplib2.Http(proxy_info=proxy_info)

            if custom_http:
                service = build('gmail', 'v1', credentials=creds, http=custom_http, static_discovery=False)
            else:
                service = build('gmail', 'v1', credentials=creds, static_discovery=False)

            query = "label:unread newer_than:1d in:anywhere"
            results = service.users().messages().list(
                userId='me',
                q=query,
                includeSpamTrash=True
            ).execute(num_retries=2)

            messages = results.get('messages', [])
            if not messages: return []

            formatted_messages = []
            for m_info in messages:
                msg = service.users().messages().get(userId='me', id=m_info['id'], format='full').execute()
                headers = msg.get('payload', {}).get('headers', [])
                delivered_to = next((h['value'] for h in headers if h['name'].lower() == 'delivered-to'), "").lower()
                to_addr = next((h['value'] for h in headers if h['name'].lower() == 'to'), "").lower()
                real_recipient = delivered_to if delivered_to else to_addr

                def get_body(parts):
                    for p in parts:
                        if p.get('mimeType') == 'text/plain': return p.get('body', {}).get('data', '')
                        if 'parts' in p:
                            res = get_body(p['parts'])
                            if res: return res
                    return ""

                payload = msg.get('payload', {})
                body_raw = get_body(payload.get('parts', [])) if 'parts' in payload else payload.get('body', {}).get(
                    'data', '')

                formatted_messages.append({
                    "id": m_info['id'],
                    "toRecipients": [{"emailAddress": {"address": real_recipient}}],
                    "body": {
                        "content": base64.urlsafe_b64decode(body_raw).decode('utf-8', 'ignore') if body_raw else ""}
                })

                service.users().messages().batchModify(userId='me', body={'ids': [m_info['id']],
                                                                          'removeLabelIds': ['UNREAD']}).execute()

            return formatted_messages

        except Exception as e:
            print(f"[{cfg.ts()}] [GmailFission] 扫信逻辑异常: {e}")
            return []
        finally:
            GmailOAuthHandler._clear_proxy()