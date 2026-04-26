import os
import re
import time
from utils.email_providers.gmail_oauth_handler import GmailOAuthHandler
from utils import config as cfg


def get_gmail_otp_via_oauth(target_email, proxy=None):
    config_dir = os.path.dirname(cfg.CONFIG_PATH)
    client_secrets = os.path.join(config_dir, "credentials.json")
    token_path = os.path.join(config_dir, "token.json")

    handler = GmailOAuthHandler()
    service = handler.get_service(client_secrets, token_path, proxy=proxy)

    if not service:
        return None

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