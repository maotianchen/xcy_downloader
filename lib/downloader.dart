import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:encrypt/encrypt.dart' as encrypt_pkg;
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

import 'log_parser.dart';

class CancellationToken {
  bool isCancelled = false;

  void cancel() {
    isCancelled = true;
  }

  void reset() {
    isCancelled = false;
  }
}

enum DownloadStatus { success, skipped, failed, cancelled }

class DownloadItemResult {
  final DownloadStatus status;
  final String? savedPath;
  final String? error;

  const DownloadItemResult({
    required this.status,
    this.savedPath,
    this.error,
  });

  bool get isOk =>
      status == DownloadStatus.success || status == DownloadStatus.skipped;
}

class BatchDownloadSummary {
  final int success;
  final int skipped;
  final int failed;
  final int cancelled;
  final Map<String, String> savedPaths;

  const BatchDownloadSummary({
    required this.success,
    required this.skipped,
    required this.failed,
    required this.cancelled,
    required this.savedPaths,
  });
}

class M3u8KeyInfo {
  final String method;
  final String? uri;
  final List<int>? iv;

  const M3u8KeyInfo({
    required this.method,
    this.uri,
    this.iv,
  });
}

class M3u8Segment {
  final String url;
  final int sequence;
  final M3u8KeyInfo? key;
  final bool isInit;

  const M3u8Segment({
    required this.url,
    required this.sequence,
    this.key,
    this.isInit = false,
  });
}

class M3u8Plan {
  final String finalUrl;
  final int mediaSequence;
  final List<M3u8Segment> segments;

  const M3u8Plan({
    required this.finalUrl,
    required this.mediaSequence,
    required this.segments,
  });
}

/// Legacy compatibility snapshot of an M3U8 playlist.
class M3u8Info {
  final String finalUrl;
  final String? keyMethod;
  final String? keyUrl;
  final List<int>? iv;
  final int mediaSequence;
  final List<String> segments;

  const M3u8Info({
    required this.finalUrl,
    this.keyMethod,
    this.keyUrl,
    this.iv,
    required this.mediaSequence,
    required this.segments,
  });
}

/// Extracts elementary ADTS AAC audio frames from audio-only MPEG-TS buffers.
class TsAudioExtractor {
  static const int _packetSize = 188;
  static const int _syncByte = 0x47;

  /// Returns ADTS AAC bytes if [tsBytes] is an audio-only MPEG-TS stream with
  /// valid ADTS frames; returns `null` if video is present or audio cannot be
  /// cleanly extracted so callers fall back to saving raw `.ts`.
  static Uint8List? extractAdtsFromTs(Uint8List tsBytes) {
    final startOffset = _findSyncOffset(tsBytes);
    if (startOffset < 0) return null;

    final audioPayloads = BytesBuilder(copy: false);
    final audioPids = <int>{};

    for (var off = startOffset;
        off + _packetSize <= tsBytes.length;
        off += _packetSize) {
      if (tsBytes[off] != _syncByte) {
        return null;
      }
      final pid = ((tsBytes[off + 1] & 0x1F) << 8) | tsBytes[off + 2];
      if (pid == 0x1FFF) continue; // Null stuffing packet.

      final pusi = (tsBytes[off + 1] & 0x40) != 0;
      final afc = (tsBytes[off + 3] >> 4) & 0x03;
      if ((afc & 0x01) == 0) continue; // Adaptation-only packet, no payload.

      var payloadStart = off + 4;
      if ((afc & 0x02) != 0) {
        if (payloadStart >= off + _packetSize) continue;
        final adapLen = tsBytes[payloadStart];
        payloadStart += 1 + adapLen;
      }
      if (payloadStart >= off + _packetSize) continue;

      if (pusi && pid != 0 && payloadStart + 9 <= off + _packetSize) {
        if (tsBytes[payloadStart] == 0x00 &&
            tsBytes[payloadStart + 1] == 0x00 &&
            tsBytes[payloadStart + 2] == 0x01) {
          final streamId = tsBytes[payloadStart + 3];
          if (streamId >= 0xE0 && streamId <= 0xEF) {
            // Video PES detected: keep full TS stream instead of stripping.
            return null;
          }
          if (streamId >= 0xC0 && streamId <= 0xDF) {
            audioPids.add(pid);
            final headerDataLen = tsBytes[payloadStart + 8];
            final esStart = payloadStart + 9 + headerDataLen;
            if (esStart < off + _packetSize) {
              audioPayloads.add(
                Uint8List.sublistView(tsBytes, esStart, off + _packetSize),
              );
            }
            continue;
          }
        }
      }

      if (!pusi && audioPids.contains(pid)) {
        audioPayloads.add(
          Uint8List.sublistView(tsBytes, payloadStart, off + _packetSize),
        );
      }
    }

    final rawAudio = audioPayloads.takeBytes();
    if (!_looksLikeAdtsStream(rawAudio)) {
      return null;
    }
    return rawAudio;
  }

  static int _findSyncOffset(Uint8List bytes) {
    final limit = min(bytes.length, _packetSize);
    for (var i = 0; i < limit; i++) {
      if (bytes[i] != _syncByte) continue;
      if (i + _packetSize < bytes.length && bytes[i + _packetSize] != _syncByte) {
        continue;
      }
      if (i + (_packetSize * 2) < bytes.length &&
          bytes[i + (_packetSize * 2)] != _syncByte) {
        continue;
      }
      return i;
    }
    return -1;
  }

  static bool _looksLikeAdtsStream(Uint8List data) {
    if (data.length < 7) return false;
    if (data[0] != 0xFF || (data[1] & 0xF0) != 0xF0) return false;
    final frameLen =
        ((data[3] & 0x03) << 11) | (data[4] << 3) | ((data[5] >> 5) & 0x07);
    if (frameLen < 7 || frameLen > data.length) return false;
    if (frameLen == data.length) return true;
    return frameLen + 1 < data.length &&
        data[frameLen] == 0xFF &&
        (data[frameLen + 1] & 0xF0) == 0xF0;
  }
}

class Downloader {
  static const Map<String, String> _headers = {
    'User-Agent':
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': '*/*',
  };

  static const int _segmentConcurrency = 4;
  static const int _maxRetries = 3;

  static String resolveSubDir(String itemType) =>
      itemType == 'image' ? 'images' : 'media';

  /// Probes whether an item has already been downloaded cleanly into [outDir].
  static String? resolveExistingDownloadPath(String outDir, DownloadItem item) {
    final subDir = '$outDir/${resolveSubDir(item.type)}';
    final candidates = <String>{'$subDir/${item.filename}'};
    if (item.type != 'image') {
      final base = LogParser.stripExtension(item.filename);
      for (final ext in const ['.aac', '.ts', '.mp4', '.m4a', '.mp3']) {
        candidates.add('$subDir/$base$ext');
      }
    }
    for (final path in candidates) {
      final file = File(path);
      if (file.existsSync() && file.lengthSync() > 0) {
        return path;
      }
    }
    return null;
  }

  /// Resolves a writable output directory with automatic scoped-storage fallback.
  static Future<String> getSafeOutputDir(String userPath) async {
    Future<bool> testWritable(String dirPath) async {
      try {
        final dir = Directory(dirPath);
        if (!await dir.exists()) {
          await dir.create(recursive: true);
        }
        final probe = File('$dirPath/.write_test.tmp');
        await probe.writeAsString('ok');
        await probe.delete();
        return true;
      } catch (_) {
        return false;
      }
    }

    final normalized = userPath.trim();
    if (normalized.isNotEmpty && await testWritable(normalized)) {
      return normalized;
    }

    if (Platform.isAndroid) {
      final extDir = await getExternalStorageDirectory();
      if (extDir != null) {
        final parts = extDir.path.split('/Android/');
        if (parts.isNotEmpty && parts[0].isNotEmpty) {
          final mediaDir =
              '${parts[0]}/Android/media/com.qimiaoyvzhou.xcy.xcy_downloa_app/XcyDownloads';
          if (await testWritable(mediaDir)) {
            return mediaDir;
          }
        }
        final appDir = '${extDir.path}/XcyDownloads';
        if (await testWritable(appDir)) {
          return appDir;
        }
      }
    }

    final docDir = await getApplicationDocumentsDirectory();
    final fallbackDir = '${docDir.path}/XcyDownloads';
    await Directory(fallbackDir).create(recursive: true);
    return fallbackDir;
  }

  /// Selects the highest-bandwidth variant URL if [text] is a master playlist.
  static String? pickBestMasterVariant(String text, Uri baseUri) {
    final lines = text.split(RegExp(r'\r?\n'));
    int? pendingBandwidth;
    String? bestUrl;
    var bestBandwidth = -1;

    for (final raw in lines) {
      final line = raw.trim();
      if (line.isEmpty) continue;

      if (line.startsWith('#EXT-X-STREAM-INF')) {
        final bwMatch = RegExp(
          r'BANDWIDTH=(\d+)',
          caseSensitive: false,
        ).firstMatch(line);
        pendingBandwidth =
            bwMatch != null ? (int.tryParse(bwMatch.group(1)!) ?? 0) : 0;
        continue;
      }

      if (!line.startsWith('#') && pendingBandwidth != null) {
        final resolved = baseUri.resolve(line).toString();
        if (pendingBandwidth > bestBandwidth || bestUrl == null) {
          bestBandwidth = pendingBandwidth;
          bestUrl = resolved;
        }
        pendingBandwidth = null;
      }
    }
    return bestUrl;
  }

  /// Parses an M3U8 media playlist into an ordered [M3u8Plan], tracking
  /// relative URL resolution, `#EXT-X-KEY` rotation, `METHOD=NONE`, and `#EXT-X-MAP`.
  static M3u8Plan parseMediaPlaylist(String text, Uri baseUri) {
    var mediaSequence = 0;
    final seqMatch = RegExp(r'#EXT-X-MEDIA-SEQUENCE:(\d+)').firstMatch(text);
    if (seqMatch != null) {
      mediaSequence = int.tryParse(seqMatch.group(1)!) ?? 0;
    }

    M3u8KeyInfo? currentKey;
    final segments = <M3u8Segment>[];
    var segIdx = 0;

    for (final raw in text.split(RegExp(r'\r?\n'))) {
      final line = raw.trim();
      if (line.isEmpty) continue;

      if (line.startsWith('#EXT-X-KEY:')) {
        currentKey = _parseKeyTag(line, baseUri);
        continue;
      }

      if (line.startsWith('#EXT-X-MAP:')) {
        final uriMatch = RegExp(r'URI="([^"]+)"').firstMatch(line);
        if (uriMatch != null) {
          segments.add(
            M3u8Segment(
              url: baseUri.resolve(uriMatch.group(1)!).toString(),
              sequence: mediaSequence,
              key: null,
              isInit: true,
            ),
          );
        }
        continue;
      }

      if (!line.startsWith('#')) {
        segments.add(
          M3u8Segment(
            url: baseUri.resolve(line).toString(),
            sequence: mediaSequence + segIdx,
            key: currentKey,
          ),
        );
        segIdx++;
      }
    }

    return M3u8Plan(
      finalUrl: baseUri.toString(),
      mediaSequence: mediaSequence,
      segments: segments,
    );
  }

  static M3u8KeyInfo? _parseKeyTag(String line, Uri baseUri) {
    final methodMatch = RegExp(
      r'METHOD=([A-Z0-9\-]+)',
      caseSensitive: false,
    ).firstMatch(line);
    if (methodMatch == null) return null;

    final method = methodMatch.group(1)!.toUpperCase();
    if (method == 'NONE') return null;

    final uriMatch = RegExp(r'URI="([^"]+)"').firstMatch(line) ??
        RegExp(r'URI=([^,\s]+)').firstMatch(line);
    final keyUrl =
        uriMatch != null ? baseUri.resolve(uriMatch.group(1)!).toString() : null;

    List<int>? ivBytes;
    final ivMatch = RegExp(
      r'IV=0[xX]([0-9a-fA-F]+)',
    ).firstMatch(line);
    if (ivMatch != null) {
      final paddedHex = ivMatch.group(1)!.padLeft(32, '0');
      final hexSlice =
          paddedHex.length > 32 ? paddedHex.substring(paddedHex.length - 32) : paddedHex;
      ivBytes = List<int>.generate(
        16,
        (i) => int.parse(hexSlice.substring(i * 2, i * 2 + 2), radix: 16),
      );
    }

    return M3u8KeyInfo(method: method, uri: keyUrl, iv: ivBytes);
  }

  static Future<M3u8Plan> resolveM3u8Plan(
    String url, {
    http.Client? client,
    CancellationToken? cancellationToken,
    int depth = 0,
  }) async {
    if (depth > 4) {
      throw Exception('M3U8 嵌套层级过深');
    }
    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    try {
      final uri = Uri.parse(url);
      final bytes = await _getBytesWithRetry(
        activeClient,
        uri,
        cancellationToken ?? CancellationToken(),
      );
      final text = utf8.decode(bytes, allowMalformed: true);
      final variantUrl = pickBestMasterVariant(text, uri);
      if (variantUrl != null && variantUrl != url) {
        return resolveM3u8Plan(
          variantUrl,
          client: activeClient,
          cancellationToken: cancellationToken,
          depth: depth + 1,
        );
      }
      return parseMediaPlaylist(text, uri);
    } finally {
      if (ownClient) activeClient.close();
    }
  }

  /// Legacy compatibility wrapper around [resolveM3u8Plan].
  static Future<M3u8Info> resolveM3u8(
    String url, {
    http.Client? client,
  }) async {
    final plan = await resolveM3u8Plan(url, client: client);
    M3u8KeyInfo? firstKey;
    for (final seg in plan.segments) {
      if (seg.key != null) {
        firstKey = seg.key;
        break;
      }
    }
    return M3u8Info(
      finalUrl: plan.finalUrl,
      keyMethod: firstKey?.method,
      keyUrl: firstKey?.uri,
      iv: firstKey?.iv,
      mediaSequence: plan.mediaSequence,
      segments: plan.segments.map((s) => s.url).toList(),
    );
  }

  /// Computes the 16-byte AES-128-CBC IV for segment sequence [seqIndex].
  static List<int> ivBytesForSequence(List<int>? explicitIv, int seqIndex) {
    if (explicitIv != null && explicitIv.length == 16) {
      return explicitIv;
    }
    final out = List<int>.filled(16, 0);
    var v = seqIndex;
    for (var i = 15; i >= 8; i--) {
      out[i] = v & 0xFF;
      v >>= 8;
    }
    return out;
  }

  static List<int> getIvBytes(M3u8Info info, int seqIndex) =>
      ivBytesForSequence(info.iv, seqIndex);

  /// Decrypts an AES-128-CBC HLS segment and safely removes valid PKCS7 padding.
  static Uint8List decryptAes128CbcSegment(
    Uint8List encryptedBytes,
    Uint8List keyBytes,
    List<int> ivBytes,
  ) {
    if (encryptedBytes.isEmpty || encryptedBytes.length % 16 != 0) {
      throw FormatException(
        'AES-128 片段长度无效 (${encryptedBytes.length} 字节)',
      );
    }
    final encrypter = encrypt_pkg.Encrypter(
      encrypt_pkg.AES(
        encrypt_pkg.Key(keyBytes),
        mode: encrypt_pkg.AESMode.cbc,
        padding: null,
      ),
    );
    final decrypted = encrypter.decryptBytes(
      encrypt_pkg.Encrypted(encryptedBytes),
      iv: encrypt_pkg.IV(Uint8List.fromList(ivBytes)),
    );
    if (decrypted.isEmpty) return Uint8List(0);

    final pad = decrypted.last;
    if (pad >= 1 && pad <= 16 && pad <= decrypted.length) {
      var validPadding = true;
      for (var i = decrypted.length - pad; i < decrypted.length; i++) {
        if (decrypted[i] != pad) {
          validPadding = false;
          break;
        }
      }
      if (validPadding) {
        return Uint8List.fromList(
          decrypted.sublist(0, decrypted.length - pad),
        );
      }
    }
    return Uint8List.fromList(decrypted);
  }

  static Future<Uint8List> _getBytesWithRetry(
    http.Client client,
    Uri uri,
    CancellationToken token, {
    int maxRetries = _maxRetries,
    Duration timeout = const Duration(seconds: 20),
  }) async {
    Object? lastError;
    for (var attempt = 0; attempt <= maxRetries; attempt++) {
      if (token.isCancelled) {
        throw const _CancelledException();
      }
      try {
        final response =
            await client.get(uri, headers: _headers).timeout(timeout);
        if (response.statusCode >= 200 && response.statusCode < 300) {
          return response.bodyBytes;
        }
        lastError = HttpException(
          'HTTP ${response.statusCode}',
          uri: uri,
        );
      } catch (e) {
        if (e is _CancelledException) rethrow;
        lastError = e;
      }
      if (attempt < maxRetries) {
        await Future.delayed(Duration(milliseconds: 300 * (1 << attempt)));
      }
    }
    throw lastError ?? Exception('请求失败: $uri');
  }

  /// Downloads and decrypts an M3U8 stream, saving valid audio-only streams as
  /// `.aac` and mixed/non-ADTS streams as `.ts`. Fails strictly if any segment
  /// fails after retries.
  static Future<DownloadItemResult> downloadM3u8({
    required String url,
    required String baseOutDir,
    required String saveFilename,
    required Function(String) logFn,
    required CancellationToken cancellationToken,
    http.Client? client,
    bool forceOverwrite = false,
  }) async {
    final baseName = LogParser.stripExtension(saveFilename);
    final subDir = '$baseOutDir/media';
    await Directory(subDir).create(recursive: true);

    if (!forceOverwrite) {
      final existing = resolveExistingDownloadPath(
        baseOutDir,
        DownloadItem(filename: saveFilename, type: 'audio', url: url),
      );
      if (existing != null) {
        logFn('    [跳过] 已存在: $existing');
        return DownloadItemResult(
          status: DownloadStatus.skipped,
          savedPath: existing,
        );
      }
    }

    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    final tempPath =
        '$subDir/.tmp_${baseName}_${DateTime.now().microsecondsSinceEpoch}.part';
    final tempFile = File(tempPath);

    try {
      final plan = await resolveM3u8Plan(
        url,
        client: activeClient,
        cancellationToken: cancellationToken,
      );
      final total = plan.segments.length;
      if (total == 0) {
        logFn('    [-] M3U8 列表为空: $url');
        return const DownloadItemResult(
          status: DownloadStatus.failed,
          error: 'M3U8 列表为空',
        );
      }

      logFn('    [*] 解析成功: 共 $total 个加密/分片片段');

      final keyCache = <String, Future<Uint8List>>{};
      Future<Uint8List> fetchKey(String keyUrl) {
        return keyCache.putIfAbsent(
          keyUrl,
          () => _getBytesWithRetry(
            activeClient,
            Uri.parse(keyUrl),
            cancellationToken,
            timeout: const Duration(seconds: 15),
          ),
        );
      }

      final readyBuffers = <int, Uint8List>{};
      final sink = tempFile.openWrite();
      var nextIndex = 0;
      var nextToWrite = 0;
      var completed = 0;
      Object? workerError;

      void drainOrderedBuffers() {
        while (readyBuffers.containsKey(nextToWrite)) {
          final chunk = readyBuffers.remove(nextToWrite)!;
          sink.add(chunk);
          nextToWrite++;
        }
      }

      Future<void> runWorker() async {
        while (true) {
          if (cancellationToken.isCancelled || workerError != null) return;
          final idx = nextIndex++;
          if (idx >= total) return;

          // Backpressure so fast workers do not buffer unbounded segments in RAM.
          while (idx - nextToWrite >= 12) {
            if (cancellationToken.isCancelled || workerError != null) return;
            await Future.delayed(const Duration(milliseconds: 20));
          }

          final seg = plan.segments[idx];
          try {
            var segBytes = await _getBytesWithRetry(
              activeClient,
              Uri.parse(seg.url),
              cancellationToken,
              timeout: const Duration(seconds: 25),
            );

            final keyInfo = seg.key;
            if (!seg.isInit &&
                keyInfo != null &&
                keyInfo.method == 'AES-128' &&
                keyInfo.uri != null) {
              final keyBytes = await fetchKey(keyInfo.uri!);
              final ivBytes = ivBytesForSequence(keyInfo.iv, seg.sequence);
              segBytes = decryptAes128CbcSegment(segBytes, keyBytes, ivBytes);
            }

            readyBuffers[idx] = segBytes;
            drainOrderedBuffers();

            completed++;
            if (completed % 15 == 0 || completed == total) {
              logFn('      -> 进度: $completed / $total');
            }
          } catch (e) {
            workerError ??= e;
            return;
          }
        }
      }

      try {
        final workerCount = min(_segmentConcurrency, total);
        await Future.wait(List.generate(workerCount, (_) => runWorker()));
        drainOrderedBuffers();
      } finally {
        await sink.flush();
        await sink.close();
      }

      if (cancellationToken.isCancelled || workerError is _CancelledException) {
        if (await tempFile.exists()) await tempFile.delete();
        logFn('    [!] 任务被用户终止');
        return const DownloadItemResult(status: DownloadStatus.cancelled);
      }

      if (workerError != null || nextToWrite != total) {
        if (await tempFile.exists()) await tempFile.delete();
        final errStr = '${workerError ?? "片段不完整 ($nextToWrite/$total)"}';
        logFn('    [-] M3U8 下载失败 (已清理残片): $errStr');
        return DownloadItemResult(
          status: DownloadStatus.failed,
          error: errStr,
        );
      }

      // Convert audio-only TS streams into standalone `.aac` cleanly.
      final tsBytes = await tempFile.readAsBytes();
      final adtsBytes = TsAudioExtractor.extractAdtsFromTs(tsBytes);
      if (adtsBytes != null && adtsBytes.isNotEmpty) {
        final aacPath = '$subDir/$baseName.aac';
        await File(aacPath).writeAsBytes(adtsBytes, flush: true);
        if (await tempFile.exists()) await tempFile.delete();
        logFn('    [+] 音视频分片提取成功 (AAC): $aacPath');
        return DownloadItemResult(
          status: DownloadStatus.success,
          savedPath: aacPath,
        );
      }

      final tsPath = '$subDir/$baseName.ts';
      if (await File(tsPath).exists()) await File(tsPath).delete();
      await tempFile.rename(tsPath);
      logFn('    [+] 视频/音视频合并成功 (TS): $tsPath');
      return DownloadItemResult(
        status: DownloadStatus.success,
        savedPath: tsPath,
      );
    } catch (e) {
      if (await tempFile.exists()) {
        try {
          await tempFile.delete();
        } catch (_) {}
      }
      if (e is _CancelledException || cancellationToken.isCancelled) {
        return const DownloadItemResult(status: DownloadStatus.cancelled);
      }
      logFn('    [-] M3U8 异常: $e');
      return DownloadItemResult(status: DownloadStatus.failed, error: '$e');
    } finally {
      if (ownClient) activeClient.close();
    }
  }

  /// Backwards-compatible `Future<bool>` wrapper around [downloadM3u8].
  static Future<bool> downloadM3u8AsTs({
    required String url,
    required String baseOutDir,
    required String saveFilename,
    required Function(String) logFn,
    required CancellationToken cancellationToken,
    http.Client? client,
  }) async {
    final res = await downloadM3u8(
      url: url,
      baseOutDir: baseOutDir,
      saveFilename: saveFilename,
      logFn: logFn,
      cancellationToken: cancellationToken,
      client: client,
    );
    return res.isOk;
  }

  static Future<DownloadItemResult> downloadDirectFile({
    required String url,
    required String baseOutDir,
    required String saveFilename,
    required String itemType,
    required Function(String) logFn,
    required CancellationToken cancellationToken,
    http.Client? client,
    bool forceOverwrite = false,
  }) async {
    final subDir = '$baseOutDir/${resolveSubDir(itemType)}';
    await Directory(subDir).create(recursive: true);
    final outPath = '$subDir/$saveFilename';

    if (!forceOverwrite) {
      final outFile = File(outPath);
      if (outFile.existsSync() && outFile.lengthSync() > 0) {
        logFn('    [跳过] 已存在: $outPath');
        return DownloadItemResult(
          status: DownloadStatus.skipped,
          savedPath: outPath,
        );
      }
    }

    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    final tempPath =
        '$subDir/.tmp_${LogParser.stripExtension(saveFilename)}_${DateTime.now().microsecondsSinceEpoch}.part';
    final tempFile = File(tempPath);

    try {
      final bytes = await _getBytesWithRetry(
        activeClient,
        Uri.parse(url),
        cancellationToken,
        timeout: const Duration(seconds: 30),
      );
      if (bytes.isEmpty) {
        logFn('    [-] 空响应内容: $url');
        return const DownloadItemResult(
          status: DownloadStatus.failed,
          error: '空响应内容',
        );
      }
      await tempFile.writeAsBytes(bytes, flush: true);
      if (await File(outPath).exists()) await File(outPath).delete();
      await tempFile.rename(outPath);
      logFn('    [+] 下载成功: $outPath');
      return DownloadItemResult(
        status: DownloadStatus.success,
        savedPath: outPath,
      );
    } catch (e) {
      if (await tempFile.exists()) {
        try {
          await tempFile.delete();
        } catch (_) {}
      }
      if (e is _CancelledException || cancellationToken.isCancelled) {
        logFn('    [!] 任务被用户终止');
        return const DownloadItemResult(status: DownloadStatus.cancelled);
      }
      logFn('    [-] 下载异常: $e');
      return DownloadItemResult(status: DownloadStatus.failed, error: '$e');
    } finally {
      if (ownClient) activeClient.close();
    }
  }

  /// Ensures unique target filenames within a batch before downloading.
  static List<DownloadItem> deduplicateBatchItems(List<DownloadItem> items) {
    final seen = <String, int>{};
    final result = <DownloadItem>[];

    for (final item in items) {
      final sub = resolveSubDir(item.type);
      final dot = item.filename.lastIndexOf('.');
      final base = dot > 0 ? item.filename.substring(0, dot) : item.filename;
      final ext = dot > 0 ? item.filename.substring(dot) : '';

      var candidate = item.filename;
      var counter = 2;
      while (seen.containsKey('$sub/$candidate'.toLowerCase())) {
        candidate = '${base}_$counter$ext';
        counter++;
      }
      seen['$sub/$candidate'.toLowerCase()] = 1;
      result.add(
        candidate == item.filename
            ? item
            : DownloadItem(
                filename: candidate,
                type: item.type,
                url: item.url,
              ),
      );
    }
    return result;
  }

  static Future<BatchDownloadSummary> runDownloadTask({
    required List<DownloadItem> selectedItems,
    required String outDir,
    required Function(String) logFn,
    required Function(int completed, int total) progressFn,
    required Function() doneFn,
    required CancellationToken cancellationToken,
    void Function(DownloadItem item, DownloadItemResult result)? onItemFinished,
  }) async {
    final items = deduplicateBatchItems(selectedItems);
    final total = items.length;
    var completed = 0;
    var successCount = 0;
    var skippedCount = 0;
    var failedCount = 0;
    var cancelledCount = 0;
    final savedPaths = <String, String>{};

    final client = http.Client();
    try {
      final actualOutDir = await getSafeOutputDir(outDir);
      if (actualOutDir != outDir) {
        logFn('[!] 提示: Android 权限或路径不可写，自动降级存储至私有目录:\n    -> $actualOutDir');
      } else {
        logFn('[*] 输出目录: $actualOutDir');
      }

      for (final item in items) {
        if (cancellationToken.isCancelled) {
          logFn('[!] 下载已手动停止');
          cancelledCount += total - completed;
          break;
        }

        logFn('[${completed + 1}/$total] 处理: ${item.filename}');
        final isM3u8 = item.url.toLowerCase().contains('.m3u8');

        final DownloadItemResult res;
        if (isM3u8) {
          res = await downloadM3u8(
            url: item.url,
            baseOutDir: actualOutDir,
            saveFilename: item.filename,
            logFn: logFn,
            cancellationToken: cancellationToken,
            client: client,
          );
        } else {
          res = await downloadDirectFile(
            url: item.url,
            baseOutDir: actualOutDir,
            saveFilename: item.filename,
            itemType: item.type,
            logFn: logFn,
            cancellationToken: cancellationToken,
            client: client,
          );
        }

        if (res.savedPath != null) {
          savedPaths[item.url] = res.savedPath!;
        }
        switch (res.status) {
          case DownloadStatus.success:
            successCount++;
          case DownloadStatus.skipped:
            skippedCount++;
          case DownloadStatus.failed:
            failedCount++;
          case DownloadStatus.cancelled:
            cancelledCount++;
        }

        onItemFinished?.call(item, res);
        completed++;
        progressFn(completed, total);
      }

      logFn(
        '=== 全部任务完成: 成功 $successCount，跳过 $skippedCount，失败 $failedCount'
        '${cancelledCount > 0 ? "，取消 $cancelledCount" : ""} ===',
      );
    } finally {
      client.close();
      doneFn();
    }

    return BatchDownloadSummary(
      success: successCount,
      skipped: skippedCount,
      failed: failedCount,
      cancelled: cancelledCount,
      savedPaths: savedPaths,
    );
  }
}

class _CancelledException implements Exception {
  const _CancelledException();
}

/// Client for fetching user profile, book catalog, and full chapter lists.
class XcyApiClient {
  static const String apiBase = 'https://service.meother.com/qmyz-server-provider';

  static Map<String, String> _authHeaders(String token) => {
        'Authorization': 'Bearer $token',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'Dart/xcy-downloader',
      };

  static Future<dynamic> _requestJson(
    http.Client client,
    String method,
    String path,
    String token, {
    Object? body,
  }) async {
    final uri = Uri.parse('$apiBase$path');
    final headers = _authHeaders(token);
    final http.Response res;
    if (method == 'POST') {
      res = await client
          .post(uri, headers: headers, body: jsonEncode(body ?? {}))
          .timeout(const Duration(seconds: 15));
    } else {
      res = await client
          .get(uri, headers: headers)
          .timeout(const Duration(seconds: 15));
    }
    if (res.statusCode == 401) {
      throw Exception('HTTP 401 Token 已失效或无效');
    }
    if (res.statusCode < 200 || res.statusCode >= 300) {
      throw Exception('HTTP ${res.statusCode}');
    }
    final decoded = jsonDecode(utf8.decode(res.bodyBytes));
    if (decoded is Map<String, dynamic>) {
      final code = decoded['code'];
      if (code != null && code != 200) {
        throw Exception(decoded['msg'] ?? 'API 错误 code=$code');
      }
    }
    return decoded;
  }

  static Future<String> verifyToken(
    String token, {
    http.Client? client,
  }) async {
    final cleanToken = token.trim();
    if (cleanToken.isEmpty) throw Exception('Token 不能为空');
    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    try {
      final res = await _requestJson(
        activeClient,
        'GET',
        '/sysUser/getUserInfo',
        cleanToken,
      );
      if (res is Map<String, dynamic>) {
        final data = res['data'];
        if (data is Map<String, dynamic>) {
          return (data['username'] ?? data['nickName'] ?? '未知用户').toString();
        }
      }
      return '已验证用户';
    } finally {
      if (ownClient) activeClient.close();
    }
  }

  static Future<Map<String, SeriesInfo>> fetchBookList(
    String token, {
    http.Client? client,
  }) async {
    final cleanToken = token.trim();
    if (cleanToken.isEmpty) throw Exception('Token 不能为空');
    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    try {
      final seriesMap = <String, SeriesInfo>{};
      for (final endpoint in const [
        '/business/square/recommend/books',
        '/business/square/recommend/books/type',
      ]) {
        try {
          final res = await _requestJson(activeClient, 'GET', endpoint, cleanToken);
          _collectBooks(res, seriesMap);
        } catch (_) {}
      }
      return seriesMap;
    } finally {
      if (ownClient) activeClient.close();
    }
  }

  static void _collectBooks(dynamic node, Map<String, SeriesInfo> out) {
    if (node is List) {
      for (final item in node) {
        _collectBooks(item, out);
      }
      return;
    }
    if (node is! Map) return;

    final bookId = (node['bookId'] ?? node['id'] ?? '').toString().trim();
    final bookTitle = (node['bookTitle'] ?? node['title'] ?? '').toString().trim();
    if (bookId.isNotEmpty && bookTitle.isNotEmpty) {
      final key = bookId;
      out.putIfAbsent(
        key,
        () => SeriesInfo(bookTitle: bookTitle, bookId: bookId),
      );
    }

    for (final value in node.values) {
      if (value is List || value is Map) {
        _collectBooks(value, out);
      }
    }
  }

  static Future<List<EpisodeInfo>> fetchEpisodesFromApi(
    String token,
    String bookId,
    String bookTitle, {
    http.Client? client,
    void Function(int page, int collected)? onPage,
  }) async {
    final cleanToken = token.trim();
    if (cleanToken.isEmpty || bookId.trim().isEmpty) return [];

    final ownClient = client == null;
    final activeClient = client ?? http.Client();
    try {
      final episodes = <EpisodeInfo>[];
      final seenUrls = <String>{};
      var page = 1;
      const pageSize = 100;

      while (true) {
        final res = await _requestJson(
          activeClient,
          'POST',
          '/business/book/$bookId/chapter/page',
          cleanToken,
          body: {
            'pageObject': {'pageNum': page, 'pageSize': pageSize},
          },
        );

        final data = res is Map ? res['data'] : null;
        final records = data is Map
            ? (data['records'] ?? data['list'] ?? data['rows'])
            : (data is List ? data : null);

        if (records is! List || records.isEmpty) break;

        var addedInPage = 0;
        for (final rec in records) {
          if (rec is! Map) continue;
          final rawUrl = (rec['ossAudioUrl'] ??
                  rec['ossVideoUrl'] ??
                  rec['audioUrl'] ??
                  '')
              .toString()
              .trim();
          if (rawUrl.isEmpty || seenUrls.contains(rawUrl)) continue;
          seenUrls.add(rawUrl);
          addedInPage++;

          final title = (rec['title'] ?? '').toString().trim();
          final epBookTitle = (rec['bookTitle'] ?? bookTitle).toString().trim();
          final epBookId = (rec['bookId'] ?? bookId).toString().trim();
          final epId = (rec['episodeId'] ?? rec['id'] ?? '').toString().trim();
          final freeFlag = int.tryParse('${rec['freeFlag'] ?? 1}') ?? 1;
          final weight = int.tryParse('${rec['weight'] ?? 0}') ?? 0;
          final duration = int.tryParse('${rec['duration'] ?? 0}') ?? 0;

          episodes.add(
            EpisodeInfo(
              bookTitle: epBookTitle.isEmpty ? bookTitle : epBookTitle,
              bookId: epBookId.isEmpty ? bookId : epBookId,
              episodeId: epId,
              title: title,
              ossAudioUrl: rawUrl,
              episodeNum: LogParser.extractEpisodeNumber(title),
              duration: duration,
              freeFlag: freeFlag,
              weight: weight,
            ),
          );
        }

        onPage?.call(page, episodes.length);
        final pages = data is Map ? (int.tryParse('${data['pages'] ?? 0}') ?? 0) : 0;
        if (addedInPage == 0 ||
            records.length < pageSize ||
            (pages > 0 && page >= pages) ||
            episodes.length >= 10000) {
          break;
        }
        page++;
      }

      episodes.sort(LogParser.compareEpisodes);
      return episodes;
    } finally {
      if (ownClient) activeClient.close();
    }
  }
}
