#!/bin/bash
if [[ "$1" != "--server" ]] ; then
    # Being run locally, just use the args as given
    /usr/bin/rsync "$@"
else
    # Sort out /srv/netboot/images paths for bootstrap2020 --upload-to
    . /etc/os-release
    mkdir --parents /srv/netboot/images/
    ln -svfT "/run/live/medium/latest"    "/srv/netboot/images/${BOOTSTRAP2020_TEMPLATE}-latest"  >&2
    ln -svfT "/run/live/medium/previous"  "/srv/netboot/images/${BOOTSTRAP2020_TEMPLATE}-previous"  >&2
    # FIXME: Only really supports uploads for **today's** SOEs
    ln -svfT "/run/live/medium/pending"   "/srv/netboot/images/${BOOTSTRAP2020_TEMPLATE}-$(date --rfc-3339=date)"  >&2

    mount -o remount,rw /run/live/medium
    mkdir /run/live/medium/pending
    /usr/bin/rsync "$@"
    # Don't replace 'previous' if it's the currently booted one
    if ! losetup /dev/loop0 | grep /run/live/medium/previous ; then
        rm -r /run/live/medium/previous
        mv /run/live/medium/latest /run/live/medium/previous
    else
        rm -r /run/live/medium/latest
    fi
    mv /run/live/medium/pending /run/live/medium/latest
    # This just kinda fails sometimes, not really sure why.
    # It's not hugely important because we manually reboot right after this process anyway.
    mount -o remount,ro /run/live/medium || true
fi
