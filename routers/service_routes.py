import os
import asyncio
import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from cloudflare import Cloudflare
from curl_cffi import requests as cffi_requests

from global_state import verify_token
from utils import core_engine
import utils.config as cfg
import utils.integrations.clash_manager as clash_manager
from utils.email_providers.gmail_oauth_handler import GmailOAuthHandler

router = APIRouter()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GMAIL_CLIENT_SECRETS = os.path.join(BASE_DIR, "data", "credentials.json")
GMAIL_TOKEN_PATH = os.path.join(BASE_DIR, "data", "token.json")
GMAIL_VERIFIER_PATH = os.path.join(BASE_DIR, "data", "temp_verifier.txt")

class CFSyncExistingReq(BaseModel): sub_domains: str; api_email: str; api_key: str
class LuckMailBulkBuyReq(BaseModel): quantity: int; auto_tag: bool; config: dict
class GmailExchangeReq(BaseModel): code: str
class ClashDeployReq(BaseModel): count: int
class ClashUpdateReq(BaseModel): sub_url: str; target: str = "all"


@router.post("/api/config/add_wildcard_dns")
async def add_wildcard_dns(req: CFSyncExistingReq, token: str = Depends(verify_token)):
    try:
        main_list = [d.strip() for d in req.sub_domains.split(",") if d.strip()]
        if not main_list: return {"status": "error", "message": "❌ 没有找到有效的主域名"}

        proxy_url = getattr(core_engine.cfg, 'DEFAULT_PROXY', None)
        headers = {"X-Auth-Email": req.api_email, "X-Auth-Key": req.api_key, "Content-Type": "application/json"}
        client_kwargs = {"timeout": 30.0}
        if proxy_url: client_kwargs["proxy"] = proxy_url

        semaphore = asyncio.Semaphore(2)

        async def process_single_domain(client, domain):
            async with semaphore:
                try:
                    zone_resp = await client.get(f"https://api.cloudflare.com/client/v4/zones?name={domain}",
                                                 headers=headers)
                    zone_data = zone_resp.json()
                    if not zone_data.get("success") or not zone_data.get("result"): return False
                    zone_id = zone_data["result"][0]["id"]

                    records = [
                        {"type": "MX", "name": "*", "content": "route3.mx.cloudflare.net", "priority": 36},
                        {"type": "MX", "name": "*", "content": "route2.mx.cloudflare.net", "priority": 25},
                        {"type": "MX", "name": "*", "content": "route1.mx.cloudflare.net", "priority": 51},
                        {"type": "TXT", "name": "*", "content": '"v=spf1 include:_spf.mx.cloudflare.net ~all"'}
                    ]
                    for rec in records:
                        rec_resp = await client.post(
                            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records", headers=headers,
                            json=rec)
                        rec_data = rec_resp.json()
                        if not rec_data.get("success"):
                            errors = rec_data.get("errors", [])
                            is_quota_exceeded = any(err.get("code") == 81045 for err in errors)
                            is_exist = any(err.get("code") in {81057, 81058} for err in errors)

                            if is_quota_exceeded:
                                print(f"[{core_engine.ts()}] [CF] [{domain}] 记录配额已超出，无法继续创建，请手动去cf官网清除记录后在推送。")
                                continue
                            elif is_exist:
                                print(f"[{core_engine.ts()}] [CF] [{domain}] 记录已存在无需重复创建。")
                                continue

                            print(f"[{core_engine.ts()}] [ERROR] [{domain}] 记录创建报错: {errors}")
                        print(f"[{core_engine.ts()}] [SUCCESS] [{domain}] 创建成功")
                        await asyncio.sleep(0.5)
                    print(f"[{core_engine.ts()}] [CF] ✅ [{domain}] 解析处理成功，防止遗漏，请等待日志输出完毕后，重新点击推送！")
                    return True
                except:
                    return False
                finally:
                    await asyncio.sleep(0.5)

        async with httpx.AsyncClient(**client_kwargs) as client:
            tasks = [process_single_domain(client, dom) for dom in main_list]
            results = await asyncio.gather(*tasks)

        success_count = sum(1 for r in results if r)
        return {"status": "success", "message": f"成功处理 {success_count}/{len(main_list)} 个域名。"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/config/cf_global_status")
def get_cf_global_status(main_domain: str, token: str = Depends(verify_token)):
    try:
        cf_cfg = getattr(core_engine.cfg, '_c', {})
        api_email, api_key = cf_cfg.get("cf_api_email"), cf_cfg.get("cf_api_key")
        if not api_email or not api_key: return {"status": "error", "message": "未配置 CF 账号信息"}

        cf = Cloudflare(api_email=api_email, api_key=api_key)
        domains = [d.strip() for d in main_domain.split(",") if d.strip()]
        results = []

        for dom in domains:
            zones = cf.zones.list(name=dom)
            if not zones.result:
                results.append({"domain": dom, "is_enabled": False, "dns_status": "not_found"})
                continue
            zone_id = zones.result[0].id
            routing_info = cf.email_routing.get(zone_id=zone_id)

            def safe_get(obj, attr, default=None):
                val = getattr(obj, attr, None)
                if val is None and hasattr(obj, 'result'): val = getattr(obj.result, attr, None)
                return val if val is not None else default

            raw_status, raw_synced = safe_get(routing_info, 'status', 'unknown'), safe_get(routing_info, 'synced',
                                                                                           False)
            results.append({"domain": dom, "is_enabled": (raw_status == 'ready' and raw_synced is True),
                            "dns_status": "active" if raw_synced else "pending"})

        return {"status": "success", "data": results}
    except Exception as e:
        return {"status": "error", "message": f"状态同步失败: {str(e)}"}


@router.post("/api/luckmail/bulk_buy")
def api_luckmail_bulk_buy(req: LuckMailBulkBuyReq, token: str = Depends(verify_token)):
    try:
        from utils.email_providers.luckmail_service import LuckMailService
        lm_service = LuckMailService(api_key=req.config.get("api_key"),
                                     preferred_domain=req.config.get("preferred_domain", ""),
                                     email_type=req.config.get("email_type", "ms_graph"),
                                     variant_mode=req.config.get("variant_mode", ""))
        tag_id = req.config.get("tag_id") or lm_service.get_or_create_tag_id("已使用")
        results = lm_service.bulk_purchase(quantity=req.quantity, auto_tag=req.auto_tag, tag_id=tag_id)
        return {"status": "success", "message": f"成功购买 {len(results)} 个邮箱！", "data": results}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/gmail/auth_url")
async def get_gmail_auth_url(token: str = Depends(verify_token)):
    if not os.path.exists(GMAIL_CLIENT_SECRETS): return {"status": "error",
                                                         "message": f"❌ 未找到凭证文件！请上传至: {GMAIL_CLIENT_SECRETS}"}
    try:
        url, verifier = GmailOAuthHandler.get_authorization_url(GMAIL_CLIENT_SECRETS)
        with open(GMAIL_VERIFIER_PATH, "w") as f:
            f.write(verifier)
        return {"status": "success", "url": url}
    except Exception as e:
        return {"status": "error", "message": f"生成链接失败: {str(e)}"}


@router.post("/api/gmail/exchange_code")
async def exchange_gmail_code(req: GmailExchangeReq, token: str = Depends(verify_token)):
    if not req.code: return {"status": "error", "message": "授权码不能为空"}
    try:
        if not os.path.exists(GMAIL_VERIFIER_PATH): return {"status": "error", "message": "会话已过期，请重新生成链接"}
        with open(GMAIL_VERIFIER_PATH, "r") as f:
            stored_verifier = f.read().strip()
        success, msg = GmailOAuthHandler.save_token_from_code(GMAIL_CLIENT_SECRETS, req.code, GMAIL_TOKEN_PATH,
                                                              code_verifier=stored_verifier,
                                                              proxy=getattr(core_engine.cfg, 'DEFAULT_PROXY', None))
        if success and os.path.exists(GMAIL_VERIFIER_PATH):
            os.remove(GMAIL_VERIFIER_PATH)
            return {"status": "success", "message": "✨ 授权成功！token.json 已保存在 data 目录。"}
        return {"status": "error", "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/sub2api/groups")
def get_sub2api_groups(token: str = Depends(verify_token)):
    from curl_cffi import requests as cffi_requests
    sub2api_url = getattr(core_engine.cfg, "SUB2API_URL", "").strip()
    sub2api_key = getattr(core_engine.cfg, "SUB2API_KEY", "").strip()
    if not sub2api_url or not sub2api_key: return {"status": "error",
                                                   "message": "Please save the Sub2API URL and API key first."}
    try:
        response = cffi_requests.get(f"{sub2api_url.rstrip('/')}/api/v1/admin/groups/all",
                                     headers={"x-api-key": sub2api_key, "Content-Type": "application/json"}, timeout=10,
                                     impersonate="chrome110")
        if response.status_code != 200: return {"status": "error",
                                                "message": f"HTTP {response.status_code}: {response.text[:200]}"}
        return {"status": "success", "data": response.json().get("data", [])}
    except Exception as exc:
        return {"status": "error", "message": f"Failed to fetch Sub2API groups: {exc}"}

@router.get("/api/clash/status")
async def get_clash_status(token: str = Depends(verify_token)):
    res = clash_manager.get_pool_status()
    if "error" in res:
        return {"status": "error", "message": res["error"]}
    return {"status": "success", "data": res}

@router.post("/api/clash/deploy")
async def post_clash_deploy(req: ClashDeployReq, token: str = Depends(verify_token)):
    success, msg = clash_manager.deploy_clash_pool(req.count)
    return {"status": "success" if success else "error", "message": msg}

@router.post("/api/clash/update")
async def post_clash_update(req: ClashUpdateReq, token: str = Depends(verify_token)):
    success, msg = clash_manager.patch_and_update(req.sub_url, req.target)
    return {"status": "success" if success else "error", "message": msg}