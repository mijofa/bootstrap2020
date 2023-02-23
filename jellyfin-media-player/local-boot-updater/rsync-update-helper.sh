#!/bin/bash
if ! echo "$@" | grep --quiet '/run/live/medium/pending' ; then
    # Just rsync as normal
    rsync "$@"
else
    mount -o remount,rw /run/live/medium
    mkdir /run/live/medium/pending
    rsync "$@"
    # Don't replace 'previous' if it's the currently booted one
    if ! losetup /dev/loop0 | grep /run/live/medium/previous ; then
        rm -r /run/live/medium/previous
        mv /run/live/medium/latest /run/live/medium/previous
    else
        rm -r /run/live/medium/latest
    fi
    mv /run/live/medium/pending /run/live/medium/latest
    mount -o remount,ro /run/live/medium
fi
