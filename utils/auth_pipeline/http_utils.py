import os
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

from curl_cffi import requests
from utils import config as cfg


def _ssl_verify() -> bool:
    flag = os.getenv("OPENAI_SSL_VERIFY", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _skip_net_check() -> bool:
    flag = os.getenv("SKIP_NET_CHECK", "0").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _post_form(
        url: str,
        data: Dict[str, str],
        proxies: Any = None,
        timeout: int = 30,
        retries: int = 3,
) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url, data=data, headers=headers,
                proxies=proxies, verify=_ssl_verify(),
                timeout=timeout, impersonate="chrome110",
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"token exchange failed: {resp.status_code}: {resp.text}"
                )
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                print(f"\n[{cfg.ts()}] [WARNING] 换取 Token 时遇到网络异常: {exc}。"
                      f"准备第 {attempt + 1}/{retries} 次重试...")
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"token exchange failed after {retries} retries: {last_error}"
    ) from last_error


def _post_with_retry(
        session: requests.Session,
        url: str,
        *,
        headers: Dict[str, Any],
        data: Any = None,
        json_body: Any = None,
        proxies: Any = None,
        timeout: int = 30,
        retries: int = 2,
        allow_redirects: bool = True,
) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        if getattr(cfg, 'GLOBAL_STOP', False): raise RuntimeError("系统已停止，强制中断网络请求")
        try:
            if json_body is not None:
                return session.post(
                    url, headers=headers, json=json_body,
                    proxies=proxies, verify=_ssl_verify(),
                    timeout=timeout, allow_redirects=allow_redirects,
                )
            return session.post(
                url, headers=headers, data=data,
                proxies=proxies, verify=_ssl_verify(),
                timeout=timeout, allow_redirects=allow_redirects,
            )
        except Exception as e:
            last_error = e
            if attempt >= retries:
                break
            time.sleep(2 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Request failed without exception")


def _oai_headers(did: str, extra: dict = None) -> dict:
    h = {
        "accept": "application/json",
        "accept-language": "ko-KR,ko;q=0.9",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/110.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "oai-device-id": did,
    }
    if extra:
        h.update(extra)
    return h


def _follow_redirect_chain_local(
        session: requests.Session,
        start_url: str,
        proxies: Any = None,
        max_redirects: int = 12,
) -> Tuple[Any, str]:
    current_url = start_url
    response = None
    for _ in range(max_redirects):
        try:
            response = session.get(
                current_url,
                allow_redirects=False,
                proxies=proxies,
                verify=_ssl_verify(),
                timeout=15,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, current_url
            loc = response.headers.get("Location", "")
            if not loc:
                return response, current_url
            current_url = urllib.parse.urljoin(current_url, loc)
            if "code=" in current_url and "state=" in current_url:
                return None, current_url
        except Exception:
            return None, current_url
    return response, current_url