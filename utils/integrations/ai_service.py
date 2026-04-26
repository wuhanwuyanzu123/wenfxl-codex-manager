import json
from curl_cffi import requests
from utils import config as cfg

class AIService:
    def __init__(self):
        self.api_base = getattr(cfg, 'AI_API_BASE', "https://api.openai.com/v1").rstrip("/")
        self.api_key = getattr(cfg, 'AI_API_KEY', "")
        self.model = getattr(cfg, 'AI_MODEL', "gpt-5.1-codex-mini")

    def _call_ai(self, prompt: str) -> list:
        if not self.api_key: return []
        try:
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.9
                },
                timeout=30,
                impersonate="chrome110"
            )
            res = resp.json()['choices'][0]['message']['content'].strip()
            return [k.strip().lower().replace(" ", "").replace(".", "").replace("-", "") for k in res.split(",") if k.strip()]
        except:
            return []

    def fetch_names(self):
        prompt = "Generate 30 realistic English name combinations (format: firstname.lastname). CRITICAL: Each combination MUST be at least 8 characters long. Return as a comma-separated list only."
        return self._call_ai(prompt)

    def fetch_keywords(self):
        prompt = "Generate 30 trending tech and AI keywords (e.g., openai, neural, compute). Return as a comma-separated list only."
        return self._call_ai(prompt)

    def generate_realistic_profile(self):
        prompt = "请生成一个真实的欧美用户注册资料，包括：一个英文名，一个姓氏。只需要返回JSON格式：{\"first_name\": \"...\", \"last_name\": \"...\"}，不要有其他描述。"
        messages = [{"role": "user", "content": prompt}]
        res = self.chat_completion(messages)
        try:
            return json.loads(res)
        except:
            return None

    def generate_bulk_keywords(self, count=20):
        prompt = f"请生成{count}个与 OpenAI、ChatGPT、AI 技术相关的热门英文关键词。只需返回单词，逗号分隔，不要编号。"
        res = self.chat_completion([{"role": "user", "content": prompt}])
        if "Error" in res: return ["openai", "chatgpt", "api", "ai", "auth"]
        return [k.strip().lower() for k in res.split(",") if k.strip()]

    def generate_bulk_names(self, count=20):
        prompt = f"请生成{count}个真实的欧美姓名（名.姓）。只需返回结果，逗号分隔，不要编号。例如: james.smith, emily.white"
        res = self.chat_completion([{"role": "user", "content": prompt}])
        if "Error" in res: return ["james.smith"]
        return [n.strip().lower() for n in res.split(",") if n.strip()]