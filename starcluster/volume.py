#!/usr/bin/env python
import os
import time
import string
from starcluster.logger import log
from starcluster.node import Node
from starcluster.spinner import Spinner
from starcluster import static
from starcluster import utils
from starcluster.utils import print_timing
from starcluster import exception

class VolumeCreator(object):
    def __init__(self, cfg, add_to_cfg=False, keypair=None, device='/dev/sdz',
                 image_id=static.BASE_AMI_32, instance_type="m1.small",
                 shutdown_instance=False):
        self._cfg = cfg
        self._ec2 = cfg.get_easy_ec2()
        self._keypair = keypair
        self._key_location = None
        self._add_to_cfg = add_to_cfg
        self._resv = None
        self._instance = None
        self._volume = None
        self._device = device or '/dev/sdz'
        self._node = None
        self._image_id = image_id or BASE_AMI_32
        self._instance_type = instance_type or 'm1.small'
        self._shutdown = shutdown_instance
        self._security_group = None

    @property
    def security_group(self):
        if not self._security_group:
            self._security_group = self._ec2.get_or_create_group(static.VOLUME_GROUP,
                                               static.VOLUME_GROUP_DESCRIPTION,
                                               auth_ssh=True)
        return self._security_group

    def _request_instance(self, zone):
        for i in self.security_group.instances():
            if i.state in ['pending','running'] and i.placement == zone:
                log.info("Using existing instance %s in group %s" % \
                         (i.id,self.security_group.name))
                self._instance = Node(i, self._key_location, 'vol_host')
                break
        if not self._instance:
            log.info("No instance in group %s for zone %s, launching one now." % \
                     (self.security_group.name, zone))
            self._resv = self._ec2.run_instances(
                image_id=self._image_id,
                instance_type=self._instance_type,
                min_count=1, max_count=1,
                security_groups=[self.security_group.name],
                key_name=self._keypair,
                placement=zone,
                user_data='master',
            )
            instance = self._resv.instances[0]
            self._instance = Node(instance, self._key_location, 'vol_host')
        s = Spinner()
        log.info("Waiting for instance %s to come up..." % self._instance.id)
        s.start()
        while not self._instance.is_up():
            time.sleep(15)
        s.stop()
        return self._instance

    def _create_volume(self, size, zone):
        vol = self._ec2.conn.create_volume(size, zone)
        while vol.status != 'available':
            time.sleep(5)
            vol.update()
        self._volume = vol
        return self._volume

    def _determine_device(self):
        block_dev_map = self._instance.block_device_mapping
        for char in string.lowercase[::-1]:
            dev = '/dev/sd%s' % char
            if not block_dev_map.get(dev):
                self._device = dev
                return self._device

    def _attach_volume(self, instance_id, device):
        vol = self._volume
        vol.attach(instance_id, device)
        while True:
            vol.update()
            if vol.attachment_state() == 'attached':
                break
            time.sleep(5)
        return self._volume

    def _validate_image(self, image):
        i = self._ec2.get_image(image)
        if not i or i.id != image:
            raise exception.ValidationError(
                'image %s does not exist' % image
            )

    def _validate_zone(self, zone):
        z = self._ec2.get_zone(zone)
        if not z:
            raise exception.ValidationError(
                'zone %s does not exist' % zone
            )
        if z.state != 'available':
            log.warn('zone %s is not available at this time' % zone)
        return True

    def _validate_size(self, size):
        try:
            volume_size = int(size)
            if volume_size < 1:
                raise exception.ValidationError(
                    "volume_size must be an integer >= 1")
        except ValueError:
            raise exception.ValidationError("volume_size must be an integer")

    def _validate_device(self, device):
        if not utils.is_valid_device(device):
            raise exception.ValidationError("volume device %s is not valid" % \
                                            device)

    def validate(self, size, zone, device, image):
        self._validate_size(size)
        self._validate_zone(zone)
        self._validate_device(device)
        self._validate_image(image)

    def is_valid(self, size, zone, device, image):
        try:
            self.validate(size, zone, device, image)
            return True
        except exception.ValidationError,e:
            log.error(e.msg)
            return False

    def _partition_volume(self):
        self._instance.ssh.execute('echo ",,L" | sfdisk %s' % self._device,
                                   silent=False)

    def _format_volume_partitions(self):
        self._instance.ssh.execute('mkfs.ext3 %s' % (self._device + '1'),
                                   silent=False)

    def _load_keypair(self):
        ec2 = self._ec2; cfg = self._cfg
        if self._keypair:
            kp = ec2.get_keypair(self._keypair)
            key = cfg.get_key(kp.name)
            self._key_location = key.get('key_location','')
        else:
            for kp in ec2.keypairs:
                if self._cfg.keys.has_key(kp.name):
                    self._keypair = kp.name
                    self._key_location = kl = cfg.get_key(kp.name).get('key_location','')
                    if os.path.exists(kl) and os.path.isfile(kl):
                        self._key_location = kl
        log.info('Using keypair %s' % self._keypair)
        if not os.path.exists(self._key_location):
            raise exception.ValidationError(
                'key_location=%s does not exist.' % \
                self._key_location)
        elif not os.path.isfile(self._key_location):
            raise exception.ValidationError(
                'key_location=%s is not a file.' % \
                self._key_location)

    @print_timing("Creating volume")
    def create(self, volume_size, volume_zone):
        self.validate(volume_size, volume_zone, self._device, self._image_id)
        try:
            self._load_keypair()
            log.info(("Requesting host instance in zone %s to attach volume" + \
                     " to...") % volume_zone)
            instance = self._request_instance(volume_zone)
            self._determine_device()
            log.info("Creating %sGB volume in zone %s" % (volume_size,
                                                          volume_zone))
            vol = self._create_volume(volume_size, volume_zone)
            log.info("New volume id: %s" % vol.id)
            log.info("Attaching volume to instance %s" % instance.id)
            self._attach_volume(instance.id, self._device)
            log.info("Partitioning the volume")
            self._partition_volume()
            log.info("Formatting volume")
            self._format_volume_partitions()
            if self._shutdown:
                log.info("Detaching volume %s from instance %s" % \
                         (vol.id,self._instance.id))
                vol.detach()
                time.sleep(5)
                for i in self.security_group.instances():
                    log.info("Shutting down instance %s" % i.id)
                    i.terminate()
                log.info("Removing security group %s" % \
                         self.security_group.name)
                self.security_group.delete()
            else:
                log.info(("The volume host instance %s is still running. " + \
                         "Run 'starcluster stop volumecreator' to shut " + \
                         "down all volume host instances manually.") %
                         self._instance.id)
            return vol.id
        except Exception,e:
            log.error("exception thrown: %s" % e)
            if self._volume:
                self._volume.detach()
                time.sleep(5)
            if self._instance:
                self._instance.terminate()
