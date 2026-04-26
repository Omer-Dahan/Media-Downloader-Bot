import logging
import math
import os
from contextlib import contextmanager
from typing import Literal

from sqlalchemy import (
    BigInteger,
    Column,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

from config import ENABLE_VIP, FREE_DOWNLOAD, FREE_BANDWIDTH, OWNER


class PaymentStatus:
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


Base = declarative_base()


class CreditsExhaustedException(Exception):
    """Exception raised when a user has run out of download credits."""


class BandwidthExhaustedException(Exception):
    """Exception raised when a free user has reached their daily bandwidth limit."""


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, unique=True, nullable=False)  # telegram user id
    first_name = Column(String(100))  # User's first name
    username = Column(String(100))  # User's @username
    free = Column(Integer, default=FREE_DOWNLOAD)
    paid = Column(Integer, default=0)
    bandwidth_used = Column(BigInteger, default=0)  # Daily bandwidth used in bytes
    total_bandwidth = Column(BigInteger, default=0)  # All-time bandwidth used in bytes
    is_blocked = Column(Integer, default=0)  # 0 = active, 1 = blocked
    config = Column(JSON)

    settings = relationship(
        "Setting", back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    payments = relationship(
        "Payment", back_populates="user", cascade="all, delete-orphan"
    )


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    quality = Column(
        Enum("high", "medium", "low", "audio", "custom"), nullable=False, default="high"
    )
    format = Column(Enum("video", "audio", "document"), nullable=False, default="video")
    subtitles = Column(Integer, nullable=False, default=0)  # 0 = off, 1 = on
    title_length = Column(
        Integer, nullable=False, default=500
    )  # Max chars for caption title: 100, 250, 500, 1000
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user = relationship("User", back_populates="settings")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    method = Column(String(50), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(
        Enum(
            PaymentStatus.PENDING,
            PaymentStatus.COMPLETED,
            PaymentStatus.FAILED,
            PaymentStatus.REFUNDED,
        ),
        nullable=False,
    )
    transaction_id = Column(String(100))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user = relationship("User", back_populates="payments")


def create_session():
    engine = create_engine(
        os.getenv("DB_DSN"),
        pool_size=50,
        max_overflow=100,
        pool_timeout=30,
        pool_recycle=1800,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


SessionFactory = create_session()


@contextmanager
def session_manager():
    s = SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_quality_settings(tgid) -> Literal["high", "medium", "low", "audio", "custom"]:
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == tgid).first()
        if user and user.settings:
            return user.settings.quality

        return "high"


def get_format_settings(tgid) -> Literal["video", "audio", "document"]:
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == tgid).first()
        if user and user.settings:
            return user.settings.format
        return "video"


def get_subtitles_settings(tgid) -> bool:
    """Check if user has subtitles download enabled."""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == tgid).first()
        if user and user.settings:
            return bool(user.settings.subtitles)
        return False


def get_title_length_settings(tgid) -> int:
    """Get user's preferred title length for captions."""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == tgid).first()
        if user and user.settings and user.settings.title_length is not None:
            return user.settings.title_length
        return 500  # Default to 500 chars


def get_long_description_settings(tgid) -> bool:
    """Check if user wants full description in separate message."""
    # Column removed to avoid DB issues, using title_length instead
    return False


def set_user_settings(tgid: int, key: str, value):
    # set quality, format, or subtitles settings
    with session_manager() as session:
        # find user first
        user = session.query(User).filter(User.user_id == tgid).first()
        # upsert
        setting = session.query(Setting).filter(Setting.user_id == user.id).first()
        if setting:
            setattr(setting, key, value)
        else:
            session.add(Setting(user_id=user.id, **{key: value}))


def get_free_quota(uid: int):
    if not ENABLE_VIP:
        return math.inf

    with session_manager() as session:
        data = session.query(User).filter(User.user_id == uid).first()
        if data:
            return data.free
        return FREE_DOWNLOAD


def get_paid_quota(uid: int):
    if ENABLE_VIP:
        with session_manager() as session:
            data = session.query(User).filter(User.user_id == uid).first()
            if data:
                return data.paid

            return 0

    return math.inf


def add_paid_quota(uid: int, amount: int):
    with session_manager() as session:
        data = session.query(User).filter(User.user_id == uid).first()
        if data:
            data.paid += amount
        else:
            # Create user if not exists
            session.add(User(user_id=uid, paid=amount))


def get_total_credits(uid: int) -> int:
    """Get total available credits (free + paid) for a user."""
    if not ENABLE_VIP:
        return math.inf

    with session_manager() as session:
        data = session.query(User).filter(User.user_id == uid).first()
        if data:
            return (data.free or 0) + (data.paid or 0)
        return FREE_DOWNLOAD


def check_quota(uid: int):
    if not ENABLE_VIP:
        return

    # Owners are exempt from quota limits
    if uid in OWNER:
        return

    with session_manager() as session:
        data = session.query(User).filter(User.user_id == uid).first()
        if data:
            # Check if user is blocked
            if data.is_blocked:
                raise Exception("המשתמש שלך נחסם. פנה למנהל.")
            # Check if credits are exhausted
            if (data.free + data.paid) <= 0:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")
            # Check bandwidth limit
            if data.paid <= 0 and data.bandwidth_used >= FREE_BANDWIDTH:
                raise BandwidthExhaustedException(
                    "הגעת למגבלת 2GB יומית למשתמשים חינמיים.\nלרכישת חבילה ללא הגבלה שלח /buy"
                )


def use_quota(uid: int):
    # use free first, then paid
    if not ENABLE_VIP:
        return

    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            if user.free > 0:
                user.free -= 1
            elif user.paid > 0:
                user.paid -= 1
            else:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")


def use_quota_dynamic(uid: int, file_sizes: list[int] | int) -> int:
    """Deduct credits based on file sizes.

    Args:
        uid: User ID
        file_sizes: List of file sizes in bytes, or single file size for backwards compatibility

    Returns:
        Remaining credits after deduction

    Deduction rules:
        - 1 credit per 200MB (rounded up)
        - Example: 400MB = 2 credits, 1GB = 5 credits
    """
    if not ENABLE_VIP:
        return math.inf

    # Handle backwards compatibility - single int becomes list
    if isinstance(file_sizes, int):
        file_sizes = [file_sizes] if file_sizes > 0 else []

    # Calculate total size in MB
    total_size_bytes = sum(file_sizes)
    total_size_mb = total_size_bytes / (1024 * 1024)

    # 1 credit per 200MB, minimum 1 credit if any files
    credits_to_deduct = max(1, math.ceil(total_size_mb / 200)) if file_sizes else 0

    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            # Deduct from free first, then paid
            for _ in range(credits_to_deduct):
                if user.free > 0:
                    user.free -= 1
                elif user.paid > 0:
                    user.paid -= 1
                else:
                    break  # No more credits to deduct

            remaining = (user.free or 0) + (user.paid or 0)
            logging.info(
                "User %s: deducted %d credits (%d files), remaining: %d",
                uid,
                credits_to_deduct,
                len(file_sizes),
                remaining,
            )
            return remaining
        return 0


def init_user(uid: int, first_name: str = None, username: str = None):
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if not user:
            session.add(User(user_id=uid, first_name=first_name, username=username))
        else:
            # Update name if provided (in case user changed their name)
            if first_name:
                user.first_name = first_name
            if username:
                user.username = username


def add_bandwidth_used(uid: int, size: int):
    """Add bandwidth usage for a user (in bytes)"""
    if not ENABLE_VIP:
        return

    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            user.bandwidth_used += size
            user.total_bandwidth = (
                user.total_bandwidth or 0
            ) + size  # All-time tracking
            logging.info(
                "User %s bandwidth usage: %s bytes (total: %s)",
                uid,
                user.bandwidth_used,
                user.total_bandwidth,
            )


def credit_account(who, total_amount: int, quota: int, transaction, method="stripe"):
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == who).first()
        if user:
            dollar = total_amount / 100
            user.paid += quota
            logging.info(
                "user %d credited with %d tokens, payment:$%.2f", who, user.paid, dollar
            )
            session.add(
                Payment(
                    method=method,
                    amount=total_amount,
                    status=PaymentStatus.COMPLETED,
                    transaction_id=transaction,
                    user_id=user.id,
                )
            )
            session.commit()
            return user.free, user.paid

        return None, None


# ============== Admin Functions ==============


def get_all_users(page: int = 0, per_page: int = 10):
    """Get paginated list of all users"""
    with session_manager() as session:
        total = session.query(User).count()
        users = session.query(User).offset(page * per_page).limit(per_page).all()
        return [
            {
                "user_id": u.user_id,
                "first_name": u.first_name,
                "username": u.username,
                "free": u.free,
                "paid": u.paid,
                "bandwidth_used": u.bandwidth_used,
                "is_blocked": u.is_blocked,
            }
            for u in users
        ], total


def get_paid_users(page: int = 0, per_page: int = 10):
    """Get paginated list of users with paid credits"""
    with session_manager() as session:
        total = session.query(User).filter(User.paid > 0).count()
        users = (
            session.query(User)
            .filter(User.paid > 0)
            .offset(page * per_page)
            .limit(per_page)
            .all()
        )
        return [
            {
                "user_id": u.user_id,
                "first_name": u.first_name,
                "username": u.username,
                "paid": u.paid,
            }
            for u in users
        ], total


def get_user_stats(uid: int):
    """Get stats for a specific user"""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            return {
                "user_id": user.user_id,
                "first_name": user.first_name,
                "username": user.username,
                "free": user.free,
                "paid": user.paid,
                "bandwidth_used": user.bandwidth_used,
                "is_blocked": user.is_blocked,
            }
        return None


def get_download_stats():
    """Get overall download statistics"""
    with session_manager() as session:
        from sqlalchemy import func

        total_users = session.query(User).count()
        paid_users = session.query(User).filter(User.paid > 0).count()
        total_free = session.query(func.sum(User.free)).scalar() or 0
        total_paid = session.query(func.sum(User.paid)).scalar() or 0
        total_bandwidth = session.query(func.sum(User.bandwidth_used)).scalar() or 0
        all_time_bandwidth = session.query(func.sum(User.total_bandwidth)).scalar() or 0

        return {
            "total_users": total_users,
            "paid_users": paid_users,
            "total_free_remaining": total_free,
            "total_paid": total_paid,
            "total_bandwidth_used": total_bandwidth,
            "all_time_bandwidth": all_time_bandwidth,
        }


def reset_user_quota(uid: int):
    """Reset quota for a specific user"""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            user.free = FREE_DOWNLOAD
            user.bandwidth_used = 0


def block_user(uid: int):
    """Block a user"""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            user.is_blocked = 1


def unblock_user(uid: int):
    """Unblock a user"""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            user.is_blocked = 0


def delete_user(uid: int):
    """Delete a user completely"""
    with session_manager() as session:
        user = session.query(User).filter(User.user_id == uid).first()
        if user:
            session.delete(user)
