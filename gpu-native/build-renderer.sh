#!/bin/sh
set -eu
cd "$(dirname "$0")"
G=/mnt/d/FRL-Work/driver-adaptation/gpu
V=/mnt/d/FRL-Work/driver-adaptation/vendor-selected-native10/runtime/lib64/bionic
mkdir -p build
aarch64-linux-gnu-gcc -std=gnu11 -Wall -Wextra -Werror -Wno-misleading-indentation -O2 -fPIE -fno-stack-protector -fno-builtin -D__ANDROID__ -I "$G/ndk-inputs" -c renderer.c -o build/renderer.o
aarch64-linux-gnu-gcc -nostdlib -pie -Wl,-e,_start,-z,relro,-z,now,--no-undefined -Wl,--dynamic-linker=/apex/com.android.runtime/bin/linker64,-rpath-link,"$G/build/stock-link" "$G/ndk-inputs/crtbegin_dynamic.o" build/renderer.o -L "$V" -lc -ldl "$G/ndk-inputs/crtend_android.o" -o build/frl-gpu-renderer
sha256sum build/frl-gpu-renderer
