import os
import json
import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from datetime import datetime

import sys
print(">>> Using openai from:", __import__("openai").__file__)

# ==== 設定 ====
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL")
SHARED_TOKEN   = os.getenv("SHARED_TOKEN")
SERVER_API_KEY = os.getenv("SERVER_API_KEY")

# ==== アプリ ====
app = FastAPI()

# ==== 一時メモリ（multi-turn用） ====
pending_tasks = {}  # {user_id: GasPayload}


# ==== モデル定義 ====
class GasPayload(BaseModel):
    token: str
    intent: str
    sheet: str
    body: dict


# ==== OpenAI クライアント ====
def get_openai_client():
    from openai import OpenAI
    import httpx
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")

    transport = httpx.HTTPTransport(retries=3)
    http_client = httpx.Client(transport=transport, follow_redirects=True)

    return OpenAI(api_key=api_key, http_client=http_client)


# ==== ヘルスチェック ====
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


# ==== LLMからJSON生成 ====
def nl_to_gas_payload(user_text: str) -> GasPayload:
    client = get_openai_client()
    today = datetime.now().strftime("%Y/%m/%d")

    schema = {
        "name": "GasPayload",
        "schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "sheet": {"type": "string"},
                "body": {
                    "type": "object",
                    "properties": {
                        "固有ID": {"type": "string"},
                        "追加日": {"type": "string"},
                        "担当": {"type": "string"},
                        "内容": {"type": "string"},
                        "期限": {"type": "string"}
                    },
                    "required": ["固有ID", "追加日", "担当", "内容", "期限"]
                }
            },
            "required": ["intent", "sheet", "body"]
        }
    }

    system = (
        "あなたは自然文をGoogleスプレッドシート task-list への書き込み用JSONに変換するアシスタントです。"
        "必ず intent, sheet, body の3要素を返します。"
        "sheet は常に 'task-list'。"
        "body には以下のキーを含めてください：固有ID, 追加日, 担当, 内容, 期限。"
        f"固有IDは空文字列。追加日は必ず {today}。"
        "担当は文章から名前を抽出。なければ空文字にしてください。"
        "期限は文章から日付を抽出。なければ空文字にしてください。"
        "質問文（例：誰が担当ですか？、期限はいつですか？）は出力に含めないこと。"
        "内容はユーザー指示を簡潔に要約してください。"
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
        raise HTTPException(status_code=502, detail="LLM出力のJSON変換に失敗しました")

    # 追加日はサーバーで上書き
    if "body" in data:
        data["body"]["追加日"] = today

    return GasPayload(
        token="recora-secret-0324",
        intent=data.get("intent", "create_task"),
        sheet=data.get("sheet", "task-list"),
        body=data.get("body", {}),
    )


# ==== 検証関数 ====
def validate_task_fields(gas_payload: GasPayload):
    body = gas_payload.body or {}
    担当 = (body.get("担当") or "").strip()
    期限 = (body.get("期限") or "").strip()

    if not 担当:
        return {"ok": False, "needs_user": True, "missing": "担当", "message": "誰のタスクとして登録しますか？（例：山田、佐藤など）"}
    if not 期限:
        return {"ok": False, "needs_user": True, "missing": "期限", "message": "期限はいつにしますか？（例：12/05など）"}

    return {"ok": True}


# ==== タスク登録 ====
@app.post("/ingest")
def ingest(payload: dict, x_api_key: str = Header(None), x_user_id: str = Header("default-user")):
    if (SERVER_API_KEY or "") != (x_api_key or ""):
        raise HTTPException(status_code=401, detail="Unauthorized")

    user_text = (payload.get("user_text") or "").strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="user_text required")

    # ---- 既に未確定タスクがある場合（multi-turn対応） ----
    if x_user_id in pending_tasks:
        prev = pending_tasks[x_user_id]
        body = prev.body
        # どちらが欠けているか確認して補完
        if not body.get("担当"):
            body["担当"] = user_text.strip()
        elif not body.get("期限"):
            body["期限"] = user_text.strip()

        # 再検証
        validation = validate_task_fields(prev)
        if validation["ok"]:
            # ✅ すべて揃ったので登録実行
            del pending_tasks[x_user_id]
            return send_to_gas(prev)
        else:
            # まだ足りない
            return validation

    # ---- 通常処理 ----
    gas_payload = nl_to_gas_payload(user_text)
    validation = validate_task_fields(gas_payload)
    if not validation["ok"]:
        # 未確定ならpendingに保存
        pending_tasks[x_user_id] = gas_payload
        return validation

    return send_to_gas(gas_payload)


# ==== GAS送信 ====
def send_to_gas(gas_payload: GasPayload):
    if not GAS_WEBAPP_URL:
        raise HTTPException(status_code=500, detail="GAS_WEBAPP_URL is_
