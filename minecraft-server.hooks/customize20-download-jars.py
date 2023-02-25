#!/usr/bin/python3
"""Download the jar files for Minecraft and various plugins."""
import argparse
import json
import pathlib
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--minecraft-version', type=str, default='1.19.3')
parser.add_argument('chroot_path', type=pathlib.Path)
args = parser.parse_args()

lib_path = args.chroot_path / pathlib.Path('usr/lib/minecraft')
lib_path.mkdir(exist_ok=True)  # FIXME: parents=True? mode=???
download_path = lib_path / pathlib.Path('plugins')
download_path.mkdir(exist_ok=True)  # FIXME: parents=True? mode=???

# Some things were blocking Python's user agent, but not Curl's? Why?!?
opener = urllib.request.build_opener()
opener.addheaders = [('User-agent', 'curl/Why is python blocked?!?'),
                     # This accept is unnecessary, but since I'm already messing with the headers I might as well add it.
                     ('Accept', 'application/json,application/java-archive')]
urllib.request.install_opener(opener)

# Papermc (bukkit/spigot alternative) #
# NOTE: This is a bootstrap that I believe will download upstream's minecraft.jar on first startup
print('Downloading paperclip.jar')
papermc_url = 'https://api.papermc.io/v2/projects/paper/versions/'
# FIXME: Is this considering snapshot vs. stable releases?
papermc_releases = json.load(urllib.request.urlopen(
    urllib.parse.urljoin(papermc_url, args.minecraft_version)))
assert papermc_releases['version'] == args.minecraft_version
urllib.request.urlretrieve(urllib.parse.urljoin(papermc_url,
                                                f'{args.minecraft_version}/'
                                                'builds/'
                                                f"{papermc_releases['builds'][-1]}/"
                                                'downloads/'
                                                f"paper-{args.minecraft_version}-{papermc_releases['builds'][-1]}.jar"),
                           lib_path / 'paperclip.jar')

# Geyser & Floodgate (Bedrock compatibility plugin) #
print('Downloading Geyser & Floodgate')
urllib.request.urlretrieve('https://ci.opencollab.dev/job/GeyserMC/job/Geyser/job/master/lastSuccessfulBuild/artifact/'
                           'bootstrap/spigot/build/libs/Geyser-Spigot.jar',
                           download_path / 'Geyser-Spigot.jar')
urllib.request.urlretrieve('https://ci.opencollab.dev/job/GeyserMC/job/Floodgate/job/master/lastSuccessfulBuild/artifact/'
                           'spigot/build/libs/floodgate-spigot.jar',
                           download_path / 'floodgate-spigot.jar')

# Discord integration #
print('Downloading DiscordSRV')
discordsrv_release = json.load(urllib.request.urlopen('https://api.github.com/repos/DiscordSRV/DiscordSRV/releases/latest'))
discordsrv_jar_assets = [a for a in discordsrv_release['assets'] if a['name'].endswith('.jar')]
assert len(discordsrv_jar_assets) == 1
urllib.request.urlretrieve(discordsrv_jar_assets[0]['browser_download_url'], download_path / 'DiscordSRV.jar')

# A few random plugins I liked #
# From bukkit.org
print('Downloading Mini Blocks')
urllib.request.urlretrieve('https://dev.bukkit.org/projects/mini-blocks/files/latest',
                           download_path / 'mini-blocks.jar')
# FIXME: Temporarily disabled due to a bug that is not fixed in this particular version
# print('Downloading Dynmap')
# urllib.request.urlretrieve('https://dev.bukkit.org/projects/dynmap/files/latest',
#                            download_path / 'dynmap.jar')
print('Downloading Chunky')
urllib.request.urlretrieve('https://dev.bukkit.org/projects/chunky-pregenerator/files/latest',
                           download_path / 'chunky-pregenerator.jar')

# From spigotmv.org
spigotmc_downloader_api_endpoint = 'https://api.spiget.org/v2/resources/{resource_id}/download'
# NOTE: There's very little need for the names here, but they serve as a bit of documentation too
for resource in ['chestsort-api.59773',  # https://www.spigotmc.org/resources/chestsort-api.59773
                 'overleveled-enchanter.93379',  # https://www.spigotmc.org/resources/overleveled-enchanter.93379
                 'view-distance-tweaks-1-14-1-17.75164',  # https://www.spigotmc.org/resources/view-distance-tweaks-1-14-1-17.75164
                 # Why is this one 404ing? This one's kinda important, but doing it manually worked. :shrug:
                 # It hasn't been updated in ~3yrs, so I'd say doing this one manually is fine, it's not likely to update any time soon.
                 # 'customcommandprefix.87224',  # https://www.spigotmc.org/resources/customcommandprefix.87224
                 'petting.74710',  # https://www.spigotmc.org/resources/petting.74710
                 'bsb-better-shulker-boxes-1-13-1-19-2.58837',  # https://www.spigotmc.org/resources/bsb-better-shulker-boxes-1-13-1-19-2.58837/
                 ]:
    print('Downloading', ' '.join(resource.partition('.')[0].split('-')).title())
    urllib.request.urlretrieve(
        spigotmc_downloader_api_endpoint.format(resource_id=resource.partition('.')[-1]),
        download_path / f"{resource.partition('.')[0]}.jar")
