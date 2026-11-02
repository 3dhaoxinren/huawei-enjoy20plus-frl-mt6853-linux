# 第三方组件与许可边界

本项目不是华为官方发行版。Huawei、Mali、Ubuntu 等名称归各自权利人所有。

- Linux 内核、Ubuntu 软件包、BusyBox、runit、Pango/Cairo、Glances 等保持各自许可证，不随自研代码一并改为 MIT。
- 厂商 GPU/Bionic、APEX 与其他厂商库未包含在此次源码快照中。已有构建脚本引用它们不表示本仓库提供或授予其再分发权。
- 原始 NDK 链接对象和头文件没有混入首次源码快照；需按构建环境取得相应 NDK，并保留上游许可证。
- 现有移植依赖的 Glances 历史版本为 4.5.8，独立安装、默认关闭；不随本次源码打包。
- 不发布设备 NVRAM/NVDATA/OEMINFO/PERSIST、SSH 主机私钥、个人 authorized_keys、Wi-Fi 配置、生产日志或整盘用户数据。

自研部分许可证待仓库所有者确认；公开可阅读不自动意味着所有第三方组件都可任意再分发。镜像附件的组件和源代码材料以相应 Release 说明为准。
