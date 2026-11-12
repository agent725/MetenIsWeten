#!/bin/sh
if [ -z "$1" ] ; then
 echo "You must specify IP of which you accept SSH access!"
 exit 0
fi
sudo iptables -I INPUT 1 -p tcp -m tcp -s $1 --dport 22 -j ACCEPT
