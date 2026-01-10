# Google Geotag

A python command-line application for geotagging photos using Google Maps app location history exports and existing GPS data from nearby photos.

Export your location history JSON from the Google Maps app (Timeline).

## Requirements

- Python 3
- ExifTool installed and available on PATH

## Usage

Geotag all photos in a directory using `location-history.json` in the project root:

    python google-geotag.py --dir {photos directory}

Optional flags:

- `--error_hours` (default: 1) allowed time difference for matching.
- `--timezone` adjust photo time by hours to match your timezone.
- `--force` overwrite existing GPS data.
- `--dry-run` show what would change without writing to files.

## How it works

- Matches each photo timestamp to the nearest coordinates in time from:
  - Google Maps app location history exports.
  - GPS data already embedded in other photos in the same folder.
- Skips photos that already have GPS data unless `--force` is used.
- Groups consecutive untaggable photos into batches and asks once per batch how to resolve:
  - use coordinates of the previous photo
  - use coordinates of the subsequent photo
  - enter coordinates manually
  - skip geotagging
- Output includes aligned columns and a source tag (google, photo, previous photo, subsequent photo, manual).
