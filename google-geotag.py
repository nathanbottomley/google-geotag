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
import argparse
import json
import os
import time
from bisect import bisect_left
from datetime import datetime
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


class Location(object):
    def __init__(self, d: dict = None):
        if d is None:
            d = {}
        self.timestamp: float = self.get_timestamp(d.get("timestamp"))
        self.latitude = d.get("latitudeE7")
        self.longitude = d.get("longitudeE7")
        self.altitude = d.get("altitude", 0)

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


def get_image_time_unix(date_time_original: str) -> float:
    # converts the image time string into a time object
    image_time = datetime.strptime(date_time_original, "%Y:%m:%d %H:%M:%S")
    # converts the image time object into a unix time object
    return time.mktime(image_time.timetuple())


def geotag_image(
    image_file_path: str, approx_location: Location
) -> Tuple[float, float]:
    lat_decimal = float(approx_location.latitude) / 1e7
    lon_decimal = float(approx_location.longitude) / 1e7

    with ExifToolHelper() as et:
        et.set_tags(
            image_file_path,
            tags={
                "GPSVersionID": "2 2 0 0",
                "GPSAltitudeRef": 0 if approx_location.altitude > 0 else 1,
                "GPSAltitude": abs(approx_location.altitude),
                "GPSLatitudeRef": "S" if lat_decimal < 0 else "N",
                "GPSLatitude": lat_decimal,
                "GPSLongitudeRef": "W" if lon_decimal < 0 else "E",
                "GPSLongitude": lon_decimal,
            },
            params=["-P", "-overwrite_original"],
        )

    return (lat_decimal, lon_decimal)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-j",
        "--json",
        help="The JSON file containing your location history.",
        required=True,
    )
    parser.add_argument("-d", "--dir", help="Images folder.", required=True)
    parser.add_argument(
        "-t", "--time", help="Hours of tolerance.", default=1, required=False
    )
    args = vars(parser.parse_args())
    locations_file = args["json"]
    image_dir = args["dir"]
    hours_threshold = int(args["time"])

    included_extensions = ["jpg", "JPG", "jpeg", "JPEG", "arw", "ARW"]
    try:
        image_files = [
            fn
            for fn in os.listdir(image_dir)
            if any(fn.endswith(ext) for ext in included_extensions)
        ]
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

    print(
        f"Loading location data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
    )
    with open(locations_file) as f:
        location_data = json.load(f)
    print(
        f"{BLUE_TEXT}{BOLD_TEXT}{WHITE_BACKGROUND}Found {len(location_data['locations']):,} locations{RESET_FORMAT}",
    )
    locations_list = [Location(location) for location in location_data["locations"]]
    print("Loaded all locations.", end="\n\n")

    for num, image_file in enumerate(image_files):
        image_file_path = os.path.join(image_dir, image_file)

        with ExifToolHelper() as et:
            metadata = et.get_metadata(image_file_path)[0]
        date_time_original = metadata["EXIF:DateTimeOriginal"]
        image_time_unix = get_image_time_unix(date_time_original)

        image_location = Location()
        image_location.timestamp = int(image_time_unix)
        approx_location = find_closest_location_in_time(locations_list, image_location)
        hours_away = abs(approx_location.timestamp - image_time_unix) / 3600

        if hours_away < hours_threshold:
            latitude, longitude = geotag_image(image_file_path, approx_location)
            print(
                f"{FAINT_TEXT}{num+1}/{len(image_files)} {RESET_FORMAT}{GREEN_TEXT}{BOLD_TEXT}Geotagged:{RESET_FORMAT}  {image_file} - {date_time_original} ({hours_away:.2f} hours away)     {latitude}, {longitude}"
            )
        else:
            print(
                f"{FAINT_TEXT}{num+1}/{len(image_files)} {RESET_FORMAT}{RED_TEXT}{BOLD_TEXT}Not geotagged.{RESET_FORMAT} {image_file} - {date_time_original} ({hours_away:.2f} hours away.)"
            )
