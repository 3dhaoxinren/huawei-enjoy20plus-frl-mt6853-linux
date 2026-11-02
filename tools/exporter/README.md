# 华为畅享 20 Plus：系统导出工具

用于在另一台电脑提取当前 FRL 原生 Linux 系统，供后续净化、重建完整 rootfs 和编写配套安装流程。**它是导出工具，不是刷机工具；输出也不是可直接刷入的镜像。**

## 需要什么

- 手机已进入当前可用的 FRL 原生 Linux，连接与电脑互通的网络。
- 电脑有 Python 3.10 或以上（图形界面需 Tkinter；Windows 常规 Python 安装包含它），以及 OpenSSH 客户端。
- 能以 `frl` 用户通过 SSH 公钥登录，并使用 `sudo -n`。
- 电脑输出盘至少有 8 GiB 空闲。导出时不要安装/更新软件或改系统配置。

**新电脑不会自动拥有旧电脑的 SSH 登录权限。** 如果没有已授权密钥，先通过手机本地终端或另一台已授权电脑，把新电脑的 SSH 公钥加入手机的 `/etc/frl/ssh/authorized_keys/frl`。不要把 SSH 私钥发给别人，也不必开启密码登录。

首次 SSH 连接先在手机终端查看：

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

然后在电脑执行 `ssh frl@手机实际IP`，核对显示的指纹后再接受。工具要求严格主机密钥检查，不会自动信任陌生主机。如果私钥有密码，请先用本机 ssh-agent / `ssh-add` 解锁；工具不保存私钥密码。

## Windows 图形界面

1. 解压工具包，双击 **启动导出工具.cmd**。
2. 填手机当前 IP、用户名 `frl`、端口 `22`。
3. 若使用指定 SSH 私钥，点“选择文件”；已经能正常 SSH 登录时可以留空。
4. 选择电脑保存目录，先点“检查连接”，通过后点“开始导出”。
5. 等界面提示完成。不要仅凭看到 `.partial` 文件就认为导出成功。

界面显示已接收 MiB 和用时；最后会完整读取归档进行 CRC、结构和 payload 校验，因此接收结束后还需要等待校验。

## 命令行（工具文件夹中运行）

```sh
python collect_frl.py --host 手机实际IP --check-only --output ./FRL-export
python collect_frl.py --host 手机实际IP --output ./FRL-export
```

可加 `--identity SSH私钥文件路径`、`--user frl`、`--port 22`。这些命令是参数用法；用实际 IP 和本机路径替换中文参数。

## 导出后私下提供整个输出文件夹

每次创建独立的 `frl-private-export-日期-时间-随机后缀` 目录：

- `system-files.private.tar.gz`：当前 `/etc`、`/usr`、`/opt`、必要 `/var`、`/boot` 和 usrmerge 链接，保留 Unix UID/GID、权限、硬链接、ACL/xattrs。
- `device-info.private.json`：内核/系统版本、软件包版本、挂载关系、分区名称与容量、部署标志、完整 payload 哈希、所需空目录元数据；可读取时附内核配置和只读厂商 build.prop。
- `result.json`：必须有 `complete: true` 且 `check_only: false`，并包含归档 SHA256、字节数、校验条目数。
- `export.private.log`：导出过程和诊断信息。
- `DO-NOT-PUBLISH.txt`：材料仍需隐私审查的提示。

可以把整个目录再打成 ZIP 私下交付。**不要把导出结果直接传到公开 GitHub。** 常见敏感文件虽已排除，剩余自定义配置仍可能有凭据；后续需逐项审查并在全新文件系统中重建。

## 默认不提取的内容

- `/home`、`/root`、`/srv` 的个人文件和应用数据。
- SSH 主机密钥、已授权公钥、shadow/gshadow、Wi-Fi 配置、固定 MAC、machine-id、随机种子。
- 日志、缓存、备份、容器数据、常见数据库目录。
- `/dev`、`/proc`、`/sys`、`/run` 的目录内容、16 GiB swap 和正在使用的 rootfs 原始文件。
- userdata 整分区，以及 NVRAM、NVDATA、OEMINFO、PERSIST 等身份/校准分区的原始内容。

分区布局只读 sysfs 的名称/容量，不读取原始分区。内核配置等元数据只读指定文件，不递归复制 `/proc` 或 `/sys`。

## 失败处理和验证范围

- SSH 权限/主机指纹/连接失败：查看界面或私有日志，先修复正常 SSH 登录；不要关闭主机密钥检查。
- payload 校验不匹配、tar 报文件变化、前后软件包状态不同：保留日志，停止修改系统后重新导出，不要伪造成功标记。
- 断网或取消会保留 `.partial` 与 `complete: false`，不会覆盖已有导出。取消结束的是本机 SSH 导出连接，不会重启或停止手机系统服务。
- **这是在线文件级导出，不是全部可变状态的原子冷备。** 前后 payload 和软件包数据库一致不能证明每一个可变文件都同时取自同一瞬间。
- 工具已做离线测试；实际手机目前不在场，真实 SSH 导出、传输速度及设备兼容性仍待运行确认。

本工具默认不上传网络服务，也不修改手机文件、清理手机、冻结文件系统、刷写或重启。手机无需为本次导出进入 Fastboot；以后刷入发布镜像仍必须先完成 Bootloader 解锁。
