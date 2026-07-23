import asyncio, logging, os, re
from datetime import timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from database import get_collection
logger=logging.getLogger(__name__)
REPORT_RE=re.compile(r"You invited\s+(\d{5,}\*+)\s+to join Argo",re.IGNORECASE)
async def run_argo_listener():
    api_id=int(os.getenv("TELEGRAM_API_ID","0") or 0); api_hash=os.getenv("TELEGRAM_API_HASH",""); session=os.getenv("TELEGRAM_SESSION",""); username=os.getenv("ARGO_BOT_USERNAME","argox").lstrip("@")
    if not api_id or not api_hash or not session: logger.warning("Argo listener disabled: env missing"); return
    client=TelegramClient(StringSession(session),api_id,api_hash); await client.connect()
    if not await client.is_user_authorized(): logger.error("Argo session unauthorized"); await client.disconnect(); return
    argo=await client.get_entity(username); reports=get_collection("external_reports")
    reports.create_index([("source_bot_id",1),("telegram_message_id",1)],unique=True); reports.create_index([("used",1),("message_date",1)])
    @client.on(events.NewMessage(from_users=argo.id))
    async def handler(event):
        m=REPORT_RE.search(event.raw_text or "")
        if not m: return
        dt=event.message.date
        if dt.tzinfo is not None: dt=dt.astimezone(timezone.utc).replace(tzinfo=None)
        reports.update_one({"source_bot_id":argo.id,"telegram_message_id":event.message.id},{"$setOnInsert":{"source_bot_id":argo.id,"telegram_message_id":event.message.id,"masked_user_id":m.group(1),"message_date":dt,"used":False}},upsert=True)
    try: await client.run_until_disconnected()
    finally: await client.disconnect()
def start_argo_listener_thread(): asyncio.run(run_argo_listener())
