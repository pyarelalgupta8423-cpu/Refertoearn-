import os
from dotenv import load_dotenv

load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")


# Admin IDs
ADMIN_IDS = [
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip()
]


# Default points configuration
DEFAULT_POINTS = {
    "refer_level_1": 50,
    "refer_level_2": 25,

    "group_add_small": 100,
    "group_add_medium": 250,
    "group_add_m2": 500,
    "group_add_m3": 750,
    "group_add_m4": 1000,
    "group_add_big": 2000,

    "min_withdraw": 500,
}


# MongoDB collection mapping
COLLECTIONS = {
    # Core
    "users": "users",
    "channels": "force_join_channels",
    "external_links": "external_links",
    "external_reports": "external_reports",
    "tasks": "tasks",
    "task_verification_requests": "task_verification_requests",
    "withdraw_requests": "withdraw_requests",
    "settings": "settings",
    "groups": "groups",
    "counters": "counters",

    # CMS
    "ui_screens": "ui_screens",
    "ui_buttons": "ui_buttons",
    # "ui_services" is DEPRECATED – now using unified "services"
    "services": "services",
    "claims": "claims",
}


# Required environment validation
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is missing")

# Second bot username (without @)
SECOND_BOT_USERNAME = os.getenv("SECOND_BOT_USERNAME", "")
