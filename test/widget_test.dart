import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:encrypt/encrypt.dart' as encrypt_pkg;
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:xcy_downloa_app/downloader.dart';
import 'package:xcy_downloa_app/log_parser.dart';
import 'package:xcy_downloa_app/main.dart';

Uint8List _buildAdtsFrame(List<int> payload) {
  final frameLen = 7 + payload.length;
  final out = Uint8List(frameLen);
  out[0] = 0xFF;
  out[1] = 0xF1; // MPEG-4 ADTS, no CRC
  out[2] = 0x50; // AAC-LC, 44.1kHz index
  out[3] = 0x80 | ((frameLen >> 11) & 0x03);
  out[4] = (frameLen >> 3) & 0xFF;
  out[5] = ((frameLen & 0x07) << 5) | 0x1F;
  out[6] = 0xFC;
  out.setRange(7, frameLen, payload);
  return out;
}

Uint8List _buildSingleTsPacket({
  required int pid,
  required int streamId,
  required Uint8List esPayload,
  bool isPusi = true,
}) {
  final pkt = Uint8List(188)..fillRange(0, 188, 0xFF);
  pkt[0] = 0x47;
  pkt[1] = (isPusi ? 0x40 : 0x00) | ((pid >> 8) & 0x1F);
  pkt[2] = pid & 0xFF;

  final headerAndEs = <int>[
    if (isPusi) ...[0x00, 0x00, 0x01, streamId, 0x00, 0x00, 0x80, 0x00, 0x00],
    ...esPayload,
  ];

  final stuffingBytes = 184 - headerAndEs.length;
  if (stuffingBytes > 0) {
    // Use adaptation field stuffing so payload exact bounds match esPayload.
    pkt[3] = 0x30; // adaptation + payload
    pkt[4] = stuffingBytes - 1;
    if (stuffingBytes > 1) {
      pkt[5] = 0x00; // flags
    }
    final start = 4 + stuffingBytes;
    pkt.setRange(start, 188, headerAndEs);
  } else {
    pkt[3] = 0x10; // payload only
    pkt.setRange(4, 188, headerAndEs.sublist(0, 184));
  }
  return pkt;
}

Uint8List _aes128EncryptWithPkcs7(
  Uint8List plain,
  Uint8List keyBytes,
  List<int> ivBytes,
) {
  final padLen = 16 - (plain.length % 16);
  final padded = Uint8List(plain.length + padLen)
    ..setRange(0, plain.length, plain)
    ..fillRange(plain.length, plain.length + padLen, padLen);
  final encrypter = encrypt_pkg.Encrypter(
    encrypt_pkg.AES(
      encrypt_pkg.Key(keyBytes),
      mode: encrypt_pkg.AESMode.cbc,
      padding: null,
    ),
  );
  return encrypter
      .encryptBytes(padded, iv: encrypt_pkg.IV(Uint8List.fromList(ivBytes)))
      .bytes;
}

void main() {
  group('LogParser', () {
    test('stripExtension preserves multi-dot titles cleanly', () {
      expect(LogParser.stripExtension('第1.5集 故事.v2.m4a'), '第1.5集 故事.v2');
      expect(LogParser.stripExtension('no_ext_name'), 'no_ext_name');
      expect(LogParser.stripExtension('.hidden'), '.hidden');
    });

    test('sanitizeFilename preserves tail episode identifier when truncated', () {
      final longName = '${'超长剧名测试_' * 20}第128集_大结局.m4a';
      final sanitized = LogParser.sanitizeFilename(longName, maxLen: 80);
      expect(sanitized.length <= 80, isTrue);
      expect(sanitized.contains('第128集'), isTrue);
    });

    test('parseLogContent extracts token, series, signed URLs without image misclass', () {
      const sampleLog = '''
2026-05-01 INFO {"access_token":"jwt.header.payload123","username":"tester"}
2026-05-01 DEBUG {"bookId":"b100","bookTitle":"测试广播剧","episodeId":"e1","title":"第1集 相遇","ossAudioUrl":"https://cdn.example.com/AUDIO/Ep1.M3U8?Expires=999&Signature=AbCdEf"}
2026-05-01 DEBUG {"url":"https://cdn.example.com/standalone/BGM_Track.MP3?Token=XyZ"}
2026-05-01 DEBUG cover="https://img.example.com/poster/cover_01.webp?w=600"
''';

      final parsed = LogParser.parseLogContent(sampleLog);
      expect(parsed.accessToken, 'jwt.header.payload123');
      expect(parsed.seriesMap.containsKey('b100'), isTrue);
      expect(parsed.seriesMap['b100']!.chapters.length, 1);

      // Query string casing and params must be preserved.
      expect(
        parsed.media.any(
          (m) => m.url ==
              'https://cdn.example.com/AUDIO/Ep1.M3U8?Expires=999&Signature=AbCdEf',
        ),
        isTrue,
      );
      expect(
        parsed.media.any(
          (m) => m.url ==
              'https://cdn.example.com/standalone/BGM_Track.MP3?Token=XyZ',
        ),
        isTrue,
      );
      // Audio JSON "url" must NOT leak into images.
      expect(parsed.image.length, 1);
      expect(
        parsed.image.first.url,
        'https://img.example.com/poster/cover_01.webp?w=600',
      );
    });
  });

  group('M3U8 Parsing & Crypto & TS Extraction', () {
    test('pickBestMasterVariant chooses highest bandwidth with relative URL', () {
      const master = '''
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=320000
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1280000
high/index.m3u8?token=abc
''';
      final chosen = Downloader.pickBestMasterVariant(
        master,
        Uri.parse('https://cdn.example.com/vod/master.m3u8'),
      );
      expect(chosen, 'https://cdn.example.com/vod/high/index.m3u8?token=abc');
    });

    test('parseMediaPlaylist handles key rotation and METHOD=NONE', () {
      const playlist = '''
#EXTM3U
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-KEY:METHOD=AES-128,URI="/keys/k1.key",IV=0x00000000000000000000000000000001
seg1.ts
#EXT-X-KEY:METHOD=NONE
/open/seg2.ts?sig=1
#EXT-X-KEY:METHOD=AES-128,URI="keys/k2.key"
../seg3.ts
''';
      final plan = Downloader.parseMediaPlaylist(
        playlist,
        Uri.parse('https://cdn.example.com/a/b/index.m3u8'),
      );
      expect(plan.mediaSequence, 10);
      expect(plan.segments.length, 3);
      expect(plan.segments[0].url, 'https://cdn.example.com/a/b/seg1.ts');
      expect(plan.segments[0].key?.uri, 'https://cdn.example.com/keys/k1.key');
      expect(plan.segments[0].key?.iv?.last, 1);

      expect(plan.segments[1].url, 'https://cdn.example.com/open/seg2.ts?sig=1');
      expect(plan.segments[1].key, isNull);

      expect(plan.segments[2].url, 'https://cdn.example.com/a/seg3.ts');
      expect(plan.segments[2].sequence, 12);
      expect(plan.segments[2].key?.uri, 'https://cdn.example.com/a/b/keys/k2.key');
      expect(Downloader.ivBytesForSequence(null, 12).last, 12);
    });

    test('TsAudioExtractor extracts ADTS AAC from audio-only TS and rejects video TS', () {
      final adts1 = _buildAdtsFrame(List<int>.filled(20, 0xAA));
      final adts2 = _buildAdtsFrame(List<int>.filled(24, 0xBB));
      final audioTs = Uint8List.fromList([
        ..._buildSingleTsPacket(pid: 0x101, streamId: 0xC0, esPayload: adts1),
        ..._buildSingleTsPacket(pid: 0x101, streamId: 0xC0, esPayload: adts2),
      ]);

      final extracted = TsAudioExtractor.extractAdtsFromTs(audioTs);
      expect(extracted, isNotNull);
      expect(extracted, Uint8List.fromList([...adts1, ...adts2]));

      final mixedTs = Uint8List.fromList([
        ..._buildSingleTsPacket(pid: 0x100, streamId: 0xE0, esPayload: Uint8List(16)),
        ...audioTs,
      ]);
      expect(TsAudioExtractor.extractAdtsFromTs(mixedTs), isNull);
    });

    test('decryptAes128CbcSegment round-trips PKCS7 payload cleanly', () {
      final key = Uint8List.fromList(List<int>.generate(16, (i) => i + 1));
      final iv = List<int>.generate(16, (i) => 16 - i);
      final plain = Uint8List.fromList(utf8.encode('Hello XCY AES-128 Segment!'));
      final cipher = _aes128EncryptWithPkcs7(plain, key, iv);
      final roundTrip = Downloader.decryptAes128CbcSegment(cipher, key, iv);
      expect(roundTrip, plain);
    });
  });

  group('Downloader Integration (Mock HTTP Client)', () {
    late Directory tempDir;

    final key1 = Uint8List.fromList(List<int>.generate(16, (i) => 0x10 + i));
    final adts1 = _buildAdtsFrame(List<int>.filled(30, 0x5A));
    final adts2 = _buildAdtsFrame(List<int>.filled(28, 0xA5));
    final seg0Ts = _buildSingleTsPacket(pid: 0x101, streamId: 0xC0, esPayload: adts1);
    final seg1Ts = _buildSingleTsPacket(pid: 0x101, streamId: 0xC0, esPayload: adts2);
    var seg0FailuresRemaining = 1;

    MockClient buildMockClient() => MockClient((req) async {
          final path = req.url.path;
          if (path == '/master.m3u8') {
            return http.Response(
              '#EXTM3U\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=100000\n'
              '/low.m3u8\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=900000\n'
              '/playlist.m3u8\n',
              200,
            );
          }
          if (path == '/playlist.m3u8') {
            return http.Response(
              '#EXTM3U\n'
              '#EXT-X-MEDIA-SEQUENCE:0\n'
              '#EXT-X-KEY:METHOD=AES-128,URI="/k1.key"\n'
              '/seg0.ts\n'
              '#EXT-X-KEY:METHOD=NONE\n'
              '/seg1.ts\n',
              200,
            );
          }
          if (path == '/broken.m3u8') {
            return http.Response('#EXTM3U\n/seg0_ok.ts\n/missing_404.ts\n', 200);
          }
          if (path == '/k1.key') {
            return http.Response.bytes(key1, 200);
          }
          if (path == '/seg0.ts') {
            if (seg0FailuresRemaining > 0) {
              seg0FailuresRemaining--;
              return http.Response('transient error', 500);
            }
            final iv = Downloader.ivBytesForSequence(null, 0);
            return http.Response.bytes(
              _aes128EncryptWithPkcs7(seg0Ts, key1, iv),
              200,
            );
          }
          if (path == '/seg1.ts' || path == '/seg0_ok.ts') {
            return http.Response.bytes(seg1Ts, 200);
          }
          return http.Response('not found', 404);
        });

    setUp(() async {
      tempDir = await Directory.systemTemp.createTemp('xcy_dl_test_');
      seg0FailuresRemaining = 1;
    });

    tearDown(() async {
      if (await tempDir.exists()) {
        await tempDir.delete(recursive: true);
      }
    });

    test('downloads encrypted+mixed M3U8 with retry, extracts AAC, skips rerun', () async {
      const baseUrl = 'https://mock.xcy.local';
      final client = buildMockClient();
      final logs = <String>[];
      final res = await Downloader.downloadM3u8(
        url: '$baseUrl/master.m3u8',
        baseOutDir: tempDir.path,
        saveFilename: '第1.5集 测试.mp4',
        logFn: logs.add,
        cancellationToken: CancellationToken(),
        client: client,
      );

      expect(res.status, DownloadStatus.success);
      expect(res.savedPath, isNotNull);
      expect(res.savedPath!.endsWith('.aac'), isTrue);
      final savedBytes = await File(res.savedPath!).readAsBytes();
      expect(savedBytes, Uint8List.fromList([...adts1, ...adts2]));

      // Re-running without forceOverwrite must skip cleanly.
      final rerun = await Downloader.downloadM3u8(
        url: '$baseUrl/master.m3u8',
        baseOutDir: tempDir.path,
        saveFilename: '第1.5集 测试.mp4',
        logFn: logs.add,
        cancellationToken: CancellationToken(),
        client: client,
      );
      expect(rerun.status, DownloadStatus.skipped);
      expect(rerun.savedPath, res.savedPath);
    });

    test('fails strictly and cleans temp files when any segment permanently 404s', () async {
      const baseUrl = 'https://mock.xcy.local';
      final res = await Downloader.downloadM3u8(
        url: '$baseUrl/broken.m3u8',
        baseOutDir: tempDir.path,
        saveFilename: '坏掉的剧集.mp4',
        logFn: (_) {},
        cancellationToken: CancellationToken(),
        client: buildMockClient(),
      );

      expect(res.status, DownloadStatus.failed);
      final mediaDir = Directory('${tempDir.path}/media');
      if (await mediaDir.exists()) {
        expect(mediaDir.listSync(), isEmpty);
      }
    });

    test('deduplicateBatchItems avoids filename collisions', () {
      final deduped = Downloader.deduplicateBatchItems([
        DownloadItem(filename: 'ep1.aac', type: 'audio', url: 'https://a/1'),
        DownloadItem(filename: 'ep1.aac', type: 'audio', url: 'https://a/2'),
        DownloadItem(filename: 'cover.jpg', type: 'image', url: 'https://a/3'),
      ]);
      expect(deduped.map((e) => e.filename).toList(), [
        'ep1.aac',
        'ep1_2.aac',
        'cover.jpg',
      ]);
    });
  });

  testWidgets('MyApp renders cleanly without overflow', (tester) async {
    await tester.pumpWidget(const MyApp());
    await tester.pumpAndSettle();

    expect(find.text('星次元下载器'), findsOneWidget);
    expect(find.text('v2.5.0'), findsOneWidget);
    expect(find.text('音视频 (0)'), findsOneWidget);
    expect(find.text('图片 (0)'), findsOneWidget);
    expect(find.text('开始下载 (0)'), findsOneWidget);
  });
}
