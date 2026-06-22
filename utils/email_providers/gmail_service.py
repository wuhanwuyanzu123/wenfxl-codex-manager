import os
import re
import time
import json
from utils.email_providers.gmail_oauth_handler import GmailOAuthHandler
from utils import config as cfg


def get_gmail_otp_via_oauth(target_email, proxy=None):
    from utils import db_manager
    creds_json = db_manager.get_sys_kv('gmail_credentials_json')
    token_json = db_manager.get_sys_kv('gmail_token_json')

    if not creds_json or not token_json:
        print(f"[{cfg.ts()}] [Gmail] 数据库中缺少凭据或Token，无法提取验证码")
        return None

    handler = GmailOAuthHandler()
    service, updated_token = handler.get_service(json.loads(creds_json), token_json, proxy=proxy)

    if not service:
        return None

    if updated_token:
        db_manager.set_sys_kv('gmail_token_json', updated_token)

    emails = handler.fetch_and_mark_read(service, target_email, search_query="is:unread")
    if not emails:
        return None

    for mail in emails:
        body = mail.get('body', '')
        subject = mail.get('subject', '')

        new_format = re.findall(r"enter this code:\s*(\d{6})", body, re.I)
        if not new_format:
            new_format = re.findall(r"verification code to continue:\s*(\d{6})", body, re.I)

        if new_format:
            return new_format[-1]

        direct = re.findall(r"Your ChatGPT code is (\d{6})", body, re.I)
        if direct:
            return direct[-1]

        if "ChatGPT" in subject or "OpenAI" in subject or "ChatGPT" in body:
            generic = re.findall(r"\b(\d{6})\b", body)
            if generic:
                return generic[-1]

    return None