#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "colorama",
#     "platformdirs",
#     "requests",
#     "semver",
#     "toml",
# ]
# ///
"""
GitHub assets management

This script keeps an inventory of assets (currently GitHub release assets) in a
TOML file, resolves their versions (via GitHub API and semver ranges),
calculates file checksums, and downloads assets based on a JSON “lock” file.
"""

import argparse
import contextlib
import dataclasses
import fnmatch
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tarfile
import traceback
import zipfile
from pathlib import Path

import requests
import semver
import toml
from colorama import Fore, Style
from platformdirs import user_cache_dir

# CONSTANTS
CONFIG_FILE = Path("mazette.toml")
PYPROJECT_FILE = Path("pyproject.toml")
LOCK_FILE = Path("mazette.lock")
GITHUB_API_URL = "https://api.github.com"

# Determine the cache directory using platformdirs
CACHE_ROOT = Path(user_cache_dir("mazette"))


logger = logging.getLogger(__name__)


class AssetException(Exception):
    """Application error that can be printed to the terminal."""

    pass


@dataclasses.dataclass
class ExtractOpts:
    """Extract options for the asset, as taken from the lock file."""

    globs: list[str]
    filetype: str | None = None
    flatten: bool = False


@dataclasses.dataclass
class Asset:
    """Asset information, as taken from the lock file."""

    repo: str
    download_url: str
    version: str
    destination: str
    checksum: str
    executable: bool = False
    extract: ExtractOpts | bool | dict = False

    def __post_init__(self):
        self.destination = Path(self.destination)
        if isinstance(self.extract, dict):
            self.extract = ExtractOpts(**self.extract)


# HELPER FUNCTIONS
@contextlib.contextmanager
def report_error(verbose=False, fail=False):
    """Report errors in a more uniform way.

    Report errors to the user, based on their type and the log verbosity. In case of
    non-recoverable errors (defined by the user), exit with status code 1
    """
    try:
        yield
    except AssetException as e:
        if not verbose:
            print(f"{Fore.RED}{e}{Style.RESET_ALL}", file=sys.stderr)
        else:
            traceback.print_exception(e, chain=True)
    except Exception:
        logger.exception("An unknown error occurred:")
    else:
        return

    if fail:
        sys.exit(1)


def read_config():
    """
    Read the config for the mazette tool, either from its own configuration file
    (mazette.toml) or a [tool.mazette] section in pyproject.toml.
    """
    if CONFIG_FILE.exists():
        # First, attempt to read the configuration from mazette.toml.
        try:
            return toml.loads(CONFIG_FILE.read_text())
        except Exception as e:
            msg = f"Could not load configuration file '{CONFIG_FILE}': {e}"
            raise AssetException(msg) from e
    elif PYPROJECT_FILE.exists():
        # Then, attempt to read the configuration from pyproject.toml.
        try:
            config = toml.load(PYPROJECT_FILE.open("r"))
        except Exception as e:
            msg = f"Could not load configuration file '{PYPROJECT_FILE}': {e}"
            raise AssetException(msg) from e

        # If the pyproject.toml file does not have a [tool.mazette] section, return an
        # error.
        try:
            return config["tool"]["mazette"]
        except KeyError:
            raise AssetException(
                f"Missing a '[tool.mazette]' section in {PYPROJECT_FILE} or"
                f" a separate {CONFIG_FILE}"
            )

    raise AssetException(
        f"Missing a {PYPROJECT_FILE} with a '[tool.mazette]' or a separate"
        f" {CONFIG_FILE}"
    )


def write_lock(lock_data):
    config = read_config()
    config_hash = hashlib.sha256(json.dumps(config).encode()).hexdigest()
    lock_data["config_checksum"] = config_hash
    with open(LOCK_FILE, "w") as fp:
        json.dump(lock_data, fp, indent=2)


def check_lock_stale(lock):
    config = read_config()
    config_hash = hashlib.sha256(json.dumps(config).encode()).hexdigest()
    if config_hash != lock["config_checksum"]:
        raise AssetException(
            f"The asset list and the {LOCK_FILE} file are not in sync. This can be"
            " fixed by running the 'lock' command."
        )


def load_lock(check=True):
    try:
        with open(LOCK_FILE, "r") as fp:
            lock = json.load(fp)
            if check:
                check_lock_stale(lock)
            return lock
    except Exception as e:
        raise AssetException(f"Could not load lock file '{LOCK_FILE}': {e}") from e


def calc_checksum(stream):
    """
    Calculate a SHA256 hash of a binary stream by reading 1MiB intervals.
    """
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024**3), b""):
        h.update(chunk)
    return h.hexdigest()


def cache_file_path(url):
    """
    Generate a safe cache file path for a given URL, using the SHA-256 hash of the path,
    plus the asset name.
    """
    # Calculate a unique hash for this URL name, so that it doesn't clash with different
    # versions of the same asset.
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()

    # Get the name of the asset from the URL, which is typically the last part of the
    # URL. However, if the asset is the GitHub-generated zipball/tarball, use a
    # different naming scheme.
    parsed = url.split("/")
    if len(parsed) < 3:
        raise AssetException(f"Malformed download URL: {url}")

    asset_name = parsed[-1]
    if parsed[-2] in ("zipball", "tarball"):
        repo_name = parsed[-3]
        asset_type = parsed[-2]
        asset_name = f"{repo_name}-{asset_type}"

    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT / f"{url_hash}-{asset_name}"


def checksum_file_path(url):
    """Generate checksum filename for a given URL"""
    path = cache_file_path(url)
    return path.parent / (path.name + ".sha256")


def store_checksum_in_cache(url, checksum):
    """Store the checksum in a file whose name is based on the URL hash."""
    with open(checksum_file_path(url), "w") as fp:
        fp.write(checksum)


def read_checksum_from_cache(url):
    checksum_file = checksum_file_path(url)
    if checksum_file.exists():
        return checksum_file.read_text().strip()
    return None


def get_cached_url(url):
    """
    If the URL exists in our local cache, return the file path;
    otherwise return None.
    """
    file_path = cache_file_path(url)
    if file_path.exists():
        return file_path
    return None


def download_to_cache(url):
    """
    Download an asset from the given URL to the cache directory.
    If the asset already exists in the cache, return its path.
    Otherwise, download it, store a parallel .sha256 (with the computed hash)
    and return its path.
    """
    cached = get_cached_url(url)
    if cached:
        return cached

    logger.info(f"Downloading {url} into cache...")
    response = requests.get(url, stream=True)
    response.raise_for_status()

    cached = cache_file_path(url)
    with open(cached, "wb") as f:
        shutil.copyfileobj(response.raw, f)
    # Calculate and store checksum in cache
    with open(cached, "rb") as f:
        checksum = calc_checksum(f)
    store_checksum_in_cache(url, checksum)
    logger.debug("Download to cache completed.")
    return cached


def detect_platform():
    """Detect the platform that the script runs in"""
    # Return a string like 'windows/amd64' or 'linux/amd64' or 'darwin/amd64'
    os_name = platform.system().lower()
    machine = platform.machine().lower()
    # Normalize architecture names
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64"}.get(machine, machine)

    return f"{os_name}/{arch}"


def get_latest_release(repo, semver_range):
    """
    Query the GitHub API for repo releases, parse semver, and choose the
    latest release matching the given semver_range string (e.g., ">=1.0.1", "==1.2.2").
    """
    url = f"{GITHUB_API_URL}/repos/{repo}/releases"
    response = requests.get(url)
    response.raise_for_status()
    if response.status_code != 200:
        raise AssetException(
            f"Unexpected response when fetching releases for repo '{repo}': HTTP"
            f" {response.status_code}"
        )
    releases = response.json()

    matching = []
    for release in releases:
        tag = release.get("tag_name", "")
        version_str = tag.lstrip("v")

        # Attempt to parse asset version as semver. If the project has a tag that does
        # not conform to SemVer, just skip it.
        try:
            version = semver.VersionInfo.parse(version_str)
        except ValueError:
            logger.debug(
                f"Skipping non SemVer-compliant version '{version_str}' from repo"
                f" '{repo}'"
            )
            continue

        # Skip prereleases and non-matching versions
        if release["prerelease"]:
            logger.debug(
                f"Skipping prerelease version '{version_str}' from repo '{repo}'"
            )
            continue
        elif not version.match(semver_range):
            logger.debug(
                f"Skipping version '{version_str}' from repo '{repo}' because it does"
                f" not match the '{semver_range}' requirement"
            )
            continue
        matching.append((release, version))

    if not matching:
        raise AssetException(
            f"No releases match version requirement {semver_range} for repo '{repo}'"
        )

    return max(matching, key=lambda x: x[1])[0]


def get_download_url(release, name):
    """
    Given the release JSON and an asset name, find the asset download URL by matching
    filename.  If the asset name contains "{version}", it will be formatted using the
    release tag.
    """
    if name == "!tarball":
        return release["tarball_url"]
    elif name == "!zipball":
        return release["zipball_url"]

    # Format the name with the found version, in case it requires it.
    version = release.get("tag_name").lstrip("v")
    expected_name = name.format(version=version)

    assets = release.get("assets", [])
    for asset in assets:
        if asset.get("name") == expected_name:
            return asset.get("browser_download_url")

    raise AssetException(f"Could not find asset '{name}'")


def hash_asset(url):
    """
    Download the asset using caching and return its SHA256 checksum.
    The checksum is also stored in the cache as a .sha256 file.
    """
    # If we have downloaded the file and hashed it before, return the checksum
    # immediately.
    checksum_file = checksum_file_path(url)
    if checksum_file.exists():
        logger.debug(f"Using cached checksum for URL: {url}")
        with open(checksum_file, "r") as f:
            return f.read()

    # Else, download the file, hash it, and store the checksum in the cache.
    cached_file = download_to_cache(url)
    with open(cached_file, "rb") as f:
        checksum = calc_checksum(f)
    store_checksum_in_cache(url, checksum)
    return checksum


def download_to_cache_and_verify(url, expected_checksum):
    """
    Using caching, first download an asset to the cache dir.
    Verify its checksum against the expected_checksum.
    If they match, return the cached file.
    If not, remove the cached file and raise an exception.
    """
    cached_file = download_to_cache(url)
    checksum_file = checksum_file_path(url)
    with open(cached_file, "rb") as f:
        computed_checksum = calc_checksum(f)

    if computed_checksum != expected_checksum:
        # Remove cache file and its checksum file
        cached_file.unlink(missing_ok=True)
        checksum_file.unlink(missing_ok=True)
        raise AssetException(
            f"Hash mismatch for URL {url}: computed '{computed_checksum}',"
            f" expected '{expected_checksum}'"
        )
    return cached_file


def determine_extract_opts(extract):
    """
    Determine globs and flatten settings.
    """
    if isinstance(extract, dict):
        globs = extract.get("globs", ["*"])
        flatten = extract.get("flatten", False)
    elif isinstance(extract, list):
        globs = extract
        flatten = False
    elif isinstance(extract, bool):
        globs = ["*"]
        flatten = False
    else:
        raise AssetException(f"Unexpected format for 'extract' field: {extract}")

    return {
        "globs": globs,
        "flatten": flatten,
    }


def filter_files(files, globs):
    """Filter filenames against a list of globs.

    Filtering a list of filenames (N) against a list of globs (k) can be seen as
    quadratic in nature, if k is as large as N. In most cases though, we expect that
    users will pass a small number of globs, and therefore the main slowdown of this
    function will be pattern-matching the list of files in an archive against a few
    globs.

    To make things faster, this function uses `fnmatch.filter()` [1] for a more
    efficient pattern-matching against the list of provided files. Whenever we have a
    match, we remove it from the list of files, so that we don't return duplicates, and
    to speedup the operation.

    [1] https://docs.python.org/3/library/fnmatch.html#fnmatch.filter
    """
    files = set(files)
    matched = False

    for glob in globs:
        for m in fnmatch.filter(files, glob):
            matched = True
            files.remove(m)
            yield m

    if not matched:
        raise AssetException("Globs did not match any files in the archive")


def detect_archive_type(name):
    """
    Detect the filetype of the archive based on its name.
    """
    if name.endswith(".tar.gz") or name.endswith(".tgz") or name == "!tarball":
        return "tar.gz"
    if name.endswith(".tar"):
        return "tar"
    if name.endswith(".zip") or name == "!zipball":
        return "zip"
    raise AssetException(f"Unsupported archive type for extraction: {name}")


def flatten_extracted_files(destination):
    """
    After extraction, move all files found in subdirectories of destination into
    destination root.
    """
    for root, dirs, files in os.walk(destination):
        # Skip the root directory itself
        if Path(root) == destination:
            continue
        for file in files:
            src_file = Path(root) / file
            dst_file = destination / file
            # If a file with the same name exists, we can overwrite or rename.
            shutil.move(str(src_file), str(dst_file))
    # Optionally, remove now-empty subdirectories.
    for root, dirs, files in os.walk(destination, topdown=False):
        for d in dirs:
            dir_path = Path(root) / d
            try:
                dir_path.rmdir()
            except OSError:
                pass


def extract_asset(archive_path, destination, filetype, globs=(), flatten=False):
    """
    Extract the asset from archive_path to destination.

    Also accepts the following options:
    * 'filetype': The type of the archive, which indicates how it will get extracted.
    * 'globs': A list of patterns that will be used to match members in the archive.
      If a member does not match a pattern, it will not be extracted.
    * 'flatten': A boolean value. If true, after extraction, move all files to the
       destination root.

    For tarfiles, use filter="data" when extracting to mitigate malicious tar entries.
    """
    logger.info(f"Extracting '{archive_path}' to '{destination}'...")

    try:
        if filetype in ("tar.gz", "tar"):
            mode = "r:gz" if filetype == "tar.gz" else "r"
            with tarfile.open(archive_path, mode) as tar:
                members = filter_files({m.name for m in tar.getmembers()}, globs)
                tar.extractall(path=destination, members=members, filter="data")
        elif filetype == "zip":
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                members = filter_files(zip_ref.namelist(), globs)
                zip_ref.extractall(path=destination, members=members)
        else:
            raise AssetException(f"Unsupported archive type: {filetype}")
    except Exception as e:
        raise AssetException(f"Error extracting '{archive_path}': {e}") from e

    if flatten:
        flatten_extracted_files(destination)

    logger.debug(f"Successfully extracted '{archive_path}'")


def get_platform_assets(assets, platform):
    """
    List the assets that are associated with a specific platform.
    """

    plat_assets = {}
    for asset_name, asset_entry in assets.items():
        if platform in asset_entry:
            plat_assets[asset_name] = asset_entry[platform]
        elif "all" in asset_entry:
            plat_assets[asset_name] = asset_entry["all"]
    return plat_assets


def chmod_exec(path):
    if path.is_dir():
        for root, _, files in path.walk():
            for name in files:
                f = root / name
                f.chmod(f.stat().st_mode | 0o111)
    else:
        path.chmod(path.stat().st_mode | 0o111)


def compute_asset_lock(asset_name, asset):
    try:
        repo = asset["repo"]
        version_range = asset["version"]
        asset_map = asset["platform"]  # mapping platform -> asset file name
        destination_str = asset["destination"]
        executable = asset.get("executable", False)
        extract = asset.get("extract", False)
    except KeyError as e:
        raise AssetException(f"Required field {e} is missing")

    if extract:
        extract = determine_extract_opts(extract)

    logger.debug(
        f"Fetching a release that satisfies version range '{version_range}' for repo"
        f" '{repo}'"
    )
    release = get_latest_release(repo, version_range)
    version = release["tag_name"].lstrip("v")
    logger.debug(f"Found release '{version}' for repo '{repo}'")

    asset_lock_data = {}
    # Process each defined platform key in the asset_map
    for plat_key, plat_name in asset_map.items():
        logger.debug(f"Getting download URL for asset '{asset_name}' of repo '{repo}'")
        download_url = get_download_url(release, plat_name)
        logger.debug(f"Found download URL: {download_url}")

        if extract:
            extract = extract.copy()
            extract["filetype"] = detect_archive_type(plat_name)

        logger.info(
            f"Hashing asset '{asset_name}' of repo '{repo}' for platform"
            f" '{plat_key}'..."
        )
        checksum = hash_asset(download_url)
        logger.debug(f"Computed the following SHA-256 checksum: {checksum}")
        asset_lock_data[plat_key] = {
            "repo": repo,
            "download_url": download_url,
            "version": version,
            "checksum": checksum,
            "executable": executable,
            "destination": destination_str,
            "extract": extract,
        }

    return asset_lock_data


def install_asset(name, platform, asset_dict):
    # If an asset entry contains "platform.all", then we should fallback to that, if
    # the specific platform we're looking for is not defined.
    if platform not in asset_dict:
        if "all" in asset_dict:
            platform = "all"
        else:
            raise AssetException(
                f"No entry for platform '{platform}' or 'platform.all'"
            )

    asset = Asset(**asset_dict[platform])

    logger.debug(
        f"Downloading asset '{name}' with URL '{asset.download_url}' and"
        f" verifying its checksum matches '{asset.checksum}'..."
    )
    cached_file = download_to_cache_and_verify(asset.download_url, asset.checksum)
    # Remove destination if it exists already.
    if asset.destination.exists():
        logger.debug(
            f"Removing destination path '{asset.destination}' of asset '{name}'"
        )
        if asset.destination.is_dir():
            shutil.rmtree(asset.destination)
        else:
            asset.destination.unlink()
    # If extraction is requested
    if asset.extract:
        logger.debug(f"Extracting asset '{name}' to '{asset.destination}'")
        asset.destination.mkdir(parents=True, exist_ok=True)
        filename = asset.download_url.split("/")[-1]
        extract_asset(
            cached_file, asset.destination, **dataclasses.asdict(asset.extract)
        )
    else:
        logger.debug(f"Copying asset '{name}' to '{asset.destination}'")
        asset.destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached_file, asset.destination)
        if asset.executable:
            logger.debug(f"Marking '{asset.destination}' as executable")
            chmod_exec(asset.destination)


# COMMAND FUNCTIONS
def cmd_lock(args):
    """
    Reads the configuration file, queries GitHub for each asset to determine
    the actual release and asset URL, calculates checksum if the asset exists locally.
    Then outputs/updates the lock file (in JSON format).

    Changes:
      - Uses "destination" instead of "download_path" in the config.
      - Uses caching when fetching/hashing assets.
      - Renders asset filenames if they contain {version}.
      - Supports a 'platform.all' key for platform-agnostic assets.
    """
    config = read_config()
    lock = {"assets": {}}
    # Expected config structure in config.toml:
    # [asset.<asset_name>]
    #   repo = "owner/repo"
    #   version = ">=1.0.1"  # semver expression
    #   platform."windows/amd64" = "asset-windows.exe"
    #   platform."linux/amd64"   = "asset-linux"
    #   platform.all             = "universal-asset.zip"
    #   executable = true|false  # whether to mark downloaded file as executable
    #   destination = "./downloads/asset.exe"
    #   extract = either false, a list of globs,
    #             or a table with keys: globs = ["glob1", "glob2"]
    #             and flatten = True|False.
    assets_cfg = config.get("asset", {})
    if not assets_cfg:
        raise AssetException(
            "No assets defined under the [asset] section in the config file."
        )

    lock_assets = lock["assets"]
    for asset_name, asset in assets_cfg.items():
        print(f"Processing asset '{asset_name}'...")
        try:
            lock_assets[asset_name] = compute_asset_lock(asset_name, asset)
        except Exception as e:
            raise AssetException(
                f"Error when processing asset '{asset_name}': {e}"
            ) from e
        logger.debug(f"Successfully processed asset '{asset_name}'")

    write_lock(lock)
    print(f"Lock file '{LOCK_FILE}' updated.")


def cmd_install(args):
    """
    Install assets based on the lock file. Accepts an optional platform argument
    to limit downloads and an optional list of asset names.

    Features:
      - Uses caching: downloads happen into the cache, then verified against the expected hash,
        and finally copied to the destination.
      - If executable field is set, mark the downloaded file(s) as executable.
      - If extract field is set:
          o If False or missing: no extraction, just copy.
          o Otherwise, if extract is set:
               - If extract is a list, treat it as a list of globs.
               - If extract is a table, expect keys "globs" and optional "flatten".
      - For platform-agnostic assets, an entry with key "platform.all" is used if the requested
        platform is not found.
    """
    lock = load_lock()
    target_plat = args.platform
    logger.debug(f"Target platform: {target_plat}")
    lock_assets = lock["assets"]
    asset_list = (
        args.assets
        if args.assets
        else get_platform_assets(lock_assets, target_plat).keys()
    )

    # Validate asset names and platform entries
    for asset_name in asset_list:
        if asset_name not in lock_assets:
            raise AssetException(f"Asset '{asset_name}' not found in the lock file.")

        print(f"Installing asset '{asset_name}'...")
        asset = lock_assets[asset_name]
        try:
            install_asset(asset_name, target_plat, asset)
        except Exception as e:
            raise AssetException(
                f"Error when installing asset '{asset_name}': {e}"
            ) from e
        logger.debug(f"Successfully installed asset '{asset_name}'")

    print(f"Installed {len(asset_list)} assets.")


def cmd_list(args):
    """
    List assets and their versions based on the lock file and the provided platform. If
    a platform is not provided, list assets for the current one.
    """
    lock = load_lock()
    target_plat = args.platform if args.platform else detect_platform()
    assets = get_platform_assets(lock["assets"], target_plat)
    for asset_name in sorted(assets.keys()):
        asset = assets[asset_name]
        print(f"{asset_name} {asset['version']} {asset['download_url']}")
        logger.debug(f"Full asset details: {asset}")


def parse_args():
    parser = argparse.ArgumentParser(description="GitHub asset management")
    subparsers = parser.add_subparsers(dest="command", required=True)

    lock_parser = subparsers.add_parser("lock", help="Update lock file from config")
    lock_parser.set_defaults(func=cmd_lock)

    install_parser = subparsers.add_parser(
        "install", help="Install assets as per lock file"
    )
    install_parser.add_argument(
        "assets",
        nargs="*",
        help="Specific asset names to download. If omitted, download all assets.",
    )
    install_parser.set_defaults(func=cmd_install)

    list_parser = subparsers.add_parser("list", help="List assets for a platform")
    list_parser.set_defaults(func=cmd_list)

    # Add common arguments.
    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "-p",
            "--platform",
            default=detect_platform(),
            help=(
                "The platform to choose when determining which assets to work"
                " on. Examples: windows/amd64, linux/amd64, darwin/amd64, darwin/arm64."
                " Defaults to the current platform if not provided (%(default)s)."
            ),
        )
        subparser.add_argument(
            "-v",
            "--verbose",
            action="count",
            default=0,
            help="Enable verbose logging",
        )
        subparser.add_argument(
            "-d",
            "--directory",
            help=(
                "The working directory for the script (defaults to the current working"
                " directory)"
            ),
        )

    args = parser.parse_args()
    return args


def setup_logging(verbose=False):
    """Simple way to setup logging.

    Copied from: https://docs.python.org/3/howto/logging.html
    """
    # specify level
    if not verbose:
        lvl = logging.WARN
    elif verbose == 1:
        lvl = logging.INFO
    else:
        lvl = logging.DEBUG

    logging.basicConfig(
        level=lvl,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    args = parse_args()
    setup_logging(args.verbose)

    with report_error(verbose=args.verbose, fail=False):
        if args.directory:
            logger.info(f"Changing current working dir to '{args.directory}'")
            os.chdir(args.directory)
        return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
