# 第三方组件与许可边界

本项目不是华为官方发行版。Huawei、Mali、Ubuntu 等名称归各自权利人所有。

- Linux 内核、Ubuntu 软件包、BusyBox、runit、Pango/Cairo、Glances 等保持各自许可证，不随自研代码一并改为 MIT。
- 厂商 GPU/Bionic、APEX 与其他厂商库未包含在此次源码快照中。已有构建脚本引用它们不表示本仓库提供或授予其再分发权。
- 原始 NDK 链接对象和头文件没有混入首次源码快照；需按构建环境取得相应 NDK，并保留上游许可证。
- 现有移植依赖的 Glances 历史版本为 4.5.8，独立安装、默认关闭；不随本次源码打包。
- 不发布设备 NVRAM/NVDATA/OEMINFO/PERSIST、SSH 主机私钥、个人 authorized_keys、Wi-Fi 配置、生产日志或整盘用户数据。

经仓库所有者确认，自研移植代码及文档采用根目录 LICENSE 所载 MIT 许可证。该授权不覆盖第三方组件，不改变 Linux、BusyBox、厂商驱动及其他上游材料原有许可证。镜像附件的组件和源代码材料以相应 Release 说明为准。

## 2026.11.02 系统镜像附件

首次“源码快照不含厂商库”的范围不能套用于后续 rootfs 附件。系统镜像保留了工作系统中必要的厂商派生运行库/固件；它们不因此成为 MIT 软件。
已整理 548 个 Ubuntu 二进制包的原版权文件、359 个对应源码包版本，以及额外 Python 运行时中现有的 42 份许可文件。
Ubuntu 源码归档和 DSC、额外 BusyBox/glibc 源码作为附件提供；校验为 HTTPS 下载后核对 DSC 长度与 SHA256，不宣称新做过 PGP 签名验证或逐包可复现编译。

厂商条目的来源为所有者提供的现有系统，并列入 `NON-DPKG-COMPONENTS.json`；不能独立确认的许可证用 `NOASSERTION` 标记，清单本身不授予新的厂商再分发权。
更多范围与安装条件见 [系统镜像说明](docs/ROOTFS-20261102.md)。
