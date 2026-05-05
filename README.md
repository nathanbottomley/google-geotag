# Google Geotag

A python command-line application for geotagging photos using Google Maps app location history exports and existing GPS data from nearby photos.

Export your location history JSON from the Google Maps app (Timeline).

## Requirements

- Python 3.9+ (for `zoneinfo`)
- ExifTool installed and available on PATH

## Usage

Geotag all photos in a directory using `location-history.json` in the project root:

    python google-geotag.py --dir {photos directory}

Optional flags:

- `--error_hours` (default: 1) allowed time difference for matching.
- `--timezone` manual timezone offset override in hours (e.g. `-5`, `1`). Omit to auto-detect from photo location, with daylight saving handled automatically.
- `--force` overwrite existing GPS data.
- `--dry-run` show what would change without writing to files.

## How it works

- Auto-detects each photo's timezone from the closest known location at the time it was taken (using offline timezone polygons), and uses Python's `zoneinfo` to convert local EXIF timestamps to UTC — so daylight saving and travel between zones are handled without you tracking them by hand.
- Matches each photo timestamp to the nearest coordinates in time from:
  - Google Maps app location history exports.
  - GPS data already embedded in other photos in the same folder.
- Skips photos that already have GPS data unless `--force` is used.
- Prints the detected starting timezone, and announces any zone transitions when you cross into a new one — so a single run can cover photos from different timezones in one folder.
- Groups consecutive untaggable photos into batches and asks once per batch how to resolve:
  - use coordinates of the previous photo
  - use coordinates of the subsequent photo
  - enter coordinates manually
  - skip geotagging
- Output includes aligned columns and a source tag (google, photo, previous photo, subsequent photo, manual).
