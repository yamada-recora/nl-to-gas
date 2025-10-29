# main.py
import os
import json
import uuid
import requests
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from openai import OpenAI

# === 環境変数 ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL")
SHARED_TOKEN    = os.getenv("SHARED_TOKEN")     # GAS 側検証用
SERVER_API_KEY  = os.getenv("SERVER_API_KEY")   # サーバ側APIキー

if not all([OPENAI_API_KEY, GAS_WEBAPP_URL, SHARED_TOKEN, SERVER_API_KEY]):
    # 起動時チェック（不足があれば 500 を返すより前に気づける）
    missing = [k for k, v in {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "GAS_WEBAPP_URL": GAS_WEBAPP_URL,
        "SHARED_TOKEN": SHARED_TOKEN,
        "SERVER_API_KEY": SERVER_API_KEY,
    }.items() if not v]
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI(title="NL→GAS Bridge", version="1.0.0")


# === GAS へ送る最終ペイロード ===
class GasPayload(BaseModel):
    token: str
    intent: str
    sheet: str
    body: Dict[str, Any]


# === /ingest の入力スキーマ（Actions用と同等） ===
class IngestData(BaseModel):
    text: str = Field(..., description="自然言語の指示文")
    user_id: Optional[str] = Field(None, description="呼び出しユーザーID（任意）")

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("text must be non-empty")
        return v.strip()


class IngestRequest(BaseModel):
    idempotency_key: str = Field(..., description="重複実行防止キー（UUID推奨）")
    event: str = Field(..., description="イベント種別。nl_command を想定")
    data: IngestData
    timestamp: Optional[str] = Field(
        None, description="ISO8601 (UTC) 推奨。未指定時はサーバ側で現在時刻を補完"
    )

    @field_validator("event")
    @classmethod
    def event_must_be_nl_command(cls, v: str) -> str:
        if v != "nl_command":
            raise ValueError("event must be 'nl_command'")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def idem_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("idempotency_key must be non-empty")
        return v.strip()


# シンプルな冪等性メモリ（本番はDB/Redis推奨）
IDEMPOTENCY_CACHE: set[str] = set()


@app.get("/")
def health():
    return {"ok": True, "message": "FastAPI is running!"}


def nl_to_gas_payload(user_text: str, user_id: Optional[str]) -> GasPayload:
    """
    ChatGPT(Responses API)で自然文を {intent, sheet, body} に正規化。
    sheet が無ければ 'orders' にフォールバック。
    """
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
            {"role": "user", "content": user_text},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )

    # SDKにより名称が異なる場合があるため両対応
    output_text = getattr(resp, "output_text", None) or getattr(resp, "content", None)
    if isinstance(output_text, list):
        # content が list のとき最初の text を拾う想定（SDK差異吸収）
        for part in output_text:
            if getattr(part, "type", None) == "output_text" and getattr(part, "text", None):
                output_text = part.text
                break

    if not isinstance(output_text, str):
        raise HTTPException(status_code=502, detail="LLM response parsing failed")

    data = json.loads(output_text)

    # body に user_id を付与（下流で使いたいケースに備える）
    body = data.get("body", {}) or {}
    if user_id:
        body.setdefault("_meta", {})["_caller_user_id"] = user_id

    return GasPayload(
        token=SHARED_TOKEN,
        intent=data["intent"],
        sheet=data["sheet"] or "orders",
        body=body,
    )


@app.post("/ingest")
def ingest(
    payload: IngestRequest,
    x_server_api_key: str = Header(None, alias="X-Server-Api-Key"),
    x_shared_token: str = Header(None, alias="X-Shared-Token"),
):
    # === 認証 ===
    if x_server_api_key != SERVER_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid X-Server-Api-Key")
    if x_shared_token != SHARED_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid X-Shared-Token")

    # === 冪等性チェック ===
    idem = payload.idempotency_key
    if idem in IDEMPOTENCY_CACHE:
        # 二重送信を抑制（必要に応じ 200/409 を選択）
        return {
            "status": "ok",
            "ingest_id": idem,
            "message": "Duplicate suppressed (idempotent)",
            "received": {"idempotency_key": idem, "event": payload.event},
        }
    IDEMPOTENCY_CACHE.add(idem)

    # === timestamp 補完 ===
    ts = payload.timestamp or datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # === NL → GAS 変換 ===
    gas_payload = nl_to_gas_payload(payload.data.text, payload.data.user_id)

    # === GAS WebApp へ転送 ===
    try:
        r = requests.post(
            GAS_WEBAPP_URL,
            headers={"Content-Type": "application/json"},
            json=gas_payload.model_dump(),
            timeout=30,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GAS request failed: {e}")

    # GAS応答の要約（全文はログのみにするのが安全）
    result = {
        "status": "ok" if r.ok else "error",
        "ingest_id": idem,
        "message": "Queued" if r.ok else f"GAS error {r.status_code}",
        "received": {
            "idempotency_key": idem,
            "event": payload.event,
            "timestamp": ts,
        },
        "gas_response": {
            "status_code": r.status_code,
            "text_head": (r.text or "")[:300],
        },
    }

    if not r.ok:
        # GAS 側で非200なら 502 にマップ（上位がリトライ判断可）
        raise HTTPException(status_code=502, detail=result)

    return result
