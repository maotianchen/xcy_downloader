import 'dart:convert';
import 'dart:io';

class DownloadItem {
  final String filename;
  final String type; // "音视频" or "图片"
  final String url;

  DownloadItem({required this.filename, required this.type, required this.url});

  Map<String, dynamic> toJson() => {
    'filename': filename,
    'type': type,
    'url': url,
  };

  @override
  String toString() => '$type: $filename ($url)';
}

class LogParser {
  /// 将文件名非法字符替换为下划线，保留最大长度
  static String sanitizeFilename(String name, {int maxLen = 120}) {
    final sanitized = name.replaceAll(RegExp(r'[\\/:*?"<>|]'), '_');
    return sanitized.substring(0, sanitized.length > maxLen ? maxLen : sanitized.length);
  }

  /// 扫描日志文件，提取所有的 M3U8 和图片 URL
  static Future<Map<String, List<DownloadItem>>> parseLog(String logPath) async {
    final file = File(logPath);
    if (!await file.exists()) {
      return {'media': [], 'image': []};
    }

    final content = await file.readAsString(encoding: utf8);

    // 1. 搜集所有 M3U8 和图片 URL 作为兜底
    final m3u8Regex = RegExp('https?://[^\\s"\'<>]+?\\.m3u8');
    final imageRegex = RegExp('https?://[^\\s"\'<>]+?\\.(?:png|jpg|jpeg|gif|webp)');

    final m3u8Links = m3u8Regex.allMatches(content).map((m) => m.group(0)!).toSet().toList()..sort();
    final imageLinks = imageRegex.allMatches(content).map((m) => m.group(0)!).toSet().toList()..sort();

    // 默认映射 (使用 URL 中的原始文件名作为兜底)
    final Map<String, Map<String, dynamic>> urlToInfo = {};

    for (final url in m3u8Links) {
      final parts = url.split("/");
      final baseName = parts.isEmpty ? "unknown" : parts.last.split(".m3u8").first;
      urlToInfo[url] = {
        "name": sanitizeFilename(baseName),
        "type": "media",
        "source": "fallback"
      };
    }

    for (final url in imageLinks) {
      final parts = url.split("/");
      final baseName = parts.isEmpty ? "unknown" : parts.last.split("?").first;
      urlToInfo[url] = {
        "name": sanitizeFilename(baseName),
        "type": "image",
        "source": "fallback"
      };
    }

    // 2. 深入解析日志中的所有 JSON 块
    final lines = content.split('\n');
    for (final line in lines) {
      if (line.contains('{') && line.contains('}')) {
        final startIdx = line.indexOf('{');
        final jsonStr = line.substring(startIdx);
        
        // 尝试提取合法的 JSON 块以防末尾包含非 JSON 的日志杂质
        final extractedJson = _extractJsonBlock(jsonStr);
        if (extractedJson != null) {
          try {
            final data = jsonDecode(extractedJson);
            _extractFromJson(data, urlToInfo);
          } catch (_) {}
        } else {
          // 兜底尝试整行解析
          try {
            final data = jsonDecode(jsonStr.trim());
            _extractFromJson(data, urlToInfo);
          } catch (_) {}
        }
      }
    }

    // 3. 将映射关系导出为列表并整理后缀
    final List<DownloadItem> m3u8Items = [];
    final List<DownloadItem> imageItems = [];

    urlToInfo.forEach((url, inf) {
      final String source = inf["source"] ?? "";
      final String type = inf["type"] ?? "image";
      String name = inf["name"] ?? "unknown";

      if (type == "media") {
        String ext = ".ts";
        final lowerUrl = url.toLowerCase();
        if (lowerUrl.contains("story") ||
            lowerUrl.contains("audio") ||
            lowerUrl.contains("music") ||
            lowerUrl.contains("voice") ||
            lowerUrl.contains("sound") ||
            source == "chapter") {
          ext = ".m4a";
        } else {
          ext = ".mp4";
        }

        if (!name.endsWith(".mp4") && !name.endsWith(".m4a") && !name.endsWith(".ts")) {
          name = "$name$ext";
        }
        m3u8Items.add(DownloadItem(filename: name, type: "音视频", url: url));
      } else {
        if (!name.endsWith(".png") &&
            !name.endsWith(".jpg") &&
            !name.endsWith(".jpeg") &&
            !name.endsWith(".gif") &&
            !name.endsWith(".webp")) {
          final parts = url.split("/");
          var ext = parts.isEmpty ? "jpg" : parts.last.split("?").first.split(".").last;
          if (ext.length > 4 || ext.isEmpty) {
            ext = "jpg";
          }
          name = "$name.$ext";
        }
        imageItems.add(DownloadItem(filename: name, type: "图片", url: url));
      }
    });

    // 排序
    m3u8Items.sort((a, b) => a.filename.compareTo(b.filename));
    imageItems.sort((a, b) => a.filename.compareTo(b.filename));

    return {
      'media': m3u8Items,
      'image': imageItems,
    };
  }

  /// 提取匹配大括号 {} 范围的 JSON 块
  static String? _extractJsonBlock(String text) {
    int braceCount = 0;
    int firstBraceIdx = text.indexOf('{');
    if (firstBraceIdx == -1) return null;

    for (int i = firstBraceIdx; i < text.length; i++) {
      if (text[i] == '{') {
        braceCount++;
      } else if (text[i] == '}') {
        braceCount--;
        if (braceCount == 0) {
          return text.substring(firstBraceIdx, i + 1);
        }
      }
    }
    return null;
  }

  /// 递归提取 JSON 中 URL 与名称的关联关系
  static void _extractFromJson(dynamic data, Map<String, Map<String, dynamic>> urlToInfo) {
    if (data is Map) {
      final bookTitle = (data["bookTitle"]?.toString() ?? "").trim();
      final title = (data["title"]?.toString() ?? "").trim();

      // 1. 提取音视频并建立映射
      final ossUrl = data["ossAudioUrl"]?.toString() ??
                     data["ossVideoUrl"]?.toString() ??
                     data["url"]?.toString() ?? "";

      if (ossUrl.isNotEmpty && ossUrl.startsWith("http")) {
        String name = "";
        if (bookTitle.isNotEmpty && title.isNotEmpty) {
          name = "$bookTitle-$title";
        } else if (title.isNotEmpty) {
          name = title;
        } else if (data["name"] != null) {
          name = data["name"].toString().trim();
        }

        if (name.isNotEmpty) {
          name = name.replaceAll(" | ", " - ").replaceAll("|", "-");
          name = sanitizeFilename(name);
          final bool isMedia = ossUrl.contains(".m3u8") ||
                               ossUrl.contains(".ts") ||
                               ossUrl.contains("media") ||
                               ossUrl.contains("chapter") ||
                               ossUrl.contains("audio");
          urlToInfo[ossUrl] = {
            "name": name,
            "type": isMedia ? "media" : "image",
            "source": "chapter"
          };
        }
      }

      // 2. 提取封面图、头像、静态资源的名称映射
      final cover = data["coverImage"]?.toString() ??
                    data["headerUrl"]?.toString() ??
                    data["image"]?.toString() ?? "";

      if (cover.isNotEmpty && cover.startsWith("http")) {
        final siblingName = title.isNotEmpty
            ? title
            : (data["nickname"]?.toString() ?? data["name"]?.toString() ?? "").trim();
        if (siblingName.isNotEmpty) {
          final cleanedSiblingName = sanitizeFilename(
            siblingName.replaceAll(" | ", " - ").replaceAll("|", "-").trim()
          );
          urlToInfo[cover] = {
            "name": cleanedSiblingName,
            "type": "image",
            "source": "cover"
          };
        }
      }

      // 递归子节点
      data.forEach((k, v) {
        _extractFromJson(v, urlToInfo);
      });
    } else if (data is List) {
      for (final item in data) {
        _extractFromJson(item, urlToInfo);
      }
    }
  }
}
