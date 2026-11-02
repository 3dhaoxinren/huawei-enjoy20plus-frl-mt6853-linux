# v15 serial-fd 启动入口（历史已刷写版本）

> **必须先完成 Bootloader 解锁，才能刷入本项目镜像。未解锁不能直接刷入。操作前备份本机数据和原始分区。**

这些是 **2026-10-31 构建的启动入口**，以原有 SHA256 发布。它们是本机移植所用的启动分区附件，不包含完整 rootfs。

| 分区镜像 | 大小 | SHA256 |
| --- | --- | --- |
| frl-native-v15-serial-fd-ramdisk.img | 2 MiB | e652894be6aa5c87a46b783e075d50f92f8ede1fdf6d3830e79b4889fb36ccf1 |
| frl-native-v15-serial-fd-recovery_ramdisk.img | 32 MiB | e9c6c618495073605781057cdaeb3cbd6061f3be3941b8a9e60a32deaf7a4370 |

两个文件的 Android boot header 中 kernel_size 都为 0，包含相同 CPIO。它们不包含完整内核、完整 rootfs、userdata 或完整原厂固件。需要兼容且已允许自定义启动的 FRL 引导环境、原设备厂商内核/分区、正确准备的 rootfs 以及阶段标志。

实际检查还看到沿用的镜像头命令行含 `androidboot.hardware=qcom` 等模板字段；本次没有擅自改写它们。附件身份依据完整 SHA256 和历史 FRL 写入记录，而不能把头部字符串视为机型自动识别。不同引导器是否采用这些字段未做跨设备验证，禁止据此推断可通刷。

镜像 SHA256 与历史成功写入记录吻合。本次从镜像 gzip 段实际解包，检查了 24 个 CPIO 项，两份内容一致；未发现 SSH 私钥/authorized_keys、Wi-Fi 配置或设备身份分区文件。`image-inventory.json` 给出逐文件哈希和检查范围。模式扫描不构成对任意未知数据的绝对无隐私证明。

## 源码来源与边界

- `initramfs/` 下的七个脚本/JSON 文件直接取自上述镜像，保留原字节。
- `src/` 是本地保留的启动器、根 init、检查器、watchdog 和串口会话 C 源码；`serial-session-fd-fix.c` 是此次入口使用的 FD 修正方向。没有在本次重新交叉编译并证明 C 二进制位级复现。
- 当前根文件系统服务入口在仓库 `integration/` 中；它与 initramfs 内的 bootstrap 是不同层，不得按同名文件相互替换。
- 镜像内 BusyBox 为 Ubuntu busybox-static 1:1.37.0-7ubuntu1 arm64，静态关联 glibc 2.42-2ubuntu4；对应第三方源材料随 Release 提供，保留原许可证。

## 使用前提

请先阅读 [启动校验](../docs/DEPLOYMENT.md) 与 [已知缺项](../docs/STATUS.md)。当前没有发布可配套一键刷入的新版 rootfs，因此不提供忽略前置条件的一键刷机脚本。

写入分区会覆盖该分区原内容；必须先保留本机原始 ramdisk/recovery_ramdisk 及 SHA256，并核对机型、分区名、容量和恢复入口。恢复时使用本机原分区备份，而不是别人的校准/身份分区。

下载后在 Linux 中以 Release 附件目录为工作目录执行 `sha256sum -c SHA256SUMS`；Windows 可用 `Get-FileHash -Algorithm SHA256` 对照同一清单。不要从 Git 网页预览复制镜像内容，也不要只凭文件名判断版本。
