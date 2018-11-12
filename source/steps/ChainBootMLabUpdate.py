#!/usr/bin/python
#
# Copyright (c) 2003 Intel Corporation
# All rights reserved.
#
# Copyright (c) 2004-2006 The Trustees of Princeton University
# All rights reserved.


import string
import re
import os

import UpdateNodeConfiguration
import MakeInitrd
import StopRunlevelAgent
from Exceptions import *
import utils
import systeminfo
import BootAPI
import notify_messages
import time

import ModelOptions

def Run( vars, log ):
    """
    Load the kernel off of a node and boot to it.
    This step assumes the disks are mounted on SYSIMG_PATH.
    If successful, this function will not return. If it returns, no chain
    booting has occurred.

    Expect the following variables:
    SYSIMG_PATH           the path where the system image will be mounted
                          (always starts with TEMP_PATH)
    ROOT_MOUNTED          the node root file system is mounted
    NODE_SESSION             the unique session val set when we requested
                             the current boot state
    PLCONF_DIR               The directory to store PL configuration files in

    Sets the following variables:
    ROOT_MOUNTED          the node root file system is mounted
    """

    log.write( "\n\nStep: Chain booting node.\n" )

    # make sure we have the variables we need
    try:
        SYSIMG_PATH= vars["SYSIMG_PATH"]
        if SYSIMG_PATH == "":
            raise ValueError, "SYSIMG_PATH"

        PLCONF_DIR= vars["PLCONF_DIR"]
        if PLCONF_DIR == "":
            raise ValueError, "PLCONF_DIR"

        # its ok if this is blank
        NODE_SESSION= vars["NODE_SESSION"]

        NODE_MODEL_OPTIONS= vars["NODE_MODEL_OPTIONS"]

        PARTITIONS= vars["PARTITIONS"]
        if PARTITIONS == None:
            raise ValueError, "PARTITIONS"

    except KeyError, var:
        raise BootManagerException, "Missing variable in vars: %s\n" % var
    except ValueError, var:
        raise BootManagerException, "Variable in vars, shouldn't be: %s\n" % var

    ROOT_MOUNTED= 0
    if vars.has_key('ROOT_MOUNTED'):
        ROOT_MOUNTED= vars['ROOT_MOUNTED']

    if ROOT_MOUNTED == 0:
        log.write( "Mounting node partitions\n" )

        # simply creating an instance of this class and listing the system
        # block devices will make them show up so vgscan can find the planetlab
        # volume group
        systeminfo.get_block_device_list(vars, log)

        utils.sysexec( "vgscan", log )
        utils.sysexec( "vgchange -ay planetlab", log )

        utils.makedirs( SYSIMG_PATH )

        cmd = "mount %s %s" % (PARTITIONS["root"],SYSIMG_PATH)
        utils.sysexec( cmd, log )
        cmd = "mount -t proc none %s/proc" % SYSIMG_PATH
        utils.sysexec( cmd, log )
        cmd = "mount %s %s/vservers" % (PARTITIONS["vservers"],SYSIMG_PATH)
        utils.sysexec( cmd, log )

        ROOT_MOUNTED= 1
        vars['ROOT_MOUNTED']= 1


    # write out the session value /etc/planetlab/session
    try:
        session_file_path= "%s/%s/session" % (SYSIMG_PATH,PLCONF_DIR)
        session_file= file( session_file_path, "w" )
        session_file.write( str(NODE_SESSION) )
        session_file.close()
        session_file= None
        log.write( "Updated /etc/planetlab/session\n" )
    except IOError, e:
        log.write( "Unable to write out /etc/planetlab/session, continuing anyway\n" )


    log.write( "Copying epoxy_client for booting.\n" )
    utils.sysexec( "cp --preserve=mode %s/epoxy_client /tmp/epoxy_client" % (SYSIMG_PATH), log )

    BootAPI.save(vars)

    log.write( "Unmounting disks.\n" )
    utils.sysexec( "umount %s/vservers" % SYSIMG_PATH, log )
    utils.sysexec_noerr( "umount %s/proc" % SYSIMG_PATH, log )
    utils.sysexec_noerr( "umount %s/dev" % SYSIMG_PATH, log )
    utils.sysexec_noerr( "umount %s/sys" % SYSIMG_PATH, log )
    utils.sysexec( "umount %s" % SYSIMG_PATH, log )
    utils.sysexec( "vgchange -an", log )

    ROOT_MOUNTED= 0
    vars['ROOT_MOUNTED']= 0

    # Change runlevel to 'boot' prior to kexec.
    #StopRunlevelAgent.Run( vars, log )

    log.write( "Unloading modules and chain booting to new kernel.\n" )

    # further use of log after Upload will only output to screen
    # log.Upload("/root/.bash_eternal_history")

    # regardless of whether kexec works or not, we need to stop trying to
    # run anything
    cancel_boot_flag= "/tmp/CANCEL_BOOT"
    utils.sysexec( "touch %s" % cancel_boot_flag, log )

    # on 2.x cds (2.4 kernel) for sure, we need to shutdown everything
    # to get kexec to work correctly. Even on 3.x cds (2.6 kernel),
    # there are a few buggy drivers that don't disable their hardware
    # correctly unless they are first unloaded.

    #utils.sysexec_noerr( "ifconfig eth0 down", log )

    utils.sysexec_noerr( "killall dhclient", log )

    utils.sysexec_noerr( "umount -a -r -t ext2,ext3", log )
    utils.sysexec_noerr( "modprobe -r lvm-mod", log )

    # modules that should not get unloaded
    # unloading cpqphp causes a kernel panic
    blacklist = [ "floppy", "cpqphp", "i82875p_edac", "mptspi", "mlx_en", "mlx_core"]
    try:
        modules= file("/tmp/loadedmodules","r")

        for line in modules:
            module= string.strip(line)
            if module in blacklist :
                log.write("Skipping unload of kernel module '%s'.\n"%module)
            elif module != "":
                log.write( "Unloading %s\n" % module )
                utils.sysexec_noerr( "modprobe -r %s" % module, log )
                if "e1000" in module:
                    log.write("Unloading e1000 driver; sleeping 4 seconds...\n")
                    time.sleep(4)

        modules.close()
    except IOError:
        log.write( "Couldn't read /tmp/loadedmodules, continuing.\n" )

    try:
        modules= file("/proc/modules", "r")

        # Get usage count for USB
        usb_usage = 0
        for line in modules:
            try:
                # Module Size UsageCount UsedBy State LoadAddress
                parts= string.split(line)

                if parts[0] == "usb_storage":
                    usb_usage += int(parts[2])
            except IndexError, e:
                log.write( "Couldn't parse /proc/modules, continuing.\n" )

        modules.seek(0)

        for line in modules:
            try:
                # Module Size UsageCount UsedBy State LoadAddress
                parts= string.split(line)

                # While we would like to remove all "unused" modules,
                # you can't trust usage count, especially for things
                # like network drivers or RAID array drivers. Just try
                # and unload a few specific modules that we know cause
                # problems during chain boot, such as USB host
                # controller drivers (HCDs) (PL6577).
                # if int(parts[2]) == 0:
                if False and re.search('_hcd$', parts[0]):
                    if usb_usage > 0:
                        log.write( "NOT unloading %s since USB may be in use\n" % parts[0] )
                    else:
                        log.write( "Unloading %s\n" % parts[0] )
                        utils.sysexec_noerr( "modprobe -r %s" % parts[0], log )
            except IndexError, e:
                log.write( "Couldn't parse /proc/modules, continuing.\n" )
    except IOError:
        log.write( "Couldn't read /proc/modules, continuing.\n" )

    try:
        INTERFACE_SETTINGS= vars['INTERFACE_SETTINGS']
    except KeyError, e:
        raise BootManagerException, "No interface settings found in vars."

    # Determine M-Lab GCP target project based on machine fqdn.
    hostname = INTERFACE_SETTINGS.get('hostname', '')
    site = INTERFACE_SETTINGS.get('domainname', '').split('.')[0]
    if (not hostname or not site or
        len(site) != 5 or len(hostname) != 5 or site[4] == 't'):
        project = 'mlab-sandbox'
    elif hostname[4] == '4':
        project = 'mlab-staging'
    elif hostname[4] in ['1', '2', '3']:
        project = 'mlab-oti'
    else:
        # Default case for anything really weird.
        project = 'mlab-sandbox'

    # TODO: force sandbox project for testing.
    project = 'mlab-sandbox'
    ARGS = [
        # Disable interface naming by the kernel. Preserves the use of `eth0`, etc.
        "net.ifnames=0",

        # Canonical epoxy network configuration.
        "epoxy.hostname=%(hostname)s.%(domainname)s",
        "epoxy.interface=eth0",
        "epoxy.ipv4=%(ip)s/26,%(gateway)s,8.8.8.8,8.8.4.4",
        "epoxy.ip=%(ip)s::%(gateway)s:255.255.255.192:%(hostname)s.%(domainname)s:eth0::8.8.8.8",

        # ePoxy server & project.
        "epoxy.project=%(project)s",

        # ePoxy stage1 URL.
        "epoxy.stage1=https://boot-api-dot-%(project)s.appspot.com/v1/boot/%(hostname)s.%(domainname)s/stage1.json",
    ]

    INTERFACE_SETTINGS['project'] = project
    cmdline = ' '.join(ARGS)
    kargs = cmdline % INTERFACE_SETTINGS

    fd = open("/tmp/cmdline", 'w')
    fd.write(kargs)
    fd.close()

    utils.sysexec_noerr( 'hwclock --systohc --utc ', log )
    utils.breakpoint ("Before epoxy_client")
    try:
        utils.sysexec( '/tmp/epoxy_client -cmdline /tmp/cmdline -action epoxy.stage1 -add-kargs', log)

    except BootManagerException, e:
        # if kexec fails, we've shut the machine down to a point where nothing
        # can run usefully anymore (network down, all modules unloaded, file
        # systems unmounted. write out the error, and cancel the boot process

        log.write( "\n\n" )
        log.write( "-------------------------------------------------------\n" )
        log.write( "kexec failed with the following error. Please report\n" )
        log.write( "this problem to support@planet-lab.org.\n\n" )
        log.write( str(e) + "\n\n" )
        log.write( "The boot process has been canceled.\n" )
        log.write( "-------------------------------------------------------\n\n" )

    return
