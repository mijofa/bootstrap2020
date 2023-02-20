#!/usr/bin/python3
"""Download client pack files from vanillatweaks.net."""

import argparse
import json
import pathlib
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--minecraft-version', type=str, default='1.19')
parser.add_argument('chroot_path', type=pathlib.Path, default='/tmp/')
args = parser.parse_args()

opener = urllib.request.build_opener()
opener.addheaders = [('User-agent', 'curl/Why is python blocked?!?'),
                     # This accept is unnecessary, but since I'm already messing with the headers I might as well add it.
                     ('Accept', 'application/json,application/zip')]
urllib.request.install_opener(opener)

download_path = args.chroot_path / pathlib.Path('resourcepacks')
# download_path.mkdir(exist_ok=True)

# ref: https://vanillatweaks.net/picker/resource-packs/
resourcepacks_form_data = {'packs': json.dumps({
    "aesthetic": [
        "AccurateSpyglass",
        "SplashXpBottle",
        "UniqueDyes",
        "PinkEndRods",
        "WarmGlow",
        "FlintTippedArrows",
        "HDShieldBanners",
        "RedIronGolemFlowers",
        "BetterParticles",
        "DifferentStems",
        "AlternateBlockDestruction"],
    "connected-textures": [
        "ConnectedIronBlocks",
        "ConnectedLapisBlocks",
        "ConnectedPolishedStones"],
    "3d": [
        "3DBookshelves",
        "3DStonecutters",
        "3DGlowLichen",
        "3DVines",
        "3DMushrooms",
        "3DTrapdoors",
        "3DLadders",
        "3DRails",
        "3DSugarcane",
        "3DIronBars",
        "3DLilyPads",
        "3DDoors",
        "3DTiles",
        "3DRedstoneWire",
        "3DChains"],
    "hud": [
        "WitherHearts",
        "PingColorIndicator",
        "RainbowExperience"],
    "gui": [
        "DarkUI1193",
        "NumberedHotbar"],
    "unobtrusive": [
        "UnobtrusiveSnow",
        "UnobtrusiveRain",
        "ShortSwords",
        "SmallerUtilities",
        "NoFog",
        "TransparentPumpkin",
        "TransparentSpyglassOverlay",
        "LowerFire",
        "AlternateEnchantGlint",
        "UnobtrusiveScaffolding",
        "BorderlessTintedGlass",
        "BorderlessStainedGlass",
        "BorderlessGlass",
        "LowerShield",
        "UnobtrusiveParticles"],
    "fixes": [
        "CloudFogFix",
        "CatFix",
        "HopperBottomFix",
        "ItemHoldFix",
        "DoubleSlabFix",
        "BlazeFix",
        "SoulSoilSoulCampfire",
        "PixelConsistentGuardianBeam",
        "PixelConsistentSonicBoom",
        "PixelConsistentBeaconBeam",
        "PixelConsistentXPOrbs",
        "PixelConsistentSigns",
        "TripwireHookFix",
        "PixelConsistentWither",
        "PixelConsistentElderGuardian",
        "PixelConsistentGhast",
        "PixelConsistentBat",
        "CactusBottomFix",
        "ConsistentTadpoleBucket",
        "ConsistentUIFix",
        "ConsistentBucketFix",
        "DripleafFixSmall",
        "DripleafFixBig",
        "RedstoneWireFix",
        "JappaSpecIcons",
        "JappaStatsIcons",
        "JappaRecipeButton",
        "JappaToasts",
        "JappaObserver",
        "NicerFastLeaves",
        "ProperBreakParticles",
        "SlimeParticleFix"],
    "terrain": [
        "ClearerWater",
        "ShorterTallGrass",
        "ShorterGrass"],
    "utility": [
        "ArabicNumerals",
        "NoteblockBanners",
        "VisualSaplingGrowth",
        "VisualComposterStages",
        "VisualCauldronStages",
        "VisualHoney",
        "BrewingGuide",
        "CompassLodestone",
        "GroovyLevers",
        "UnlitRedstoneOre",
        "RedstonePowerLevels",
        "BetterObservers",
        "DirectionalDispensersDroppers",
        "DirectionalHoppers",
        "StickyPistonSides",
        "HungerPreview",
        "ClearBannerPatterns",
        "Age25Kelp",
        "FullAgeAmethystMarker",
        "FullAgeCropMarker",
        "VisualWaxedCopperItems",
        "VisualInfestedStoneItems",
        "BuddingAmethystBorders",
        "OreBorders",
        "DiminishingTools"
    ]}),
    'version': 1.19}

with urllib.request.urlopen(' https://vanillatweaks.net/assets/server/zipresourcepacks.php',
                            data=urllib.parse.urlencode(resourcepacks_form_data).encode()) as json_req:
    json_response = json.load(json_req)
    filename = pathlib.Path(json_response['link']).name
    urllib.request.urlretrieve(urllib.parse.urljoin('https://vanillatweaks.net/', json_response['link']),
                               download_path / filename)
