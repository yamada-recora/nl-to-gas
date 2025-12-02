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
    import httpx
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")

    transport = httpx.HTTPTransport(retries=3)
    http_client = httpx.Client(transport=transport, follow_redirects=True)

    return OpenAI(api_key=api_key, http_client=http_client)

# ---- OpenAI のバージョンは失敗しても起動を止めない ----
def get_openai_version() -> str:
    try:
        from importlib.metadata import version
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


# ------------------------------------------
# ✅ LLM 出力生成関数
# ------------------------------------------
def nl_to_gas_payload(user_text: str) -> GasPayload:
    from datetime import datetime
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
                    "additionalProperties": False,
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
        },
        "strict": True
    }

    today = datetime.now().strftime("%Y/%m/%d")

    # 🔧 改訂済み system プロンプト
    system = (
        "あなたは自然文をGoogleスプレッドシート task-list への書き込み用JSONに変換するアシスタントです。"
        "必ず intent, sheet, body の3要素を返します。"
        "sheet は常に 'task-list'。"
        "body には以下のキーを含めてください：固有ID, 追加日, 担当, 内容, 期限。"
        f"固有IDは空文字列。追加日は必ず {today}。"
        "担当は文章から人名（例：山田、佐藤）を抽出。名前がない場合は空文字にしてください。"
        "期限は文章から日付（例：12/05、2025-12-05）を抽出。なければ空文字にしてください。"
        "『誰が担当ですか？』『期限はいつですか？』のような質問文はJSONに含めないでください。"
        "内容はユーザーの指示を簡潔に要約してください。"
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

    # 日付を上書き
    if "body" in data:
        data["body"]["追加日"] = today

    return GasPayload(
        token="recora-secret-0324",
        intent=data.get("intent", "write"),
        sheet=data.get("sheet", "task-list"),
        body=data.get("body", {}),
    )


# ------------------------------------------
# ✅ LLM出力検証関数（空・質問文を検知）
# ------------------------------------------
def validate_task_fields(gas_payload: GasPayload):
    body = gas_payload.body or {}
    担当 = body.get("担当", "").strip()
    期限 = body.get("期限", "").strip()

    # 担当未確定
    if not 担当 or any(x in 担当 for x in ["誰", "担当ですか", "？", "?"]):
        return {
            "ok": False,
            "needs_user": True,
            "message": "誰のタスクを追加しますか？（例：山田、佐藤）"
        }

    # 期限未確定
    if not 期限 or any(x in 期限 for x in ["期限", "？", "?"]):
        return {
            "ok": False,
            "needs_user": True,
            "message": "期限はいつにしますか？（例：12/05など）"
        }

    return {"ok": True}


# ------------------------------------------
# ✅ タスク登録エンドポイント
# ------------------------------------------
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

    # ✅ 担当・期限の事前検証（GAS送信前）
    validation = validate_task_fields(gas_payload)
    if not validation["ok"]:
        return validation

    if not GAS_WEBAPP_URL:
        raise HTTPException(status_code=500, detail="GAS_WEBAPP_URL is not set")

    try:
        print(">>> GAS_PAYLOAD (before send) =", json.dumps(gas_payload.model_dump(), ensure_ascii=False))
        r = requests.post(
            GAS_WEBAPP_URL,
            headers={"Content-Type": "application/json"},
            json=gas_payload.model_dump(),
            timeout=20,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GAS request failed: {e}")

    return {"ok": r.ok, "status": r.status_code, "text": r.text[:1000]}


# ------------------------------------------
# ✅ タスク取得エンドポイント
# ------------------------------------------
@app.get("/tasks")
def get_tasks(user: str = None):
    if not GAS_WEBAPP_URL:
        raise HTTPException(status_code=500, detail="GAS_WEBAPP_URL is not set")

    params = {"sheet": "task-list"}
    if user:
        params["user"] = user

    try:
        r = requests.get(GAS_WEBAPP_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()

        # 担当者指定なしメッセージに対応
        if not data.get("ok") and "誰のタスクを表示しますか" in data.get("message", ""):
            return {"ok": False, "needs_user": True, "message": data["message"]}

        return data

    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GAS fetch failed: {e}")
