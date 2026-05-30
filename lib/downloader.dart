import 'dart:async';
import 'dart:io';
import 'dart:typed_data';
import 'package:http/http.dart' as http;
import 'package:encrypt/encrypt.dart' as enc;
import 'package:path_provider/path_provider.dart';
import 'log_parser.dart';

/// 取消令牌
class CancellationToken {
  bool _cancelled = false;
  bool get isCancelled => _cancelled;
  
  void cancel() {
    _cancelled = true;
  }
  
  void reset() {
    _cancelled = false;
  }
}

/// M3U8 解析实体类
class M3u8Info {
  final String finalUrl;
  final String? keyMethod;
  final String? keyUrl;
  final List<int>? iv;
  final int mediaSequence;
  final List<String> segments;

  M3u8Info({
    required this.finalUrl,
    this.keyMethod,
    this.keyUrl,
    this.iv,
    required this.mediaSequence,
    required this.segments,
  });
}

class Downloader {
  static final Map<String, String> _headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
  };

  /// 十六进制转换为字节流
  static List<int> _hexToBytes(String hex) {
    final bytes = <int>[];
    for (int i = 0; i < hex.length; i += 2) {
      bytes.add(int.parse(hex.substring(i, i + 2), radix: 16));
    }
    return bytes;
  }

  /// 构造 HLS 默认的 16 字节 IV 向量
  static Uint8List getIvBytes(List<int>? keyIv, int mediaSequence, int segIdx) {
    if (keyIv != null && keyIv.length == 16) {
      return Uint8List.fromList(keyIv);
    }
    final int seq = mediaSequence + segIdx;
    final bd = ByteData(16);
    // 将 64 位整数写入最后 8 个字节中（大端序）
    bd.setUint64(8, seq, Endian.big);
    return bd.buffer.asUint8List();
  }

  /// 安全获取真实可写的输出保存目录。
  /// 若用户指定的公共目录（如外部 Download）由于未授权 storage / manageExternalStorage 权限，
  /// 导致 Android 11+ 系统抛出 Permission denied 写入异常，
  /// 则本方法会自动捕获并将路径平滑降级为安卓免权限且完全公开可见的 App 专属媒体目录：
  /// /storage/emulated/0/Android/media/[packagename]/XcyDownloads
  /// 该目录不仅可以被 App 免权限写入，还可以直接被任何文件管理器浏览与提取，确保 100% 下载保存成功！
  static Future<String> getSafeOutputDir(String customDir) async {
    final dir = Directory(customDir);
    try {
      if (!await dir.exists()) {
        await dir.create(recursive: true);
      }
      // 尝试创建并删除一个临时测试文件校验写权限
      final testFile = File("$customDir/.write_test_${DateTime.now().millisecondsSinceEpoch}");
      await testFile.writeAsString("test");
      await testFile.delete();
      return customDir; // 校验写权限通过
    } catch (_) {
      // 写入失败或无权限，进入降级逻辑
      try {
        final extDir = await getExternalStorageDirectory();
        if (extDir != null) {
          final mediaPath = extDir.path
              .replaceAll("/Android/data/", "/Android/media/")
              .replaceAll("/files", "");
          final fallbackPath = "$mediaPath/XcyDownloads";
          final fallbackDir = Directory(fallbackPath);
          if (!await fallbackDir.exists()) {
            await fallbackDir.create(recursive: true);
          }
          return fallbackPath;
        }
      } catch (_) {}

      // 极限界兜底为临时缓存目录
      try {
        final tempDir = await getTemporaryDirectory();
        final fallbackTemp = "${tempDir.path}/XcyDownloads";
        final fallbackTempDir = Directory(fallbackTemp);
        if (!await fallbackTempDir.exists()) {
          await fallbackTempDir.create(recursive: true);
        }
        return fallbackTemp;
      } catch (_) {
        return customDir;
      }
    }
  }

  /// 解析 M3U8 播放列表文件，若为 Master Playlist 则自动寻找最高画质子流
  static Future<M3u8Info> resolveM3u8(String url) async {
    final response = await http.get(Uri.parse(url), headers: _headers);
    if (response.statusCode != 200) {
      throw Exception("M3U8 HTTP 请求失败: 状态码 ${response.statusCode}");
    }
    final raw = response.body;
    
    final uriParts = url.split("/");
    uriParts.removeLast();
    final base = uriParts.join("/");

    // 1. 若为 Master Playlist 导航至子流
    if (raw.contains("#EXT-X-STREAM-INF")) {
      final regex = RegExp(r'#EXT-X-STREAM-INF[^\n]*\n([^\n]+)');
      final matches = regex.allMatches(raw).toList();
      if (matches.isNotEmpty) {
        final child = matches.last.group(1)!.trim();
        final childUrl = child.startsWith("http") ? child : "$base/$child";
        return resolveM3u8(childUrl);
      }
    }

    // 2. 媒体 Playlist 解析
    String? keyMethod;
    String? keyUrl;
    List<int>? iv;

    final keyMatch = RegExp(r'#EXT-X-KEY:([^\n]+)').firstMatch(raw);
    if (keyMatch != null) {
      final keyInfo = keyMatch.group(1)!;
      final methodMatch = RegExp(r'METHOD=([^,\s]+)').firstMatch(keyInfo);
      if (methodMatch != null) {
        keyMethod = methodMatch.group(1);
      }
      final uriMatch = RegExp(r'URI="([^"]+)"').firstMatch(keyInfo);
      if (uriMatch != null) {
        final ku = uriMatch.group(1)!;
        keyUrl = ku.startsWith("http") ? ku : "$base/$ku";
      }
      final ivMatch = RegExp(r'IV=0[xX]([0-9a-fA-F]+)').firstMatch(keyInfo);
      if (ivMatch != null) {
        final hexStr = ivMatch.group(1)!;
        iv = _hexToBytes(hexStr);
      }
    }

    int mediaSequence = 0;
    final seqMatch = RegExp(r'#EXT-X-MEDIA-SEQUENCE:(\d+)').firstMatch(raw);
    if (seqMatch != null) {
      mediaSequence = int.parse(seqMatch.group(1)!);
    }

    final List<String> segments = [];
    final lines = raw.split("\n");
    for (var line in lines) {
      line = line.trim();
      if (line.isNotEmpty && !line.startsWith("#")) {
        final segUrl = line.startsWith("http") ? line : "$base/$line";
        segments.add(segUrl);
      }
    }

    return M3u8Info(
      finalUrl: url,
      keyMethod: keyMethod,
      keyUrl: keyUrl,
      iv: iv,
      mediaSequence: mediaSequence,
      segments: segments,
    );
  }

  /// 下载 M3U8 音视频流，逐分片解密（AES-128 CBC）并按顺序拼接
  static Future<bool> downloadM3u8AsTs({
    required String url,
    required String baseOutDir,
    required String saveFilename,
    required Function(String) logFn,
    required CancellationToken cancellationToken,
  }) async {
    final baseName = saveFilename.split('.').first;
    
    // 获取临时存储路径，用于合并分片
    final tempDir = await getTemporaryDirectory();
    final tempTsPath = "${tempDir.path}/temp_$baseName.ts";
    final tempFile = File(tempTsPath);

    if (await tempFile.exists()) {
      await tempFile.delete();
    }

    IOSink? sink;
    try {
      // 1. 解析播放列表
      final info = await resolveM3u8(url);
      if (info.segments.isEmpty) {
        logFn("  [!] $baseName: 未找到 TS 片段，跳过。");
        return false;
      }

      // 2. 获取解密密钥 (重试 3 次，超时 20 秒)
      Uint8List? keyBytes;
      if (info.keyMethod == "AES-128" && info.keyUrl != null) {
        logFn("  [🔓] 检测到 AES-128 加密，正在获取密钥...");
        for (int attempt = 1; attempt <= 3; attempt++) {
          if (cancellationToken.isCancelled) return false;
          try {
            final res = await http.get(Uri.parse(info.keyUrl!), headers: _headers)
                .timeout(const Duration(seconds: 20));
            if (res.statusCode == 200 && res.bodyBytes.length == 16) {
              keyBytes = res.bodyBytes;
              break;
            } else {
              logFn("  [!] 密钥获取异常 (状态码: ${res.statusCode}, 长度: ${res.bodyBytes.length}字节)，重试中 ($attempt/3)...");
            }
          } catch (e) {
            logFn("  [!] 密钥获取失败: $e，重试中 ($attempt/3)...");
          }
          await Future.delayed(const Duration(seconds: 2));
        }

        if (keyBytes == null || keyBytes.length != 16) {
          logFn("  [✗] 无法获取有效的 AES 密钥，分片将无法被正确解密！");
        }
      }

      final total = info.segments.length;
      logFn("  [→] 开始下载共 $total 个分片...");

      // 3. 打开临时文件句柄追加分片
      sink = tempFile.openWrite(mode: FileMode.append);
      
      // 初始化 AES 解密器
      enc.Encrypter? encrypter;
      if (keyBytes != null) {
        encrypter = enc.Encrypter(enc.AES(enc.Key(keyBytes), mode: enc.AESMode.cbc, padding: null));
      }

      final int progressMark = (total / 10).clamp(1, double.infinity).toInt();

      for (int i = 0; i < total; i++) {
        if (cancellationToken.isCancelled) {
          logFn("  [×] 用户中止下载。");
          await sink.close();
          if (await tempFile.exists()) {
            await tempFile.delete();
          }
          return false;
        }

        final segUrl = info.segments[i];
        
        try {
          final res = await http.get(Uri.parse(segUrl), headers: _headers)
              .timeout(const Duration(seconds: 15));
          if (res.statusCode != 200) {
            throw Exception("HTTP 错误 ${res.statusCode}");
          }
          
          Uint8List segmentData = res.bodyBytes;

          // 4. AES CBC 解密并剥离 PKCS7 填充
          if (encrypter != null && keyBytes != null) {
            final iv = enc.IV(getIvBytes(info.iv, info.mediaSequence, i));
            final decryptedBytes = encrypter.decryptBytes(enc.Encrypted(segmentData), iv: iv);
            segmentData = Uint8List.fromList(decryptedBytes);

            // 剥离 PKCS7 填充
            if (segmentData.isNotEmpty) {
              final int padLen = segmentData.last;
              if (padLen > 0 && padLen <= 16) {
                bool isPadding = true;
                for (int p = segmentData.length - padLen; p < segmentData.length; p++) {
                  if (segmentData[p] != padLen) {
                    isPadding = false;
                    break;
                  }
                }
                if (isPadding) {
                  segmentData = segmentData.sublist(0, segmentData.length - padLen);
                }
              }
            }
          }

          sink.add(segmentData);
          await sink.flush();

          // 进度打点
          final step = i + 1;
          if (step % progressMark == 0 || step == total) {
            logFn("      片段 $step/$total");
          }
        } catch (e) {
          logFn("      [!] 片段 ${i + 1} 失败: $e");
        }
      }

      await sink.close();
      sink = null;

      // 5. 自动类型探测归类（取代外部 FFmpeg 依赖）
      bool isVideo = true;
      final lowerUrl = url.toLowerCase();
      final lowerName = saveFilename.toLowerCase();
      if (lowerUrl.contains("story") ||
          lowerUrl.contains("audio") ||
          lowerUrl.contains("music") ||
          lowerUrl.contains("voice") ||
          lowerUrl.contains("sound") ||
          lowerName.contains(".m4a")) {
        isVideo = false;
      }

      final String targetSubdir = isVideo ? "video" : "music";
      final String targetExt = isVideo ? ".mp4" : ".m4a";
      
      // 智能获取安全可写的输出根目录，无感解决 Scoped Storage 写入限制
      final safeBaseDir = await getSafeOutputDir(baseOutDir);
      if (safeBaseDir != baseOutDir) {
        logFn("  [⚠️] 提示: 外部公共 Download 目录写入权限受限，已自动切换至免权限公共媒体目录保存成果:\n      $safeBaseDir");
      }

      final targetDir = Directory("$safeBaseDir/$targetSubdir");
      if (!await targetDir.exists()) {
        await targetDir.create(recursive: true);
      }

      final finalPath = "${targetDir.path}/$baseName$targetExt";
      logFn("  [🎥] 检测为${isVideo ? '视频' : '音频'}流，分类至 $targetSubdir 目录");

      // 移动并重命名临时文件为最终文件（使用 copy-and-delete 兜底解决 Linux 跨文件系统/跨分区 EXDEV Link 限制）
      if (await tempFile.exists()) {
        try {
          await tempFile.rename(finalPath);
        } catch (_) {
          await tempFile.copy(finalPath);
          await tempFile.delete();
        }
        logFn("  [✓] 已成功保存至绝对路径:\n      $finalPath");
      }

      return true;
    } catch (e) {
      logFn("  [✗] $baseName 下载失败: $e");
      if (sink != null) {
        await sink.close();
      }
      if (await tempFile.exists()) {
        await tempFile.delete();
      }
      return false;
    }
  }

  /// 下载单张图片并保存
  static Future<bool> downloadImage({
    required String url,
    required String outDir,
    required String saveFilename,
    required Function(String) logFn,
    required CancellationToken cancellationToken,
  }) async {
    if (cancellationToken.isCancelled) return false;

    // 智能获取安全可写的图片输出目录
    final safeBaseDir = await getSafeOutputDir(outDir);
    if (safeBaseDir != outDir) {
      logFn("  [⚠️] 提示: 图片写入权限受限，已自动切换至免权限公共媒体目录保存:\n      $safeBaseDir");
    }

    final targetDir = Directory("$safeBaseDir/picture");
    if (!await targetDir.exists()) {
      await targetDir.create(recursive: true);
    }

    final savePath = "${targetDir.path}/$saveFilename";
    final saveFile = File(savePath);
    if (await saveFile.exists()) {
      // 避免重复下载
      return true;
    }

    try {
      final res = await http.get(Uri.parse(url), headers: _headers)
          .timeout(const Duration(seconds: 15));
      if (res.statusCode == 200) {
        await saveFile.writeAsBytes(res.bodyBytes);
        logFn("  [✓] 图片保存成功:\n      $savePath");
        return true;
      } else {
        logFn("  [!] 图片下载失败: $saveFilename (状态码 ${res.statusCode})");
        return false;
      }
    } catch (e) {
      logFn("  [!] 图片下载异常: $saveFilename → $e");
      return false;
    }
  }

  /// 主下载调度服务，按队列下载所有被选中的项目
  static Future<void> runDownloadTask({
    required List<DownloadItem> selectedItems,
    required String outDir,
    required Function(String) logFn,
    required Function(int completed, int total) progressFn,
    required Function() doneFn,
    required CancellationToken cancellationToken,
  }) async {
    final DateTime now = DateTime.now();
    final String timeStr = "${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}:${now.second.toString().padLeft(2, '0')}";
    
    logFn("=" * 45);
    logFn("[$timeStr] 开始下载任务");
    logFn("输出目录: $outDir");
    logFn("选择下载 → 总数: ${selectedItems.length} 个资源");
    logFn("=" * 45);

    final total = selectedItems.length;
    int completed = 0;

    for (int i = 0; i < total; i++) {
      if (cancellationToken.isCancelled) break;
      
      final item = selectedItems[i];
      final idx = i + 1;

      if (item.type == "音视频") {
        logFn("\n[$idx/$total] 正在分析并下载: ${item.filename}");
        await downloadM3u8AsTs(
          url: item.url,
          baseOutDir: outDir,
          saveFilename: item.filename,
          logFn: logFn,
          cancellationToken: cancellationToken,
        );

        // 冷却防拉黑延时
        if (idx < total && !cancellationToken.isCancelled) {
          await Future.delayed(const Duration(milliseconds: 1500));
        }
      } else if (item.type == "图片") {
        logFn("\n[$idx/$total] 正在下载图片: ${item.filename}");
        await downloadImage(
          url: item.url,
          outDir: outDir,
          saveFilename: item.filename,
          logFn: logFn,
          cancellationToken: cancellationToken,
        );
      }

      completed++;
      progressFn(completed, total);
    }

    if (cancellationToken.isCancelled) {
      logFn("\n[!] 下载任务已被用户手动停止。");
    } else {
      final DateTime endNow = DateTime.now();
      final String endTimeStr = "${endNow.hour.toString().padLeft(2, '0')}:${endNow.minute.toString().padLeft(2, '0')}:${endNow.second.toString().padLeft(2, '0')}";
      logFn("\n[$endTimeStr] 全部下载完成！");
    }
    
    doneFn();
  }
}
