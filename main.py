# main.py（起動安全版）
import os
import json
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import sys
print(">>> Using openai from:", __import__("openai").__file__)

# ---- OpenAI client は遅延生成（起動時に例外回避）----
def get_openai_client():
    from openai import OpenAI
    import httpx, os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")

    # proxies=None は削除
    transport = httpx.HTTPTransport(retries=3)
    http_client = httpx.Client(transport=transport, follow_redirects=True)

    return OpenAI(api_key=api_key, http_client=http_client)

# ---- OpenAI のバージョンは失敗しても起動を止めない ----
def get_openai_version() -> str:
    try:
        from importlib.metadata import version, PackageNotFoundError
        return version("openai")
    except Exception:
        return "unknown"

GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL")
SHARED_TOKEN   = os.getenv("SHARED_TOKEN")
SERVER_API_KEY = os.getenv("SERVER_API_KEY")

app = FastAPI()

class GasPayload(BaseModel):
    token: str
    intent: str
    sheet: str
    body: dict

@app.get("/")
def health():
    import openai
    missing = [k for k in ["OPENAI_API_KEY", "GAS_WEBAPP_URL", "SHARED_TOKEN", "SERVER_API_KEY"] if not os.getenv(k)]
    return {
        "ok": True,
        "message": "FastAPI is running!",
        "openai_version": getattr(openai, "__version__", "unknown"),
        "openai_path": getattr(openai, "__file__", "unknown"),
        "missing_env": missing
    }


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
                "body": {
                    "type": "object",
                    "additionalProperties": False,  # ← これを追加！
                    "properties": {}
                }
            },
            "required": ["intent", "sheet", "body"]
        },
        "strict": True
    }

    system = (
        "あなたは自然文をGoogleスプレッドシートへの書き込み用JSONに変換するアシスタントです。"
        "sheetが無ければ 'orders'。数値は数値、日付はYYYY-MM-DD。"
        "出力は {intent, sheet, body} のみ。"
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    content = resp.choices[0].message.content
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="LLM output JSON parse error")

    return GasPayload(
        token="recora-secret-0324",
        intent=data.get("intent", "generic_post"),
        sheet=data.get("sheet", "orders"),
        body=data.get("body", {}),
    )

@app.post("/ingest")
def ingest(payload: dict, x_api_key: str = Header(None)):
    if (SERVER_API_KEY or "") != (x_api_key or ""):
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_text = (payload.get("user_text") or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    try:
        gas_payload = nl_to_gas_payload(user_text)
    except Exception as e:
        import traceback
        print(">>> ERROR in nl_to_gas_payload:", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"nl_to_gas_payload error: {e}")

    if not GAS_WEBAPP_URL:
        raise HTTPException(status_code=500, detail="GAS_WEBAPP_URL is not set")

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









