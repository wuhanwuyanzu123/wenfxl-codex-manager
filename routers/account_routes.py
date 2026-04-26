import json
import urllib.parse
from typing import List, Any, Optional
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from curl_cffi import requests as cffi_requests
from global_state import verify_token
from utils import core_engine, db_manager
import utils.config as cfg
from utils.integrations.sub2api_client import Sub2APIClient, build_sub2api_export_bundle, get_sub2api_push_settings

router = APIRouter()

class ExportReq(BaseModel): emails: list[str]
class DeleteReq(BaseModel): emails: list[str]
class CloudAccountItem(BaseModel): id: str; type: str
class CloudActionReq(BaseModel): accounts: List[CloudAccountItem]; action: str
class ImportMailboxReq(BaseModel): raw_text: str
class DeleteMailboxReq(BaseModel): ids: list[Any]
class OutlookAuthUrlReq(BaseModel): client_id: str
class OutlookExchangeReq(BaseModel): email: str; client_id: str; code_or_url: str
class UpdateMailboxStatusReq(BaseModel): emails: list[str]; status: int

def parse_cpa_usage_to_details(raw_usage: dict) -> dict:
    details = {"is_cpa": True}
    try:
        payload = raw_usage
        if "body" in raw_usage and isinstance(raw_usage["body"], str):
            try:
                payload = json.loads(raw_usage["body"])
            except:
                pass
        details["cpa_plan_type"] = str(payload.get("plan_type", "未知")).upper()
        total = payload.get("total_granted") or payload.get("hard_limit_usd") or payload.get("total")
        used = payload.get("total_used") or payload.get("total_usage") or payload.get("used")
        if total is not None and used is not None:
            total_val = float(total)
            used_val = float(used)
            details["cpa_total"] = f"${total_val:.2f}"
            details["cpa_remaining"] = f"${max(0.0, total_val - used_val):.2f}"
        else:
            details["cpa_total"] = "100%"
            details["cpa_remaining"] = "未知"

        rate_limit = payload.get("rate_limit", {})
        if isinstance(rate_limit, dict):
            primary = rate_limit.get("primary_window", {})
            if primary:
                p_remain = primary.get("remaining_percent")
                if p_remain is None and primary.get("used_percent") is not None:
                    p_remain = 100.0 - float(primary.get("used_percent"))
                details["cpa_primary_remain_pct"] = round(float(p_remain if p_remain is not None else 100.0), 1)

        code_review = payload.get("code_review_rate_limit", {})
        if isinstance(code_review, dict):
            c_primary = code_review.get("primary_window", {})
            if c_primary:
                c_remain = c_primary.get("remaining_percent")
                if c_remain is None and c_primary.get("used_percent") is not None:
                    c_remain = 100.0 - float(c_primary.get("used_percent"))
                details["cpa_codex_remain_pct"] = round(float(c_remain if c_remain is not None else 100.0), 1)

        details["cpa_used_percent"] = round(100.0 - details.get("cpa_primary_remain_pct", 100.0), 1)
        return details
    except Exception as e:
        print(f"[DEBUG] 解析CPA用量异常: {e}")
    details["cpa_total"] = "0.00";
    details["cpa_remaining"] = "0.00";
    details["cpa_used_percent"] = 0.0;
    details["cpa_plan_type"] = "未知"
    return details


def parse_sub2api_proxy(proxy_url: str):
    """提取代理URL为Sub2API所需格式"""
    if not proxy_url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        protocol = parsed.scheme
        host = parsed.hostname
        port = parsed.port
        username = parsed.username or ""
        password = parsed.password or ""

        if not protocol or not host or not port:
            return None

        proxy_key = f"{protocol}|{host}|{port}|{username}|{password}"
        proxy_dict = {
            "proxy_key": proxy_key,
            "name": "openai-cpa",
            "protocol": protocol,
            "host": host,
            "port": port,
            "status": "active"
        }
        if username and password:
            proxy_dict["username"] = username
            proxy_dict["password"] = password

        return proxy_dict
    except:
        return None

@router.get("/api/accounts")
async def get_accounts(page: int = Query(1), page_size: int = Query(50), hide_reg: str = Query("0"), search: Optional[str] = Query(None), token: str = Depends(verify_token)):
    result = db_manager.get_accounts_page(page, page_size, hide_reg=hide_reg, search=search)
    return {"status": "success", "data": result["data"], "total": result["total"], "page": page, "page_size": page_size}


@router.post("/api/accounts/export_selected")
async def export_selected_accounts(req: ExportReq, token: str = Depends(verify_token)):
    if not req.emails: return {"status": "error", "message": "未收到任何要导出的账号"}
    tokens = db_manager.get_tokens_by_emails(req.emails)
    return {"status": "success", "data": tokens} if tokens else {"status": "error", "message": "未能提取到选中账号的有效 Token"}


@router.post("/api/accounts/delete")
async def delete_selected_accounts(req: DeleteReq, token: str = Depends(verify_token)):
    if not req.emails: return {"status": "error", "message": "未收到任何要删除的账号"}
    return {"status": "success", "message": f"成功删除 {len(req.emails)} 个账号"} if db_manager.delete_accounts_by_emails(
        req.emails) else {"status": "error", "message": "删除操作失败"}


@router.post("/api/account/action")
def account_action(data: dict, token: str = Depends(verify_token)):
    try:
        email, action = data.get("email"), data.get("action")
        config = getattr(core_engine.cfg, '_c', {})
        token_data = db_manager.get_token_by_email(email)
        if not token_data: return {"status": "error", "message": f"未找到 {email} 的 Token。"}

        if action == "push":
            if not config.get("cpa_mode", {}).get("enable", False): return {"status": "error",
                                                                            "message": "🚫 推送失败：未开启 CPA 模式！"}
            success, msg = core_engine.upload_to_cpa_integrated(token_data,
                                                                config.get("cpa_mode", {}).get("api_url", ""),
                                                                config.get("cpa_mode", {}).get("api_token", ""))
            return {"status": "success", "message": f"账号 {email} 已成功推送到 CPA！"} if success else {"status": "error",
                                                                                                "message": f"CPA 推送失败: {msg}"}

        elif action == "push_sub2api":
            if not getattr(core_engine.cfg, 'ENABLE_SUB2API_MODE', False): return {"status": "error",
                                                                                   "message": "🚫 推送失败：未开启 Sub2API 模式！"}
            client = Sub2APIClient(api_url=getattr(core_engine.cfg, 'SUB2API_URL', ''),
                                   api_key=getattr(core_engine.cfg, 'SUB2API_KEY', ''))
            success, resp = client.add_account(token_data)
            return {"status": "success", "message": f"账号 {email} 已同步至 Sub2API！"} if success else {"status": "error",
                                                                                                  "message": f"Sub2API 推送失败: {resp}"}
    except Exception as e:
        return {"status": "error", "message": f"后端推送异常: {str(e)}"}


@router.post("/api/accounts/export_sub2api")
async def export_sub2api_accounts(req: ExportReq, token: str = Depends(verify_token)):
    from datetime import datetime, timezone
    try:
        tokens = db_manager.get_tokens_by_emails(req.emails)
        if not tokens: return {"status": "error", "message": "未提取到Token"}

        bundle = build_sub2api_export_bundle(tokens, get_sub2api_push_settings(), rotate_missing_proxy=True)
        return {"status": "success", "data": bundle}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/accounts/export_all")
async def export_all_accounts(token: str = Depends(verify_token)):
    data = db_manager.get_all_accounts_raw()
    return {"status": "success", "data": data}

@router.post("/api/accounts/clear_all")
async def clear_all_accounts_api(token: str = Depends(verify_token)):
    if db_manager.clear_all_accounts():
        return {"status": "success", "message": "账号库已全部清空"}
    return {"status": "error", "message": "清空失败"}


@router.get("/api/cloud/accounts")
def get_cloud_accounts(types: str = "sub2api,cpa", status_filter: str = Query("all"), page: int = Query(1), page_size: int = Query(50), search: Optional[str] = Query(None),
                       token: str = Depends(verify_token)):
    type_list = types.split(",")
    combined_data = []
    try:
        if "sub2api" in type_list and getattr(cfg, 'SUB2API_URL', None) and getattr(cfg, 'SUB2API_KEY', None):
            client = Sub2APIClient(api_url=cfg.SUB2API_URL, api_key=cfg.SUB2API_KEY)
            success, raw_sub2_data = client.get_all_accounts()
            if success:
                for item in raw_sub2_data:
                    raw_time = item.get("updated_at", "-")
                    if raw_time != "-":
                        try:
                            raw_time = raw_time.split(".")[0].replace("T", " ")
                        except:
                            pass
                    extra = item.get("extra", {})
                    combined_data.append({
                        "id": str(item.get("id", "")), "account_type": "sub2api",
                        "credential": item.get("name", "未知账号"),
                        "status": "disabled" if item.get("status") == "inactive" else (
                            "active" if item.get("status") == "active" else "dead"),
                        "last_check": raw_time,
                        "details": {"plan_type": item.get("credentials", {}).get("plan_type", "未知"),
                                    "codex_5h_used_percent": extra.get("codex_5h_used_percent", 0),
                                    "codex_7d_used_percent": extra.get("codex_7d_used_percent", 0)}
                    })

        if "cpa" in type_list and getattr(cfg, 'CPA_API_URL', None) and getattr(cfg, 'CPA_API_TOKEN', None):
            from curl_cffi import requests
            res = requests.get(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                               headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"}, timeout=20,
                               impersonate="chrome110")
            if res.status_code == 200:
                for item in [f for f in res.json().get("files", []) if
                             "codex" in str(f.get("type", "")).lower() or "codex" in str(
                                     f.get("provider", "")).lower()]:
                    combined_data.append({"id": item.get("name", ""), "account_type": "cpa",
                                          "credential": item.get("name", "").replace(".json", ""),
                                          "status": "disabled" if item.get("disabled", False) else "active",
                                          "details": {}, "last_check": "-"})
        if status_filter != "all":
            combined_data = [item for item in combined_data if item.get("status") == status_filter]

        if search:
            search_lower = search.lower()
            combined_data = [
                item for item in combined_data
                if search_lower in str(item.get("credential", "")).lower() or
                   search_lower in str(item.get("id", "")).lower()
            ]
        total_count = len(combined_data)
        start_idx = (page - 1) * page_size
        end_idx = page * page_size
        paged_data = combined_data[start_idx:end_idx]
        return {
            "status": "success",
            "data": paged_data,
            "total": total_count
        }
    except Exception as e:
        return {"status": "error", "message": f"拉取云端库存数据失败，请检查网络或者URL和KEY是否填写正确"}


@router.post("/api/cloud/action")
def process_cloud_action(req: CloudActionReq, token: str = Depends(verify_token)):
    from curl_cffi import requests
    from concurrent.futures import ThreadPoolExecutor

    success_count, fail_count, updated_details_map = 0, 0, {}
    sub2api_client = Sub2APIClient(api_url=cfg.SUB2API_URL, api_key=cfg.SUB2API_KEY) if getattr(cfg, 'SUB2API_URL',
                                                                                                None) and getattr(cfg,
                                                                                                                  'SUB2API_KEY',
                                                                                                                  None) else None

    cpa_files_map = {}
    if any(a.type == "cpa" for a in req.accounts) and req.action == "check" and getattr(cfg, 'CPA_API_URL', None):
        try:
            res = requests.get(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                               headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"}, timeout=15,
                               impersonate="chrome110")
            if res.status_code == 200: cpa_files_map = {f.get("name"): f for f in res.json().get("files", [])}
        except:
            pass

    def _worker(acc: CloudAccountItem):
        is_success, details = False, None
        try:
            if acc.type == "sub2api" and sub2api_client:
                if req.action == "check":
                    result, _ = sub2api_client.test_account(acc.id)
                    is_success = (result == "ok")
                    if not is_success: sub2api_client.set_account_status(acc.id, disabled=True)
                elif req.action in ["enable", "disable"]:
                    is_success = sub2api_client.set_account_status(acc.id, disabled=(req.action == "disable"))
                elif req.action == "delete":
                    is_success, _ = sub2api_client.delete_account(acc.id)

            elif acc.type == "cpa" and getattr(cfg, 'CPA_API_URL', None):
                if req.action == "check":
                    item = cpa_files_map.get(acc.id, {"name": acc.id, "disabled": False})
                    is_success, _ = core_engine.test_cliproxy_auth_file(item, cfg.CPA_API_URL, cfg.CPA_API_TOKEN)
                    if '_raw_usage' in item: details = parse_cpa_usage_to_details(item['_raw_usage'])
                    if not is_success: core_engine.set_cpa_auth_file_status(cfg.CPA_API_URL, cfg.CPA_API_TOKEN, acc.id,
                                                                            disabled=True)
                elif req.action in ["enable", "disable"]:
                    is_success = core_engine.set_cpa_auth_file_status(cfg.CPA_API_URL, cfg.CPA_API_TOKEN, acc.id,
                                                                      disabled=(req.action == "disable"))
                elif req.action == "delete":
                    is_success = requests.delete(core_engine._normalize_cpa_auth_files_url(cfg.CPA_API_URL),
                                                 headers={"Authorization": f"Bearer {cfg.CPA_API_TOKEN}"},
                                                 params={"name": acc.id}, impersonate="chrome110").status_code in (
                                 200, 204)
        except:
            pass
        return (is_success, acc.id, details)

    target_threads = 5
    if any(a.type == "cpa" for a in req.accounts): target_threads = max(target_threads, int(
        getattr(cfg, '_c', {}).get('cpa_mode', {}).get('threads', 10)))
    if any(a.type == "sub2api" for a in req.accounts): target_threads = max(target_threads, int(
        getattr(cfg, '_c', {}).get('sub2api_mode', {}).get('threads', 10)))

    with ThreadPoolExecutor(max_workers=max(1, min(target_threads, 50))) as executor:
        for is_success, acc_id, details in executor.map(_worker, req.accounts):
            if is_success:
                success_count += 1
            else:
                fail_count += 1
            if details: updated_details_map[acc_id] = details

    msg = f"测活完毕 | 存活: {success_count} 个 | 失效并已自动禁用: {fail_count} 个" if req.action == "check" else f"指令已下发 | 成功: {success_count} 个 | 失败: {fail_count} 个"

    return {"status": "success" if fail_count == 0 else "warning", "message": msg,
            "updated_details": updated_details_map}


@router.get("/api/mailboxes")
async def get_mailboxes(page: int = Query(1), page_size: int = Query(50), search: Optional[str] = Query(None), token: str = Depends(verify_token)):
    result = db_manager.get_local_mailboxes_page(page, page_size, search=search)
    return {"status": "success", "data": result["data"], "total": result["total"], "page": page, "page_size": page_size}


@router.post("/api/mailboxes/import")
async def import_mailboxes(req: ImportMailboxReq, token: str = Depends(verify_token)):
    if not req.raw_text: return {"status": "error", "message": "内容为空"}

    parsed_mailboxes = []
    lines = req.raw_text.strip().split("\n")
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"): continue
        parts = [p.strip() for p in text.split("----")]
        if len(parts) >= 2 and "@" in parts[0]:
            parsed_mailboxes.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2] if len(parts) >= 3 else "",
                "refresh_token": parts[3] if len(parts) >= 4 else ""
            })

    if not parsed_mailboxes: return {"status": "error", "message": "未能识别出有效数据"}
    count = db_manager.import_local_mailboxes(parsed_mailboxes)
    return {"status": "success", "count": count}


@router.post("/api/mailboxes/delete")
async def delete_mailboxes(req: DeleteMailboxReq, token: str = Depends(verify_token)):
    if not req.ids: return {"status": "error", "message": "未收到任何要删除的ID"}
    if db_manager.delete_local_mailboxes(req.ids):
        return {"status": "success", "message": "删除成功"}
    return {"status": "error", "message": "删除操作失败"}


@router.post("/api/mailboxes/oauth_url")
async def get_outlook_oauth_url(req: OutlookAuthUrlReq, token: str = Depends(verify_token)):
    if not req.client_id:
        return {"status": "error", "message": "缺少 Client ID"}

    redirect_uri = "http://localhost"
    scope_str = urllib.parse.quote("offline_access https://graph.microsoft.com/Mail.Read")
    auth_url = (
        f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        f"?client_id={req.client_id}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&response_mode=query"
        f"&scope={scope_str}"
    )
    return {"status": "success", "url": auth_url}


@router.post("/api/mailboxes/oauth_exchange")
async def exchange_outlook_oauth_code(req: OutlookExchangeReq, token: str = Depends(verify_token)):
    try:
        auth_code = req.code_or_url.strip()
        if "http" in auth_code or "code=" in auth_code:
            parsed_url = urllib.parse.urlparse(auth_code)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            extracted = query_params.get("code", [None])[0]
            if extracted:
                auth_code = extracted
            else:
                return {"status": "error", "message": "无法从网址中提取 code 参数，请确保复制了完整的网址。"}

        token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        payload = {
            "client_id": req.client_id,
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": "http://localhost"
        }

        proxy_url = getattr(cfg, 'DEFAULT_PROXY', None)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        response = cffi_requests.post(token_url, data=payload, proxies=proxies, timeout=15, impersonate="chrome110")
        data = response.json()

        if response.status_code == 200:
            refresh_token = data.get("refresh_token")
            import sqlite3
            from utils.db_manager import get_db_conn, get_cursor, execute_sql
            try:
                with get_db_conn() as conn:
                    c = get_cursor(conn)
                    execute_sql(c,
                                "UPDATE local_mailboxes SET client_id = ?, refresh_token = ?, status = 0 WHERE email = ?",
                                (req.client_id, refresh_token, req.email)
                                )
            except Exception as e:
                print(f"[ERROR] 数据库更新 OAuth Token 失败: {e}")

            return {"status": "success", "message": f"授权成功！已为 {req.email} 绑定永久 Token。", "refresh_token": refresh_token}
        else:
            return {"status": "error", "message": f"获取失败: {data.get('error_description', data)}"}

    except Exception as e:
        return {"status": "error", "message": f"处理异常: {str(e)}"}


@router.post("/api/mailboxes/update_status")
async def update_mailboxes_status(req: UpdateMailboxStatusReq, token: str = Depends(verify_token)):
    if not req.emails:
        return {"status": "error", "message": "未收到任何邮箱"}

    success_count = 0
    for email in req.emails:
        try:
            db_manager.update_local_mailbox_status(email, req.status)
            db_manager.clear_retry_master_status(email)
            success_count += 1
        except Exception as e:
            pass

    return {"status": "success", "message": f"成功将 {success_count} 个邮箱状态重置！"}

@router.post("/api/mailboxes/export_all")
async def export_all_mailboxes(token: str = Depends(verify_token)):
    data = db_manager.get_all_mailboxes_raw()
    return {"status": "success", "data": data}

@router.post("/api/mailboxes/clear_all")
async def clear_all_mailboxes_api(token: str = Depends(verify_token)):
    if db_manager.clear_all_mailboxes():
        return {"status": "success", "message": "邮箱库已全部清空"}
    return {"status": "error", "message": "清空失败"}