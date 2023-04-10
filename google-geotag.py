# Geotagging using Google location history.
#
# Command
# python google-geotag.py --dir {photos directory} --json {location history json file}
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
from fractions import Fraction
from typing import List, Tuple

import piexif
from PIL import Image
from PIL.JpegImagePlugin import JpegImageFile

# Print formatting
BOLD_TEXT = "\033[1m"
FAINT_TEXT = "\033[2m"
ITALIC_TEXT = "\033[3m"
UNDERLINE_TEXT = "\033[4m"
GREEN_TEXT = "\033[32m"
BLUE_TEXT = "\033[34m"
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


def find_closest_in_time(locations: List[Location], a_location: Location) -> Location:
    pos = bisect_left(locations, a_location)
    if pos == 0:
        return locations[0]
    if pos == len(locations):
        return locations[-1]
    before = locations[pos - 1]
    after = locations[pos]
    if after.timestamp - a_location.timestamp < a_location.timestamp - before.timestamp:
        return after
    else:
        return before


def to_deg(value, loc):
    """convert decimal coordinates into degrees, minutes and seconds tuple
    Keyword arguments: value is float gps-value, loc is direction list ["S", "N"] or ["W", "E"]
    return: tuple like (25, 13, 48.343 ,'N')
    """
    # 1. Check if the value is negative or positive
    if value < 0:
        loc_value = loc[0]
    elif value > 0:
        loc_value = loc[1]
    else:
        loc_value = ""

    # 2. Get the absolute value of the value
    abs_value = abs(value)
    deg = int(abs_value)

    # 3. Convert the value into minutes
    t1 = (abs_value - deg) * 60
    min = int(t1)
    sec = round((t1 - min) * 60, 5)

    # 4. Return the result
    return (deg, min, sec, loc_value)


def change_to_rational(number):
    """convert a number to rational
    Keyword arguments: number
    return: tuple like (1, 2), (numerator, denominator)
    """
    fraction = Fraction(str(number))
    return (fraction.numerator, fraction.denominator)


def get_image_time_unix(image: JpegImageFile) -> float:
    # Get image time from exif data: 36867 is the EXIF tag for DateTimeOriginal
    image_time_str = image._getexif()[36867]
    # converts the image time string into a time object
    image_time = datetime.strptime(image_time_str, "%Y:%m:%d %H:%M:%S")
    # converts the image time object into a unix time object
    return time.mktime(image_time.timetuple())


def geotag_image(
    image: JpegImageFile, image_file: str, approx_location: Location
) -> Tuple[Tuple[Tuple[float]]]:
    lat_f = float(approx_location.latitude) / 10000000.0
    lon_f = float(approx_location.longitude) / 10000000.0

    exif_dict = piexif.load(image_file)
    exif_dict["GPS"][piexif.GPSIFD.GPSVersionID] = (2, 0, 0, 0)
    exif_dict["GPS"][piexif.GPSIFD.GPSAltitudeRef] = (
        0 if approx_location.altitude > 0 else 1
    )
    exif_dict["GPS"][piexif.GPSIFD.GPSAltitude] = change_to_rational(
        abs(approx_location.altitude)
    )
    exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = "S" if lat_f < 0 else "N"
    exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = "W" if lon_f < 0 else "E"

    lat_deg = to_deg(lat_f, ["S", "N"])
    lng_deg = to_deg(lon_f, ["W", "E"])
    exif_lat = (
        change_to_rational(lat_deg[0]),
        change_to_rational(lat_deg[1]),
        change_to_rational(lat_deg[2]),
    )
    exif_lng = (
        change_to_rational(lng_deg[0]),
        change_to_rational(lng_deg[1]),
        change_to_rational(lng_deg[2]),
    )
    exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = exif_lat
    exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = exif_lng

    exif_bytes = piexif.dump(exif_dict)
    image.save(image_file, exif=exif_bytes)
    return (lat_f, lon_f)


def main():
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

    print(
        f"Loading location data ... {ITALIC_TEXT}{FAINT_TEXT}(can take a while){RESET_FORMAT}"
    )
    with open(locations_file) as f:
        location_data = json.load(f)

    print(
        f"{BLUE_TEXT}{BOLD_TEXT}{WHITE_BACKGROUND}Found {len(location_data['locations']):,} locations{RESET_FORMAT}",
        end="\n\n",
    )

    locations_list = [Location(location) for location in location_data["locations"]]

    included_extensions = ["jpg", "JPG", "jpeg", "JPEG"]
    image_files = [
        fn
        for fn in os.listdir(image_dir)
        if any(fn.endswith(ext) for ext in included_extensions)
    ]
    print(f"Selected {BLUE_TEXT}{len(image_files)}{RESET_FORMAT} images to geotag.")
    print(f"In the folder {BLUE_TEXT}{image_dir}{RESET_FORMAT}", end="\n\n")

    for image_file in image_files:
        image_file_path = os.path.join(image_dir, image_file)
        image = Image.open(image_file_path)
        image_time_unix = get_image_time_unix(image)

        image_location = Location()
        image_location.timestamp = int(image_time_unix)
        approx_location = find_closest_in_time(locations_list, image_location)
        hours_away = abs(approx_location.timestamp - image_time_unix) / 3600

        if hours_away < hours_threshold:
            exiv_lat, exiv_long = geotag_image(image, image_file_path, approx_location)
            print(
                f"{GREEN_TEXT}{BOLD_TEXT}Geotagged:{RESET_FORMAT}  {image_file} ({hours_away:.2f} hours away)     {exiv_lat}, {exiv_long}"
            )
        else:
            print(
                f"{RED_TEXT}{BOLD_TEXT}Not geotagged.{RESET_FORMAT} {image_file} ({hours_away:.2f} hours away.)"
            )


if __name__ == "__main__":
    main()
