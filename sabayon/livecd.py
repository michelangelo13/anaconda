#
# livecd.py
#
# Copyright (C) 2010 Fabio Erculiani
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

# System imports
import os
import statvfs
import subprocess
import commands
import stat
import time
import shutil

import gettext
_ = lambda x: gettext.ldgettext("anaconda", x)

# Anaconda imports
import storage
import flags
from constants import productPath as PRODUCT_PATH, productName as PRODUCT_NAME, \
    DISPATCH_BACK
import backend
import isys
import iutil
import logging
from anaconda_log import PROGRAM_LOG_FILE
import sabayon.utils
from sabayon import Entropy

# Entropy imports
from entropy.const import etpConst, const_kill_threads
from entropy.misc import TimeScheduled, ParallelTask
from entropy.cache import EntropyCacher
import entropy.tools

log = logging.getLogger("anaconda")

class LiveCDCopyBackend(backend.AnacondaBackend):

    def __init__(self, anaconda):
        backend.AnacondaBackend.__init__(self, anaconda)
        flags.livecdInstall = True
        self.supportsUpgrades = True
        self.supportsPackageSelection = False
        self._root = anaconda.rootPath

        self.osimg = anaconda.methodstr[8:]
        if not os.path.ismount(self.osimg):
            anaconda.intf.messageWindow(_("Unable to find image"),
               _("The given location [%s] isn't a valid %s "
                 "live CD to use as an installation source.")
               %(self.osimg, PRODUCT_NAME), type = "custom",
               custom_icon="error",
               custom_buttons=[_("Exit installer")])
            raise SystemExit(1)

    def _getLiveSize(self):
        st = os.statvfs(PRODUCT_PATH)
        compressed_byte_size = st.f_blocks * st.f_bsize
        return compressed_byte_size * 3 # 3 times is enough

    def _getLiveSizeMB(self):
        return self._getLiveSize() / 1048576

    def postAction(self, anaconda):
        try:
            anaconda.storage.umountFilesystems(swapoff = False)
            os.rmdir(self._root)
        except Exception, e:
            log.error("Unable to unmount filesystems: %s" % e)

    def checkSupportedUpgrade(self, anaconda):
        if anaconda.dir == DISPATCH_BACK:
            return

    def doPreInstall(self, anaconda):
        self._progress = sabayon.utils.SabayonProgress(anaconda)
        self._progress.start()
        self._entropy = Entropy()
        self._entropy.connect_progress(self._progress)
        self._sabayon_install = sabayon.utils.SabayonInstall(anaconda)
        # We use anaconda.upgrade as bootloader recovery step
        self._bootloader_recovery = anaconda.upgrade
        self._install_grub = not self.anaconda.dispatch.stepInSkipList("instbootloader")

    def doInstall(self, anaconda):

        # Disable internal Anaconda bootloader setup, doesn't support GRUB2
        anaconda.dispatch.skipStep("instbootloader", skip = 1)

        if self._bootloader_recovery:
            log.info("Preparing to recover Sabayon")
            self._progress.set_label(_("Recovering Sabayon."))
            self._progress.set_fraction(0.0)
            return
        else:
            log.info("Preparing to install Sabayon")

        self._progress.set_label(_("Installing Sabayon onto hard drive."))
        self._progress.set_fraction(0.0)

        # Actually install
        self._sabayon_install.live_install()
        self._sabayon_install.setup_users()
        self._sabayon_install.setup_language() # before ldconfig, thx
        # if simple networking is enabled, disable NetworkManager
        if self.anaconda.instClass.simplenet:
            self._sabayon_install.setup_manual_networking()
        else:
            self._sabayon_install.setup_networkmanager_networking()
        self._sabayon_install.setup_keyboard()

        action = _("Configuring Sabayon")
        self._progress.set_label(action)
        self._progress.set_fraction(0.9)

        self._sabayon_install.setup_sudo()
        self._sabayon_install.setup_audio()
        self._sabayon_install.setup_xorg()
        self._sabayon_install.remove_proprietary_drivers()
        try:
            self._sabayon_install.setup_nvidia_legacy()
        except Exception as e:
            # caused by Entropy bug <0.99.47.2, remove in future
            log.error("Unable to install legacy nvidia drivers: %s" % e)

        self._progress.set_fraction(0.95)
        self._sabayon_install.configure_services()
        self._sabayon_install.copy_udev()
        self._sabayon_install.env_update()
        self._sabayon_install.spawn_chroot("locale-gen", silent = True)
        self._sabayon_install.spawn_chroot("ldconfig")
        # Fix a possible /tmp problem
        self._sabayon_install.spawn("chmod a+w "+self._root+"/tmp")
        var_tmp = self._root + "/var/tmp"
        if not os.path.isdir(var_tmp): # wtf!
            os.makedirs(var_tmp)
        var_tmp_keep = os.path.join(var_tmp, ".keep")
        if not os.path.isfile(var_tmp_keep):
            with open(var_tmp_keep, "w") as wt:
                wt.flush()

        action = _("Sabayon configuration complete")
        self._progress.set_label(action)
        self._progress.set_fraction(1.0)

    def doPostInstall(self, anaconda):

        self._sabayon_install.language_packs_install()
        self._sabayon_install.setup_entropy_mirrors()
        self._sabayon_install.emit_install_done()

        storage.writeEscrowPackets(anaconda)

        self._sabayon_install.destroy()
        if hasattr(self._entropy, "shutdown"):
            self._entropy.shutdown()
        else:
            self._entropy.destroy()
            EntropyCacher().stop()

        const_kill_threads()
        anaconda.intf.setInstallProgressClass(None)

    def writeConfiguration(self):
        """
        System configuration is written in anaconda.write().
        Add extra config files setup here.
        """

        # Write critical configuration not automatically written
        self.anaconda.storage.fsset.write()

        log.info("Do we need to run GRUB2 setup? => %s" % (self._install_grub,))

        if self._install_grub:

            # HACK: since Anaconda doesn't support grub2 yet
            # Grub configuration is disabled
            # and this code overrides it
            encrypted = self._setup_grub2()
            if encrypted:
                # HACK: since swap device path value is potentially changed
                # it is required to rewrite the fstab (circular dependency, sigh)
                self.anaconda.storage.fsset.write()

        self._copy_logs()

    def _copy_logs(self):

        # copy log files into chroot
        isys.sync()
        config_files = ["/tmp/anaconda.log", "/tmp/lvmout", "/tmp/resize.out",
             "/tmp/program.log", "/tmp/storage.log"]
        install_dir = self._root + "/var/log/installer"
        if not os.path.isdir(install_dir):
            os.makedirs(install_dir)
        for config_file in config_files:
            if not os.path.isfile(config_file):
                continue
            dest_path = os.path.join(install_dir, os.path.basename(config_file))
            shutil.copy2(config_file, dest_path)

    def _get_bootloader_args(self):

        # look for kernel arguments we know should be preserved and add them
        ourargs = ["speakup_synth=", "apic", "noapic", "apm=", "ide=", "noht",
            "acpi=", "video=", "vga=", "init=", "splash=", "console=",
            "pci=routeirq", "irqpoll", "nohdparm", "pci=", "floppy.floppy=",
            "all-generic-ide", "gentoo=", "res=", "hsync=", "refresh=", "noddc",
            "xdriver=", "onlyvesa", "nvidia=", "dodmraid", "dmraid",
            "sabayonmce", "quiet", "scandelay=", "doslowusb", "docrypt" ]

        # Sabayon MCE install -> MCE support
        # use reference, yeah
        cmdline = self._sabayon_install.cmdline
        if Entropy.is_sabayon_mce() and ("sabayonmce" not in cmdline):
            cmdline.append("sabayonmce")

        usb_storage_dir = "/sys/bus/usb/drivers/usb-storage"
        if os.path.isdir(usb_storage_dir):
            for cdir, subdirs, files in os.walk(usb_storage_dir):
                subdirs = set(subdirs)
                subdirs.discard("module")
                if subdirs:
                    cmdline.append("doslowusb")
                    cmdline.append("scandelay=10")
                    break

        previous_vga = None
        final_cmdline = []
        for arg in cmdline:
            for check in ourargs:
                if arg.startswith(check):
                    final_cmdline.append(arg)
                    if arg.startswith("vga="):
                        if previous_vga in final_cmdline:
                            final_cmdline.remove(previous_vga)
                        previous_vga = arg

        fsset = self.anaconda.storage.fsset
        swap_devices = fsset.swapDevices
        # <storage.devices.Device> subclass
        root_device = self.anaconda.storage.rootDevice
        # device.format.mountpoint, device.format.type, device.format.mountable,
        # device.format.options, device.path, device.fstabSpec
        root_crypted = False
        swap_crypted = False
        delayed_crypt_swap = None

        if swap_devices:
            log.info("Found swap devices: %s" % (swap_devices,))
            swap_dev = swap_devices[0]

            swap_crypto_dev = None
            for name in fsset.cryptTab.mappings.keys():
                swap_crypto_dev = fsset.cryptTab[name]['device']
                if swap_dev == swap_crypto_dev or swap_dev.dependsOn(
                    swap_crypto_dev):
                    swap_crypted = True
                    break

            if swap_crypted:
                # genkernel hardcoded bullshit, cannot change /dev/mapper/swap
                # change inside swap_dev, fstabSpec should return /dev/mapper/swap
                swap_dev._name = "swap"
                final_cmdline.append("resume=swap:%s" % (swap_dev.path,))
                final_cmdline.append("real_resume=%s" % (swap_dev.path,))
                # NOTE: cannot use swap_crypto_dev.fstabSpec because
                # genkernel doesn't support UUID= on crypto
                delayed_crypt_swap = swap_crypto_dev.path
            else:
                final_cmdline.append("resume=swap:%s" % (swap_dev.fstabSpec,))
                final_cmdline.append("real_resume=%s" % (swap_dev.fstabSpec,))

        # setup LVM
        lvscan_out = commands.getoutput("LANG=C LC_ALL=C lvscan").split("\n")[0].strip()
        if not lvscan_out.startswith("No volume groups found"):
            final_cmdline.append("dolvm")

        crypto_dev = None
        for name in fsset.cryptTab.mappings.keys():
            crypto_dev = fsset.cryptTab[name]['device']
            if root_device == crypto_dev or root_device.dependsOn(crypto_dev):
                root_crypted = True
                break

        def translate_real_root(root_device):
            if isinstance(root_device, storage.devices.MDRaidArrayDevice):
                return root_device.path
            return root_device.fstabSpec

        crypt_root = None
        if root_crypted:
            log.info("Root crypted? %s, %s, crypto_dev: %s" % (root_crypted,
                root_device.path, crypto_dev.path))

            # NOTE: cannot use crypto_dev.fstabSpec because
            # genkernel doesn't support UUID= on crypto
            final_cmdline.append("root=%s crypt_root=%s" % (
                translate_real_root(root_device), crypto_dev.path,))
            # due to genkernel initramfs stupidity, when crypt_root = crypt_swap
            # do not add crypt_swap.
            if delayed_crypt_swap == crypto_dev.path:
                delayed_crypt_swap = None

        else:
            log.info("Root crypted? Nope!")
            final_cmdline.append("root=%s" % (
                translate_real_root(root_device),))

        # always add docrypt, loads kernel mods required by cryptsetup devices
        if "docrypt" not in final_cmdline:
            final_cmdline.append("docrypt")

        if delayed_crypt_swap:
            final_cmdline.append("crypt_swap=%s" % (delayed_crypt_swap,))

        log.info("Generated boot cmdline: %s" % (final_cmdline,))

        return final_cmdline, root_crypted, swap_crypted

    def _setup_grub2(self):

        cmdline_args, root_crypted, swap_crypted = self._get_bootloader_args()

        log.info("_setup_grub2, cmdline_args: %s | "
            "root_crypted: %s | swap_crypted: %s" % (cmdline_args,
            root_crypted, swap_crypted,))

        # "sda" <string>
        grub_target = self.anaconda.bootloader.getDevice()
        try:
            # <storage.device.PartitionDevice>
            boot_device = self.anaconda.storage.mountpoints["/boot"]
        except KeyError:
            boot_device = self.anaconda.storage.mountpoints["/"]

        cmdline_str = ' '.join(cmdline_args)

        # if root_device or swap encrypted, replace splash=silent
        if root_crypted or swap_crypted:
            cmdline_str = cmdline_str.replace('splash=silent', 'splash=verbose')

        log.info("_setup_grub2, grub_target: %s | "
            "boot_device: %s | cmdline_str: %s" % (grub_target,
            boot_device, cmdline_str,))

        self._write_grub2(cmdline_str, grub_target)
        # disable Anaconda bootloader code
        self.anaconda.bootloader.defaultDevice = -1
        return root_crypted or swap_crypted

    def _write_grub2(self, cmdline, grub_target):

        default_file_noroot = "/etc/default/grub"
        grub_cfg_noroot = "/boot/grub/grub.cfg"

        log.info("%s: %s => %s\n" % ("_write_grub2", "begin", locals()))

        # setup grub variables
        # this file must exist

        # drop vga= from cmdline
        #cmdline = ' '.join([x for x in cmdline.split() if \
        #    not x.startswith("vga=")])

        # Since Sabayon 5.4, we also write to /etc/default/sabayon-grub
        grub_sabayon_file = self._root + "/etc/default/sabayon-grub"
        grub_sabayon_dir = os.path.dirname(grub_sabayon_file)
        if not os.path.isdir(grub_sabayon_dir):
            os.makedirs(grub_sabayon_dir)
        with open(grub_sabayon_file, "w") as f_w:
            f_w.write("# this file has been added by the Anaconda Installer\n")
            f_w.write("# containing default installer bootloader arguments.\n")
            f_w.write("# DO NOT EDIT NOR REMOVE THIS FILE DIRECTLY !!!\n")
            f_w.write('GRUB_CMDLINE_LINUX="${GRUB_CMDLINE_LINUX} %s"\n' % (
                cmdline,))
            f_w.flush()

        if self.anaconda.bootloader.password and self.anaconda.bootloader.pure:
            # still no proper support, so implement what can be implemented
            # XXX: unencrypted password support
            pass_file = self._root + "/etc/grub.d/00_password"
            f_w = open(pass_file, "w")
            f_w.write("""\
set superuser="root"
password root """+str(self.anaconda.bootloader.pure)+"""
            """)
            f_w.flush()
            f_w.close()

        # remove device.map if found
        dev_map = self._root + "/boot/grub/device.map"
        if os.path.isfile(dev_map):
            os.remove(dev_map)

        # this must be done before, otherwise gfx mode is not enabled
        iutil.execWithRedirect('/sbin/grub2-install',
            ["/dev/" + grub_target, "--recheck"],
            stdout = PROGRAM_LOG_FILE,
            stderr = PROGRAM_LOG_FILE,
            root = self._root
        )

        iutil.execWithRedirect('/sbin/grub-mkconfig',
            ["--output=%s" % (grub_cfg_noroot,)],
            stdout = PROGRAM_LOG_FILE,
            stderr = PROGRAM_LOG_FILE,
            root = self._root
        )

        log.info("%s: %s => %s\n" % ("_write_grub2", "end", locals()))

    def kernelVersionList(self, rootPath = "/"):
        """
        This won't be used, because our Anaconda codebase is using grub2
        """
        return []

    def doBackendSetup(self, anaconda):

        ossize = self._getLiveSizeMB()
        slash = anaconda.storage.rootDevice
        if slash.size < ossize:
            rc = anaconda.intf.messageWindow(_("Error"),
                _("The root filesystem you created is "
                  "not large enough for this live "
                  "image (%.2f MB required).") % ossize,
                type = "custom",
                custom_icon = "error",
                custom_buttons=[_("_Back"),
                                _("_Exit installer")])
            if rc == 0:
                return DISPATCH_BACK
            else:
                raise SystemExit(1)

    # package/group selection doesn't apply for this backend
    def groupExists(self, group):
        pass

    def selectGroup(self, group, *args):
        pass

    def deselectGroup(self, group, *args):
        pass

    def selectPackage(self, pkg, *args):
        pass

    def deselectPackage(self, pkg, *args):
        pass

    def packageExists(self, pkg):
        return True

    def getDefaultGroups(self, anaconda):
        return []

    def writePackagesKS(self, f, anaconda):
        pass