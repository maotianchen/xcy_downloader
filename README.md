# xcy_downloa_app
星次元调试模式算码器
```
https://suanma.xcy.maotianchen.com/
```

## V2.5.0（桌面端 & 安卓端全面修复升级）

### 主要更新
- **桌面端 (`xcy_downloader_gui.py` / `dist/星次元下载器.exe`)**：
  - 新增界面内日志控制台与清空日志；Token 验证、发现书单、ADB 拉取与本地大日志解析全部异步化，彻底解决卡顿。
  - 解耦 ADB 与手动 Token / 本地日志解析流程；优化 ADB 设备状态识别与单次 `adb pull` 拉取，避免重复解析。
  - 默认开启 TLS 证书校验（支持界面开关），支持记住 Token 配置持久化；支持并发分片 M3U8、Master 最高码率选择、多密钥轮换与 `METHOD=NONE`、直链音视频下载及严格分片校验。
  - 修正超长标题尾部集数截断保留、链接 Query 参数保留及批内重名去重。
- **安卓端 (`Flutter` `2.5.0+2`)**：
  - 日志解析迁至后台 `Isolate` 容错解码，同步补齐 Token 自动提取、推荐书单发现、云端全集补全与按集数区间勾选。
  - 重构 M3U8 并发分片与重试引擎，新增纯 Dart MPEG-TS 音频提取（纯音频自动导出真 `.aac`，含视频保留 `.ts`），严格清理失败残片。
  - 改为自适应布局并追踪实际保存路径（修复存储降级目录后的「分享」异常）。


## 若GitHub下载过慢/中断，请从cloudflare存储桶下载（历史版本）：

#### Android：

V1.0.0
```
https://download.maotianchen.com/_V1.1.0.apk
```

SHA256:dda2f0e60834acf53ea0721c25aec626cc1c777aa268b2afc340dbcf8dbec7ae

V2.5.0
```
https://download.maotianchen.com/V2.5.0.apk
```

SHA256:f9a59b377920c71c5720a266797e947781b9efcef5a90ada99a9ccf05c635af2

#### Windows：

V1.0.0

```
https://download.maotianchen.com/_V1.1.0.exe
```

SHA256:4329b4cf82965dfb5e3f3d68219ece5ac0d4b219fe279400ce8ecfd5fd4acc12

V2.0.0_beta

```
https://download.maotianchen.com/_v2.0.0_beta.exe
```

SHA256:4153d24c8cc2f015a54c6e830790b7393b5c60c9f8d1fa016cc5e427199acf59

V2.5.0

```
https://download.maotianchen.com/V2.5.0.exe
```

SHA256:f9a59b377920c71c5720a266797e947781b9efcef5a90ada99a9ccf05c635af2

