#!/usr/bin/python3
"""Get assets from a Github repo's latest release."""
import sys
import urllib.request
import pathlib
import hashlib
import shutil
import subprocess

import github


BOOT_PATH = pathlib.Path('.').resolve()
GITHUB_REPO = sys.argv[1]


def get_currently_booted_soe():
    """Get the currently booted SOE version."""
    # Sanity-check that the loopback device is mounted where it should be.
    loop_mounts = [line.split() for line in pathlib.Path('/proc/mounts').read_text().splitlines()
                   if line.startswith('/dev/loop0 ')]
    assert all(mp in ('/run/live/rootfs/filesystem.squashfs', '/lib/live/mount/rootfs/filesystem.squashfs') and t == 'squashfs'
               for d, mp, t, o, _, _, in loop_mounts), "Unexpected loopback mount(s)"

    # Check what squashfs file the loopback device is actually attached to.
    loop = subprocess.check_output(['losetup', '/dev/loop0'], text=True).strip().split()
    if loop[-1] == '(deleted))':
        # Well it's already been deleted somehow, just accept that and carry-on
        return None

    assert len(loop) == 3 and loop[0] == '/dev/loop0:', "Unexpected losetup output"
    current_squashfs = pathlib.Path(loop[2].strip('()'))
    assert current_squashfs.exists(), "Current squashfs doesn't exist"

    return current_squashfs.parent.name


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
    hash_algo = hash_filename[:-4].lower()  # Just remove the 'SUMS' from the end

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

    hash_data = {name.strip(): hash_sum for hash_sum, name in (
        line.decode(hash_file.headers.get_content_charset() or 'utf-8').split(maxsplit=1) for line in hash_file.readlines())}

    for filename in assets:
        asset = assets[filename]
        if filename in hash_data:
            if hash_algo == 'sha3':
                # Not sure if we should be allowing multiple different hash lengths in one file, but we do
                assets[filename].hash_func = getattr(hashlib, 'sha3_' + str(len(hash_data[filename]) * 4))
            else:
                assets[filename].hash_func = hash_func
            assets[filename].hash = hash_data[filename]
        yield asset


def maybe_get_new_assets(repo_name: str, old_dir: pathlib.Path, new_dir: pathlib.Path):
    """
    Check if the assets in old_dir need an update and updates them into new_dir accordingly.

    Returns the list of filenames that were updated
    """
    if not new_dir.exists():
        new_dir.mkdir()

    assets_updated = []
    for asset in get_repo_latest_assets(repo_name):
        if not hasattr(asset, 'hash'):
            # We can't confirm the hash of this file, so we don't even bother with it.
            # It might be the SUMS file itself.
            # FIXME: I actually want to keep these, but only if we other assets got updated too
            # FIXME: What if it is a detached signature?
            #        and not (asset.name.endswith('.asc') or asset.name.endswith('.sig'))
            print('Skipping', asset.name)
            continue

        old_asset = old_dir / asset.name
        new_asset = new_dir / asset.name
        if new_asset.exists() and asset.hash_func(new_asset.read_bytes()).hexdigest() == asset.hash:
            # Exists in pending, probably crashed mid-update
            print(asset.name, "already pending, and hashes match. Skipping, but still adding to queue")
            assets_updated.append(asset.name)
        elif old_asset.exists() and asset.hash_func(old_asset.read_bytes()).hexdigest() == asset.hash:
            # Exists in latest, as expected
            print(asset.name, "exists in latest, and hashes match. Skipping")
        else:
            # Hash mismatch or doesn't exist
            print(asset.name, "hash mismatch or doesn't exist. Downloading new")

            # FIXME: Can we do anything like an rsync update here?
            # FIXME: Use the reporthook= arg and do some sort of progress bar, only if stdout is a TTY
            urllib.request.urlretrieve(asset.browser_download_url, new_asset)
            assets_updated.append(asset.name)

    return assets_updated


def increment_stored_releases(previous: pathlib.Path, latest: pathlib.Path, pending: pathlib.Path):
    """
    Increment the SOE versions so that we always have only previous & latest.

    This is done by copying all of latest/* to pending/ without overwriting,
    then moving latest to previous (if we're not currently running previous)
    and moving pending to latest.

    This would be better/easier if we were using a filesystem capable of copy-on-write,
    or even just hardlinks. However I want this on the ESP directly, so we can't do that.
    """
    assert previous.is_dir() and latest.is_dir() and pending.is_dir()

    # Copy latest to pending
    shutil.copytree(latest, pending, dirs_exist_ok=True,
                    # Ignore files already in pending
                    ignore=lambda src, names: (path.name for path in pending.iterdir()))

    if get_currently_booted_soe() == previous.name:
        # Don't update previous if we're still running that since the last update
        print("Ignoring previous as it is what we are currently running")
        shutil.rmtree(latest)
    else:
        # Otherwise, move latest to previous just in case the new version doesn't boot
        print("Moving latest to previous")
        shutil.rmtree(previous)
        latest.rename(previous)

    print("Moving pending to latest")
    pending.rename(latest)


if maybe_get_new_assets(GITHUB_REPO, BOOT_PATH / 'latest', BOOT_PATH / 'pending'):
    increment_stored_releases(BOOT_PATH / 'previous', BOOT_PATH / 'latest', BOOT_PATH / 'pending')
