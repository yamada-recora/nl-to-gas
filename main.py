# main.py
import os
import json
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from openai import OpenAI
from openai import __version__ as openai_version

# ---- 環境変数 ----
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL")
SHARED_TOKEN   = os.getenv("SHARED_TOKEN")
SERVER_API_KEY = os.getenv("SERVER_API_KEY")

app = FastAPI()

# ---- 型定義 ----
class GasPayload(BaseModel):
    token: str
    intent: str
    sheet: str
    body: dict

# ---- OpenAIクライアントを必要時に生成（遅延生成）----
def get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        # 起動は落とさず、呼ばれた時に分かるようにする
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")
    # proxies など未対応引数は渡さない
    return OpenAI(api_key=api_key)

# ---- ヘルスチェック（不足env確認 & openaiバージョン確認用）----
@app.get("/")
def health():
    missing = [
        k for k in ["OPENAI_API_KEY", "GAS_WEBAPP_URL", "SHARED_TOKEN", "SERVER_API_KEY"]
        if not os.getenv(k)
    ]
    return {
        "ok": True,
        "message": "FastAPI is running!",
        "openai_version": openai_version,
        "missing_env": missing
    }

# ---- 自然文 → GAS向けPayload 変換 ----
def nl_to_gas_payload(user_text: str) -> GasPayload:
    client = get_openai_client()

    schema = {
        "name": "GasPayload",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {"type": "string"},
                "sheet": {"type": "string"},
                "body": {"type": "object"}
            },
            "required": ["intent", "sheet", "body"]
        },
        "strict": True
    }

    system = (
        "あなたは自然文をGoogleスプレッドシートへの書き込み用JSONに変換するアシスタントです。"
        "sheetが無ければ 'orders' とします。"
        "数値は数値、日付は YYYY-MM-DD を推奨。"
        "出力は {intent, sheet, body} のみ。"
    )

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    try:
        data = json.loads(resp.output_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="LLM output JSON parse error")

    return GasPayload(
        token=SHARED_TOKEN or "",
        intent=data.get("intent", "generic_post"),
        sheet=data.get("sheet", "orders"),
        body=data.get("body", {}),
    )

# ---- 入口：ChatGPT Action から叩くエンドポイント ----
@app.post("/ingest")
def ingest(payload: dict, x_api_key: str = Header(None)):
    # 認証（ChatGPT→このサーバー）
    if (SERVER_API_KEY or "") != (x_api_key or ""):
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_text = (payload.get("user_text") or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    # LLMで構造化
    gas_payload = nl_to_gas_payload(user_text)

    # GASのURLが未設定なら 500
    if not GAS_WEBAPP_URL:
        raise HTTPException(status_code=500, detail="GAS_WEBAPP_URL is not set")

    # GASへ転送
    try:
        r = requests.post(
            GAS_WEBAPP_URL,
            headers={"Content-Type": "application/json"},
            json=gas_payload.model_dump(),
            timeout=20,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GAS request failed: {e}")

    return {"ok": r.ok, "status": r.status_code, "text": r.text[:1000]}
