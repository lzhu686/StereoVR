#!/bin/bash

# Copyright (c) 2018-2021, NVIDIA CORPORATION.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

curdir=$(cd $1 && pwd);
echo $curdir
showlogs=0;
if [ "$1" = "--showlogs" ]; then
	showlogs=1;
fi;

# Find devices to flash
devpaths=($(find /sys/bus/usb/devices/usb*/ \
		-name devnum -print0 | {
	found=()
	while read -r -d "" fn_devnum; do
		dir="$(dirname "${fn_devnum}")"
		vendor="$(cat "${dir}/idVendor")"
		if [ "${vendor}" != "0955" ]; then
			continue
		fi
		product="$(cat "${dir}/idProduct")"
		case "${product}" in
		"7721") ;;
		"7f21") ;;
		"7018") ;;
		"7c18") ;;
		"7121") ;;
		"7019") ;;
		"7819") ;;
		"7e19") ;;
		"7418") ;;
		*)
			continue
			;;
		esac
		fn_busnum="${dir}/busnum"
		if [ ! -f "${fn_busnum}" ]; then
			continue
		fi
		fn_devpath="${dir}/devpath"
		if [ ! -f "${fn_devpath}" ]; then
			continue
		fi
		busnum="$(cat "${fn_busnum}")"
		devpath="$(cat "${fn_devpath}")"
		found+=("${busnum}-${devpath}")
	done
	echo "${found[@]}"
}))

# Exit if no devices to flash
if [ ${#devpaths[@]} -eq 0 ]; then
	echo "No devices to flash"
	exit 1
fi

# Create a folder for saving log
mkdir -p mfilogs;
pid="$$"
ts=`date +%Y%m%d-%H%M%S`;

# Clear old gpt crufts
rm -f mbr_* gpt_*;

# Flash all devices in background
flash_pids=()
for devpath in "${devpaths[@]}"; do
	fn_log="mfilogs/${ts}_${pid}_flash_${devpath}.log"
	cmd="${curdir}/nvaflash.sh ${devpath}";
	${cmd} > "${fn_log}" 2>&1 &
	flash_pid="$!";
	flash_pids+=("${flash_pid}")
	echo "Start flashing device: ${devpath}, PID: ${flash_pid}";
	if [ ${showlogs} -eq 1 ]; then
		gnome-terminal -e "tail -f ${fn_log}" -t ${fn_log} > /dev/null 2>&1 &
	fi;
done

# Wait until all flash processes done
failure=0
while true; do
	running=0
	if [ ${showlogs} -ne 1 ]; then
		echo -n "Ongoing processes:"
	fi;
	new_flash_pids=()
	for flash_pid in "${flash_pids[@]}"; do
		if [ -e "/proc/${flash_pid}" ]; then
			if [ ${showlogs} -ne 1 ]; then
				echo -n " ${flash_pid}"
			fi;
			running=$((${running} + 1))
			new_flash_pids+=("${flash_pid}")
		else
			wait "${flash_pid}" || failure=1
		fi
	done
	if [ ${showlogs} -ne 1 ]; then
		echo
	fi;
	if [ ${running} -eq 0 ]; then
		break
	fi
	flash_pids=("${new_flash_pids[@]}")
	sleep 5
done

if [ ${failure} -ne 0 ]; then
	echo "Flash complete (WITH FAILURES)";
	exit 1
fi

echo "Flash complete (SUCCESS)"
