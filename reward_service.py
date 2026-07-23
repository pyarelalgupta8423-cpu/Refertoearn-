"""
Centralized atomic reward operations using MongoDB transactions with callback API.
All point-credit operations are all-or-nothing with proper retry handling.
"""
from database import *
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

def credit_group_reward_atomic(chat_id, added_by_id, group_title, member_count, points):
    """
    Atomic group reward using transaction callback API.
    Guards:
    - Only rewards if reward_given=False (state transition False→True)
    - Validates user exists via matched_count
    - Uses with_transaction() for retry on transient errors
    """
    groups = get_collection("groups")
    users = get_collection("users")
    settings = get_collection("settings")
    
    def txn_callback(session):
        # Step 1: Atomic claim - only transition from False to True
        claimed = groups.find_one_and_update(
            {
                "chat_id": chat_id,
                "reward_given": False
            },
            {
                "$set": {
                    "title": group_title or "Unknown",
                    "member_count": member_count,
                    "added_by": added_by_id,
                    "reward_given": True,
                    "reward_points": points,
                    "reward_status": "completed",
                    "rewarded_at": datetime.now()
                }
            },
            session=session,
            return_document=ReturnDocument.AFTER
        )
        
        if not claimed:
            return None
        
        # Step 2: Credit points - validate user exists
        user_result = users.update_one(
            {"user_id": added_by_id},
            {"$inc": {"points": points}},
            session=session
        )
        
        if user_result.matched_count != 1:
            raise RuntimeError(
                f"User {added_by_id} not found for group reward"
            )
        
        # Step 3: Update stats
        settings.update_one(
            {"type": "bot_stats"},
            {
                "$inc": {"total_groups": 1},
                "$set": {"updated_at": datetime.now()}
            },
            session=session
        )
        
        return {
            "chat_id": chat_id,
            "user_id": added_by_id,
            "points": points,
            "member_count": member_count
        }
    
    try:
        with client.start_session() as session:
            return session.with_transaction(txn_callback)
    except Exception as e:
        logger.error(f"Group reward transaction failed for {chat_id}: {e}")
        return None


def create_group_pending_reward(chat_id, added_by_id, group_title, member_count, points):
    """
    Create pending group reward for unverified users.
    FIXED: Never overwrites an already-rewarded group.
    """
    groups = get_collection("groups")
    
    try:
        # First check if already rewarded
        existing = groups.find_one({"chat_id": chat_id})
        
        if existing and existing.get("reward_given"):
            logger.info(f"Group {chat_id} already rewarded, skipping pending creation")
            return False
        
        # Atomic upsert that preserves reward_given if document exists
        result = groups.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "title": group_title or "Unknown",
                    "member_count": member_count,
                    "added_by": added_by_id,
                    "reward_points": points,
                    "reward_status": "pending_verification"
                },
                "$setOnInsert": {
                    "chat_id": chat_id,
                    "reward_given": False,
                    "added_at": datetime.now()
                }
            },
            upsert=True
        )
        
        return True
        
    except Exception as e:
        if "duplicate key" in str(e).lower() or "E11000" in str(e):
            logger.info(f"Group {chat_id} already registered (concurrent)")
            return False
        logger.error(f"Failed to create pending reward for {chat_id}: {e}")
        return False


def process_pending_group_rewards_atomic(user_id):
    """
    Process ALL pending group rewards for a user atomically.
    Each reward is a separate transaction with callback API.
    """
    groups = get_collection("groups")
    users = get_collection("users")
    processed_rewards = []
    
    pending_groups = list(groups.find({
        "added_by": user_id,
        "reward_given": False,
        "reward_status": "pending_verification"
    }))
    
    for group in pending_groups:
        group_id = group["_id"]
        reward_points = group["reward_points"]
        
        def txn_callback(session):
            claimed = groups.find_one_and_update(
                {
                    "_id": group_id,
                    "reward_given": False,
                    "reward_status": "pending_verification"
                },
                {
                    "$set": {
                        "reward_given": True,
                        "reward_status": "completed",
                        "rewarded_at": datetime.now()
                    }
                },
                session=session,
                return_document=ReturnDocument.AFTER
            )
            
            if not claimed:
                return None
            
            user_result = users.update_one(
                {"user_id": user_id},
                {"$inc": {"points": reward_points}},
                session=session
            )
            
            if user_result.matched_count != 1:
                raise RuntimeError(f"User {user_id} not found for pending reward")
            
            return {
                "chat_id": group["chat_id"],
                "title": group.get("title", "Unknown"),
                "points": reward_points
            }
        
        try:
            with client.start_session() as session:
                result = session.with_transaction(txn_callback)
                if result:
                    processed_rewards.append(result)
        except Exception as e:
            logger.error(f"Pending reward transaction failed for group {group_id}: {e}")
            continue
    
    return processed_rewards


def credit_referral_atomic(user_id, referrer_id):
    """
    Atomic referral reward using transaction callback API.
    Guards:
    - Marks user as referred atomically
    - Validates referrer exists before crediting
    - Credits both levels in same transaction
    - Uses with_transaction() for retry handling
    """
    users = get_collection("users")
    points_config = get_points_config()
    
    def txn_callback(session):
        # Step 1: Atomically mark user as referred
        user = users.find_one_and_update(
            {
                "user_id": user_id,
                "referral_rewarded": {"$ne": True}
            },
            {
                "$set": {
                    "referred_by": referrer_id,
                    "referral_rewarded": True,
                    "pending_referrer": None
                }
            },
            session=session,
            return_document=ReturnDocument.BEFORE
        )
        
        if not user:
            return None
        
        # Step 2: Validate referrer exists
        referrer = users.find_one(
            {"user_id": referrer_id},
            session=session
        )
        
        if not referrer:
            raise ValueError(
                f"Referrer {referrer_id} does not exist for user {user_id}"
            )
        
        # Step 3: Credit Level 1
        level1_result = users.update_one(
            {"user_id": referrer_id},
            {
                "$inc": {"points": points_config["refer_level_1"]},
                "$addToSet": {"referrals": user_id}
            },
            session=session
        )
        
        if level1_result.matched_count != 1:
            raise RuntimeError(
                f"Level 1 reward update failed for referrer {referrer_id}"
            )
        
        # Step 4: Check Level 2 (skip if not found, don't abort)
        level2_id = referrer.get("referred_by")
        level2_points = 0
        
        if level2_id and level2_id != user_id:
            level2_user = users.find_one(
                {"user_id": level2_id},
                session=session
            )
            
            if level2_user:
                users.update_one(
                    {"user_id": level2_id},
                    {
                        "$inc": {"points": points_config["refer_level_2"]},
                        "$addToSet": {"level2_referrals": user_id}
                    },
                    session=session
                )
                
                users.update_one(
                    {"user_id": user_id},
                    {"$set": {"referred_by_level2": level2_id}},
                    session=session
                )
                
                level2_points = points_config["refer_level_2"]
            else:
                level2_id = None
                logger.warning(
                    f"Level 2 referrer {level2_id} not found for user {user_id}"
                )
        
        return {
            "user_id": user_id,
            "referrer_id": referrer_id,
            "level1_points": points_config["refer_level_1"],
            "level2_id": level2_id,
            "level2_points": level2_points
        }
    
    try:
        with client.start_session() as session:
            return session.with_transaction(txn_callback)
    except Exception as e:
        logger.error(f"Referral transaction failed for user {user_id}: {e}")
        return None


def create_withdrawal_atomic(
    user_id,
    withdraw_amount,
    username="",
    full_name="",
    service_id="",
    service_name="",
    service_emoji="💳",
    required_referrals=0,
    user_input="N/A"
):
    users = get_collection("users")
    counters = get_collection("counters")
    withdraw_requests = get_collection("withdraw_requests")

    def txn_callback(session):
        user = users.find_one_and_update(
            {
                "user_id": user_id,
                "points": {"$gte": withdraw_amount}
            },
            {
                "$inc": {
                    "points": -withdraw_amount
                }
            },
            session=session,
            return_document=ReturnDocument.BEFORE
        )

        if not user:
            return None

        counter = counters.find_one_and_update(
            {
                "_id": "withdraw_serial"
            },
            {
                "$inc": {
                    "sequence": 1
                }
            },
            upsert=True,
            session=session,
            return_document=ReturnDocument.AFTER
        )

        serial_no = counter["sequence"]

        withdraw_requests.insert_one(
            {
                "serial_no": serial_no,
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "points": withdraw_amount,

                "service_id": service_id,
                "service_name": service_name,
                "service_emoji": service_emoji,
                "required_referrals": required_referrals,
                "user_input": user_input,

                "status": "pending",
                "request_date": datetime.now(),
                "processed_date": None,
                "processed_by": None,
                "refund_completed": False
            },
            session=session
        )

        return {
            "serial_no": serial_no,
            "previous_balance": user["points"],
            "withdraw_amount": withdraw_amount,
            "new_balance": user["points"] - withdraw_amount
        }

    try:
        with client.start_session() as session:
            return session.with_transaction(txn_callback)

    except Exception as e:
        logger.error(
            f"Withdrawal transaction failed for user {user_id}: {e}"
        )
        return None
