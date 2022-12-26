Running flatpak apps
====================
The flatpak partition must be setup manually over SSH.
I'll leave setting up the actual partition as an exercise for the reader, but the partlabel needs to be 'flatpak'.
The filesystem probably should be ext or similar, as it likely needs decent permissions,
but I'm assuming that as a flatpak restrictio, not one I have set.

These are each of the flatpaks I have installed (but not really tested at time of writing)::

    flatpak --system install --assumeyes --noninteractive --from https://dl.flathub.org/repo/appstream/net.supertuxkart.SuperTuxKart.flatpakref  # SuperTuxKart
    flatpak --system install --assumeyes --noninteractive --from https://dl.flathub.org/repo/appstream/com.valvesoftware.SteamLink.flatpakref  # Steam Link
    flatpak --system install --assumeyes --noninteractive --from https://dl.flathub.org/repo/appstream/com.mojang.Minecraft.flatpakref  # Minecraft (Java edition)
    flatpak --system install --assumeyes --noninteractive --from https://dl.flathub.org/repo/appstream/org.libretro.RetroArch.flatpakref  # RetroArch

And then I cleaned up as much execess repo cache as I could to reduce space::

    rm -r /var/lib/flatpak/repo
    # Recreate the bare minimum required for flatpak to actually run properly in future
    mkdir -p /var/lib/flatpak/repo/{objects,tmp,refs/remotes,refs/heads}
    printf '%s\n' '[core]' 'repo_version=1' > /var/lib/flatpak/repo/config

This should then be automatically mounted and usable after a reboot

If you want the flatpak's app config to be persistent across reboots, then you will also need to do this::

    install -m1777 -d /var/lib/flatpak/user-var-app/
