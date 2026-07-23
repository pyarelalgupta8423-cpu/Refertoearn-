from pymongo import MongoClient, ReturnDocument
from datetime import datetime, timedelta
from config import MONGO_URI, DEFAULT_POINTS, COLLECTIONS
import logging
from bson import ObjectId

logger = logging.getLogger(__name__)

client = MongoClient(MONGO_URI)
db = client["referral_bot"]


def get_collection(name):
    return db[COLLECTIONS[name]]


def init_db():
    settings = get_collection("settings")

    if not settings.find_one({"type": "points"}):
        settings.insert_one({
            "type": "points",
            "data": DEFAULT_POINTS,
            "updated_at": datetime.now()
        })

    if not settings.find_one({"type": "bot_stats"}):
        settings.insert_one({
            "type": "bot_stats",
            "total_users": 0,
            "total_groups": 0,
            "updated_at": datetime.now()
        })

    if not settings.find_one({"type": "verification"}):
        settings.insert_one({
            "type": "verification",
            "version": 1,
            "updated_at": datetime.now()
        })

    # Core indexes
    get_collection("users").create_index("user_id", unique=True)
    get_collection("groups").create_index("chat_id", unique=True)
    get_collection("tasks").create_index("name")
    get_collection("withdraw_requests").create_index([("user_id", 1), ("status", 1)])
    get_collection("withdraw_requests").create_index("serial_no", unique=True)
    get_collection("counters").create_index("_id")
    get_collection("ui_screens").create_index("screen_id", unique=True)
    get_collection("ui_buttons").create_index([("screen_id", 1), ("order", 1)])

    # Unified services & claims
    services = get_collection("services")
    services.create_index("name", unique=True)
    services.create_index("type")
    services.create_index("is_active")

    claims = get_collection("claims")
    claims.create_index("token", unique=True)
    claims.create_index("expires_at", expireAfterSeconds=0)  # TTL auto-delete

    logger.info("Database initialized with all indexes")


def get_next_sequence(name):
    counter = get_collection("counters").find_one_and_update(
        {"_id": name},
        {"$inc": {"sequence": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return counter["sequence"]


def get_verification_version():
    config = get_collection("settings").find_one({"type": "verification"})
    return config.get("version", 1) if config else 1


def increment_verification_version():
    get_collection("settings").update_one(
        {"type": "verification"},
        {"$inc": {"version": 1}, "$set": {"updated_at": datetime.now()}},
        upsert=True
    )


def get_user(user_id):
    users = get_collection("users")
    user = users.find_one({"user_id": user_id})
    if not user:
        user = {
            "user_id": user_id,
            "username": "",
            "full_name": "",
            "points": 0,
            "refer_code": str(user_id),
            "referred_by": None,
            "pending_referrer": None,
            "referral_rewarded": False,
            "referred_by_level2": None,
            "referrals": [],
            "level2_referrals": [],
            "completed_tasks": [],
            "task_attempts": {},
            "verification": {},
            "force_join_completed": False,
            "external_tasks_completed": False,
            "verification_version": 0,
            "join_date": datetime.now(),
            "is_banned": False
        }
        try:
            users.insert_one(user)
            get_collection("settings").update_one(
                {"type": "bot_stats"},
                {"$inc": {"total_users": 1}, "$set": {"updated_at": datetime.now()}}
            )
        except Exception:
            user = users.find_one({"user_id": user_id})
            if not user:
                raise
    return user


def get_points_config():
    config = get_collection("settings").find_one({"type": "points"})
    return config["data"] if config else DEFAULT_POINTS


def update_points_config(new_config):
    get_collection("settings").update_one(
        {"type": "points"},
        {"$set": {"data": new_config, "updated_at": datetime.now()}},
        upsert=True
    )


def get_task_by_id(task_id):
    try:
        return get_collection("tasks").find_one({"_id": ObjectId(task_id)})
    except:
        return None


# ========== UNIFIED SERVICES (shared with second bot) ==========
def get_all_services(only_accounts=True):
    """Get active services. For withdrawal, we only need account type."""
    query = {"is_active": True}
    if only_accounts:
        query["type"] = "account"
    return list(get_collection("services").find(query).sort("name", 1))


def get_service(service_id):
    try:
        return get_collection("services").find_one({"_id": ObjectId(service_id)})
    except:
        return get_collection("services").find_one({"_id": service_id})


def get_service_by_name(name):
    return get_collection("services").find_one({"name": name, "is_active": True})


def create_service(name, price, points, platform="telegram", type="account", description=""):
    doc = {
        "name": name,
        "type": type,
        "platform": platform if type == "account" else None,
        "price": price,          # INR for second bot
        "points": points,        # points cost in this bot
        "description": description,
        "is_active": True,
        "total_items": 0,
        "available_items": 0,
        "created_at": datetime.now(),
        "updated_at": datetime.now()
    }
    result = get_collection("services").insert_one(doc)
    return doc


def update_service(service_id, data):
    data["updated_at"] = datetime.now()
    try:
        return get_collection("services").update_one({"_id": ObjectId(service_id)}, {"$set": data}).modified_count > 0
    except:
        return get_collection("services").update_one({"_id": service_id}, {"$set": data}).modified_count > 0


def delete_service(service_id):
    try:
        return get_collection("services").update_one({"_id": ObjectId(service_id)}, {"$set": {"is_active": False, "updated_at": datetime.now()}}).modified_count > 0
    except:
        return get_collection("services").update_one({"_id": service_id}, {"$set": {"is_active": False, "updated_at": datetime.now()}}).modified_count > 0


def get_service_available_count(service_id):
    """Count available accounts in second bot's accounts collection."""
    try:
        accounts_col = db["accounts"]  # from second bot
        return accounts_col.count_documents({"service_id": service_id, "status": "available"})
    except:
        return 0


# ========== CLAIMS (for second bot) ==========
def create_claim(user_id, service_id, expires_hours=24):
    import secrets
    token = secrets.token_urlsafe(16)
    expires_at = datetime.now() + timedelta(hours=expires_hours)
    claim = {
        "token": token,
        "user_id": user_id,
        "service_id": ObjectId(service_id) if isinstance(service_id, str) else service_id,
        "used": False,
        "created_at": datetime.now(),
        "expires_at": expires_at,
        "used_at": None
    }
    get_collection("claims").insert_one(claim)
    return token
