# 正式发布工具与启动校验

## 刷写前置条件

**必须先完成目标手机的 Bootloader 解锁，才能刷入本项目镜像。未解锁设备不能直接刷入。** 先备份本机数据与原始分区镜像，核对华为畅享 20 Plus / FRL 机型、镜像 SHA256 和配套文件，再进入刷写流程。下述 bundle/deploy 是已运行 Linux 内的发布工具，不能代替 Bootloader 解锁。

`release-tools/bundle.py` 和 `release-tools/deploy.py` 为现有受保护安装流程的源码快照，使用 Python 标准库，没有额外本地模板模块。deploy.py 在导入/执行时就检查 root、PID 1、设备 compatible 和参数，不能当普通库在主机导入。

## 运行系统的文件更新

1. 在副本上改代码，保留原 SHA256、差异及恢复副本。
2. 将目标路径（相对 `/`）、内容、权限和 mutable 标志交给 `bundle.build(path, files)`。
3. 记录 bundle 返回的 SHA256，通过已有可信 SSH 传输到目标 `/tmp`。
4. 在已运行且旧 payload 全部校验通过的 FRL 系统，以 root 调用：

```sh
python3 deploy.py /tmp/release.tar.gz EXPECTED_SHA256 UNIQUE_RELEASE_LABEL
```

此命令是参数格式，不是可直接复制的完整刷机指令。必须先审查 bundle、替换实际 SHA 和独立标签，不能使用 `python -O` 关闭断言。

安装器验证 `mediatek,MT6853` 和 `frl-init`，拒绝路径穿越与不允许的目标目录；备份原文件/清单，原子写文件，同步 `/etc/frl/root-payload.sha256` 并完整复核。失败时尝试恢复已写文件和原清单；回执位于本次备份目录。

这是现有工具的行为说明，不是“任意输入均安全”的新审计认证。它只用于可信、人工审查的本项目发布包。

## 修改启动时需要同步什么

- `/usr/lib/frl/root-boot.sh`、`root-shutdown.sh` 或其他被跟踪文件改变后，同步当前 `/etc/frl/root-payload.sha256` 对应条目，并校验所有其他条目。
- 如同时改了服务 run、启用服务列表、挂载依赖或 helper，必须成套发布，不能只改启动脚本。
- 修改 initramfs 脚本或二进制，需要重新打包对应 ramdisk、更新相关输入/暂存清单、验证最终镜像和解包内容。根文件系统版本和 initramfs 版本可能不同，不能只按文件同名互换。
- 不应简单删除 payload 清单、放宽失败分支、覆盖为一份历史完整清单或跳过显示所有权断言。

正常系统可只读校验：

```sh
cd /
sha256sum -c /etc/frl/root-payload.sha256
```

已经校验失败而停在早期恢复环境时，deploy.py 的正常在线前置条件不成立。应先确定挂载的是哪个 rootfs，恢复已知匹配文件或完成审查后的离线成套修复；此仓库不提供未经当前设备验证的盲改恢复命令。

## rootfs 阶段标志

历史当前流程使用五行标志，结构示意为：

```text
FRL_V15_IMAGE_V2
rootfs-v3.ext4
<32 hex UUID>
<64 hex staged-image SHA256>
accepted
```

首次 staged 与后续 accepted 阶段校验语义不同。accepted 根文件系统会被正常运行更新，不应直接重算整个已使用根镜像然后替换标志以绕过 payload 校验。

恢复须以本次变更备份和最新完整清单为基准，避免回退时覆盖其他后续更新。
