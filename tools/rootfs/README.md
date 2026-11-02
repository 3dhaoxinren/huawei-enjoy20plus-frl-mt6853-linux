# Rootfs 发布与离线校验工具

本目录提供本次镜像实际使用的首次启动初始化源码、8 项测试，以及不刷写手机的镜像校验器。

## 下载包校验

下载并解压系统镜像 Release 的 ZIP，在电脑执行：

```sh
python tools/verify_candidate.py
```

或者在本仓库执行，显式指定下载包目录：

```sh
python tools/rootfs/verify_candidate.py /path/to/extracted-release
```

脚本名 `verify_candidate.py` 为原构建工具名，不影响发布标题。它只读取 gzip、标志和构建报告；
验证压缩文件 SHA256、解压后的 4 GiB 大小/完整 SHA256、ext4 UUID/特性和 staged 标志，不向手机写入。
这些检查不能替代设备启动测试，也不是发布者身份的密码学签名。

## 首次启动

`release_firstboot.py` 在 `prepare-native.py` 之后、`claim-mutable-root.py` 与服务启动之前执行。
它生成每台设备独立的 machine-id、Wi-Fi 本地管理 MAC 和 SSH 主机密钥；不植入授权公钥或共享密码。
对已有有效身份保持不变，对不安全权限、符号链接和不匹配密钥明确拒绝。

在 Linux 临时测试环境以 root 运行（测试只操作自己的临时目录）：

```sh
python3 -B tools/rootfs/test_firstboot.py
```

真实初始化入口要求本机 PID 1 为 `frl-init`、当前根为 PID 1 的根，且 compatible 为 `mediatek,MT6853`。
不要在普通 PC 上手工调用 `initialize('/')`。

更多说明见 [本次系统镜像发布](../../docs/ROOTFS-20261102.md)。
