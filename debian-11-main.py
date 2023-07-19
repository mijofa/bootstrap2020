#!/usr/bin/python3
import argparse
import configparser
import datetime
import io
import json
import logging
import os
import pathlib
import pprint
import re
import shutil
import subprocess
import tarfile
import tempfile
import types

import hyperlink                # URL validation
import requests                 # FIXME: h2 support!
import pypass                   # for tvserver PSKs

__author__ = "Trent W. Buck"
__copyright__ = "Copyright © 2021 Trent W. Buck"
__license__ = "expat"

__doc__ = """ build simple Debian Live image that can boot

This uses mmdebstrap to do the heavy lifting;
it can run entirely without root privileges.
Bootloader is out-of-scope (but --boot-test --netboot-only has an example PXELINUX.cfg).
"""


def validate_unescaped_path_is_safe(path: pathlib.Path) -> None:
    for part in pathlib.Path(path).parts:
        if not (part == '/' or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}', part)):
            raise NotImplementedError('Path component should not need shell quoting', part, path)


def hostname_or_fqdn_with_optional_user_at(s: str) -> str:
    if re.fullmatch(r'([a-z]+@)?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*', s):
        return s
    else:
        raise ValueError()


parser = argparse.ArgumentParser(description=__doc__)
group = parser.add_argument_group('debugging')
group.add_argument('--debug-shell', action='store_true',
                   help='quick-and-dirty chroot debug shell')
group.add_argument('--boot-test', action='store_true',
                   help='quick-and-dirty boot test via qemu')
group.add_argument('--break', default='',
                   choices=('top', 'modules', 'premount', 'mount',
                            'mountroot', 'bottom', 'init',
                            'live-realpremount'),
                   dest='maybe_break',  # "break" is python keyword
                   help='pause boot test during initrd')
group.add_argument('--backdoor-enable', action='store_true',
                   help='login as root with no password')
group.add_argument('--host-port-for-boot-test-ssh', type=int, default=2022, metavar='N',
                   help='so you can run two of these at once')
group.add_argument('--host-port-for-boot-test-vnc', type=int, default=5900, metavar='N',
                   help='so you can run two of these at once')
group.add_argument('--opengl-for-boot-test-ssh', action='store_true',
                   help='Enable OpenGL in --boot-test (requires qemu 7.1)')
group.add_argument('--measure-install-footprints', action='store_true')
parser.add_argument('--destdir', type=lambda s: pathlib.Path(s).resolve(),
                    default='/tmp/bootstrap2020/')
parser.add_argument('--template', default='main',
                    choices=('main',
                             'dban',
                             'zfs',
                             'desktop',
                             'jellyfin-media-player',
                             'minecraft-server',
                             'cec-androidtv-fixes'
                             ),
                    help=(
                        'main: small CLI image; '
                        'dban: erase recycled HDDs; '
                        'zfs: install/rescue Debian root-on-ZFS; '
                        'desktop: tweaked XFCE; '
                        'jellyfin-media-player: Jellyfin frontend for use on TV systems; '
                        'minecraft-server: Runs a standalone Minecraft server; '
                        'cec-androidtv-fixes: Runs some workarounds for annoyances with AndroidTV; '
                    ))
parser.add_argument('--rpi', default=None, choices=['armel'],
                    help=('Target Raspberry Pi devices instead of basic amd64.'
                          'armel: Raspberry Pi Zero, Zero W and 1;'
                          'armhf: (UNTESTED) Raspberry Pi 2;'
                          'arm64: (UNTESTED) Raspberry Pi 3 and 4;'))
group = parser.add_argument_group('optimization')
group.add_argument('--optimize', choices=('size', 'speed', 'simplicity'), default='size',
                   help='build slower to get a smaller image? (default=size)')
mutex = group.add_mutually_exclusive_group()
mutex.add_argument('--netboot-only', '--no-local-boot', action='store_true',
                   help='save space/time by omitting USB/SSD stuff')
mutex.add_argument('--local-boot-only', '--no-netboot', action='store_true',
                   help='save space/time by omitting PXE/NFS/SMB stuff')
mutex = group.add_mutually_exclusive_group()
mutex.add_argument('--virtual-only', '--no-physical', action='store_true',
                   help='save space/time by omitting physical hw support')
mutex.add_argument('--physical-only', '--no-virtual', action='store_true',
                   help='save space/time by omitting qemu/VM support')
parser.add_argument('--reproducible', metavar='EPOCHTIME',
                    type=lambda s: datetime.datetime.utcfromtimestamp(int(s)),
                    help='build a reproducible OS image & sign it')
group = parser.add_argument_group('customization')
group.add_argument('--LANG', default=os.environ['LANG'], metavar='xx_XX.UTF-8',
                   help='locale used inside the image',
                   type=lambda s: types.SimpleNamespace(full=s, encoding=s.partition('.')[-1]))
group.add_argument('--TZ', default=pathlib.Path('/etc/timezone').read_text().strip(),
                   help="SOE's timezone (for UTC, use Etc/UTC)", metavar='REGION/CITY',
                   type=lambda s: types.SimpleNamespace(full=s,
                                                        area=s.partition('/')[0],
                                                        zone=s.partition('/')[-1]))
group.add_argument('--ssh-server',
                   default='tinysshd',
                   choices=('tinysshd', 'dropbear', 'openssh-server'),
                   help='Use OpenSSH?  Useful if you need'
                   ' • "ssh X Y" to try /usr/local/bin/Y'
                   ' • other PAM benefits, like systemd --user'
                   ' • authorized certs, or RSA keys'
                   ' • drop-in keys (~/.ssh/authorized_keys2)')
group.add_argument('--authorized-keys-urls', metavar='URL', nargs='*',
                   type=hyperlink.URL.from_text,
                   help='who can SSH into your image?',
                   default=[hyperlink.URL.from_text('https://github.com/trentbuck.keys'),
                            hyperlink.URL.from_text('https://github.com/mijofa.keys'),
                            hyperlink.URL.from_text('https://github.com/emja.keys')])
parser.add_argument('--upload-to', nargs='+', default=[], metavar='HOST',
                    type=hostname_or_fqdn_with_optional_user_at,
                    help='hosts to rsync the finished image to e.g. "root@tweak.prisonpc.com"')
parser.add_argument('--github-release', metavar='USER/REPO',
                    type=str,  # FIXME
                    help='Github repo & release to upload the finished image to e.g. "mijofa/bootstrap2020"')
parser.add_argument('--remove-afterward', action='store_true',
                    help='delete filesystem.squashfs after boot / upload (save space locally)')
args = parser.parse_args()

# The upload code gets a bit confused if we upload "foo-2022-01-01" twice in the same day.
# As a quick-and-dirty workaround, include time in image name.
# Mike removed the time from the image name because it was making things harder to work with for the Jellyfin SOEs
# Cannot use RFC 3339 because PrisonPC tca3.py has VERY tight constraints on path name.
destdir = (args.destdir / f'{args.template}-{datetime.datetime.now().strftime("%Y-%m-%d")}')
validate_unescaped_path_is_safe(destdir)
destdir.mkdir(parents=True, mode=0o2775, exist_ok=True)

# signed-by needs an absolute path, so also validate $PWD.
validate_unescaped_path_is_safe(pathlib.Path.cwd())

apt_proxy = subprocess.check_output(['auto-apt-proxy'], text=True).strip()

git_proc = subprocess.run(
    ['git', 'describe', '--always', '--dirty', '--broken', '--abbrev=0'],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL)
git_description = git_proc.stdout.strip() if git_proc.returncode == 0 else 'UNKNOWN'

have_smbd = pathlib.Path('/usr/sbin/smbd').exists()
if args.boot_test and args.netboot_only and not have_smbd:
    logging.warning('No /usr/sbin/smbd; will test with TFTP (fetch=).'
                    '  This is OK for small images; bad for big ones!')

if args.boot_test and args.physical_only:
    raise NotImplementedError("You can't --boot-test a --physical-only (--no-virtual) build!")

template_wants_WiFi = args.template in {'jellyfin-media-player', 'cec-androidtv-fixes'}
template_wants_GUI = args.template.startswith('desktop')
template_wants_DVD = args.template.startswith('desktop')
template_wants_disks = args.template in {'dban', 'zfs'}

if template_wants_GUI and args.virtual_only:
    logging.warning('GUI on cloud kernel is a bit hinkey')

if args.reproducible:
    os.environ['SOURCE_DATE_EPOCH'] = str(int(args.reproducible.timestamp()))
    # FIXME: we also need a way to use a reproducible snapshot of the Debian mirror.
    # See /bin/debbisect for discussion re https://snapshot.debian.org.
    proc = subprocess.run(['git', 'diff', '--quiet', 'HEAD'])
    if proc.returncode != 0:
        raise RuntimeError('Unsaved changes (may) break reproducible-build! (fix "git diff")')
    if subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard']).strip():
        raise RuntimeError('Unsaved changes (may) break reproducible-build! (fix "git status")')
    if args.backdoor_enable or args.debug_shell:
        logging.warning('debug/backdoor might break reproducibility')

if subprocess.check_output(
        ['systemctl', 'is-enabled', 'systemd-resolved'],
        text=True).strip() != 'enabled':
    logging.warning(
        'If you see odd DNS errors during the build,'
        ' either run "systemctl enable --now systemd-resolved" on your host, or'
        ' make the /lib/systemd/resolv.conf line run much later.')

# Use a separate declarative file for these long, boring lists.
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    validate_unescaped_path_is_safe(td)
    # FIXME: use SSH certificates instead, and just trust a static CA!
    authorized_keys_tar_path = td / 'ssh.tar'
    with tarfile.open(authorized_keys_tar_path, 'w') as t:
        with io.BytesIO() as f:  # addfile() can't autoconvert StringIO.
            for url in args.authorized_keys_urls:
                resp = requests.get(url)
                resp.raise_for_status()
                f.write(b'#')
                f.write(url.to_text().encode())
                f.write(b'\n')
                # can't use resp.content, because website might be using BIG5 or something.
                f.write(resp.text.encode())
                f.write(b'\n')
                f.flush()
            member = tarfile.TarInfo('root/.ssh/authorized_keys')
            member.mode = 0o0400
            member.size = f.tell()
            f.seek(0)
            t.addfile(member, f)

    def create_tarball(src_path: pathlib.Path) -> pathlib.Path:
        src_path = pathlib.Path(src_path)
        assert src_path.exists(), 'The .glob() does not catch this!'
        # FIXME: this can still collide
        # FIXME: can't do symlinks, directories, &c.
        dst_path = td / f'{src_path.name}.tar'
        with tarfile.open(dst_path, 'w') as t:
            for tarinfo_path in src_path.glob('**/*.tarinfo'):
                content_path = tarinfo_path.with_suffix('')
                tarinfo_object = tarfile.TarInfo()
                # git can store *ONE* executable bit.
                # Default to "r--------" or "r-x------", not "---------".
                tarinfo_object.mode = (
                    0 if not content_path.exists() else
                    0o500 if content_path.stat().st_mode & 0o111 else 0o400)
                for k, v in json.loads(tarinfo_path.read_text()).items():
                    setattr(tarinfo_object, k, v)
                if tarinfo_object.linkpath:
                    tarinfo_object.type = tarfile.SYMTYPE
                if tarinfo_object.isreg():
                    tarinfo_object.size = content_path.stat().st_size
                    with content_path.open('rb') as content_handle:
                        t.addfile(tarinfo_object, content_handle)
                else:
                    t.addfile(tarinfo_object)
        # subprocess.check_call(['tar', 'vvvtf', dst_path])  # DEBUGGING
        return dst_path

    subprocess.check_call(
        ['nice', 'ionice', '-c3', 'chrt', '--idle', '0',
         'mmdebstrap',
         '--dpkgopt=force-confold',  # https://bugs.debian.org/981004
         '--aptopt=APT::AutoRemove::SuggestsImportant "false"',  # fix autoremove
         '--include=linux-image-rpi' if args.rpi == 'armel' else
         '--include=linux-image-rt-armmp' if args.rpi == 'armhf' else
         '--include=linux-image-rt-arm64' if args.rpi == 'arm64' else
         '--include=linux-image-cloud-amd64'
         if args.virtual_only else
         # NOTE: can't --include= this because there are too many dpkg trigger problems.
         '--include=linux-image-amd64',
         '--include=live-boot',
         *([f'--aptopt=Acquire::http::Proxy "{apt_proxy}"',  # save 12s
            '--aptopt=Acquire::https::Proxy "DIRECT"']
           if args.optimize != 'simplicity' else []),
         *(['--variant=apt',           # save 12s 30MB
            '--include=netbase',       # https://bugs.debian.org/995343 et al
            '--include=init']          # https://bugs.debian.org/993289
           if args.optimize != 'simplicity' else []),
         '--include=systemd-timesyncd',  # https://bugs.debian.org/986651
         *(['--dpkgopt=force-unsafe-io']  # save 20s (even on tmpfs!)
           if args.optimize != 'simplicity' else []),
         # Reduce peak /tmp usage by about 500MB
         *(['--essential-hook=chroot $1 apt clean',
            '--customize-hook=chroot $1 apt clean']
           if args.optimize != 'simplicity' else []),
         *(['--dpkgopt=path-exclude=/usr/share/doc/*']  # 9% to 12% smaller and
           if args.optimize == 'size' else []),
         *(['--dpkgopt=path-exclude=/usr/share/man/*']  # 8% faster to 7% SLOWER. Breaks JRE install
           if args.optimize == 'size' and args.template != 'minecraft-server' else []),
         *([]
           if args.optimize == 'simplicity' else
           ['--include=pigz']       # save 8s
           if args.optimize == 'speed' else
           ['--include=xz-utils',   # save 10MB lose 28s
            '--essential-hook=mkdir -p $1/etc/initramfs-tools/conf.d',
            '--essential-hook=>$1/etc/initramfs-tools/conf.d/xz echo COMPRESS=xz']),
         *(['--include=dbus',       # https://bugs.debian.org/814758
            '--customize-hook=ln -nsf /etc/machine-id $1/var/lib/dbus/machine-id']  # https://bugs.debian.org/994096
           if args.optimize != 'simplicity' else []),
         *(['--include=libnss-myhostname libnss-resolve',
            '--include=policykit-1',  # https://github.com/openbmc/openbmc/issues/3543
            '--customize-hook=rm $1/etc/hostname',
            '--customize-hook=ln -nsf /lib/systemd/resolv.conf $1/etc/resolv.conf',
            '--include=rsyslog-relp msmtp-mta',
            '--include=python3-dbus',  # for get-config-from-dnssd
            '--include=debian-security-support',  # for customize90-check-support-status.py
            f'--essential-hook=tar-in {create_tarball("debian-11-main")} /',
            '--hook-dir=debian-11-main.hooks',
            ]
           if args.optimize != 'simplicity' else []),
         *(['--include=tzdata',
            '--essential-hook={'
            f'    echo tzdata tzdata/Areas                select {args.TZ.area};'
            f'    echo tzdata tzdata/Zones/{args.TZ.area} select {args.TZ.zone};'
            '     } | chroot $1 debconf-set-selections']
           if args.optimize != 'simplicity' else []),
         *(['--include=locales',
            '--essential-hook={'
            f'    echo locales locales/default_environment_locale select {args.LANG.full};'
            f'    echo locales locales/locales_to_be_generated multiselect {args.LANG.full} {args.LANG.encoding};'
            '     } | chroot $1 debconf-set-selections']
           if args.optimize != 'simplicity' else []),
         # x86_64 CPUs are undocumented proprietary RISC chips that EMULATE a documented x86_64 CISC ISA.
         # The emulator is called "microcode", and is full of security vulnerabilities.
         # Make sure security patches for microcode for *ALL* CPUs are included.
         # By default, it tries to auto-detect the running CPU, so only patches the CPU of the build server.
         *([*(['--include=intel-microcode amd64-microcode'] if not args.rpi else []),
            '--essential-hook=>$1/etc/default/intel-microcode echo IUCODE_TOOL_INITRAMFS=yes IUCODE_TOOL_SCANCPUS=no',
            '--essential-hook=>$1/etc/default/amd64-microcode echo AMD64UCODE_INITRAMFS=yes',
            '--components=main contrib non-free']
           if args.optimize != 'simplicity' and not args.virtual_only else []),
         *(['--include=ca-certificates publicsuffix']
           if args.optimize != 'simplicity' else []),
         *(['--include=nfs-client',  # support NFSv4 (not just NFSv3)
            '--include=cifs-utils',  # support SMB3
            f'--essential-hook=tar-in {create_tarball("debian-11-main.netboot")} /']
           if not args.local_boot_only else []),
         *([f'--essential-hook=tar-in {create_tarball("debian-11-main.netboot-only")} /']  # 9% faster 19% smaller
           if args.netboot_only else []),
         *(['--include=nwipe']
           if args.template == 'dban' else []),
         *(['--include=zfs-dkms zfsutils-linux zfs-zed',
            '--include=mmdebstrap auto-apt-proxy',  # for installing
            '--include=linux-headers-cloud-amd64'
            if args.virtual_only else
            '--include=linux-headers-amd64']
           if args.template == 'zfs' else []),
         *(['--include=smartmontools'
            '    bsd-mailx'    # smartd calls mail(1), not sendmail(8)
            '    curl ca-certificates gnupg',  # update-smart-drivedb
            f'--essential-hook=tar-in {create_tarball("debian-11-main.disks")} /',
            '--customize-hook=chroot $1 update-smart-drivedb'
            ]
           if template_wants_disks and not args.virtual_only else []),
         *(['--include='
            '    xserver-xorg-core xserver-xorg-input-libinput'
            '    xfce4-session xfwm4 xfdesktop4 xfce4-panel thunar galculator'
            '    xdm'
            '    pulseaudio xfce4-pulseaudio-plugin pavucontrol'
            # Without "alsactl init" & /usr/share/alsa/init/default,
            # pipewire/pulseaudio use the kernel default (muted & 0%)!
            '    alsa-utils'
            '    ir-keytable'   # infrared TV remote control
            '    xfce4-xkb-plugin '  # basic foreign language input (e.g. Russian, but not Japanese)
            '    xdg-user-dirs-gtk'  # Thunar sidebar gets Documents, Music &c
            '    gvfs thunar-volman eject'  # Thunar trash://, DVD autoplay, DVD eject
            '    xfce4-notifyd '     # xfce4-panel notification popups
            # FIXME: use plocate (not mlocate) once PrisonPC master server upgrades!
            '    catfish mlocate xfce4-places-plugin'  # "Find Files" tool
            '    eog '  # chromium can't flip between 1000 photos quickly
            '    usermode'                             # password reset tool
            '    librsvg2-common'    # SVG icons in GTK3 apps
            '    gnome-themes-extra adwaita-qt'  # theming
            '    at-spi2-core gnome-accessibility-themes'
            '    plymouth-themes',
            # Workaround https://bugs.debian.org/1004001 (FIXME: fix upstream)
            '--essential-hook=chronic chroot $1 apt install -y fontconfig-config',
            # FIXME: in Debian 12, change --include=pulseaudio to --include=pipewire,pipewire-pulse
            # https://wiki.debian.org/PipeWire#Using_as_a_substitute_for_PulseAudio.2FJACK.2FALSA
            # linux-image-cloud-amd64 is CONFIG_DRM=n so Xorg sees no /dev/dri/card0.
            # It seems there is a fallback for -vga qxl, but not -vga virtio.
            '--include=xserver-xorg-video-qxl'
            if args.virtual_only else
            # Accelerated graphics drivers for several libraries & GPU families
            '--include=vdpau-driver-all'  # VA/AMD, free
            '    mesa-vulkan-drivers'     # Intel/AMD/Nvidia, free
            '    va-driver-all'           # Intel/AMD/Nvidia, free
            '    i965-va-driver-shaders'  # Intel, non-free, 2013-2017
            '    intel-media-va-driver-non-free',  # Intel, non-free, 2017+
            # For https://github.com/cyberitsolutions/bootstrap2020/blob/main/debian-11-desktop/xfce-spice-output-resizer.py
            *(['--include=python3-xlib python3-dbus spice-vdagent']
              if not args.physical_only else []),
            # Seen on H81 and H110 Pioneer AIOs.
            # Not NEEDED, just makes journalctl -p4' quieter.
            f'--essential-hook=tar-in {create_tarball("debian-11-desktop")} /'
            ]
           if template_wants_GUI else []),
         # Mike wants this for prisonpc-desktop-staff-amc in spice-html5.
         # FIXME: WHY?  Nothing in the package description sounds useful.
         # FIXME: --boot-test's kvm doesn't know to create the device!!!
         *(['--include=qemu-guest-agent']
           if not args.physical_only else []),
         *([f'--include={args.ssh_server}',
            f'--essential-hook=tar-in {authorized_keys_tar_path} /',
            # Work around https://bugs.debian.org/594175 (dropbear & openssh-server)
            '--customize-hook=rm -f $1/etc/dropbear/dropbear_*_host_key',
            '--customize-hook=rm -f $1/etc/ssh/ssh_host_*_key*',
            ]
           if args.optimize != 'simplicity' else []),
         '--customize-hook=chronic chroot $1 systemctl preset-all',  # enable ALL units!
         '--customize-hook=chronic chroot $1 systemctl preset-all --user --global',
         *(['--customize-hook=chroot $1 adduser x --gecos x --disabled-password --quiet',
            '--customize-hook=echo x:x | chroot $1 chpasswd',
            '--customize-hook=echo root: | chroot $1 chpasswd --crypt-method=NONE',
            '--include=strace',
            '--customize-hook=rm -f $1/etc/sysctl.d/bootstrap2020-hardening.conf',
            *(['--include=xfce4-terminal']
              if template_wants_GUI else [])]
           if args.backdoor_enable else []),
         *([f'--customize-hook=echo bootstrap:{git_description} >$1/etc/debian_chroot',
            '--customize-hook=chroot $1 bash -i; false',
            '--customize-hook=rm -f $1/etc/debian_chroot']
           if args.debug_shell else []),
         *(['--customize-hook=upload doc/debian-11-app-reviews.csv /tmp/app-reviews.csv',
            '--customize-hook=chroot $1 python3 < debian-11-install-footprint.py',
            '--customize-hook=download /var/log/install-footprint.csv'
            f'    doc/debian-11-install-footprint.{args.template}.csv']
           if args.measure_install_footprints else []),
         # Make a simple copy for https://kb.cyber.com.au/32894-debsecan-SOEs.sh
         # FIXME: remove once that can/does use rdsquashfs --cat (master server is Debian 11)
         *([f'--customize-hook=download /var/lib/dpkg/status {destdir}/dpkg.status']
           if args.optimize != 'simplicity' else []),
         *([f'--customize-hook=download vmlinuz {destdir}/vmlinuz',
            f'--customize-hook=download initrd.img {destdir}/initrd.img']
           if not args.rpi else [f'--customize-hook=sync-out /boot/firmware/ {destdir}/']),
         *(['--customize-hook=rm $1/boot/vmlinuz* $1/boot/initrd.img*']  # save 27s 27MB
           if args.optimize != 'simplicity' else []),
         *(['--verbose', '--logfile', destdir / 'mmdebstrap.log']
           if args.reproducible else []),
         *(['--include=wpasupplicant firmware-realtek firmware-iwlwifi']
           if template_wants_WiFi else []),
         *(['--include=python3-cec',  # Needed to control the HDMI amplifier
            '--include=python3-pip',  # Needed because python3-androidtvremote2 is not packaged for Debian
            '--include=python3-aiofiles python3-cryptography python3-protobuf',  # Dependencies of python3-androidtvremote2
            # NOTE: We could use pip from the host system with `--root=$1` but that adds more dependencies in the host,
            #       and likely to cause version mismatch between the OS and the Python library
            '--customize-hook=chroot $1 python3 -m pip install --break-system-packages --no-deps androidtvremote2',  # FIXME: Use a venv?
            '--include=python3-construct python3-packaging',  # Dependencies of python3-snapcast
            '--customize-hook=chroot $1 python3 -m pip install --break-system-packages --no-deps snapcast',  # FIXME: Use a venv?

            '--include=ir-keytable',  # infrared remote control
            # cec-utils
            '--include=v4l-utils',  # Trying to make CEC remote control work
            '--include=rsync',  # Great for dev & updates

            '--include=python3-evdev python3-pyudev',  # needed for the Python global keybindings handler

            # Append to the default /etc/rc_maps.cfg
            # FIXME: Use pathlib or os.path.join.
            f'--customize-hook=cat "{args.template}/infrared-tv-remote-control/rc_maps.cfg" >>"$1/etc/rc_maps.cfg"',
            f'--essential-hook=tar-in {create_tarball(args.template)} /',
            ]
           if args.template == 'cec-androidtv-fixes' else []),
         *(['--include=phoc xwayland',  # Let's try Wayland instead of X11  NOTE: jellyfin-media-player has issues with sway, mako-notifier can't work with weston

            # copied from wants_GUI section above because while this does want a GUI, it's not an X11 GUI so we can't use that entire section
            '--include=vdpau-driver-all'  # VA/AMD, free
            '    mesa-vulkan-drivers'     # Intel/AMD/Nvidia, free
            '    va-driver-all'           # Intel/AMD/Nvidia, free
            '    i965-va-driver-shaders'  # Intel, non-free, 2013-2017
            '    intel-media-va-driver-non-free',  # Intel, non-free, 2017+
            '--include=ir-keytable',   # infrared TV remote control
            '--include=v4l-utils',   # ir-ctl for *sending* IR signals
            '--include=plymouth-themes',  # For custom bootup logo
            # Workaround https://bugs.debian.org/1004001 (FIXME: fix upstream)
            '--essential-hook=chroot $1 apt install -y fontconfig-config',

            # Having hardware support issues, let's just throw some firmware in and see if it helps
            '--include=firmware-linux-free firmware-linux firmware-linux-nonfree',  # Lots of generic firmware stuff, normally helps
            '--include=firmware-amd-graphics firmware-intel-sound',  # I don't think I'm using any of this hardware, but shouldn't hurt
            # NOTE: firmware-ivtv has an EULA that needs to be agreed to, rather than fixing that I'm just leaving it out
            '--include=firmware-samsung',  # I don't understand how codec firmwares work, but given this is a media machine I might as well include them
            # I continued getting errors about failing to load iwlwifi firmware, but it worked.
            # it did *not* work without the firmware-iwlwifi package though,
            # so I suspect it fellback on a firmware for an older chipset from the same package
            # '--include= atmel-firmware firmware-atheros firmware-libertas firmware-ti-connectivity',  # FIXME: Worth including these as well?
            '--include=firmware-sof-signed',  # Needed this for audio on my Lenovo ThinkPad Yoga when testing for WiFi dev

            '--include=jellyfin-media-player',  # The whole point of this thing
            '--include=python3-plyvel python3-dnspython',  # Needed for set-jellyfin-server.py
            '--include=qtwayland5',  # Wayland support for jellyfin-media-player

            '--include=pulseaudio',  # Pulseaudio's role-corking makes pausing the music when movie starts a lot easier, pipewire does not seem to have this feature
            # FIXME: Just change the PA config to what I actually want rather than using pacmd in override.conf
            '--customize-hook=sed -i "/module-role-cork/ s/^load/#load/" $1/etc/pulse/default.pa',  # Disable role corking because the default config sucks, we enable it later in a systemd override.conf

            '--include=snapclient',  # Using this as the whole house audio solution
            '--include=avahi-daemon',  # Dependency of snapclient missing in control file

            '--include=python3-systemd',  # Used in some of my .py systemd units

            '--include=ydotool',  # Wayland xdotool, needed only to hide the mouse in the bottom-right  FIXME: jellyfin-media-player or phoc should handle this

            '--include=swaybg',  # For setting Phoc's background image.   NOTE Has nothing to do with sway

            # keybinds.py
            # A daemon that handles system keybindings such as volume +/-
            '--include=python3-evdev',  # The library I use to get the keypresses
            '--include=python3-pyudev',  # Used to identify new devices when they come in

            '--include=python3-paho-mqtt',  # Used by tasmota_controller.py

            '--include=python3-psutil',  # Used by snapcontroller.py to get the local mac address
            '--include=python3-gi gir1.2-notify-0.7 gir1.2-gtk-3.0',  # Libraries for notifyd & gtk icons
            '--include=mako-notifier',  # Notification daemon that supports Wayland

            '--include=sound-theme-freedesktop',  # Generic sound effects, used to notify when turning speakers/TV on/off

            '--include=grim',  # Wayland screenshot utility, not really using it yet but would like to

            '--include=python3-github',  # Github API library for the auto updater script

            '--include=lvm2',  # So that Ron can recover some data from repuprosed system if necessary

            # Steam Link
            # Don't actually install the Steam Link app here as it doubles the size of the SOE,
            # just install the necessary packages for the flatpaks to be installed on the boot media.
            '--include=flatpak',  # The offical Steam Link app is a flatpak, so just use that because CBFed doing it myself
            '--include=steam-devices',  # Some udev rules to theoretically help with Steam Controller support

            '--include=rsync',  # I like to manually update the SOE directly sometimes

            # Create the actual user that the GUI runs as
            '--customize-hook=chroot $1 adduser jellyfinuser --gecos "Jellyfin Client User" --disabled-password --quiet',
            '--customize-hook=chroot $1 adduser jellyfinuser input --quiet',  # For access to evdev devices for keybinds.py
            '--customize-hook=chroot $1 adduser jellyfinuser video --quiet',  # For access to /dev/lirc0 device to send IR signals

            '--customize-hook=systemctl disable --quiet --system --root $1 snapclient.service',  # We run snapclient as a user unit, not a system unit

            # Ugly hacks to try and make Plymouth more seamless
            '--customize-hook=rm $1/lib/systemd/system/multi-user.target.wants/plymouth-quit.service',  # disable doesn't actually work because Debian created the symlink explicitly without putting "WantedBy" in the .service file
            '--customize-hook=systemctl mask --quiet --system --root $1 plymouth-quit-wait.service',  # This is a service that waits for plymouth to stop before allowing graphical.target to start, that gets stupidly in the way for us.
            '--customize-hook=systemctl enable --quiet --system --root $1 plymouth-quit.service',  # Instead of disabling plymouth, just have the stop unit start *after* phoc

            f'--customize-hook=sed -i "s|{pathlib.Path.cwd()}/jellyfin-media-player|/etc/apt/trusted.gpg.d|" "$1/etc/apt/sources.list.d/0001main.list"',  # Fix apt sources.list for the correct public key location

            f'--essential-hook=tar-in {create_tarball(args.template)} /']
           if args.template == 'jellyfin-media-player' else []),
         *(['--include=openjdk-17-jre-headless rsync',

            '--include=zfs-dkms zfsutils-linux zfs-zed',  # ZFS support
            '--include=linux-headers-cloud-amd64'
            if args.virtual_only else
            '--include=linux-headers-amd64',
            '--customize-hook=systemctl --root $1 add-wants zfs-import.target zfs-import-scan.service',  # scan & import zfs pools on boot

            # FIXME: Use a systemd ephemeral user thing
            # NOTE: I've set the user ID because I need it to match the ownership/permissions of the data partition
            '--customize-hook=chroot $1 adduser minecraft --home /srv/mcdata --no-create-home --system --group --uid 420',
            '--hook-dir=minecraft-server.hooks',
            f'--essential-hook=tar-in {create_tarball(args.template)} /']
           if args.template == 'minecraft-server' else []),
         f'--customize-hook=echo "BOOTSTRAP2020_TEMPLATE={args.template}" >>$1/etc/os-release',
         *([f'--architecture={args.rpi}',
            '--include=raspi-firmware',
            *(['--include=firmware-atheros firmware-brcm80211 firmware-libertas firmware-misc-nonfree firmware-realtek',
               '--include=wireless-regdb',  # No idea why this one is necessary, but I had a bunch of erros in the log without it
               ] if template_wants_WiFi else []),
            # '--include=python3-rpi.gpio',
            # I want this to run **before** installing raspi-firmware, or at least before a final `update-initramfs`
            # is customize-hook good enough anyway?
            ('--essential-hook=printf >$1/etc/default/raspi-firmware-custom "%s\n"'  # This eventually makes it to /boot/firmware/config.txt
             # # https://www.raspberrypi.com/documentation/computers/config_txt.html#hdmi_enable_4kp60-raspberry-pi-4-only
             # # UNTESTED, seems like a good idea if used for media playback purposes, but I don't have an rPi4
             # ' "hdmi_enable_4kp60=1"'
             # # https://www.raspberrypi.com/documentation/computers/config_txt.html#disable_fw_kms_setup
             # # FIXME: Why is "let the kernel handle it" not the default? O.o
             # ' "disable_fw_kms_setup=1"'
             # https://www.raspberrypi.com/documentation/computers/config_txt.html#disable_overscan
             # Overscan is just annoying, ideally disable it everywhere, but some specific TVs will require it
             ' "disable_overscan=1"'
            ),
            # We're using live-boot directly from the fat32 boot/firmware filesystem,
            # stop using partition 2 as "root".
            '--essential-hook=echo >>$1/etc/default/raspi-firmware "ROOTPART=/dev/mmcblk0p1"',
            # https://wiki.debian.org/RaspberryPi4#Root_file_system_on_a_USB_disk
            # Probably not useful to me, but shouldn't hurt, and might avoid some confusion later down the track
            # FIXME: Include `raspberrypi_cpufreq` & `raspberrypi_hwmon`?
            '--essential-hook=printf >$1/etc/initramfs-tools/modules "%s\n" "reset_raspberrypi"',
            # FIXME: net.ifnames=0 is currently needed for WiFi persistent config... do better.
            '--essential-hook=echo >$1/etc/default/raspi-extra-cmdline "net.ifnames=0 boot=live live-media-path="',
            # FIXME: Somehow implement a/b partitions for some form of auto-updates later?
            #        https://www.raspberrypi.com/documentation/computers/config_txt.html#autoboot-txt
            #        Likely requires using u-boot or similar.
           ] if args.rpi else []),
         'bookworm',
         destdir / 'filesystem.squashfs',
         'debian-12.sources',
         # https://github.com/rsnapshot/rsnapshot/issues/279
         # https://tracker.debian.org/news/1238555/rsnapshot-removed-from-testing/
         *([f'deb [signed-by={pathlib.Path.cwd()}/jellyfin-media-player/mijofa-archive-pubkey.asc] https://github.com/mijofa/mijofa.github.io/releases/download/apt-bookworm-amd64 ./']
           if args.template == 'jellyfin-media-player' else []),
         ])

subprocess.check_call(
    ['du', '--human-readable', '--all', '--one-file-system', destdir])

if args.reproducible:
    (destdir / 'args.txt').write_text(pprint.pformat(args))
    (destdir / 'git-description.txt').write_text(git_description)
    (destdir / 'B2SUMS').write_bytes(subprocess.check_output(
        ['b2sum', *sorted(path.name for path in destdir.iterdir())],
        cwd=destdir))
    if False:
        # Disabled for now because:
        #   1. you have to babysit the build (otherwise "gpg: signing failed: Timeout"); and
        #   2. reproducible builds aren't byte-for-byte identical yet, so it's not useful.
        subprocess.check_call(['gpg', '--sign', '--detach-sign', '--armor', (destdir / 'B2SUMS')])


def maybe_dummy_DVD(testdir: pathlib.Path) -> list:
    if not template_wants_DVD:
        return []               # add no args to qemu cmdline
    dummy_DVD_path = testdir / 'dummy.iso'
    subprocess.check_call([
        'wget2',
        '--quiet',
        '--output-document', dummy_DVD_path,
        '--http-proxy', apt_proxy,
        'http://deb.debian.org/debian/dists/stable/main/installer-i386/current/images/netboot/mini.iso'])
    return (                    # add these args to qemu cmdline
        ['--drive', f'file={dummy_DVD_path},format=raw,media=cdrom',
         '--boot', 'order=n'])  # don't try to boot off the dummy disk


if args.template == 'jellyfin-media-player':
    # This template uses Wayland instead of X11, so all other wants_GUI things aren't valid, but we still want the VM layer to do GUI things
    template_wants_GUI = True

if args.boot_test:
    # PrisonPC SOEs are hard-coded to check their IP address.
    # This is not boot-time configurable for paranoia reasons.
    # Therefore, qemu needs to use compatible IP addresses.
    network, tftp_address, dns_address, smb_address, master_address = (
        '10.128.2.0/24', '10.128.2.2', '10.128.2.3', '10.128.2.4', '10.128.2.100')
    with tempfile.TemporaryDirectory(dir=destdir) as testdir:
        testdir = pathlib.Path(testdir)
        validate_unescaped_path_is_safe(testdir)
        subprocess.check_call(['ln', '-vt', testdir, '--',
                               destdir / 'vmlinuz',
                               destdir / 'initrd.img',
                               destdir / 'filesystem.squashfs'])
        common_boot_args = ' '.join([
            ('quiet splash'
             if template_wants_GUI else
             'earlyprintk=ttyS0 console=ttyS0 loglevel=1'),
            (f'break={args.maybe_break}'
             if args.maybe_break else '')])

        if template_wants_disks:
            dummy_path = testdir / 'dummy.img'
            size0, size1, size2 = 1, 64, 128  # in MiB
            subprocess.check_call(['truncate', f'-s{size0+size1+size2+size0}M', dummy_path])
            subprocess.check_call(['/sbin/parted', '-saopt', dummy_path,
                                   'mklabel gpt',
                                   f'mkpart ESP  {size0}MiB     {size0+size1}MiB', 'set 1 esp on',
                                   f'mkpart root {size0+size1}MiB {size0+size1+size2}MiB'])
            subprocess.check_call(['/sbin/mkfs.fat', dummy_path, '-nESP', '-F32', f'--offset={size0*2048}', f'{size1*1024}', '-v'])
            subprocess.check_call(['/sbin/mkfs.ext4', dummy_path, '-Lroot', f'-FEoffset={(size0+size1)*1024*1024}', f'{size2}M'])
        if args.netboot_only:
            subprocess.check_call(['cp', '-t', testdir, '--',
                                   '/usr/lib/PXELINUX/pxelinux.0',
                                   '/usr/lib/syslinux/modules/bios/ldlinux.c32'])
            (testdir / 'pxelinux.cfg').mkdir(exist_ok=True)
            (testdir / 'pxelinux.cfg/default').write_text(
                'DEFAULT linux\n'
                'LABEL linux\n'
                '  IPAPPEND 2\n'
                '  KERNEL vmlinuz\n'
                '  INITRD initrd.img\n'
                '  APPEND ' + ' '.join([
                    'boot=live',
                    (f'netboot=cifs nfsopts=ro,guest,vers=3.1.1 nfsroot=//{smb_address}/qemu live-media-path='
                     if have_smbd else
                     f'fetch=tftp://{tftp_address}/filesystem.squashfs'),
                    common_boot_args]))
        domain = subprocess.check_output(['hostname', '--domain'], text=True).strip()
        # We use guestfwd= to forward ldaps://10.0.2.100 to the real LDAP server.
        # We need a simple A record in the guest.
        # This is a quick-and-dirty way to achieve that (FIXME: do better).
        subprocess.check_call([
            # NOTE: doesn't need root privs
            'qemu-system-x86_64',
            '--enable-kvm',
            '--machine', 'q35',
            '--cpu', 'host',
            '-m', '2G' if template_wants_GUI else '512M',
            '--smp', '2',
            # no virtio-sound in qemu 6.1 ☹
            '--device', 'ich9-intel-hda', '--device', 'hda-output',
            *(['--nographic', '--vga', 'none']
              if not template_wants_GUI else
              ['--device', 'qxl-vga']
              if args.virtual_only else
              ['--device', 'virtio-vga']
              if not args.opengl_for_boot_test_ssh else
              ['--device', 'virtio-vga-gl', '--display', 'gtk,gl=on']),
            '-usb', '-device', 'usb-host,vendorid=0x0471,productid=0x0815',
            '--net', 'nic,model=virtio',
            '--net', ','.join([
                'user',
                f'net={network}',  # 10.0.2.0/24 or 10.128.2.0/24
                f'hostname={args.template}.{domain}',
                f'dnssearch={domain}',
                f'hostfwd=tcp::{args.host_port_for_boot_test_ssh}-:22',
                *([f'smb={testdir}'] if have_smbd else []),
                *([f'tftp={testdir}', 'bootfile=pxelinux.0']
                  if args.netboot_only else []),
            ]),
            *(['--kernel', testdir / 'vmlinuz',
               '--initrd', testdir / 'initrd.img',
               '--append', ' '.join([
                   'boot=live plainroot root=/dev/vda',
                   common_boot_args]),
               '--drive', f'file={testdir}/filesystem.squashfs,format=raw,media=disk,if=virtio,readonly=on']
              if not args.netboot_only else []),
            *maybe_dummy_DVD(testdir),
            *(['--drive', f'file={dummy_path},format=raw,media=disk,if=virtio',
               '--boot', 'order=n']  # don't try to boot off the dummy disk
              if template_wants_disks else [])])

for host in args.upload_to:
    subprocess.check_call(
        ['rsync', '-aihh', '--info=progress2', '--protect-args',
         # FIXME: remove the next line once omega-understudy is gone!
         '--chown=dnsmasq:nogroup' if re.fullmatch(r'(root@)light(\.cyber\.com\.au)?', host) else
         '--chown=0:0',  # don't use UID:GID of whoever built the images!
         # FIXME: need --bwlimit=1MiB here if-and-only-if the host is a production server.
         f'--copy-dest=/srv/netboot/images/{args.template}-latest',
         f'{destdir}/',
         f'{host}:/srv/netboot/images/{destdir.name}/'])
    rename_proc = subprocess.run(
        ['ssh', host, f'mv -vT /srv/netboot/images/{args.template}-latest /srv/netboot/images/{args.template}-previous'],
        check=False)
    if rename_proc.returncode != 0:
        # This is the first time uploading this template to this host.
        # Create a fake -previous so later commands can assume there is ALWAYS a -previous.
        subprocess.check_call(
            ['ssh', host, f'ln -vnsf {destdir.name} /srv/netboot/images/{args.template}-previous'])
    # NOTE: this stuff all assumes PrisonPC.
    subprocess.check_call([
        'ssh', host,
        f'[ ! -d /srv/netboot/images/{args.template}-previous/site.dir ] || '
        f'cp -at /srv/netboot/images/{destdir.name}/ /srv/netboot/images/{args.template}-previous/site.dir'])
    subprocess.check_call(
        ['ssh', host,
         # FIXME: remove the next line once omega-understudy is gone!
         'runuser -u dnsmasq -- ' if re.fullmatch(r'(root@)light(\.cyber\.com\.au)?', host) else '',
         f'ln -vnsf {destdir.name} /srv/netboot/images/{args.template}-latest'])
    # FIXME: https://alloc.cyber.com.au/task/task.php?taskID=34581
    if re.fullmatch(r'(root@)tweak(\.prisonpc\.com)?', host):
        soes = set(subprocess.check_output(
            ['ssh', host, 'tca get soes'],
            text=True).strip().splitlines())
        soes |= {f'{args.template}-latest',
                 f'{args.template}-previous'}
        subprocess.run(
            ['ssh', host, 'tca set soes'],
            text=True,
            check=True,
            input='\n'.join(sorted(soes)))
        # Sync /srv/netboot to /srv/tftp &c.
        subprocess.check_call(['ssh', host, 'tca', 'commit'])
    # FIXME: remove the next line once omega-understudy is gone!
    if re.fullmatch(r'(root@)light(\.cyber\.com\.au)?', host) and args.template == 'understudy':
        subprocess.check_call([
            'ssh', host,
            'runuser -u dnsmasq -- '
            f'ln -nsf ../understudy-omega.cpio /srv/netboot/images/{destdir.name}/omega.cpio'])

if args.github_release:
    # FIXME: Just put these imports up the top with the other imports
    import hashlib
    import github

    # NOTE: Uploading release assets simply will not work with user/pass credentials,
    #       you must generate a personal access token as documented here:
    #       https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/creating-a-personal-access-token
    gh_pass_path, = (p for p in pypass.PasswordStore().get_passwords_list() if pathlib.Path(p).name == 'api.github.com')
    gh_token = pypass.PasswordStore().get_decrypted_password(gh_pass_path).splitlines()[0]
    gh = github.Github(gh_token)

    gh_repo = gh.get_repo(args.github_release)
    gh_release = gh_repo.create_git_release(
        # This will create a git tag matching the current HEAD
        tag=destdir.name, target_commitish=git_description,
        name=' '.join((
            args.template.replace('-', ' ').title(),
            '-'.join(destdir.name.rsplit('-', 4)[1:]),
        )),
        message="FIXME: To be filled out manually",
        draft=True,
    )
    # gh_release = gh_repo.get_release(args.github_release.split(':', 1)[1])

    sha3sums = {}
    for f in destdir.iterdir():
        # FIXME: upload_asset does not support pathlib.
        #        In a later version there is an upload_asset_from_memory function which will take a file-like object instead.
        # FIXME: Theoretically we can add a progress bar of some sort if we were to wrap the read() of a file-like object.
        sha3sums[f.name] = hashlib.sha3_224(f.read_bytes())
        print("Uploading", f.name, "to Github, with SHA3:", sha3sums[f.name].hexdigest())
        gh_release.upload_asset(str(f.resolve()))

    # FIXME: Sign this like --reproducible does?
    #        Would it be better to sign the tag? Would that even solve the same problems?
    (destdir / 'SHA3SUMS').write_text('\n'.join(
        # NOTE: sha3sum doesn't actually use '\t', but it still supports that for --check, and makes more sense to me
        f'{sha3sums[filename].hexdigest()}\t{filename}' for filename in sha3sums) + '\n')
    gh_release.upload_asset(str((destdir / 'SHA3SUMS').resolve()))

if args.remove_afterward:
    shutil.rmtree(destdir)
