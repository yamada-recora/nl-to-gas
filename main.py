# main.py
import os, json, requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# 環境変数から設定を読み込み
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL")
SHARED_TOKEN   = os.getenv("SHARED_TOKEN")
SERVER_API_KEY = os.getenv("SERVER_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

class GasPayload(BaseModel):
    token: str
    intent: str
    sheet: str
    body: dict

@app.get("/")
def health():
    return {"ok": True, "message": "FastAPI is running!"}

def nl_to_gas_payload(user_text: str) -> GasPayload:
    """ChatGPT APIで自然文を構造化JSONに変換"""
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
        "出力は {intent, sheet, body} のみ。"
    )

    resp = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_text}
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    data = json.loads(resp.output_text)
    return GasPayload(token=SHARED_TOKEN, intent=data["intent"], sheet=data["sheet"], body=data["body"])

@app.post("/ingest")
def ingest(payload: dict, x_api_key: str = Header(None)):
    if x_api_key != SERVER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_text = payload.get("user_text", "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    gas_payload = nl_to_gas_payload(user_text)

    r = requests.post(GAS_WEBAPP_URL, headers={"Content-Type": "application/json"}, json=gas_payload.model_dump())
    return {"ok": r.ok, "status": r.status_code, "text": r.text[:300]}

