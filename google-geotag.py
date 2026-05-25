# Geotagging using Google Maps app location history exports and nearby photo GPS.
#
# Command:
# python google-geotag.py --dir {photos directory}
#
# Input parameters:
#
#   -d DIR, --dir DIR               Images folder.
#   -e HOURS, --error_hours HOURS   Hours of tolerance.
#   -tz OFFSET, --timezone OFFSET   Optional manual timezone offset override.
#                                   Omit to auto-detect from photo location.
#   -f, --force                     Overwrite existing GPS data.
#   --dry-run                       Preview changes without writing.
#   --timeline PATH                 Path to the Google Timeline JSON.
#                                   Defaults to ./location-history.json.
import argparse
import json
import os
from bisect import bisect_left
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from exiftool import ExifToolHelper
from timezonefinder import TimezoneFinder

# Print formatting
BOLD_TEXT = "\033[1m"
FAINT_TEXT = "\033[2m"
ITALIC_TEXT = "\033[3m"
UNDERLINE_TEXT = "\033[4m"
GREEN_TEXT = "\033[32m"
BLUE_TEXT = "\033[34m"
CYAN_TEXT = "\033[36m"
RED_TEXT = "\033[31m"
WHITE_BACKGROUND = "\033[47m"
RESET_FORMAT = "\033[0m"

INCLUDED_FILE_EXTENSIONS = ["jpg", "JPG", "jpeg", "JPEG", "arw", "ARW"]
TIME_FORMAT_WIDTH = 19
ACTION_WIDTH = len("Would override - unchanged")
TIME_AWAY_WIDTH = 14
SOURCE_WIDTH = len("subsequent photo")
COORDS_EQUAL_EPSILON = 1e-6  # ~10cm; well below GPS noise floor

_tz_finder = TimezoneFinder()
_zone_lookup_cache = {}


def lookup_zone(latitude, longitude) -> Optional[str]:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    # Round to ~100m precision for caching — far finer than any zone boundary.
    key = (round(lat, 3), round(lon, 3))
    if key not in _zone_lookup_cache:
        _zone_lookup_cache[key] = _tz_finder.timezone_at(lat=lat, lng=lon)
    return _zone_lookup_cache[key]


def format_zone_label(zone_name: Optional[str], reference_dt: datetime) -> str:
    if zone_name is None:
        return "unknown"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return zone_name
    aware = reference_dt.replace(tzinfo=zone) if reference_dt.tzinfo is None else reference_dt.astimezone(zone)
    offset_str = aware.strftime("%z")
    if len(offset_str) >= 5:
        offset_pretty = f"UTC{offset_str[:3]}:{offset_str[3:5]}"
    else:
        offset_pretty = "UTC?"
    abbrev = aware.tzname() or ""
    if abbrev and abbrev != zone_name:
        return f"{zone_name} ({abbrev}, {offset_pretty})"
    return f"{zone_name} ({offset_pretty})"


def exif_local_to_utc_timestamp(naive_local: datetime, zone_name: Optional[str]) -> float:
    if zone_name is None:
        return naive_local.replace(tzinfo=dt_timezone.utc).timestamp()
    try:
        return naive_local.replace(tzinfo=ZoneInfo(zone_name)).timestamp()
    except ZoneInfoNotFoundError:
        return naive_local.replace(tzinfo=dt_timezone.utc).timestamp()


def manual_offset_to_utc_timestamp(naive_local: datetime, offset_hours: float) -> float:
    """Convert a naive EXIF datetime to a UTC unix timestamp using a fixed hour offset."""
    return (naive_local - timedelta(hours=offset_hours)).replace(tzinfo=dt_timezone.utc).timestamp()


def parse_exif_datetime(date_time_original: str) -> Optional[datetime]:
    try:
        return datetime.strptime(date_time_original, "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def parse_iso_to_utc(timestamp_str: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp (with Z, ±HH:MM, or naive) and return a UTC-aware datetime."""
    if not timestamp_str:
        return None
    s = timestamp_str.replace("Z", "+00:00") if timestamp_str.endswith("Z") else timestamp_str
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


def parse_geo_point(geo_str: str) -> Optional[Tuple[str, str]]:
    """Parse a 'geo:lat,lng' string. Returns (lat, lng) as strings preserving precision, or None."""
    if not geo_str or not geo_str.startswith("geo:"):
        return None
    try:
        lat_str, lon_str = geo_str[4:].split(",")
        float(lat_str)
        float(lon_str)
    except ValueError:
        return None
    return lat_str, lon_str


class Location(object):
    def __init__(
        self, timestamp: float, latitude: str, longitude: str, source: str = ""
    ):
        self.timestamp = timestamp
        self.latitude = latitude
        self.longitude = longitude
        self.source = source

    def get_timestamp(self, timestamp):
        if timestamp is None:
            return None
        str_formats = ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"]
        for str_format in str_formats:
            try:
                return datetime.strptime(timestamp, str_format).timestamp()
            except ValueError:
                pass
        raise ValueError("No valid date format found.")

    def __lt__(self, other):
        return self.timestamp < other.timestamp


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", "--dir", help="Images folder.", required=True)
    parser.add_argument(
        "-e", "--error_hours", help="Hours of tolerance.", default=1, required=False
    )
    parser.add_argument(
        "-f",
        "--force",
        help=(
            "Force re-writing GPS tags even on photos that already have them. "
            "Output distinguishes 'Overridden - changed' from "
            "'Overridden - unchanged' so you can see which photos actually moved."
        ),
        action="store_true",
        required=False,
    )
    parser.add_argument(
        "--dry-run",
        help="Show what would be changed without modifying any files.",
        action="store_true",
        required=False,
    )
    parser.add_argument(
        "-tz",
        "--timezone",
        help=(
            "Optional manual timezone offset in hours (e.g. -5 or 1). "
            "If omitted, the timezone is auto-detected from the photo's location "
            "at the time it was taken, with daylight saving handled automatically."
        ),
        default=None,
        required=False,
    )
    parser.add_argument(
        "--timeline",
        help="Path to the Google Timeline JSON. Defaults to ./location-history.json.",
        default=None,
        required=False,
    )
    args = vars(parser.parse_args())
    image_dir = args["dir"]
    error_hours = int(args["error_hours"])
    raw_offset = args["timezone"]
    timezone_offset = float(raw_offset) if raw_offset is not None else None
    force_overwrite = args["force"]
    dry_run = args["dry_run"]
    timeline_file = args["timeline"] or "location-history.json"
    return image_dir, error_hours, timezone_offset, force_overwrite, dry_run, timeline_file


def normalize_gps_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return str(value)


def format_coords(latitude, longitude):
    lat_value = normalize_gps_value(latitude)
    lon_value = normalize_gps_value(longitude)
    if lat_value is None or lon_value is None:
        return None
    return f"{lat_value}, {lon_value}"


def gps_coords_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return (
            abs(float(a[0]) - float(b[0])) < COORDS_EQUAL_EPSILON
            and abs(float(a[1]) - float(b[1])) < COORDS_EQUAL_EPSILON
        )
    except (TypeError, ValueError):
        return False


def get_existing_gps(image_file_path):
    with ExifToolHelper() as et:
        metadata = et.get_metadata(image_file_path)[0]
    lat = metadata.get("Composite:GPSLatitude", metadata.get("EXIF:GPSLatitude"))
    lon = metadata.get("Composite:GPSLongitude", metadata.get("EXIF:GPSLongitude"))
    if lat is None or lon is None:
        return None
    lat_value = normalize_gps_value(lat)
    lon_value = normalize_gps_value(lon)
    if lat_value is None or lon_value is None:
        return None
    return lat_value, lon_value


def get_image_datetime(image_file_path):
    with ExifToolHelper() as et:
        metadata = et.get_metadata(image_file_path)[0]
    return metadata.get("EXIF:DateTimeOriginal")


def geotag_image_with_coords(
    image_file_path: str, latitude: float, longitude: float
):
    location = Location(timestamp=0, latitude=latitude, longitude=longitude)
    return geotag_image(image_file_path, location)


def format_index_label(index: int, total: int, index_width: int) -> str:
    return f"{index + 1:>{index_width}}/{total}"


def format_output_line(
    index_label: str,
    action: str,
    action_color: str,
    name: str,
    name_width: int,
    photo_time: str,
    coords: str,
    time_away: str,
    source: str,
    coords_width: int,
) -> str:
    action_label = f"{action:<{ACTION_WIDTH}}"
    name_label = f"{name:<{name_width}}"
    time_label = f"{(photo_time or '-'): <{TIME_FORMAT_WIDTH}}"
    coords_value = coords or "-"
    coords_label = f"{coords_value:<{coords_width}}"
    time_away_label = f"{(time_away or '-'): <{TIME_AWAY_WIDTH}}"
    source_label = f"{(source or '-'): <{SOURCE_WIDTH}}"
    return (
        f"{FAINT_TEXT}{index_label}{RESET_FORMAT}  "
        f"{action_color}{BOLD_TEXT}{action_label}{RESET_FORMAT} "
        f"{name_label}  "
        f"{time_label}  "
        f"{coords_label}  "
        f"{time_away_label}  "
        f"{source_label}"
    )


def read_image_file_names(image_dir):
    try:
        image_files = sorted(
            [
                fn
                for fn in os.listdir(image_dir)
                if any(fn.endswith(ext) for ext in INCLUDED_FILE_EXTENSIONS)
            ]
        )
    except FileNotFoundError:
        print(
            f"{RED_TEXT}{BOLD_TEXT}Error:{RESET_FORMAT} The folder {image_dir} does not exist."
        )
        exit()

    if not image_files:
        print(
            f"{RED_TEXT}{BOLD_TEXT}Error:{RESET_FORMAT} No images found in the folder {image_dir}."
        )
        exit()
    print(f"Selected {CYAN_TEXT}{len(image_files):,}{RESET_FORMAT} images to geotag.")
    print(f"In the folder {CYAN_TEXT}{image_dir}{RESET_FORMAT}", end="\n\n")
    return image_files


VISIT_SAMPLE_INTERVAL = timedelta(minutes=10)


def load_locations(google_locations_file):
    print(
        f"Loading Google location data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
    )
    with open(google_locations_file) as f:
        location_data = json.load(f)

    locations_list = []
    counts = {"timelinePath": 0, "visit": 0, "activity": 0}

    for entry in location_data:
        start_time = parse_iso_to_utc(entry.get("startTime"))
        end_time = parse_iso_to_utc(entry.get("endTime"))
        if start_time is None:
            continue

        if "timelinePath" in entry:
            for point in entry["timelinePath"]:
                coords = parse_geo_point(point.get("point", ""))
                offset_str = point.get("durationMinutesOffsetFromStartTime")
                if coords is None or offset_str is None:
                    continue
                try:
                    offset_minutes = int(offset_str)
                except ValueError:
                    continue
                point_time = start_time + timedelta(minutes=offset_minutes)
                locations_list.append(
                    Location(
                        point_time.timestamp(),
                        normalize_gps_value(coords[0]),
                        normalize_gps_value(coords[1]),
                        source="google",
                    )
                )
                counts["timelinePath"] += 1

        elif "visit" in entry and end_time is not None:
            # Stationary at a single place across [startTime, endTime].
            # Sample synthetic points across the visit so any photo taken
            # during it finds a near-zero time delta.
            place = entry["visit"].get("topCandidate", {}).get("placeLocation")
            coords = parse_geo_point(place or "")
            if coords is None:
                continue
            lat = normalize_gps_value(coords[0])
            lon = normalize_gps_value(coords[1])
            t = start_time
            while t < end_time:
                locations_list.append(Location(t.timestamp(), lat, lon, source="visit"))
                counts["visit"] += 1
                t += VISIT_SAMPLE_INTERVAL
            locations_list.append(Location(end_time.timestamp(), lat, lon, source="visit"))
            counts["visit"] += 1

        elif "activity" in entry and end_time is not None:
            # Movement from start to end. We anchor the endpoints in time;
            # the path between is unknown so we don't interpolate.
            activity = entry["activity"]
            start_coords = parse_geo_point(activity.get("start", ""))
            end_coords = parse_geo_point(activity.get("end", ""))
            if start_coords is not None:
                locations_list.append(
                    Location(
                        start_time.timestamp(),
                        normalize_gps_value(start_coords[0]),
                        normalize_gps_value(start_coords[1]),
                        source="activity",
                    )
                )
                counts["activity"] += 1
            if end_coords is not None:
                locations_list.append(
                    Location(
                        end_time.timestamp(),
                        normalize_gps_value(end_coords[0]),
                        normalize_gps_value(end_coords[1]),
                        source="activity",
                    )
                )
                counts["activity"] += 1

    locations_list.sort()
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}Loaded {len(locations_list):,} locations{RESET_FORMAT} "
        f"{FAINT_TEXT}(timeline: {counts['timelinePath']:,}, "
        f"visits: {counts['visit']:,}, activities: {counts['activity']:,}){RESET_FORMAT}"
    )
    return locations_list


def load_photo_locations(image_file_names, image_dir, manual_offset_hours):
    print(
        f"Scanning existing photo GPS data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
    )
    locations_list = []
    image_paths = [os.path.join(image_dir, name) for name in image_file_names]
    with ExifToolHelper() as et:
        metadata_list = et.get_metadata(image_paths)

    for metadata in metadata_list:
        date_time_original = metadata.get("EXIF:DateTimeOriginal")
        if not date_time_original:
            continue
        lat = metadata.get("Composite:GPSLatitude", metadata.get("EXIF:GPSLatitude"))
        lon = metadata.get("Composite:GPSLongitude", metadata.get("EXIF:GPSLongitude"))
        if lat is None or lon is None:
            continue
        image_time = parse_exif_datetime(date_time_original)
        if image_time is None:
            continue
        latitude = normalize_gps_value(lat)
        longitude = normalize_gps_value(lon)
        if latitude is None or longitude is None:
            continue
        if manual_offset_hours is not None:
            timestamp = manual_offset_to_utc_timestamp(image_time, manual_offset_hours)
        else:
            zone_name = lookup_zone(latitude, longitude)
            timestamp = exif_local_to_utc_timestamp(image_time, zone_name)
        locations_list.append(Location(timestamp, latitude, longitude, source="photo"))

    locations_list.sort()
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}Loaded {len(locations_list):,} photo GPS points{RESET_FORMAT}"
    )
    return locations_list


def get_approximate_image_location(
    locations_list,
    image_file_path,
    hint_zone: Optional[str] = None,
    manual_offset_hours: Optional[float] = None,
):
    if not locations_list:
        return None, None, None, None
    with ExifToolHelper() as et:
        metadata = et.get_metadata(image_file_path)[0]
    date_time_original = metadata.get("EXIF:DateTimeOriginal")
    if not date_time_original:
        print(
            f"{RED_TEXT}Warning:{RESET_FORMAT} No DateTimeOriginal for {image_file_path}. Skipping."
        )
        return None, None, None, None
    image_time = parse_exif_datetime(date_time_original)
    if image_time is None:
        print(
            f"{RED_TEXT}Warning:{RESET_FORMAT} Invalid DateTimeOriginal format: {date_time_original}. Skipping."
        )
        return None, None, None, None

    if manual_offset_hours is not None:
        image_time_unix = manual_offset_to_utc_timestamp(image_time, manual_offset_hours)
        approx_location = find_closest_location_in_time(
            locations_list,
            Location(timestamp=image_time_unix, latitude="0", longitude="0"),
        )
        hours_away = abs(approx_location.timestamp - image_time_unix) / 3600
        return date_time_original, approx_location, hours_away, None

    # Two-pass lookup: an initial guess gives us a rough location, which we use
    # to identify the zone, which gives us a corrected UTC time, which we use
    # for the real lookup. We iterate up to a few times in case the first guess
    # was so wrong that it landed in the wrong zone — this converges quickly
    # because zones are wider than the error from a single bad offset.
    current_zone = hint_zone
    image_time_unix = exif_local_to_utc_timestamp(image_time, current_zone)
    approx_location = find_closest_location_in_time(
        locations_list, Location(timestamp=image_time_unix, latitude="0", longitude="0")
    )
    for _ in range(3):
        derived_zone = lookup_zone(approx_location.latitude, approx_location.longitude)
        if derived_zone is None or derived_zone == current_zone:
            current_zone = derived_zone or current_zone
            break
        current_zone = derived_zone
        image_time_unix = exif_local_to_utc_timestamp(image_time, current_zone)
        approx_location = find_closest_location_in_time(
            locations_list,
            Location(timestamp=image_time_unix, latitude="0", longitude="0"),
        )

    hours_away = abs(approx_location.timestamp - image_time_unix) / 3600
    return date_time_original, approx_location, hours_away, current_zone


def find_closest_location_in_time(
    locations: List[Location], image_location: Location
) -> Location:
    pos = bisect_left(locations, image_location)
    if pos == 0:
        return locations[0]
    if pos == len(locations):
        return locations[-1]
    before = locations[pos - 1]
    after = locations[pos]
    if (
        after.timestamp - image_location.timestamp
        < image_location.timestamp - before.timestamp
    ):
        return after
    else:
        return before


def geotag_image(
    image_file_path: str, approx_location: Location
) -> Tuple[str, str]:
    lat_decimal = float(approx_location.latitude)
    lon_decimal = float(approx_location.longitude)
    lat_value = normalize_gps_value(approx_location.latitude)
    lon_value = normalize_gps_value(approx_location.longitude)

    with ExifToolHelper() as et:
        et.set_tags(
            image_file_path,
            tags={
                "GPSVersionID": "2 2 0 0",
                "GPSLatitudeRef": "S" if lat_decimal < 0 else "N",
                "GPSLatitude": lat_value,
                "GPSLongitudeRef": "W" if lon_decimal < 0 else "E",
                "GPSLongitude": lon_value,
            },
            params=["-P", "-overwrite_original"],
        )

    return (lat_value, lon_value)


def get_formatted_time_error(hours: float) -> str:
    """
    Takes a time in hours and returns a formatted string
    If the time is less than 1 hour, it returns the time in minutes.
    If the time is less than 120 seconds it returns the time in seconds.
    """
    if hours > 1:
        return f"{hours:.2f} hours away"
    minutes = hours * 60
    if minutes > 1:
        return f"{minutes:.1f} min away"
    seconds = minutes * 60
    return f"{int(seconds)} sec away"


def announce_zone(
    new_zone: Optional[str],
    previous_zone: Optional[str],
    reference_dt: Optional[datetime],
    image_name: str,
) -> None:
    if new_zone is None or new_zone == previous_zone:
        return
    ref = reference_dt or datetime.now()
    label = format_zone_label(new_zone, ref)
    if previous_zone is None:
        print(
            f"\n{CYAN_TEXT}{BOLD_TEXT}Detected starting timezone:{RESET_FORMAT} {label}\n"
        )
    else:
        previous_label = format_zone_label(previous_zone, ref)
        print(
            f"\n{CYAN_TEXT}{BOLD_TEXT}Timezone change at {image_name}:{RESET_FORMAT} "
            f"{previous_label} → {label}\n"
        )


if __name__ == "__main__":

    image_dir, error_hours, timezone_offset, force_overwrite, dry_run, google_locations_file = (
        parse_arguments()
    )

    image_file_names = read_image_file_names(image_dir)

    locations_list = load_locations(google_locations_file)
    if force_overwrite:
        # With --force every photo will be re-derived from external sources,
        # so including their existing GPS in the matching pool would just
        # cause each photo to match itself.
        print(
            f"{FAINT_TEXT}Skipping existing photo GPS pool (force mode){RESET_FORMAT}"
        )
    else:
        photo_locations = load_photo_locations(
            image_file_names, image_dir, timezone_offset
        )
        locations_list.extend(photo_locations)
        locations_list.sort()
    print(
        f"{CYAN_TEXT}{BOLD_TEXT}{WHITE_BACKGROUND}Total locations for matching: {len(locations_list):,}{RESET_FORMAT}\n"
    )

    total_images = len(image_file_names)
    index_width = len(str(total_images))
    name_width = max(len(name) for name in image_file_names)
    coords_width = max(
        (len(format_coords(loc.latitude, loc.longitude) or "-") for loc in locations_list),
        default=len("-"),
    )

    image_infos = []
    current_zone: Optional[str] = None
    if timezone_offset is not None:
        print(
            f"\n{CYAN_TEXT}{BOLD_TEXT}Using manual timezone offset:{RESET_FORMAT} "
            f"UTC{timezone_offset:+g} {FAINT_TEXT}(auto-detection disabled){RESET_FORMAT}\n"
        )

    for num, image_file_name in enumerate(image_file_names):
        image_file_path = os.path.join(image_dir, image_file_name)
        index_label = format_index_label(num, total_images, index_width)

        existing_gps = get_existing_gps(image_file_path)
        if existing_gps and not force_overwrite:
            latitude, longitude = existing_gps
            date_time_original = get_image_datetime(image_file_path)
            coords_label = format_coords(latitude, longitude)
            if timezone_offset is None:
                photo_zone = lookup_zone(latitude, longitude)
                announce_zone(
                    photo_zone,
                    current_zone,
                    parse_exif_datetime(date_time_original) if date_time_original else None,
                    image_file_name,
                )
                if photo_zone is not None:
                    current_zone = photo_zone
            print(
                format_output_line(
                    index_label,
                    "Already geotagged",
                    BLUE_TEXT,
                    image_file_name,
                    name_width,
                    date_time_original,
                    coords_label,
                    None,
                    "photo",
                    coords_width,
                )
            )
            image_infos.append(
                {
                    "index": num,
                    "name": image_file_name,
                    "path": image_file_path,
                    "status": "has_gps",
                    "coords": (latitude, longitude),
                    "date_time_original": date_time_original,
                }
            )
            continue

        date_time_original, approx_location, hours_away, detected_zone = (
            get_approximate_image_location(
                locations_list,
                image_file_path,
                hint_zone=current_zone,
                manual_offset_hours=timezone_offset,
            )
        )
        if timezone_offset is None and detected_zone is not None:
            announce_zone(
                detected_zone,
                current_zone,
                parse_exif_datetime(date_time_original) if date_time_original else None,
                image_file_name,
            )
            current_zone = detected_zone

        if hours_away is None or approx_location is None or date_time_original is None:
            print(
                format_output_line(
                    index_label,
                    "Not geotagged",
                    RED_TEXT,
                    image_file_name,
                    name_width,
                    None,
                    None,
                    None,
                    None,
                    coords_width,
                )
            )
            image_infos.append(
                {
                    "index": num,
                    "name": image_file_name,
                    "path": image_file_path,
                    "status": "untaggable",
                    "coords": None,
                    "date_time_original": None,
                }
            )
        elif hours_away < error_hours:
            source_label = approx_location.source or "unknown"
            new_coords = (
                normalize_gps_value(approx_location.latitude),
                normalize_gps_value(approx_location.longitude),
            )
            if existing_gps:
                unchanged = gps_coords_equal(existing_gps, new_coords)
                if dry_run:
                    action = "Would override - unchanged" if unchanged else "Would override - changed"
                    action_color = CYAN_TEXT
                else:
                    action = "Overridden - unchanged" if unchanged else "Overridden - changed"
                    action_color = BLUE_TEXT if unchanged else GREEN_TEXT
            else:
                action = "Would geotag" if dry_run else "Geotagged"
                action_color = CYAN_TEXT if dry_run else GREEN_TEXT

            if dry_run:
                latitude, longitude = new_coords
            else:
                latitude, longitude = geotag_image(image_file_path, approx_location)
            coords_label = format_coords(latitude, longitude)
            print(
                format_output_line(
                    index_label,
                    action,
                    action_color,
                    image_file_name,
                    name_width,
                    date_time_original,
                    coords_label,
                    get_formatted_time_error(hours_away),
                    source_label,
                    coords_width,
                )
            )
            image_infos.append(
                {
                    "index": num,
                    "name": image_file_name,
                    "path": image_file_path,
                    "status": "geotagged",
                    "coords": (latitude, longitude),
                    "date_time_original": date_time_original,
                }
            )
        else:
            source_label = approx_location.source or "unknown"
            print(
                format_output_line(
                    index_label,
                    "Not geotagged",
                    RED_TEXT,
                    image_file_name,
                    name_width,
                    date_time_original,
                    None,
                    get_formatted_time_error(hours_away),
                    source_label,
                    coords_width,
                )
            )
            image_infos.append(
                {
                    "index": num,
                    "name": image_file_name,
                    "path": image_file_path,
                    "status": "untaggable",
                    "coords": None,
                    "date_time_original": date_time_original,
                }
            )

    untaggable_batches = []
    current_batch = None
    for info in image_infos:
        if info["status"] == "untaggable":
            if current_batch is None:
                current_batch = {
                    "start_index": info["index"],
                    "end_index": info["index"],
                    "images": [info],
                }
            else:
                current_batch["end_index"] = info["index"]
                current_batch["images"].append(info)
        else:
            if current_batch is not None:
                untaggable_batches.append(current_batch)
                current_batch = None
    if current_batch is not None:
        untaggable_batches.append(current_batch)

    if not untaggable_batches:
        print(f"\n{GREEN_TEXT}{BOLD_TEXT}All images geotagged 🎉{RESET_FORMAT}")
        exit()

    print(
        f"{RED_TEXT}{BOLD_TEXT}There are {len(untaggable_batches)} batches of images that could not be geotagged.{RESET_FORMAT}"
    )

    def find_previous_coords(start_index):
        for idx in range(start_index - 1, -1, -1):
            coords = image_infos[idx].get("coords")
            if coords:
                return coords
        return None

    def find_next_coords(end_index):
        for idx in range(end_index + 1, len(image_infos)):
            coords = image_infos[idx].get("coords")
            if coords:
                return coords
        return None

    for batch_num, batch in enumerate(untaggable_batches, start=1):
        start_num = batch["start_index"] + 1
        end_num = batch["end_index"] + 1
        start_name = batch["images"][0]["name"]
        end_name = batch["images"][-1]["name"]
        if start_name == end_name:
            range_label = start_name
        else:
            range_label = f"{start_name} -> {end_name}"
        print(f"{BOLD_TEXT}Batch {batch_num}:{RESET_FORMAT} {range_label}")
        prev_coords = find_previous_coords(batch["start_index"])
        next_coords = find_next_coords(batch["end_index"])

        while True:
            print("Choose how to geotag this batch:")
            print("  1) use coordinates of previous image")
            print("  2) use coordinates of subsequent image")
            print("  3) enter coordinates manually")
            print("  4) do not geotag")
            choice = input("Enter choice [1-4]: ").strip()

            if choice == "1":
                if not prev_coords:
                    print(
                        f"{RED_TEXT}No previous image coordinates available for this batch.{RESET_FORMAT}"
                    )
                    continue
                chosen_coords = prev_coords
                chosen_source = "previous photo"
                break
            if choice == "2":
                if not next_coords:
                    print(
                        f"{RED_TEXT}No subsequent image coordinates available for this batch.{RESET_FORMAT}"
                    )
                    continue
                chosen_coords = next_coords
                chosen_source = "subsequent photo"
                break
            if choice == "3":
                lat_input = input("Enter latitude: ").strip()
                lon_input = input("Enter longitude: ").strip()
                try:
                    # Validate numeric input while preserving exact string precision.
                    float(lat_input)
                    float(lon_input)
                    chosen_coords = (lat_input, lon_input)
                    chosen_source = "manual"
                    break
                except ValueError:
                    print(f"{RED_TEXT}Invalid coordinates.{RESET_FORMAT}")
                    continue
            if choice == "4":
                chosen_coords = None
                break
            print(f"{RED_TEXT}Invalid choice.{RESET_FORMAT}")

        if chosen_coords is None:
            print(
                f"{FAINT_TEXT}{range_label}{RESET_FORMAT} {BLUE_TEXT}Skipped geotagging for batch {batch_num}.{RESET_FORMAT}"
            )
            continue

        for image in batch["images"]:
            index_label = format_index_label(
                image["index"], total_images, index_width
            )
            photo_time = image.get("date_time_original")
            if dry_run:
                print(
                    format_output_line(
                        index_label,
                        "Would geotag",
                        CYAN_TEXT,
                        image["name"],
                        name_width,
                        photo_time,
                        format_coords(chosen_coords[0], chosen_coords[1]),
                        None,
                        chosen_source,
                        coords_width,
                    )
                )
            else:
                latitude, longitude = geotag_image_with_coords(
                    image["path"], chosen_coords[0], chosen_coords[1]
                )
                print(
                    format_output_line(
                        index_label,
                        "Geotagged",
                        GREEN_TEXT,
                        image["name"],
                        name_width,
                        photo_time,
                        format_coords(latitude, longitude),
                        None,
                        chosen_source,
                        coords_width,
                    )
                )
