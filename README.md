# 华为畅享 20 Plus 原生 Linux 移植

**Huawei Enjoy 20 Plus · FRL / MT6853 · 原生 Linux 与 GPU 服务器面板**

在华为畅享 20 Plus 上复用厂商内核与必要硬件运行时，运行原生 ARM64 Linux、轻量服务器管理功能和 Mali GPU 加速本地监控面板。不是 Android 内的 proot 桌面，也不是适配所有 MT6853 手机的通刷 ROM。

> **刷写前提：必须先完成 Bootloader 解锁，才能刷入本项目镜像。未解锁设备不能直接刷入。请先备份本机数据及原始分区镜像，再按对应机型的配套流程操作。**

## 下载与阅读

- [发布附件 / Releases](https://github.com/3dhaoxinren/huawei-enjoy20plus-frl-mt6853-linux/releases)：已上传的镜像和源码包以附件清单为准。
- [移植思路](docs/PORTING.md)
- [功能状态与缺项](docs/STATUS.md)
- [发布工具与启动校验](docs/DEPLOYMENT.md)
- [显示接管与释放](docs/DISPLAY-LIFECYCLE.md)
- [测试方法](docs/TESTING.md)
- [首次发布范围](docs/PUBLICATION.md)
- [第三方组件](THIRD_PARTY_NOTICES.md)

**本机项目已有可在 Bootloader 解锁后直接刷写的移植版本。** 当前 GitHub Releases 已上传两份启动镜像、源码压缩包及校验清单；完整新版 rootfs 仍待上传。请区分本机完整移植环境与当前已公开的附件范围，下载时以 Release 清单和配套说明为准。

## 已整理源码

| 目录 | 内容 |
| --- | --- |
| `gpu-monitor/` | 浅色 GPU 面板、CPU/GPU/内存详情、进程操作、Wi-Fi、亮度滑条、测试 |
| `gpu-native/` | Mali GLES renderer 与 DMA-BUF 探测源码；原构建脚本尚有本地依赖路径 |
| `gpu-bridge/` | 独立 RAM 运行环境及挂载身份校验 |
| `power-terminal/` | 电源键、自动熄屏、唤醒、终端/显示切换 |
| `server/` | 服务管理、应用管理、备份、网络恢复、健康检查及测试 |
| `integration/` | root-boot、root-shutdown、runit 服务入口 |
| `boot/` | 历史 v15 serial-fd 启动源码、实际镜像内脚本及镜像清单 |
| `release-tools/` | bundle.py 与带校验/回退保护的 deploy.py |
| `historical-tests/` | 已被无级亮度滑条取代的旧版测试，不属于当前回归套件 |

依赖目标手机自己的兼容厂商分区、GPU/Bionic 运行时和其他底层适配模块；仅克隆仓库不能完整启动设备。不要删除校验断言以绕过这些条件。

## 简要状态

历史真机已验证 GPU 界面显示/触摸、熄屏唤醒、Wi-Fi 快捷入口与缓存列表、6600 满量程亮度滑条。静态页面按需绘制，GPU 利用率为 0% 或休眠并不等于使用 CPU 软件渲染。

首次公开整理在 WSL Linux 上通过 **172 项当前面板测试 + 36 项服务器测试**；这些是离线逻辑测试，不是本次真机冷启动或显示接管成功记录。

## 许可证

自研移植代码及文档采用 [MIT](LICENSE)，欢迎修改、复用和贡献。Linux、BusyBox、厂商驱动等第三方部分保留各自许可证，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
