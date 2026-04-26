"""
Archive Manager - Prepare files for Telegram upload.

Handles ZIP creation for multi-file torrents and large files.
"""

import logging
import zipfile
from pathlib import Path

from config import TG_NORMAL_MAX_SIZE


def needs_archive(file_path: Path, max_size: int = TG_NORMAL_MAX_SIZE) -> bool:
    """
    Check if a file or folder needs to be archived before upload.

    A folder always needs archiving (multi-file torrent).
    A file needs archiving only if it exceeds max_size.

    Args:
        file_path: Path to file or folder
        max_size: Maximum size in bytes (default: Telegram limit)

    Returns:
        True if archiving is needed
    """
    if file_path.is_dir():
        return True  # Multi-file torrents always get zipped

    if file_path.is_file():
        return file_path.stat().st_size > max_size

    return False


def get_folder_size(folder: Path) -> int:
    """Calculate total size of a folder recursively."""
    total = 0
    for item in folder.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def create_zip(source_path: Path, output_dir: Path = None) -> Path:
    """
    Create a ZIP archive from a file or folder.

    Args:
        source_path: Path to file or folder to archive
        output_dir: Directory to create ZIP in (default: same as source)

    Returns:
        Path to created ZIP file
    """
    if output_dir is None:
        output_dir = source_path.parent

    zip_name = f"{source_path.stem}.zip"
    zip_path = output_dir / zip_name

    logging.info("Creating ZIP archive: %s", zip_path)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if source_path.is_file():
                zf.write(source_path, source_path.name)
            else:
                # Folder - add all contents
                for item in source_path.rglob("*"):
                    if item.is_file():
                        arcname = item.relative_to(source_path.parent)
                        zf.write(item, arcname)

        zip_size = zip_path.stat().st_size
        logging.info(
            "Created ZIP archive: %s (%.1f MB)", zip_path, zip_size / (1024 * 1024)
        )
        return zip_path

    except Exception as e:
        logging.error("Failed to create ZIP archive: %s", e)
        # Cleanup partial archive
        if zip_path.exists():
            zip_path.unlink()
        raise


def split_file(
    file_path: Path, output_dir: Path = None, part_size: int = 1900 * 1024 * 1024
) -> list[Path]:
    """
    Split a large file into smaller parts using binary splitting.

    Args:
        file_path: Path to the large file
        output_dir: Directory to create parts in
        part_size: Maximum size per part in bytes

    Returns:
        List of paths to created parts
    """
    if output_dir is None:
        output_dir = file_path.parent

    file_size = file_path.stat().st_size

    # If small enough, return as-is
    if file_size <= part_size:
        return [file_path]

    base_name = file_path.name
    parts = []
    part_num = 1

    logging.info(
        "Splitting file %s (%d MB) into %d MB parts",
        base_name,
        file_size // (1024 * 1024),
        part_size // (1024 * 1024),
    )

    try:
        with open(file_path, "rb") as src:
            while True:
                chunk = src.read(part_size)
                if not chunk:
                    break

                # Format: filename.zip.001 (if original was filename.zip)
                part_path = output_dir / f"{base_name}.{part_num:03d}"
                with open(part_path, "wb") as part:
                    part.write(chunk)

                parts.append(part_path)
                logging.info("Created part %d: %s", part_num, part_path.name)
                part_num += 1

        return parts

    except Exception as e:
        logging.error("Failed to split file: %s", e)
        # Cleanup partial parts
        for part in parts:
            if part.exists():
                part.unlink()
        raise


def create_split_archive(
    source_path: Path, output_dir: Path = None, part_size: int = 1900 * 1024 * 1024
) -> list[Path]:
    """
    Create a split ZIP archive for very large files.

    Args:
        source_path: Path to file or folder to archive
        output_dir: Directory to create parts in
        part_size: Maximum size per part in bytes

    Returns:
        List of paths to created ZIP parts
    """
    if output_dir is None:
        output_dir = source_path.parent

    # First, create the full zip
    full_zip = create_zip(source_path, output_dir)

    try:
        # Then split it using the new helper
        parts = split_file(full_zip, output_dir, part_size)

        # If parts were created (splitting happened), remove the original full zip
        # If full_zip IS the only part (no split needed), split_file returns [full_zip], so don't delete it!
        if len(parts) > 1 and full_zip.exists():
            full_zip.unlink()

        return parts

    except Exception:
        # If splitting failed, try to cleanup full zip if it exists
        if full_zip.exists():
            full_zip.unlink()
        raise


def cleanup_archive(archive_paths: list[Path]):
    """Remove archive files after successful upload."""
    for path in archive_paths:
        try:
            if path.exists():
                path.unlink()
                logging.info("Cleaned up archive: %s", path.name)
        except Exception as e:
            logging.warning("Failed to cleanup archive %s: %s", path, e)
