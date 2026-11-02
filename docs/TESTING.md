# 离线验证

测试使用 Linux 的 fcntl、权限和 `/proc` 语义。首次整理在 WSL Linux 上运行，不是在手机上。GUI 依赖 Cairo/Pango 字形库，但测试不启动实际 DRM 或 GPU renderer。

Ubuntu 示例依赖：

```sh
sudo apt-get install python3-cairo python3-gi python3-gi-cairo gir1.2-pango-1.0 fonts-wqy-microhei
python3 tools/test_monitor.py
```

当前面板套件：172 项通过。测试覆盖 Wi-Fi 异步缓存、输入、GPU 文本对齐、采样、重要进程操作保护、显示设置、满量程连续亮度滑条和管理逻辑。

`server/test_operations.py` 使用 `/root` 下的临时目录测试 root 所有权/权限保护，需在隔离 Linux 测试环境以 root 运行，不要在生产设备上随意运行测试：

```sh
sudo python3 -B -m unittest discover -s server -v
```

服务器套件：36 项通过。测试的设备副作用通过 mock 或临时目录隔离，不代表底层厂商依赖已经齐全。

## 历史测试与目录适配

旧 `test_extension.py` 按 1650 上限、5% 档位与加减按钮编写，与后续 6600 无级滑条需求冲突，原样移至 `historical-tests/`。当前滑条由 `test_slider.py` 覆盖，不通过降低现行亮度上限迁就旧测试。

首次直接跑测试还发现打包后导入路径不匹配：测试 runner 显式加入 server 路径；fuel 测试改从实际 `server/fuel/` 目录加载，补入与历史 payload SHA 匹配的 service.py。没有修改设备运行逻辑来消除测试错误。

## 验证边界

语法检查与单元测试不能证明开机成功、DRM 所有权正确、厂商二进制兼容或断电恢复可靠。本次未做真机部署、重启、刷机或故障恢复。
