# active framebuffer exists：查阅入口

## 断言实际含义

`gpu-monitor/kms.py:63`：在 DRM SET_MASTER 后，对选中的 CRTC 执行 GETCRTC，读取结构里的 buffer_id；要求为 0，然后才打开 ION、创建自己的扫描缓冲。失败说明该 CRTC 仍绑定非零 framebuffer。它不是“GPU 占用率不为零”，也不单凭此证明是某个具体进程占用；需区分活跃显示持有者、fbdev/启动画面、上次异常退出残留，以及启动顺序变化。

## 启动链

1. `power-terminal/power-terminal.py` 的 Console.show：取得模式锁；启动 seatd、desktop，等待 gpu-monitor/status.json ready，然后唤醒背光。
2. `integration/etc/frl/sv/desktop/run`：server 模式 exec monitor.py；desktop 模式才进入既有 Weston 路径，两者不要混淆。
3. `gpu-monitor/monitor.py`：持有 gpu-monitor.lock，创建 Engine/Touch，进入按需绘制循环。
4. `engine.py`：校验 prepare.py，准备独立 RAM-root，再创建 Display，分配两个缓冲并启动原 Mali renderer。
5. `kms.py`：获取 DRM master、选 DSI/CRTC、要求旧 framebuffer 为 0。首次 present 才 modeset 后标记 self.active=True，后续使用 page flip。

## 正常退出链

1. Console.hide_locked 关闭背光，对 desktop、seatd 执行 `sv -w 5 down`；错误会收集并报告，不能把错误当作已完全释放。
2. monitor 收到 SIGTERM/SIGINT 后结束循环，在 finally 中关闭 Engine/Touch、发布 ready=false 并关闭锁。
3. Engine.close 关闭 renderer 输入并等待，必要时 terminate/kill；再 Display.close，最后 prepare.cleanup。
4. Display.close 只有 self.active=True 时先 disable 自己启用的 CRTC，然后 RMFB、关闭 GEM、DMA-BUF、ION handle、ION FD，再 DROP_MASTER 和关闭 DRM FD。
5. prepare.cleanup 按严格挂载身份检查并普通 umount GPU 私有挂载；保留 RAM 目录。不使用 lazy/force unmount。

如果在构造函数断言处失败，self.active 仍为 False；close 不会擅自关闭它尚未接管的旧 CRTC，这是原有保护设计。不要只删断言就抢走未知显示状态。

## 需要对照的故障证据（只读优先）

- `/etc/frl/runtime-mode`、desktop/seatd/power/power-terminal 的 sv status，以及进程树：是否同时启动 Weston、旧显示程序和新 GPU 界面。
- `/run/frl/gpu-monitor/status.json`、renderer.log、`/var/log/frl/services/desktop/current`：首次异常、之前 stop 的退出结果、是否被 SIGKILL。
- `/proc/*/fd` 指向 `/dev/dri/card0`、`/dev/fb0` 的持有者；有 debugfs 时读取 DRM state，不为排查强行改写寄存器或切换 CRTC。
- 新旧 root-boot、enabled-services、desktop run 的差异，是否在原显示链释放前就启动监控。
- 是否在 Engine.close 的 renderer 处理阶段就抛错，或在 Display.close 的某次 ioctl 抛错；这些函数并非所有子步骤都有独立 finally，后续释放不能仅凭调用过 close 就当作成功。

以上是源码行为和排查方向，**不是对当前手机的已确认根因，也不是已修复补丁**。应先抓到退出/接管顺序及 DRM 状态，再决定修复哪一层。
