import 'dart:convert';
import 'dart:io';
import 'dart:isolate';

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
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is DownloadItem &&
          runtimeType == other.runtimeType &&
          url == other.url &&
          type == other.type;

  @override
  int get hashCode => Object.hash(url, type);

  @override
  String toString() => '$type: $filename ($url)';
}

class EpisodeInfo {
  final String bookTitle;
  final String bookId;
  final String episodeId;
  final int episodeNum;
  final String title;
  final String ossAudioUrl;
  final int duration;
  final int freeFlag;
  final int weight;

  EpisodeInfo({
    this.bookTitle = '',
    this.bookId = '',
    this.episodeId = '',
    this.episodeNum = 0,
    this.title = '',
    this.ossAudioUrl = '',
    this.duration = 0,
    this.freeFlag = 1,
    this.weight = 0,
  });

  String get saveFilename {
    final raw = bookTitle.isNotEmpty ? '$bookTitle - $title' : title;
    final ext = LogParser.inferMediaExt(ossAudioUrl, source: 'chapter');
    return '${LogParser.sanitizeFilename(raw)}$ext';
  }

  DownloadItem toDownloadItem() => DownloadItem(
        filename: saveFilename,
        type: '音视频',
        url: ossAudioUrl,
      );
}

class SeriesInfo {
  String bookTitle;
  String bookId;
  final List<EpisodeInfo> chapters;

  SeriesInfo({
    required this.bookTitle,
    this.bookId = '',
    List<EpisodeInfo>? chapters,
  }) : chapters = chapters ?? [];

  String get seriesKey => bookId.isNotEmpty ? bookId : 'title:$bookTitle';
  int get count => chapters.length;

  int get minEpisode {
    var minVal = 0;
    for (final c in chapters) {
      if (c.episodeNum > 0 && (minVal == 0 || c.episodeNum < minVal)) {
        minVal = c.episodeNum;
      }
    }
    return minVal;
  }

  int get maxEpisode {
    var maxVal = 0;
    for (final c in chapters) {
      if (c.episodeNum > maxVal) {
        maxVal = c.episodeNum;
      }
    }
    return maxVal;
  }

  String get episodeRange {
    if (chapters.isEmpty) return '（无剧集信息）';
    final Map<String, List<int>> arcs = {};
    final arcRegex = RegExp(r'^(.+?篇)\s*第');
    for (final ch in chapters) {
      final match = arcRegex.firstMatch(ch.title);
      final arcName = match?.group(1) ?? '其他';
      arcs.putIfAbsent(arcName, () => []).add(ch.episodeNum);
    }
    final parts = <String>[];
    arcs.forEach((name, nums) {
      final valid = nums.where((n) => n > 0).toList();
      if (valid.isNotEmpty) {
        valid.sort();
        parts.add('$name 第${valid.first}~${valid.last}集(${valid.length}集)');
      } else {
        parts.add('$name(${nums.length}集)');
      }
    });
    var display = parts.join(' | ');
    if (display.length > 80 && parts.length > 2) {
      display = '${parts.first} | … | ${parts.last}';
    }
    return '$display 共$count集';
  }
}

class LogParseResult {
  final List<DownloadItem> media;
  final List<DownloadItem> image;
  final Map<String, SeriesInfo> seriesMap;
  final String? accessToken;

  LogParseResult({
    required this.media,
    required this.image,
    required this.seriesMap,
    this.accessToken,
  });
}

class LogParser {
  static final RegExp _m3u8Regex = RegExp(
    r'''https?://[^\s"'<>]+?\.m3u8(?:\?[^\s"'<>]*)?''',
    caseSensitive: false,
  );
  static final RegExp _imageRegex = RegExp(
    r'''https?://[^\s"'<>]+?\.(?:png|jpg|jpeg|gif|webp)(?:\?[^\s"'<>]*)?''',
    caseSensitive: false,
  );
  static final RegExp _imageExtHint = RegExp(
    r'\.(?:png|jpg|jpeg|gif|webp)(?:$|\?)',
    caseSensitive: false,
  );
  static final RegExp _mediaExtHint = RegExp(
    r'\.(?:m3u8|ts|mp4|m4a|aac|mp3)(?:$|\?)',
    caseSensitive: false,
  );
  static final RegExp _tokenRegex = RegExp(r'"access_token"\s*:\s*"([^"]+)"');
  static final RegExp _episodeNumRegex = RegExp(r'第\s*(\d+)');

  /// 按最后一个点剥离文件扩展名，避免标题中的小数点截断文件名
  static String stripExtension(String filename) {
    final dotIdx = filename.lastIndexOf('.');
    if (dotIdx <= 0) return filename;
    return filename.substring(0, dotIdx);
  }

  /// 将文件名非法字符替换为下划线，超长时保留末尾集数后缀防止重名覆盖
  static String sanitizeFilename(String name, {int maxLen = 120}) {
    var cleaned = name
        .replaceAll(RegExp(r'[\\/:*?"<>|\r\n\t]+'), '_')
        .trim()
        .replaceAll(RegExp(r'^[.\s]+|[.\s]+$'), '');
    if (cleaned.isEmpty) {
      cleaned = 'unknown';
    }
    if (cleaned.length <= maxLen) {
      return cleaned;
    }
    // 使用轻量确定性短哈希 + 尾部剧集号保留，防止截断撞名
    var hash = 0x811c9dc5;
    for (final unit in cleaned.codeUnits) {
      hash ^= unit;
      hash = (hash * 0x01000193) & 0xFFFFFFFF;
    }
    final shortHash = hash.toRadixString(16).padLeft(6, '0').substring(0, 6);
    final tailLen = maxLen ~/ 3 < 36 ? maxLen ~/ 3 : 36;
    final headLen = maxLen - tailLen - shortHash.length - 2;
    if (headLen <= 0) {
      return cleaned.substring(0, maxLen);
    }
    return '${cleaned.substring(0, headLen)}_${shortHash}_${cleaned.substring(cleaned.length - tailLen)}';
  }

  static String inferMediaExt(String url, {String source = ''}) {
    final pathLower = (Uri.tryParse(url)?.path ?? url.split('?').first).toLowerCase();
    for (final directExt in const ['.mp3', '.aac', '.m4a', '.mp4', '.ts']) {
      if (pathLower.endsWith(directExt)) {
        return directExt;
      }
    }
    final lower = url.toLowerCase();
    if (lower.contains('story') ||
        lower.contains('audio') ||
        lower.contains('music') ||
        lower.contains('voice') ||
        lower.contains('sound') ||
        source == 'chapter') {
      return '.m4a';
    }
    return '.mp4';
  }

  static int extractEpisodeNumber(String title) {
    final m = _episodeNumRegex.firstMatch(title);
    if (m == null) return 0;
    return int.tryParse(m.group(1) ?? '') ?? 0;
  }

  static int compareEpisodes(EpisodeInfo a, EpisodeInfo b) {
    final na = a.episodeNum > 0 ? a.episodeNum : 1 << 30;
    final nb = b.episodeNum > 0 ? b.episodeNum : 1 << 30;
    final cmp = na.compareTo(nb);
    if (cmp != 0) return cmp;
    final wCmp = a.weight.compareTo(b.weight);
    if (wCmp != 0) return wCmp;
    return a.title.compareTo(b.title);
  }

  static String? extractTokenFromText(String content) {
    final matches = _tokenRegex.allMatches(content).toList();
    if (matches.isEmpty) return null;
    final token = matches.last.group(1)?.trim();
    return (token != null && token.isNotEmpty) ? token : null;
  }

  /// 兼容旧版接口的日志解析入口（后台 Isolate 执行）
  static Future<Map<String, List<DownloadItem>>> parseLog(String logPath) async {
    final res = await parseLogRich(logPath);
    return {'media': res.media, 'image': res.image};
  }

  /// 后台 Isolate 容错读取并完整解析日志（资源列表 + 系列结构 + access_token）
  static Future<LogParseResult> parseLogRich(String logPath) async {
    final file = File(logPath);
    if (!await file.exists()) {
      return LogParseResult(media: [], image: [], seriesMap: {}, accessToken: null);
    }
    return Isolate.run(() async {
      final bytes = await File(logPath).readAsBytes();
      final content = utf8.decode(bytes, allowMalformed: true);
      return parseLogContent(content);
    });
  }

  /// 纯内存日志解析逻辑，便于单元测试与多端复用
  static LogParseResult parseLogContent(String content) {
    final token = extractTokenFromText(content);

    final m3u8Links = _m3u8Regex.allMatches(content).map((m) => m.group(0)!).toSet().toList()..sort();
    final imageLinks = _imageRegex.allMatches(content).map((m) => m.group(0)!).toSet().toList()..sort();

    final Map<String, Map<String, dynamic>> urlToInfo = {};

    for (final url in m3u8Links) {
      final pathPart = Uri.tryParse(url)?.path ?? url;
      final lastSeg = pathPart.split('/').last;
      final baseName = lastSeg.replaceAll(RegExp(r'\.m3u8$', caseSensitive: false), '');
      urlToInfo[url] = {
        'name': sanitizeFilename(baseName.isEmpty ? 'unknown' : baseName),
        'type': 'media',
        'source': 'fallback',
      };
    }

    for (final url in imageLinks) {
      final pathPart = Uri.tryParse(url)?.path ?? url.split('?').first;
      final baseName = pathPart.split('/').last;
      urlToInfo[url] = {
        'name': sanitizeFilename(baseName.isEmpty ? 'unknown' : baseName),
        'type': 'image',
        'source': 'fallback',
      };
    }

    final Map<String, SeriesInfo> seriesMap = {};
    final Set<String> episodeDedup = {};

    final lines = const LineSplitter().convert(content);
    for (final line in lines) {
      if (!line.contains('{') || !line.contains('}')) continue;
      for (final jsonText in _extractAllJsonBlocks(line)) {
        try {
          final data = jsonDecode(jsonText);
          _extractFromJson(data, urlToInfo);

          if (line.contains('ossAudioUrl') || line.contains('ossVideoUrl')) {
            final target = (data is Map && data['data'] != null) ? data['data'] : data;
            final episodes = <EpisodeInfo>[];
            _collectEpisodes(target, episodes);
            for (final ep in episodes) {
              final dedupKey = ep.episodeId.isNotEmpty
                  ? '${ep.bookId}::${ep.episodeId}'
                  : '${ep.bookTitle}::${ep.ossAudioUrl}';
              if (!episodeDedup.add(dedupKey)) continue;

              final bt = ep.bookTitle.isNotEmpty ? ep.bookTitle : '未分类';
              final sKey = ep.bookId.isNotEmpty ? ep.bookId : 'title:$bt';
              final series = seriesMap.putIfAbsent(
                sKey,
                () => SeriesInfo(bookTitle: bt, bookId: ep.bookId),
              );
              if (series.bookTitle == '未分类' && ep.bookTitle.isNotEmpty) {
                series.bookTitle = ep.bookTitle;
              }
              series.chapters.add(ep);
            }
          }
        } catch (_) {}
      }
    }

    for (final s in seriesMap.values) {
      s.chapters.sort(compareEpisodes);
      for (final ch in s.chapters) {
        urlToInfo[ch.ossAudioUrl] = {
          'name': stripExtension(ch.saveFilename),
          'type': 'media',
          'source': 'chapter',
        };
      }
    }

    final List<DownloadItem> m3u8Items = [];
    final List<DownloadItem> imageItems = [];

    urlToInfo.forEach((url, inf) {
      final String source = inf['source'] ?? '';
      final String type = inf['type'] ?? 'image';
      String name = inf['name'] ?? 'unknown';

      if (type == 'media') {
        final ext = inferMediaExt(url, source: source);
        final lowerName = name.toLowerCase();
        if (!lowerName.endsWith('.mp4') &&
            !lowerName.endsWith('.m4a') &&
            !lowerName.endsWith('.mp3') &&
            !lowerName.endsWith('.ts') &&
            !lowerName.endsWith('.aac')) {
          name = '$name$ext';
        }
        m3u8Items.add(DownloadItem(filename: name, type: '音视频', url: url));
      } else {
        final lowerName = name.toLowerCase();
        if (!lowerName.endsWith('.png') &&
            !lowerName.endsWith('.jpg') &&
            !lowerName.endsWith('.jpeg') &&
            !lowerName.endsWith('.gif') &&
            !lowerName.endsWith('.webp')) {
          final pathPart = Uri.tryParse(url)?.path ?? url.split('?').first;
          final lastSeg = pathPart.split('/').last;
          var ext = lastSeg.contains('.') ? lastSeg.split('.').last.toLowerCase() : 'jpg';
          if (!{'png', 'jpg', 'jpeg', 'gif', 'webp'}.contains(ext)) {
            ext = 'jpg';
          }
          name = '$name.$ext';
        }
        imageItems.add(DownloadItem(filename: name, type: '图片', url: url));
      }
    });

    m3u8Items.sort((a, b) => a.filename.compareTo(b.filename));
    imageItems.sort((a, b) => a.filename.compareTo(b.filename));

    return LogParseResult(
      media: m3u8Items,
      image: imageItems,
      seriesMap: seriesMap,
      accessToken: token,
    );
  }

  /// 提取单行内所有匹配大括号范围的 JSON 块（正确跳过字符串内的转义花括号）
  static List<String> _extractAllJsonBlocks(String text) {
    final blocks = <String>[];
    int idx = 0;
    while (idx < text.length) {
      final firstBrace = text.indexOf('{', idx);
      if (firstBrace == -1) break;

      int braceCount = 0;
      bool inString = false;
      bool escaped = false;
      int? endIdx;

      for (int i = firstBrace; i < text.length; i++) {
        final ch = text[i];
        if (escaped) {
          escaped = false;
          continue;
        }
        if (ch == '\\' && inString) {
          escaped = true;
          continue;
        }
        if (ch == '"') {
          inString = !inString;
          continue;
        }
        if (inString) continue;
        if (ch == '{') {
          braceCount++;
        } else if (ch == '}') {
          braceCount--;
          if (braceCount == 0) {
            endIdx = i;
            break;
          }
        }
      }

      if (endIdx != null) {
        blocks.add(text.substring(firstBrace, endIdx + 1));
        idx = endIdx + 1;
      } else {
        idx = firstBrace + 1;
      }
    }
    return blocks;
  }

  static EpisodeInfo? _episodeFromMap(Map data) {
    final url = (data['ossAudioUrl'] ?? data['ossVideoUrl'] ?? '').toString().trim();
    if (!url.toLowerCase().contains('.m3u8')) return null;
    final title = (data['title'] ?? data['name'] ?? '').toString().trim();
    if (title.isEmpty) return null;

    return EpisodeInfo(
      bookTitle: (data['bookTitle'] ?? '').toString().trim(),
      bookId: (data['bookId'] ?? '').toString().trim(),
      episodeId: (data['id'] ?? '').toString().trim(),
      episodeNum: extractEpisodeNumber(title),
      title: title,
      ossAudioUrl: url,
      duration: int.tryParse('${data['duration'] ?? 0}') ?? 0,
      freeFlag: int.tryParse('${data['freeFlag'] ?? 1}') ?? 1,
      weight: int.tryParse('${data['weight'] ?? 0}') ?? 0,
    );
  }

  static void _collectEpisodes(dynamic node, List<EpisodeInfo> out) {
    if (node is Map) {
      final records = node['records'];
      if (records is List) {
        for (final rec in records) {
          if (rec is Map) {
            final ep = _episodeFromMap(rec);
            if (ep != null) out.add(ep);
          }
        }
      }
      final ep = _episodeFromMap(node);
      if (ep != null) out.add(ep);

      node.forEach((k, v) {
        if (k != 'records') {
          _collectEpisodes(v, out);
        }
      });
    } else if (node is List) {
      for (final item in node) {
        _collectEpisodes(item, out);
      }
    }
  }

  static void _extractFromJson(dynamic data, Map<String, Map<String, dynamic>> urlToInfo) {
    if (data is Map) {
      final bookTitle = (data['bookTitle']?.toString() ?? '').trim();
      final title = (data['title']?.toString() ?? '').trim();

      final ossUrl = (data['ossAudioUrl'] ?? data['ossVideoUrl'] ?? data['url'] ?? '')
          .toString()
          .trim();

      if (ossUrl.startsWith('http')) {
        final lower = ossUrl.toLowerCase();
        final isMedia = _mediaExtHint.hasMatch(lower) ||
            lower.contains('media') ||
            lower.contains('chapter') ||
            lower.contains('audio') ||
            lower.contains('video');
        final isImg = _imageExtHint.hasMatch(lower);

        if (isMedia || isImg) {
          String name = '';
          var hasTitle = true;
          if (bookTitle.isNotEmpty && title.isNotEmpty) {
            name = '$bookTitle - $title';
          } else if (title.isNotEmpty) {
            name = title;
          } else if (data['name'] != null && data['name'].toString().trim().isNotEmpty) {
            name = data['name'].toString().trim();
          } else {
            hasTitle = false;
            final seg = (Uri.tryParse(ossUrl)?.path ?? ossUrl.split('?').first)
                .split('/')
                .last;
            name = seg.isNotEmpty ? seg : 'unknown';
          }

          if (name.isNotEmpty) {
            name = sanitizeFilename(name.replaceAll(' | ', ' - ').replaceAll('|', '-'));
            urlToInfo[ossUrl] = {
              'name': name,
              'type': isMedia ? 'media' : 'image',
              'source': hasTitle ? 'chapter' : 'json_url',
            };
          }
        }
      }

      final cover = (data['coverImage'] ?? data['headerUrl'] ?? data['image'] ?? '')
          .toString()
          .trim();

      if (cover.startsWith('http')) {
        final siblingName = title.isNotEmpty
            ? title
            : (data['nickname']?.toString() ?? data['name']?.toString() ?? '').trim();
        if (siblingName.isNotEmpty) {
          final cleanedSiblingName = sanitizeFilename(
            siblingName.replaceAll(' | ', ' - ').replaceAll('|', '-').trim(),
          );
          urlToInfo[cover] = {
            'name': cleanedSiblingName,
            'type': 'image',
            'source': 'cover',
          };
        }
      }

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
