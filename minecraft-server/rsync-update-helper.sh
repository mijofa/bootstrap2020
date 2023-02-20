#!/bin/bash
if ! echo "$@" | grep --quiet '/run/live/medium' ; then
    rsync "$@"
else
    mount -o remount,rw /run/live/medium
    mv /run/live/medium/previous /run/live/medium/$(date --rfc-3339=date)
    mv /run/live/medium/latest /run/live/medium/previous
    mkdir /run/live/medium/latest
    rsync "$@"
    mount -o remount,ro /run/live/medium
fi
