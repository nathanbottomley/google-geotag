# Photo/video ingest from an SD card to the working SSD (or Mac fallback),
# with optional chain into the geotag script.
#
# Command:
#   python ingest.py
#
# Options:
#   --no-confirm   Skip the proceed prompt.
#   --no-geotag    Skip geotag even if a Timeline export covers the new photos.
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

BOLD = "\033[1m"
FAINT = "\033[2m"
GREEN = "\033[32m"
BLUE = "\033[34m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

PHOTO_EXTS = {".arw", ".jpg", ".jpeg"}
VIDEO_EXTS = {".mp4", ".mov", ".mts", ".m4v"}

SSD_ROOT = Path("/Volumes/N8 SSD")
SSD_PHOTOS = SSD_ROOT / "Photos" / "Sony"
MAC_FALLBACK = Path.home() / "Pictures" / "Sony Inbox" / "Sony"

DOWNLOADS = Path.home() / "Downloads"
TIMELINE_GLOB = "location-history*.json"

REPO_ROOT = Path(__file__).resolve().parent
GEOTAG_SCRIPT = REPO_ROOT / "google-geotag.py"

COPY_CHUNK = 1 << 20


class MediaFile:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        stat = path.stat()
        self.size = stat.st_size
        # SD-card mtimes are written by the camera as wallclock seconds-since-epoch.
        # We only use this for grouping by year/month and showing a date range,
        # so reading it as UTC is "close enough" without needing exiftool here.
        self.mtime = datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def is_photo(self) -> bool:
        return self.ext in PHOTO_EXTS

    @property
    def is_video(self) -> bool:
        return self.ext in VIDEO_EXTS

    @property
    def year(self) -> int:
        return self.mtime.year

    @property
    def month(self) -> int:
        return self.mtime.month


def find_sd_card() -> Optional[Path]:
    volumes = Path("/Volumes")
    if not volumes.exists():
        print(f"{RED}{BOLD}Error:{RESET} /Volumes/ does not exist.")
        return None
    candidates = [v for v in volumes.iterdir() if (v / "DCIM").is_dir()]
    if not candidates:
        print(
            f"{RED}{BOLD}Error:{RESET} No SD card with a DCIM folder found in /Volumes/."
        )
        return None
    if len(candidates) > 1:
        print(f"{RED}{BOLD}Error:{RESET} Multiple SD cards with DCIM folders found:")
        for c in candidates:
            print(f"  {c}")
        print("Eject the ones you don't want to ingest from and try again.")
        return None
    return candidates[0]


def _scan_tree(root: Path, accept) -> List["MediaFile"]:
    files = []
    if not root.is_dir():
        return files
    for p in root.rglob("*"):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            m = MediaFile(p)
        except OSError:
            continue
        if accept(m):
            files.append(m)
    return files


def scan_sd_card(sd: Path) -> List[MediaFile]:
    # Sony stores photos in DCIM/ and videos in PRIVATE/M4ROOT/CLIP/. Each video
    # clip also has a sibling .JPG thumbnail in PRIVATE/M4ROOT/THMBNL/, so we
    # scope by directory rather than by extension to avoid picking those up as
    # photos.
    return _scan_tree(sd / "DCIM", lambda m: m.is_photo) + _scan_tree(
        sd / "PRIVATE", lambda m: m.is_video
    )


def resolve_destination() -> Tuple[Path, bool]:
    if SSD_ROOT.exists():
        return SSD_PHOTOS, True
    return MAC_FALLBACK, False


def index_destination(dest_root: Path) -> set:
    # Match by (filename, capture date). Filename alone isn't robust because Sony
    # rolls over after DSC09999, so the same name can recur years apart. Pairing
    # with the file's mtime distinguishes a rollover collision from a true
    # duplicate. The mtime is preserved across our copy (shutil.copystat) and
    # across geotagging (exiftool's -P flag), so it stays stable end-to-end.
    known = set()
    for sub in [dest_root / "RAW", dest_root / "Video"]:
        if not sub.exists():
            continue
        for p in sub.rglob("*"):
            if p.is_file():
                d = datetime.fromtimestamp(p.stat().st_mtime, tz=dt_timezone.utc).date()
                known.add((p.name, d))
    return known


def find_timeline_file() -> Optional[Path]:
    matches = list(DOWNLOADS.glob(TIMELINE_GLOB))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _parse_iso_utc(s: str) -> Optional[datetime]:
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s2)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


def timeline_range(path: Path) -> Tuple[Optional[datetime], Optional[datetime]]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    starts, ends = [], []
    for entry in data:
        s = entry.get("startTime")
        e = entry.get("endTime")
        if s:
            starts.append(s)
        if e:
            ends.append(e)
    if not starts:
        return None, None
    earliest = min(filter(None, (_parse_iso_utc(s) for s in starts)), default=None)
    latest_pool = ends if ends else starts
    latest = max(filter(None, (_parse_iso_utc(s) for s in latest_pool)), default=None)
    return earliest, latest


def next_batch_number(month_dir: Path) -> int:
    if not month_dir.exists():
        return 1
    nums = []
    for entry in month_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("Batch "):
            try:
                nums.append(int(entry.name.split(" ", 1)[1]))
            except (ValueError, IndexError):
                pass
    return max(nums, default=0) + 1


def group_by_month(files: Iterable[MediaFile]) -> dict:
    groups: dict = {}
    for f in files:
        groups.setdefault((f.year, f.month), []).append(f)
    return groups


def determine_photo_destinations(new_photos: List[MediaFile], dest_root: Path) -> dict:
    """Returns (year, month) -> batch_dir for each month containing new photos."""
    result = {}
    for (year, month) in group_by_month(new_photos).keys():
        month_dir = dest_root / "RAW" / str(year) / str(month)
        n = next_batch_number(month_dir)
        result[(year, month)] = month_dir / f"Batch {n}"
    return result


def determine_video_destinations(new_videos: List[MediaFile], dest_root: Path) -> dict:
    """Returns (year, month) -> dir for each month containing new videos. No batch folder."""
    return {
        (year, month): dest_root / "Video" / str(year) / str(month)
        for (year, month) in group_by_month(new_videos).keys()
    }


def copy_with_verify(src: Path, dst: Path) -> None:
    """Copy src → dst and SHA-1 verify. Raises RuntimeError on mismatch."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_hash = hashlib.sha1()
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(COPY_CHUNK)
            if not chunk:
                break
            src_hash.update(chunk)
            fdst.write(chunk)
    dst_hash = hashlib.sha1()
    with open(dst, "rb") as fdst:
        while True:
            chunk = fdst.read(COPY_CHUNK)
            if not chunk:
                break
            dst_hash.update(chunk)
    if src_hash.hexdigest() != dst_hash.hexdigest():
        try:
            dst.unlink()
        except OSError:
            pass
        raise RuntimeError(f"SHA-1 mismatch after copying {src.name}")
    shutil.copystat(src, dst)


def relative_date_suffix(d: date) -> str:
    today = datetime.now().date()
    if d == today:
        return " (today)"
    if d == today - timedelta(days=1):
        return " (yesterday)"
    return ""


def date_range_label(files: List[MediaFile]) -> str:
    if not files:
        return "-"
    dates = sorted({f.mtime.date() for f in files})
    if len(dates) == 1:
        return f"{dates[0]}{relative_date_suffix(dates[0])}"
    return f"{dates[0]} → {dates[-1]}{relative_date_suffix(dates[-1])}"


def print_per_date_breakdown(
    label: str, files: List[MediaFile], known: set
) -> None:
    if not files:
        return
    by_date: dict = {}
    for f in files:
        by_date.setdefault(f.mtime.date(), []).append(f)
    print(f"{BOLD}{label} by date:{RESET}")
    for d in sorted(by_date.keys()):
        items = by_date[d]
        total = len(items)
        new = sum(1 for f in items if (f.name, f.mtime.date()) not in known)
        already = total - new
        suffix = relative_date_suffix(d)
        if new == 0:
            print(
                f"  {FAINT}{d}{suffix:<11}  {total:>4}   already imported{RESET}"
            )
        elif already == 0:
            print(f"  {d}{suffix:<11}  {total:>4}   {GREEN}{new} new{RESET}")
        else:
            print(
                f"  {d}{suffix:<11}  {total:>4}   "
                f"{GREEN}{new} new{RESET} + {FAINT}{already} already imported{RESET}"
            )
    print()


def relative_to_dest(path: Path, dest_root: Path) -> str:
    try:
        return str(path.relative_to(dest_root))
    except ValueError:
        return str(path)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Ingest photos and videos from an SD card."
    )
    parser.add_argument(
        "--no-confirm", action="store_true", help="Skip the proceed prompt."
    )
    parser.add_argument(
        "--no-geotag",
        action="store_true",
        help="Don't chain to geotag after copying, even if a Timeline export is available.",
    )
    return parser.parse_args()


def print_analysis(
    sd: Path,
    photos: List[MediaFile],
    videos: List[MediaFile],
    dest_root: Path,
    dest_mounted: bool,
    known_names: set,
    new_photos: List[MediaFile],
    new_videos: List[MediaFile],
    photo_dests: dict,
    video_dests: dict,
    timeline: Optional[Path],
    timeline_start: Optional[datetime],
    timeline_end: Optional[datetime],
    can_geotag: bool,
    no_geotag_flag: bool,
) -> None:
    print(f"{BOLD}SD card:{RESET}     {sd.name} ({sd})")
    print(f"  Photos:    {len(photos)} files, {date_range_label(photos)}")
    print(f"  Videos:    {len(videos)} files, {date_range_label(videos)}")
    print()

    dest_label = (
        f"{GREEN}✓{RESET} working SSD mounted"
        if dest_mounted
        else f"{YELLOW}!{RESET} working SSD not mounted — using Mac drive"
    )
    print(f"{BOLD}Destination:{RESET} {dest_root}  {dest_label}")
    print()

    print_per_date_breakdown("Photos", photos, known_names)
    print_per_date_breakdown("Videos", videos, known_names)

    if new_photos or new_videos:
        print(f"{BOLD}New on SD:{RESET}")
        for (year, month), files in sorted(group_by_month(new_photos).items()):
            target = photo_dests[(year, month)]
            print(
                f"  {GREEN}{len(files):>4}{RESET} photos → {relative_to_dest(target, dest_root)}/"
            )
        for (year, month), files in sorted(group_by_month(new_videos).items()):
            target = video_dests[(year, month)]
            print(
                f"  {GREEN}{len(files):>4}{RESET} videos → {relative_to_dest(target, dest_root)}/"
            )
        print()

    if timeline is None:
        print(f"{BOLD}Timeline export:{RESET} {YELLOW}not found in ~/Downloads/{RESET}")
        print("  Will import only — run geotag later when you have an export.")
    elif timeline_start is None or timeline_end is None:
        print(
            f"{BOLD}Timeline export:{RESET} {timeline.name}  "
            f"{YELLOW}(could not read time range){RESET}"
        )
        print("  Will import only.")
    else:
        end_suffix = relative_date_suffix(timeline_end.date())
        print(f"{BOLD}Timeline export:{RESET} {timeline.name}")
        print(
            f"  Coverage: {timeline_start.date()} → {timeline_end.date()}{end_suffix}"
        )
        if not new_photos:
            print(f"  {FAINT}no new photos to geotag{RESET}")
        elif can_geotag:
            print(f"  {GREEN}✓ covers new photos → will geotag after import{RESET}")
        elif no_geotag_flag:
            print(f"  {FAINT}--no-geotag set; will import only{RESET}")
        else:
            photo_min = min(f.mtime.date() for f in new_photos)
            photo_max = max(f.mtime.date() for f in new_photos)
            print(
                f"  {YELLOW}does not fully cover new photos "
                f"({photo_min} → {photo_max}) — will import only{RESET}"
            )
    print()


def main() -> int:
    args = parse_arguments()

    sd = find_sd_card()
    if sd is None:
        return 1

    sd_files = scan_sd_card(sd)
    photos = [f for f in sd_files if f.is_photo]
    videos = [f for f in sd_files if f.is_video]

    dest_root, dest_mounted = resolve_destination()
    known = index_destination(dest_root)
    new_photos = [f for f in photos if (f.name, f.mtime.date()) not in known]
    new_videos = [f for f in videos if (f.name, f.mtime.date()) not in known]

    photo_dests = determine_photo_destinations(new_photos, dest_root)
    video_dests = determine_video_destinations(new_videos, dest_root)

    timeline = find_timeline_file()
    timeline_start, timeline_end = (None, None)
    if timeline is not None:
        timeline_start, timeline_end = timeline_range(timeline)

    can_geotag = False
    if (
        not args.no_geotag
        and timeline is not None
        and timeline_start is not None
        and timeline_end is not None
        and new_photos
    ):
        photo_min = min(f.mtime.date() for f in new_photos)
        photo_max = max(f.mtime.date() for f in new_photos)
        can_geotag = (
            timeline_start.date() <= photo_min and photo_max <= timeline_end.date()
        )

    print_analysis(
        sd,
        photos,
        videos,
        dest_root,
        dest_mounted,
        known,
        new_photos,
        new_videos,
        photo_dests,
        video_dests,
        timeline,
        timeline_start,
        timeline_end,
        can_geotag,
        args.no_geotag,
    )

    if not new_photos and not new_videos:
        print(f"{BLUE}Nothing to import.{RESET}")
        return 0

    if not args.no_confirm:
        try:
            response = input(f"{BOLD}Proceed?{RESET} [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            print(f"{BLUE}Aborted.{RESET}")
            return 0
        if response != "y":
            print(f"{BLUE}Aborted.{RESET}")
            return 0
    print()

    all_new = new_photos + new_videos
    total = len(all_new)
    width = len(str(total))
    for i, f in enumerate(all_new, start=1):
        dst_dir = (
            photo_dests[(f.year, f.month)]
            if f.is_photo
            else video_dests[(f.year, f.month)]
        )
        dst = dst_dir / f.name
        try:
            copy_with_verify(f.path, dst)
        except (OSError, RuntimeError) as exc:
            print(f"  {RED}✗ {f.name}{RESET} — {exc}")
            return 1
        print(
            f"  {GREEN}✓{RESET} [{i:>{width}}/{total}] {f.name}  "
            f"{FAINT}→ {relative_to_dest(dst, dest_root)}{RESET}"
        )

    print()
    print(
        f"{GREEN}{BOLD}Imported {len(new_photos)} photos and {len(new_videos)} videos.{RESET}"
    )

    if can_geotag and photo_dests:
        print()
        print(f"{BOLD}Geotagging...{RESET}")
        for dest_dir in photo_dests.values():
            print(f"\n{CYAN}→ {relative_to_dest(dest_dir, dest_root)}{RESET}")
            result = subprocess.run(
                [
                    sys.executable,
                    str(GEOTAG_SCRIPT),
                    "--dir",
                    str(dest_dir),
                    "--timeline",
                    str(timeline),
                ],
                cwd=str(REPO_ROOT),
            )
            if result.returncode != 0:
                print(f"{RED}Geotag failed for {dest_dir}.{RESET}")
                return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
