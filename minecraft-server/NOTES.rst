Worldborder
===========

Note that Minecraft's native worldborder is a "width" while chunky's generation is a "radius".
Basically meaning you should double the radius when setting the worldborder

Be careful about setting the Nether's worldborder more than 1/8th of the Overworld's worldborder.
I have not installed any plugins to translate portal co-ordinates or anything,
so nether portals could put you outside of the overworld border.

Minecraft does natively support separate worldborders for each dimension,
but the commands are not really designed for being run in the console as they change the border for "current" dimension.
Thankfully `execute in [...]` is a thing::

    echo >/run/minecraft.stdin 'worldborder set 59999968'  # Default
    echo >/run/minecraft.stdin 'worldborder set 16384'  # world "radius" of 8192
    echo >/run/minecraft.stdin 'execute in the_nether run worldborder set 2048'  # world_nether "radius" of 1024
    echo >/run/minecraft.stdin 'execute in the_end run worldborder set 8192'  # world_the_end "radius" of 4096

Minecraft doesn't natively support circular worldborders,
so probably worth having chunky render a square.
It'll be less resource intensive than having some custom worldborder plugin.
