#!/bin/bash

# config_get SECTION KEY < FILE
# read an .ini-style config file, find the KEY in the given SECTION, and return
# the value provided for that key.
# ex: product=$(config_get Main Product < /.buildstamp)
config_get() {
    local section="$1" key="$2" cursec="" k="" v=""
    while read line; do
        case "$line" in
            \#*) continue ;;
            \[*\]*) cursec="${line#[}"; cursec="${cursec%%]*}" ;;
            *=*) k=$(echo ${line%%=*}); v=$(echo ${line#*=}) ;;
        esac
        if [ "$cursec" = "$section" ] && [ "$k" == "$key" ]; then
            echo $v
            break
        fi
    done
}

find_iso() {
    local f="" iso="" isodir="$1" tmpmnt=$(mkuniqdir /run/install tmpmnt)
    for f in $isodir/*.iso; do
        [ -e $f ] || continue
        mount -o loop,ro $f $tmpmnt || continue
        [ -e $tmpmnt/.discinfo ] && iso=$f
        umount $tmpmnt
        if [ "$iso" ]; then echo "$iso"; return 0; fi
    done
    return 1
}

find_runtime() {
    local ti_img="" dir="$1$2"
    [ -e $dir/.treeinfo ] && img=$(config_get stage2 mainimage < $dir/.treeinfo)
    for f in $ti_img images/install.img LiveOS/squashfs.img; do
        [ -e "$dir/$f" ] && echo "$dir/$f" && return
    done
}

repodir="/run/install/repo"
isodir="/run/install/isodir"
rulesfile="/etc/udev/rules.d/90-anaconda.rules"

anaconda_live_root_dir() {
    local img="" iso="" dir="$1" path="$2"; shift 2
    img=$(find_runtime $repodir$path)
    if [ -n "$img" ]; then
        info "anaconda: found $img"
    else
        iso=$(find_iso $repodir$path)
        [ -n "$iso" ] || { warn "no suitable images"; return 1; }
        info "anaconda: found $iso"
        mount --move $repodir $isodir
        iso=${isodir}${iso#$repodir}
        mount -o loop,ro $iso $repodir
        img=$(find_runtime $repodir) || { warn "$iso has no suitable runtime"; }
    fi
    [ -e "$img" ] && /sbin/dmsquash-live-root $img
}

# These could probably be in dracut-lib or similar

disk_to_dev_path() {
    case "$1" in
        CDLABEL=*|LABEL=*) echo "/dev/disk/by-label/${1#*LABEL=}" ;;
        UUID=*) echo "/dev/disk/by-uuid/${1#UUID=}" ;;
        /dev/*) echo "$1" ;;
        *) echo "/dev/$1" ;;
    esac
}

when_diskdev_appears() {
    local dev="${1#/dev/}" cmd=""; shift
    cmd="/sbin/initqueue --settled --onetime $*"
    {
        printf 'SUBSYSTEM=="block", KERNEL=="%s", RUN+="%s"\n' "$dev" "$cmd"
        printf 'SUBSYSTEM=="block", SYMLINK=="%s", RUN+="%s"\n' "$dev" "$cmd"
    } >> $rulesfile
}

set_neednet() {
    if ! getargbool 0 rd.neednet; then
        echo "rd.neednet=1" >> /etc/cmdline.d/anaconda-neednet.conf
    fi
    unset CMDLINE
}

parse_kickstart() {
    /sbin/parse-kickstart $1 > /etc/cmdline.d/80kickstart.conf
    unset CMDLINE  # re-read the commandline
    . /tmp/ks.info # save the parsed kickstart
    [ -e "$parsed_kickstart" ] && cp $parsed_kickstart /run/install/ks.cfg
}

# This is where we actually run the kickstart. Whee!
# We can't just add udev rules (we'll miss devices that are already active),
# and we can't just run the scripts manually (we'll miss devices that aren't
# yet active - think driver disks!).
#
# So: we have to write out the rules and then retrigger them.
#
# Really what we want to do here is just start over from the "cmdline"
# phase, but since we can't do that, we'll kind of fake it.
run_kickstart() {
    local do_disk="" do_net=""

    # kickstart's done - time to find a real root device
    [ "$root" = "anaconda-kickstart" ] && root=""

    # re-parse new cmdline stuff from the kickstart
    . $hookdir/cmdline/*parse-anaconda-repo.sh
    # TODO: parse for other stuff ks might set (updates, dd, etc.)
    case "$repotype" in
        http*|ftp|nfs*) do_net=1 ;;
        cdrom|hd|bd)    do_disk=1 ;;
    esac
    [ "$root" = "anaconda-auto-cd" ] && do_disk=1

    # replay udev events to trigger actions
    # NOTE: this line is deprecated and unnecessary in dracut 018
    [ -f /tmp/root.info ] && echo "root='$root'" >> /tmp/root.info
    if [ "$do_disk" ]; then
        . $hookdir/pre-udev/*repo-genrules.sh
        udevadm control --reload
        udevadm trigger --action=change --subsystem-match=block
    fi
    if [ "$do_net" ]; then
        udevadm trigger --action=online --subsystem-match=net
    fi

    # and that's it - we're back to the mainloop.
    > /tmp/ks.cfg.done # let wait_for_kickstart know that we're done.
}

wait_for_kickstart() {
    echo "[ -e /tmp/ks.cfg.done ]" > $hookdir/initqueue/finished/kickstart.sh
}