import asyncio
import hmac
import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from supabase import create_client

load_dotenv()

BOT_TOKEN      = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID  = int(os.environ["ADMIN_CHAT_ID"])
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
LANDING_ORIGIN = os.environ.get("LANDING_ORIGIN", "*")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[LANDING_ORIGIN],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

try:
    db = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except Exception:
    db = None

# Fallback in-memory storage when Supabase is not configured
_memory_users: set[int] = set()


# ── Telegram helper ──────────────────────────────────────────

async def tg(method: str, **kwargs):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{TG_API}/{method}", json=kwargs)
        r.raise_for_status()
        return r.json()


# ── Approved users ───────────────────────────────────────────

def get_approved_chat_ids() -> list[int]:
    if db:
        rows = db.table("approved_users").select("chat_id").execute()
        return [row["chat_id"] for row in (rows.data or [])]
    return list(_memory_users)


def is_approved(chat_id: int) -> bool:
    if db:
        res = db.table("approved_users").select("id").eq("chat_id", chat_id).execute()
        return bool(res.data)
    return chat_id in _memory_users


# ── /webhook ─────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(secret, WEBHOOK_SECRET):
            raise HTTPException(403)

    body = await request.json()

    if "message" in body:
        msg        = body["message"]
        user       = msg.get("from", {})
        chat_id    = user.get("id")
        username   = user.get("username", "") or ""
        first_name = user.get("first_name", "") or ""

        if chat_id == ADMIN_CHAT_ID:
            # Admin always gets access; show list of approved users
            ids = get_approved_chat_ids()
            count = len(ids)
            await tg("sendMessage", chat_id=ADMIN_CHAT_ID,
                     text=f"👋 Привет, Камиль! Одобрено пользователей: {count}.\n"
                          f"Когда кто-то пишет /start, вы получите запрос на одобрение.")
            return {"ok": True}

        if is_approved(chat_id):
            await tg("sendMessage", chat_id=chat_id,
                     text="Вы уже одобрены и будете получать уведомления о новых заявках. ✅")
            return {"ok": True}

        # Send approval request to admin with inline keyboard
        username_short  = username[:15]   if username   else ""
        first_short     = first_name[:15] if first_name else ""
        callback_approve = f"approve:{chat_id}:{username_short}:{first_short}"
        callback_deny    = f"deny:{chat_id}"

        await tg(
            "sendMessage",
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"📬 <b>Новый запрос на доступ:</b>\n\n"
                f"👤 {first_name}"
                + (f" (@{username})" if username else "")
                + f"\nID: <code>{chat_id}</code>"
            ),
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [[
                    {"text": "✅ Одобрить", "callback_data": callback_approve},
                    {"text": "❌ Отклонить", "callback_data": callback_deny},
                ]]
            },
        )
        await tg("sendMessage", chat_id=chat_id,
                 text="Ваш запрос отправлен администратору. Ожидайте подтверждения.")

    elif "callback_query" in body:
        cq     = body["callback_query"]
        data   = cq["data"]
        cq_id  = cq["id"]
        msg_id = cq["message"]["message_id"]

        if data.startswith("approve:"):
            parts      = data.split(":", 3)
            target_id  = int(parts[1])
            username   = parts[2] if len(parts) > 2 else ""
            first_name = parts[3] if len(parts) > 3 else ""

            if db:
                db.table("approved_users").upsert({
                    "chat_id":    target_id,
                    "username":   username,
                    "first_name": first_name,
                }).execute()
            else:
                _memory_users.add(target_id)

            await tg("answerCallbackQuery", callback_query_id=cq_id, text="Пользователь одобрен ✅")
            await tg("editMessageReplyMarkup",
                     chat_id=ADMIN_CHAT_ID,
                     message_id=msg_id,
                     reply_markup={"inline_keyboard": []})
            await tg("sendMessage", chat_id=ADMIN_CHAT_ID,
                     text=f"✅ Пользователь {first_name or target_id} одобрен.")
            await tg("sendMessage", chat_id=target_id,
                     text="Вы одобрены! 🎉 Теперь вы будете получать уведомления о новых заявках с сайта.")

        elif data.startswith("deny:"):
            target_id = int(data.split(":", 1)[1])
            await tg("answerCallbackQuery", callback_query_id=cq_id, text="Отклонено")
            await tg("editMessageReplyMarkup",
                     chat_id=ADMIN_CHAT_ID,
                     message_id=msg_id,
                     reply_markup={"inline_keyboard": []})
            await tg("sendMessage", chat_id=target_id,
                     text="Ваш запрос отклонён администратором.")

    return {"ok": True}


# ── /submit ──────────────────────────────────────────────────

@app.post("/submit")
async def submit(request: Request):
    body    = await request.json()
    name    = str(body.get("name",    "")).strip()
    phone   = str(body.get("phone",   "")).strip()
    visa    = str(body.get("visa",    "")).strip()
    comment = str(body.get("comment", "")).strip()

    if not name or not phone or not visa:
        raise HTTPException(400, "Missing required fields")

    msk      = timezone(timedelta(hours=3))
    date_str = datetime.now(msk).strftime("%d.%m.%Y %H:%M МСК")

    text = (
        "📬 <b>Новая заявка — Visa Travel Kazan</b>\n\n"
        f"👤 <b>Имя:</b> {name}\n"
        f"📱 <b>Телефон:</b> {phone}\n"
        f"🌍 <b>Виза / страна:</b> {visa}\n"
        + (f"💬 <b>Комментарий:</b> {comment}\n" if comment else "")
        + f"\n📅 {date_str}"
    )

    chat_ids = get_approved_chat_ids()
    if not chat_ids:
        chat_ids = [ADMIN_CHAT_ID]   # fallback if no one approved yet

    results = await asyncio.gather(
        *[tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML") for cid in chat_ids],
        return_exceptions=True,
    )

    errors = [r for r in results if isinstance(r, Exception)]
    if errors and len(errors) == len(chat_ids):
        raise HTTPException(502, "Failed to deliver to Telegram")

    return {"ok": True}


# ── Health check (for UptimeRobot) ───────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Landing page ─────────────────────────────────────────────

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "index.html"))


# ── Auto-register webhook on startup ─────────────────────────

@app.on_event("startup")
async def set_webhook():
    try:
        base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        if not base:
            return
        kwargs: dict = {"url": f"{base}/webhook"}
        if WEBHOOK_SECRET:
            kwargs["secret_token"] = WEBHOOK_SECRET
        result = await tg("setWebhook", **kwargs)
        print(f"Webhook set: {result}")
    except Exception as e:
        print(f"Warning: setWebhook failed ({e}), continuing anyway")
