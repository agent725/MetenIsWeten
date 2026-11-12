#!/bin/sh
sudo systemctl disable --now serial-getty@ttyS0.service
sudo systemctl mask serial-getty@ttyS0.service
echo 'Also make sure to remove the console= line with ttyS0 from /boot/extlinux/extlinux.conf !'

