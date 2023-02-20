#!/usr/bin/python3
"""Download pack files from vanillatweaks.net."""

import argparse
import io
import json
import pathlib
import urllib.parse
import urllib.request
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--minecraft-version', type=str, default='1.19')
parser.add_argument('chroot_path', type=pathlib.Path)
args = parser.parse_args()

opener = urllib.request.build_opener()
opener.addheaders = [('User-agent', 'curl/Why is python blocked?!?'),
                     # This accept is unnecessary, but since I'm already messing with the headers I might as well add it.
                     ('Accept', 'application/json,application/zip')]
urllib.request.install_opener(opener)

lib_path = args.chroot_path / pathlib.Path('usr/lib/minecraft')
lib_path.mkdir(exist_ok=True)  # FIXME: parents=True? mode=???
download_path = lib_path / pathlib.Path('datapacks')
download_path.mkdir(exist_ok=True)  # FIXME: parents=True? mode=???

# ref: https://vanillatweaks.net/picker/datapacks/
datapacks_form_data = {'packs': json.dumps({
    "survival": [
        "unlock all recipes",
        "fast leaf decay",
        "villager workstation highlights",
        "nether portal coords",
        "track statistics",
        "custom nether portals",
        "cauldron concrete",
        "durability ping",
        "graves",
        "multiplayer sleep",
        "afk display",
        "armor statues",
    ],
    "items": [
        "player head drops",
        "armored elytra",
    ],
    "mobs": [
        "more mob heads",
        "silence mobs"
    ],
    "utilities": ["spawning spheres"],
    "experimental": [
        "elevators",
        "xp management",
    ]}),
    'version': 1.19}

with urllib.request.urlopen('https://vanillatweaks.net/assets/server/zipdatapacks.php',
                            data=urllib.parse.urlencode(datapacks_form_data).encode()) as json_req:
    json_response = json.load(json_req)
    with urllib.request.urlopen(urllib.parse.urljoin('https://vanillatweaks.net/', json_response['link'])) as zip_response:
        # Despite being kinda file-like, urllib.request.urlopen is not file-like enough for ZipFile,
        # so I'm cheating with StringIO here.
        zip_data = io.BytesIO(zip_response.read())
        zipfile.ZipFile(zip_data).extractall(download_path)

# ref: https://vanillatweaks.net/picker/crafting-tweaks/
craftingtweaks_form_data = {'packs': json.dumps({
    "craftables": [
        "craftable horse armor",
        "craftable notch apples",
        "craftable name tags",
        "craftable bundles leather",
        "craftable bundles rabbit hide",
    ],
    "quality-of-life": [
        "blackstone cobblestone",
        "universal dyeing",
        "rotten flesh to leather",
        "coal to black dye",
        "charcoal to black dye",
        "dropper to dispenser",
    ],
    "unpackables": [
        "unpackable wool",
        "unpackable ice",
    ]}),
    'version': 1.19}
with urllib.request.urlopen('https://vanillatweaks.net/assets/server/zipcraftingtweaks.php',
                            data=urllib.parse.urlencode(craftingtweaks_form_data).encode()) as json_req:
    json_response = json.load(json_req)
    # This one doesn't get unzipped... consistency's great ain't it?
    urllib.request.urlretrieve(urllib.parse.urljoin('https://vanillatweaks.net/', json_response['link']),
                               download_path / pathlib.Path('VanillaTweaks_CraftingTweaks.zip'))
