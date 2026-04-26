#!/usr/bin/env python3
# pylint: disable=wrong-import-position
# Script to clear video cache

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from database.cache import _get_session, VideoCache


def clear_video_cache():
    session = _get_session()
    try:
        deleted = session.query(VideoCache).delete()
        session.commit()
        print(f"Deleted {deleted} cache entries")
    except Exception as e:
        session.rollback()
        print(f"Error: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    clear_video_cache()
