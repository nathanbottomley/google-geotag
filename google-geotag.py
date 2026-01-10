# Geotagging using Google Maps app location history exports and nearby photo GPS.
#
# Command:
# python google-geotag.py --dir {photos directory}
#
# Input parameters:
#
#   -d DIR, --dir DIR               Images folder.
#   -e HOURS, --error_hours HOURS   Hours of tolerance.
#   -tz OFFSET, --timezone OFFSET   Timezone offset to apply to photo times.
#   -f, --force                     Overwrite existing GPS data.
#   --dry-run                       Preview changes without writing.
import argparse
import json
import os
from bisect import bisect_left
from datetime import datetime, timedelta
from typing import List, Tuple

from exiftool import ExifToolHelper

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
GPS_COORDS_WIDTH = 25
ACTION_WIDTH = len("Already geotagged")
TIME_AWAY_WIDTH = 14
SOURCE_WIDTH = len("subsequent photo")


class Location(object):
    def __init__(
        self, timestamp: float, latitude: float, longitude: float, source: str = ""
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
        help="Overwrite GPS data if it already exists.",
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
        help="Used for correcting timezone offsets as photos are not timezone aware.",
        default=0,
        required=False,
    )
    args = vars(parser.parse_args())
    image_dir = args["dir"]
    error_hours = int(args["error_hours"])
    timezone_offset = int(args["timezone"])
    force_overwrite = args["force"]
    dry_run = args["dry_run"]
    return image_dir, error_hours, timezone_offset, force_overwrite, dry_run


def _format_gps_value(value):
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return str(value)


def get_existing_gps(image_file_path):
    with ExifToolHelper() as et:
        metadata = et.get_metadata(image_file_path)[0]
    lat = metadata.get("Composite:GPSLatitude", metadata.get("EXIF:GPSLatitude"))
    lon = metadata.get("Composite:GPSLongitude", metadata.get("EXIF:GPSLongitude"))
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


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
) -> str:
    action_label = f"{action:<{ACTION_WIDTH}}"
    name_label = f"{name:<{name_width}}"
    time_label = f"{(photo_time or '-'): <{TIME_FORMAT_WIDTH}}"
    coords_label = f"{(coords or '-'): <{GPS_COORDS_WIDTH}}"
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


def load_locations(google_locations_file):
    print(
        f"Loading Google location data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
    )
    with open(google_locations_file) as f:
        location_data = json.load(f)

    locations_list = []

    for entry in location_data:
        # Handle entries with 'timelinePath' (New Format)
        if not "timelinePath" in entry:
            continue

        start_time_str = entry.get("startTime")
        if not start_time_str:
            continue
        try:
            start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            try:
                start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                print(
                    f"{RED_TEXT}Warning:{RESET_FORMAT} Invalid startTime format: {start_time_str}"
                )
                continue

        timeline_path = entry.get("timelinePath", [])
        for point in timeline_path:
            point_str = point.get("point")
            duration_offset_str = point.get("durationMinutesOffsetFromStartTime")
            if not point_str or not duration_offset_str:
                continue
            if not point_str.startswith("geo:"):
                continue
            try:
                lat_str, lon_str = point_str[4:].split(",")
                latitude = float(lat_str)
                longitude = float(lon_str)
            except ValueError:
                print(
                    f"{RED_TEXT}Warning:{RESET_FORMAT} Invalid point format: {point_str}"
                )
                continue
            try:
                duration_offset = int(duration_offset_str)
            except ValueError:
                print(
                    f"{RED_TEXT}Warning:{RESET_FORMAT} Invalid duration offset: {duration_offset_str}"
                )
                continue
            # Compute the timestamp
            point_time = start_time + timedelta(minutes=duration_offset)
            timestamp = point_time.timestamp()
            location = Location(timestamp, latitude, longitude, source="google")
            locations_list.append(location)

    # Sort the locations list by timestamp
    locations_list.sort()
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}Loaded {len(locations_list):,} locations{RESET_FORMAT}"
    )
    return locations_list


def load_photo_locations(image_file_names, image_dir, timezone_offset):
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
        try:
            image_time = datetime.strptime(date_time_original, "%Y:%m:%d %H:%M:%S")
            image_time_utc = image_time - timedelta(hours=timezone_offset)
            timestamp = image_time_utc.timestamp()
            latitude = float(lat)
            longitude = float(lon)
        except (ValueError, TypeError):
            continue
        locations_list.append(Location(timestamp, latitude, longitude, source="photo"))

    locations_list.sort()
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}Loaded {len(locations_list):,} photo GPS points{RESET_FORMAT}"
    )
    return locations_list


def get_approximate_image_location(timezone_offset, locations_list, image_file_path):
    if not locations_list:
        return None, None, None
    with ExifToolHelper() as et:
        metadata = et.get_metadata(image_file_path)[0]
    date_time_original = metadata["EXIF:DateTimeOriginal"]
    if not date_time_original:
        print(
            f"{RED_TEXT}Warning:{RESET_FORMAT} No DateTimeOriginal for {image_file_path}. Skipping."
        )
        return None, None, None
    try:
        image_time = datetime.strptime(date_time_original, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        print(
            f"{RED_TEXT}Warning:{RESET_FORMAT} Invalid DateTimeOriginal format: {date_time_original}. Skipping."
        )
        return None, None, None
    # Adjust for timezone
    image_time_utc = image_time - timedelta(hours=timezone_offset)
    image_time_unix = image_time_utc.timestamp()

    image_location = Location(timestamp=image_time_unix, latitude=0, longitude=0)
    approx_location = find_closest_location_in_time(locations_list, image_location)
    hours_away = abs(approx_location.timestamp - image_time_unix) / 3600
    return date_time_original, approx_location, hours_away


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
) -> Tuple[float, float]:
    lat_decimal = float(approx_location.latitude)
    lon_decimal = float(approx_location.longitude)

    with ExifToolHelper() as et:
        et.set_tags(
            image_file_path,
            tags={
                "GPSVersionID": "2 2 0 0",
                "GPSLatitudeRef": "S" if lat_decimal < 0 else "N",
                "GPSLatitude": lat_decimal,
                "GPSLongitudeRef": "W" if lon_decimal < 0 else "E",
                "GPSLongitude": lon_decimal,
            },
            params=["-P", "-overwrite_original"],
        )

    return (lat_decimal, lon_decimal)


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


if __name__ == "__main__":

    google_locations_file = "location-history.json"

    image_dir, error_hours, timezone_offset, force_overwrite, dry_run = (
        parse_arguments()
    )

    image_file_names = read_image_file_names(image_dir)

    locations_list = load_locations(google_locations_file)
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

    image_infos = []

    for num, image_file_name in enumerate(image_file_names):
        image_file_path = os.path.join(image_dir, image_file_name)
        index_label = format_index_label(num, total_images, index_width)

        existing_gps = get_existing_gps(image_file_path)
        if existing_gps and not force_overwrite:
            latitude, longitude = existing_gps
            date_time_original = get_image_datetime(image_file_path)
            coords_label = f"{_format_gps_value(latitude)}, {_format_gps_value(longitude)}"
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

        date_time_original, approx_location, hours_away = (
            get_approximate_image_location(
                timezone_offset,
                locations_list,
                image_file_path,
            )
        )

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
            if dry_run:
                latitude = float(approx_location.latitude)
                longitude = float(approx_location.longitude)
                coords_label = (
                    f"{_format_gps_value(latitude)}, {_format_gps_value(longitude)}"
                )
                action = "Would overwrite" if existing_gps else "Would geotag"
                print(
                    format_output_line(
                        index_label,
                        action,
                        CYAN_TEXT,
                        image_file_name,
                        name_width,
                        date_time_original,
                        coords_label,
                        get_formatted_time_error(hours_away),
                        source_label,
                    )
                )
            else:
                latitude, longitude = geotag_image(image_file_path, approx_location)
                coords_label = f"{latitude}, {longitude}"
                print(
                    format_output_line(
                        index_label,
                        "Geotagged",
                        GREEN_TEXT,
                        image_file_name,
                        name_width,
                        date_time_original,
                        coords_label,
                        get_formatted_time_error(hours_away),
                        source_label,
                    )
                )
                latitude = float(latitude)
                longitude = float(longitude)
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
                    chosen_coords = (float(lat_input), float(lon_input))
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
                        f"{_format_gps_value(chosen_coords[0])}, {_format_gps_value(chosen_coords[1])}",
                        None,
                        chosen_source,
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
                        f"{_format_gps_value(latitude)}, {_format_gps_value(longitude)}",
                        None,
                        chosen_source,
                    )
                )
