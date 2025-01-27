# Geotagging using Google location history.
#
# Command
# python google-geotag.py --json {location history json file} --dir {photos directory}
#
# Input parameters:
#
#   -j JSON, --json JSON  The JSON file containing your location history.
#   -d DIR, --dir DIR     Images folder.
#   -t TIME, --time TIME  Hours of tolerance.
#
# Google Takeout Link: https://takeout.google.com/
import argparse
import json
import os
import time
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


class Location(object):
    def __init__(self, timestamp: float, latitude: float, longitude: float):
        self.timestamp = timestamp
        self.latitude = latitude
        self.longitude = longitude

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
    return image_dir, error_hours, timezone_offset


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
        f"Loading location data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
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
            location = Location(timestamp, latitude, longitude)
            locations_list.append(location)

    # Sort the locations list by timestamp
    locations_list.sort()
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}{WHITE_BACKGROUND}Loaded {len(locations_list):,} locations{RESET_FORMAT}"
    )
    return locations_list


def get_approximate_image_location(timezone_offset, locations_list, image_file_path):
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

    image_dir, error_hours, timezone_offset = parse_arguments()

    image_file_names = read_image_file_names(image_dir)

    locations_list = load_locations(google_locations_file)

    for num, image_file_name in enumerate(image_file_names):
        image_file_path = os.path.join(image_dir, image_file_name)

        date_time_original, approx_location, hours_away = (
            get_approximate_image_location(
                timezone_offset,
                locations_list,
                image_file_path,
            )
        )

        if hours_away < error_hours:
            latitude, longitude = geotag_image(image_file_path, approx_location)
            print(
                f"{FAINT_TEXT}{num+1}/{len(image_file_names)} {RESET_FORMAT}{GREEN_TEXT}{BOLD_TEXT}Geotagged:{RESET_FORMAT}  {image_file_name} - {date_time_original} ({get_formatted_time_error(hours_away)})     {latitude}, {longitude}"
            )
        else:
            print(
                f"{FAINT_TEXT}{num+1}/{len(image_file_names)} {RESET_FORMAT}{RED_TEXT}{BOLD_TEXT}Not geotagged.{RESET_FORMAT} {image_file_name} - {date_time_original} ({get_formatted_time_error(hours_away)} min away.)"
            )
