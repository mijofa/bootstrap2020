#!/usr/bin/python3
"""Get assets from a Github repo's latest release."""
import urllib.request
import pathlib
import hashlib

import github


def get_repo_latest_assets(repo_name):
    """Get all asset URLs for the latest release in the given repo."""
    g = github.Github()
    repo = g.get_repo(repo_name)
    release = repo.get_latest_release()
    assets = {a.name: a for a in release.get_assets()}

    if 'sha512sums.txt' in assets:
        # Add sha512sums to each asset object
        # Depends on a sha512sums.txt file being included in the assets,
        # because Github don't provide that info as part of the API.
        # FIXME: Why the fuck don't Github do this? Is there a better way for me to do this?
        # FIXME: Automatically detect other hashing algorithm files and use them if available.
        req = urllib.request.urlopen(assets['sha512sums.txt'].browser_download_url)
        for line in req.readlines():
            # The HTTP server doesn't always give us a charset, so fallback on UTF-8 when that's the case.
            # FIXME: Should we fallback on ASCII instead?
            asset_sha512sum, name = line.decode(req.headers.get_content_charset() or 'utf-8').split()
            asset = assets[name]
            assets[name].sha512sum = asset_sha512sum
            yield asset


def maybe_update_assets(repo_name: str, dest_dir: pathlib.Path):
    """
    Check if the assets in dest_dir need an update and updates them accordingly.

    Returns the number of assets that were updated
    """
    num_assets_updated = 0
    for asset in get_repo_latest_assets(repo_name):
        asset_path = dest_dir / asset.name
        if asset_path.exists():
            with asset_path.open('rb') as downloaded_asset:
                checked_sha512sum = hashlib.sha512(downloaded_asset.read()).hexdigest()
                if checked_sha512sum != asset.sha512sum:
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
    """Move latest to previous, and link next into latest."""
    previous = (boot_dir / 'previous')
    latest = (boot_dir / 'latest')
    pending = (boot_dir / 'pending')
    assert previous.is_dir() and latest.is_dir() and pending.is_dir()

    # Delete all of 'previous'
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


if maybe_update_assets('mijofa/bootstrap2020', pathlib.Path('/home/mike/tmp/pending')):
    increment_stored_releases(pathlib.Path('/home/mike/tmp'))
