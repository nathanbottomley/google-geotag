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

PENDING_MARKER = ".needs-geotag"

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


def write_pending_marker(batch_dir: Path) -> None:
    (batch_dir / PENDING_MARKER).write_text(
        "Photos in this batch are awaiting geotagging.\n"
        f"Created {datetime.now().isoformat(timespec='seconds')}.\n"
    )


def remove_pending_marker(batch_dir: Path) -> None:
    try:
        (batch_dir / PENDING_MARKER).unlink()
    except FileNotFoundError:
        pass


def find_pending_batches(dest_root: Path) -> List[Path]:
    raw = dest_root / "RAW"
    if not raw.is_dir():
        return []
    return sorted(m.parent for m in raw.rglob(PENDING_MARKER) if m.is_file())


def batch_photo_dates(batch_dir: Path) -> Tuple[Optional[date], Optional[date]]:
    dates = []
    for p in batch_dir.iterdir():
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() in PHOTO_EXTS:
            d = datetime.fromtimestamp(p.stat().st_mtime, tz=dt_timezone.utc).date()
            dates.append(d)
    if not dates:
        return None, None
    return min(dates), max(dates)


def covers(
    timeline_start: Optional[datetime],
    timeline_end: Optional[datetime],
    min_date: Optional[date],
    max_date: Optional[date],
) -> bool:
    if (
        timeline_start is None
        or timeline_end is None
        or min_date is None
        or max_date is None
    ):
        return False
    return timeline_start.date() <= min_date and max_date <= timeline_end.date()


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


def _geotag_status(will_geotag: bool, no_geotag_flag: bool) -> str:
    if will_geotag:
        return f"{GREEN}✓ will geotag now{RESET}"
    if no_geotag_flag:
        return f"{FAINT}--no-geotag → leaving pending{RESET}"
    return f"{YELLOW}! not covered → leaving pending{RESET}"


def _date_span(pmin: Optional[date], pmax: Optional[date]) -> str:
    if pmin is None or pmax is None:
        return "?"
    if pmin == pmax:
        return f"{pmin}{relative_date_suffix(pmin)}"
    return f"{pmin} → {pmax}{relative_date_suffix(pmax)}"


def print_analysis(
    sd: Path,
    photos: List[MediaFile],
    videos: List[MediaFile],
    dest_root: Path,
    dest_mounted: bool,
    known: set,
    new_photos: List[MediaFile],
    new_videos: List[MediaFile],
    new_batch_plans: list,
    video_dests: dict,
    pending_plans: list,
    timeline: Optional[Path],
    timeline_start: Optional[datetime],
    timeline_end: Optional[datetime],
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

    print_per_date_breakdown("Photos", photos, known)
    print_per_date_breakdown("Videos", videos, known)

    if new_photos or new_videos:
        print(f"{BOLD}New on SD:{RESET}")
        for dest_dir, files, _, _, will_geotag in new_batch_plans:
            print(
                f"  {GREEN}{len(files):>4}{RESET} photos → "
                f"{relative_to_dest(dest_dir, dest_root)}/  "
                f"{_geotag_status(will_geotag, no_geotag_flag)}"
            )
        for (year, month), files in sorted(group_by_month(new_videos).items()):
            target = video_dests[(year, month)]
            print(
                f"  {GREEN}{len(files):>4}{RESET} videos → "
                f"{relative_to_dest(target, dest_root)}/"
            )
        print()

    if pending_plans:
        print(f"{BOLD}Pending geotag (from earlier imports):{RESET}")
        for batch_dir, pmin, pmax, will_geotag in pending_plans:
            print(
                f"  {relative_to_dest(batch_dir, dest_root)}/  "
                f"{FAINT}{_date_span(pmin, pmax)}{RESET}  "
                f"{_geotag_status(will_geotag, no_geotag_flag)}"
            )
        print()

    if timeline is None:
        print(f"{BOLD}Timeline export:{RESET} {YELLOW}not found in ~/Downloads/{RESET}")
    elif timeline_start is None or timeline_end is None:
        print(
            f"{BOLD}Timeline export:{RESET} {timeline.name}  "
            f"{YELLOW}(could not read time range){RESET}"
        )
    else:
        end_suffix = relative_date_suffix(timeline_end.date())
        print(f"{BOLD}Timeline export:{RESET} {timeline.name}")
        print(
            f"  Coverage: {timeline_start.date()} → {timeline_end.date()}{end_suffix}"
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

    # Per-batch coverage for new photos. A multi-month ingest can end up with
    # one month covered by the Timeline export and another not, so we decide
    # batch-by-batch rather than all-or-nothing.
    new_batch_plans = []  # (dest_dir, files, min_date, max_date, will_geotag)
    for (year, month), files in sorted(group_by_month(new_photos).items()):
        pmin = min(f.mtime.date() for f in files)
        pmax = max(f.mtime.date() for f in files)
        will_geotag = (
            not args.no_geotag
            and timeline is not None
            and covers(timeline_start, timeline_end, pmin, pmax)
        )
        new_batch_plans.append((photo_dests[(year, month)], files, pmin, pmax, will_geotag))

    # Pending batches from previous ingests that didn't geotag.
    pending_plans = []  # (batch_dir, min_date, max_date, will_geotag)
    for batch in find_pending_batches(dest_root):
        pmin, pmax = batch_photo_dates(batch)
        will_geotag = (
            not args.no_geotag
            and timeline is not None
            and covers(timeline_start, timeline_end, pmin, pmax)
        )
        pending_plans.append((batch, pmin, pmax, will_geotag))

    print_analysis(
        sd,
        photos,
        videos,
        dest_root,
        dest_mounted,
        known,
        new_photos,
        new_videos,
        new_batch_plans,
        video_dests,
        pending_plans,
        timeline,
        timeline_start,
        timeline_end,
        args.no_geotag,
    )

    has_new = bool(new_photos or new_videos)
    has_geotag_work = any(p[4] for p in new_batch_plans) or any(
        p[3] for p in pending_plans
    )
    if not has_new and not has_geotag_work:
        print(f"{BLUE}Nothing to do.{RESET}")
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

    if has_new:
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

        # Mark any new photo batch that's not being geotagged this run, so it
        # gets picked up on a later ingest when a covering Timeline export exists.
        for dest_dir, _, _, _, will_geotag in new_batch_plans:
            if not will_geotag:
                write_pending_marker(dest_dir)

    geotag_targets = [
        (dest_dir, True) for dest_dir, _, _, _, will in new_batch_plans if will
    ] + [(batch, False) for batch, _, _, will in pending_plans if will]

    if geotag_targets:
        print()
        print(f"{BOLD}Geotagging...{RESET}")
        for batch_dir, is_new in geotag_targets:
            label = "new" if is_new else "pending"
            print(
                f"\n{CYAN}→ {relative_to_dest(batch_dir, dest_root)}{RESET} "
                f"{FAINT}({label}){RESET}"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(GEOTAG_SCRIPT),
                    "--dir",
                    str(batch_dir),
                    "--timeline",
                    str(timeline),
                ],
                cwd=str(REPO_ROOT),
            )
            if result.returncode != 0:
                print(f"{RED}Geotag failed for {batch_dir}.{RESET}")
                return 1
            remove_pending_marker(batch_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
