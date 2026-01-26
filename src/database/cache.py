import json
import logging
import os
import threading
from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


class VideoCache(Base):
    """SQLite-based video cache table."""
    __tablename__ = "video_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(64), unique=True, nullable=False, index=True)
    file_id = Column(Text, nullable=False)  # JSON string of file IDs
    meta = Column(Text, nullable=False)  # JSON string of metadata
    created_at = Column(DateTime, default=datetime.utcnow)


# Create engine and session factory
_engine = None
_SessionFactory = None
_lock = threading.Lock()


def _get_session():
    """Get or create the SQLite session factory."""
    global _engine, _SessionFactory
    
    if _SessionFactory is None:
        with _lock:
            if _SessionFactory is None:
                db_dsn = os.getenv("DB_DSN", "sqlite:///database.sqlite3")
                try:
                    _engine = create_engine(db_dsn)
                    Base.metadata.create_all(_engine)
                    _SessionFactory = sessionmaker(bind=_engine)
                except Exception as e:
                    logging.error("Failed to initialize database: %s", e)
                    raise
                    
    return _SessionFactory()


class Redis:
    """SQLite-based cache that mimics the Redis interface."""
    
    def __init__(self):
        # Ensure table exists
        _get_session().close()
        logging.info("Using SQLite-based video cache")
    
    def __del__(self):
        pass
    
    def add_cache(self, key: str, mapping: dict):
        """Add or update a cache entry."""
        session = _get_session()
        try:
            existing = session.query(VideoCache).filter(VideoCache.cache_key == key).first()
            if existing:
                existing.file_id = mapping.get("file_id", "[]")
                existing.meta = mapping.get("meta", "{}")
                existing.created_at = datetime.utcnow()
            else:
                cache_entry = VideoCache(
                    cache_key=key,
                    file_id=mapping.get("file_id", "[]"),
                    meta=mapping.get("meta", "{}")
                )
                session.add(cache_entry)
            session.commit()
            logging.info("Cache saved for key: %s", key[:16])
        except Exception as e:
            session.rollback()
            logging.error("Failed to save cache: %s", e)
        finally:
            session.close()
    
    def get_cache(self, key: str) -> dict:
        """Get a cache entry by key."""
        session = _get_session()
        try:
            entry = session.query(VideoCache).filter(VideoCache.cache_key == key).first()
            if entry:
                logging.info("Cache hit for key: %s", key[:16])
                return {
                    "file_id": entry.file_id,
                    "meta": entry.meta
                }
            return {}
        except Exception as e:
            logging.error("Failed to get cache: %s", e)
            return {}
        finally:
            session.close()
    
    def delete_cache(self, key: str) -> bool:
        """Delete a cache entry by key."""
        session = _get_session()
        try:
            deleted = session.query(VideoCache).filter(VideoCache.cache_key == key).delete()
            session.commit()
            if deleted:
                logging.info("Cache deleted for key: %s", key[:16])
            return deleted > 0
        except Exception as e:
            session.rollback()
            logging.error("Failed to delete cache: %s", e)
            return False
        finally:
            session.close()
