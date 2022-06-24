#!/usr/bin/python3
"""Get assets from a Github repo's latest release."""
import sys
import urllib.request
import pathlib
import hashlib

import github


BOOT_PATH = pathlib.Path('.').resolve()
GITHUB_REPO = sys.argv[1]


def get_repo_latest_assets(repo_name):
    """Get all asset URLs for the latest release in the given repo."""
    g = github.Github()
    repo = g.get_repo(repo_name)
    release = repo.get_latest_release()
    assets = {a.name: a for a in release.get_assets()}

    # Add check sums to each asset object
    # Depends on a B2SUMS/SHA3SUMS/SHA512SUMS file being included in the assets.
    # FIXME: Why the fuck don't Github do this? Is there a better way for me to do this?
    # FIXME: Should we check signatures/etc here?
    hash_filename, = [name for name in assets if name.endswith('SUMS')]
    hash_file = urllib.request.urlopen(assets[hash_filename].browser_download_url)
    hash_data = {name.strip(): hash_sum for hash_sum, name in (
        line.decode(hash_file.headers.get_content_charset() or 'utf-8').split(maxsplit=1) for line in hash_file.readlines())}
    hash_algo = hash_filename[:-4].lower()  # Just remove the 'SUMS' from the end

    print(hash_algo, hash_data)

    if hash_algo == 'b2':
        # blake2b is the default for b2sum command, and is more efficient on 64-bit while blake2s is more efficient on 8/16/32-bit
        hash_algo = 'blake2b'
        hash_func = getattr(hashlib, hash_algo)
    elif hash_algo == 'sha3':
        # This one is slightly unique in that there's a different function for each hash size
        # so just kinda do nothing with it here and figure that out later when we have the actual hash lengths
        pass
    else:
        hash_func = getattr(hashlib, hash_algo)

    for filename in hash_data:
        asset = assets[filename]
        if hash_algo == 'sha3':
            # Not sure if we should be allowing multiple different hash lengths in one file, but we do
            assets[filename].hash_func = getattr(hashlib, 'sha3_' + str(len(hash_data[filename]) * 4))
        else:
            assets[filename].hash_func = hash_func
        assets[filename].hash = hash_data[filename]
        yield asset


def maybe_update_assets(repo_name: str, dest_dir: pathlib.Path):
    """
    Check if the assets in dest_dir need an update and updates them accordingly.

    Returns the number of assets that were updated
    """
    num_assets_updated = 0
    for asset in get_repo_latest_assets(repo_name):
        if not hasattr(asset, 'hash'):
            # We can't confirm the hash of this file, so we don't even bother with it.
            # It's probably he SUMS file itself.
            # FIXME: What if it is a detached signature?
            #        and not (asset.name.endswith('.asc') or asset.name.endswith('.sig'))
            continue

        asset_path = dest_dir / asset.name
        if asset_path.exists():
            checked_hash = asset.hash_func(asset_path.read_bytes()).hexdigest()
            if checked_hash != asset.hash:
                print(asset.name, "doesn't match, unlinking.")
                asset_path.unlink()
            else:
                print(asset.name, "all good!")

        if not asset_path.exists():
            print(asset.name, "downloading...")
            # FIXME: Use the reporthook= arg and do some sort of progress bar
            urllib.request.urlretrieve(asset.browser_download_url, dest_dir / asset.name)

            num_assets_updated += 1

    return num_assets_updated


def increment_stored_releases(boot_dir: pathlib.Path):
    """
    Move latest to previous, and link pending into latest.

    This is trying to do a kind of copy-on-write type thing,
    for when using simpler FAT filesystems that don't support it at the fs level.
    """
    previous = (boot_dir / 'previous')
    latest = (boot_dir / 'latest')
    pending = (boot_dir / 'pending')
    assert previous.is_dir() and latest.is_dir() and pending.is_dir()

    # Delete all of 'previous'
    # FIXME: 'previous' should actually be more like "last successful". Currently it's always 1 version behind latest
    for previous_asset in previous.iterdir():
        previous_asset.unlink()

    # Link all of 'latest' into 'previous'
    for latest_asset in latest.iterdir():
        latest_asset.link_to(previous / latest_asset.name)

    # Move all of 'pending' into 'latest' and immediately hardlink it back into 'pending'
    # This is mostly only becuse link_to won't overwrite the target,
    # so if I delete the target then do link_to there will be a small window where the file doesn't exist and the system won't boot
    for pending_asset in pending.iterdir():
        latest_asset = latest / pending_asset.name
        if not (latest_asset.exists() and pending_asset.samefile(latest_asset)):
            # Surprisingly replace() seems to be a no-op if the files are already the same hardlink anyway,
            # which in-turn causes link_to to fail, so just don't do that
            pending_asset.replace(latest_asset)
            latest_asset.link_to(pending_asset)

    # Delete any assets leftover in 'latest' which are not in 'pending'
    for latest_asset in latest.iterdir():
        if (pending / latest_asset.name) not in pending.iterdir():
            latest_asset.unlink()


if maybe_update_assets(GITHUB_REPO, BOOT_PATH / 'pending'):
    increment_stored_releases(BOOT_PATH)
