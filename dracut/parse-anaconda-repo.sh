#!/bin/bash
# parse-repo-options.sh: parse the inst.repo= arg and set root/netroot

repo="$(getarg repo= inst.repo=)"

if [ -n "$repo" ]; then
    splitsep ":" "$repo" repotype rest
    case "$repotype" in
        http|https|ftp|nfs|nfs4|nfsiso)
            set_neednet; root="anaconda-net" ;;
        hd|cd|cdrom)
            [ -n "$rest" ] && root="anaconda-disk:$rest" ;;
        *)
            warn "Invalid value for 'inst.repo': $repo" ;;
    esac
fi

if [ -z "$root" ]; then
    # No repo arg, no kickstart, and no root. Search for valid installer media.
    root="anaconda-auto-cd"
fi

# Make sure we wait for the dmsquash root device to appear
case "$root" in
    anaconda-*) wait_for_dev /dev/root ;;
esac

# We've got *some* root variable set.
# Set rootok so we can move on to anaconda-genrules.sh.
rootok=1