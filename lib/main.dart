import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:share_plus/share_plus.dart';

import 'downloader.dart';
import 'log_parser.dart';

void main() {
  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '星次元下载器',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        colorSchemeSeed: const Color(0xFF6366F1),
        scaffoldBackgroundColor: const Color(0xFF0F0F13),
      ),
      home: const MainScreen(),
    );
  }
}

class MainScreen extends StatefulWidget {
  const MainScreen({super.key});

  @override
  State<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends State<MainScreen>
    with SingleTickerProviderStateMixin {
  static const int _maxConsoleLines = 300;
  static const String _defaultAndroidLog =
      '/storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log';
  static const String _defaultAndroidOut =
      '/storage/emulated/0/Download/XcyDownloads';

  late final TextEditingController _logController;
  late final TextEditingController _outController;
  final TextEditingController _tokenController = TextEditingController();
  final TextEditingController _searchController = TextEditingController();
  final TextEditingController _rangeStartController =
      TextEditingController(text: '1');
  final TextEditingController _rangeEndController =
      TextEditingController(text: '50');
  final ScrollController _consoleScrollController = ScrollController();

  late final TabController _tabController;

  List<DownloadItem> _mediaItems = [];
  List<DownloadItem> _imageItems = [];
  final Map<String, SeriesInfo> _seriesMap = {};
  final Set<DownloadItem> _selectedItems = {};
  final Map<String, String> _savedPaths = {};

  String? _selectedSeriesKey;
  String _searchQuery = '';
  String _tokenStatus = '';

  bool _isParsing = false;
  bool _isApiBusy = false;
  bool _isDownloading = false;
  bool _isSettingsExpanded = true;
  bool _isConsoleExpanded = true;

  double _progressValue = 0.0;
  String _progressLabel = '等待开始';
  final List<String> _consoleLogs = [];
  CancellationToken _cancellationToken = CancellationToken();

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _logController = TextEditingController(text: _resolveDefaultLogPath());
    _outController = TextEditingController(text: _resolveDefaultOutPath());

    WidgetsBinding.instance.addPostFrameCallback((_) {
      _parseLogFile(silent: true);
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    _logController.dispose();
    _outController.dispose();
    _tokenController.dispose();
    _searchController.dispose();
    _rangeStartController.dispose();
    _rangeEndController.dispose();
    _consoleScrollController.dispose();
    super.dispose();
  }

  String _resolveDefaultLogPath() {
    if (Platform.isAndroid) return _defaultAndroidLog;
    return '';
  }

  String _resolveDefaultOutPath() {
    if (Platform.isAndroid) return _defaultAndroidOut;
    final home = Platform.environment['USERPROFILE'] ??
        Platform.environment['HOME'] ??
        Directory.current.path;
    return '$home${Platform.pathSeparator}Downloads${Platform.pathSeparator}XcyDownloads';
  }

  void _addLog(String msg) {
    if (!mounted) return;
    setState(() {
      _consoleLogs.add(msg);
      if (_consoleLogs.length > _maxConsoleLines) {
        _consoleLogs.removeRange(0, _consoleLogs.length - _maxConsoleLines);
      }
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_consoleScrollController.hasClients) {
        _consoleScrollController.jumpTo(
          _consoleScrollController.position.maxScrollExtent,
        );
      }
    });
  }

  void _showSnackBar(String msg, {bool isError = false}) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg, style: const TextStyle(color: Colors.white)),
        backgroundColor:
            isError ? const Color(0xFFEF4444) : const Color(0xFF10B981),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 3),
      ),
    );
  }

  Future<void> _requestPermissions() async {
    if (!Platform.isAndroid) return;
    try {
      await [
        Permission.storage,
        Permission.manageExternalStorage,
        Permission.videos,
        Permission.audio,
        Permission.photos,
      ].request();
    } catch (_) {
      // Fall back to scoped storage inside Downloader.getSafeOutputDir.
    }
  }

  Future<void> _pickLogFile() async {
    try {
      final result = await FilePicker.platform.pickFiles(type: FileType.any);
      if (result != null && result.files.single.path != null) {
        _logController.text = result.files.single.path!;
        await _parseLogFile();
      }
    } catch (e) {
      _showSnackBar('选择文件异常: $e', isError: true);
    }
  }

  Future<void> _pickOutputDirectory() async {
    try {
      final dir = await FilePicker.platform.getDirectoryPath();
      if (dir != null) {
        setState(() {
          _outController.text = dir;
        });
      }
    } catch (e) {
      _showSnackBar('选择目录异常: $e', isError: true);
    }
  }

  Future<void> _parseLogFile({bool silent = false}) async {
    final path = _logController.text.trim();
    if (path.isEmpty) {
      if (!silent) _showSnackBar('请输入或选择 app.log 路径', isError: true);
      return;
    }

    final file = File(path);
    if (!await file.exists()) {
      if (!silent) _showSnackBar('找不到日志文件: $path', isError: true);
      return;
    }

    setState(() => _isParsing = true);
    _addLog('[*] 正在后台解析日志: $path');

    try {
      final parsed = await LogParser.parseLogRich(path);
      if (!mounted) return;

      setState(() {
        _mediaItems = parsed.media;
        _imageItems = parsed.image;
        _seriesMap
          ..clear()
          ..addAll(parsed.seriesMap);
        _selectedItems.clear();
        _syncSelectedSeries();

        if (parsed.accessToken != null &&
            _tokenController.text.trim().isEmpty) {
          _tokenController.text = parsed.accessToken!;
          _tokenStatus = '已自动提取 Token';
        }
      });

      _addLog(
        '[+] 解析完成: 音视频 ${parsed.media.length} 条，图片 ${parsed.image.length} 条，'
        '系列 ${parsed.seriesMap.length} 部'
        '${parsed.accessToken != null ? "（已检测到 Token）" : ""}',
      );
      if (!silent) {
        _showSnackBar(
          '解析完成: ${parsed.media.length} 音视频 / ${parsed.image.length} 图片',
        );
      }
    } catch (e) {
      _addLog('[-] 日志解析异常: $e');
      if (!silent) _showSnackBar('解析失败: $e', isError: true);
    } finally {
      if (mounted) setState(() => _isParsing = false);
    }
  }

  void _syncSelectedSeries() {
    if (_seriesMap.isEmpty) {
      _selectedSeriesKey = null;
      return;
    }
    if (_selectedSeriesKey == null ||
        !_seriesMap.containsKey(_selectedSeriesKey)) {
      _selectedSeriesKey = _seriesMap.keys.first;
    }
    final active = _seriesMap[_selectedSeriesKey];
    if (active != null && active.chapters.isNotEmpty) {
      _rangeStartController.text = '${active.minEpisode}';
      _rangeEndController.text = '${active.maxEpisode}';
    }
  }

  Future<void> _verifyToken() async {
    final token = _tokenController.text.trim();
    if (token.isEmpty) {
      _showSnackBar('请先输入或从日志提取 Token', isError: true);
      return;
    }

    setState(() => _isApiBusy = true);
    try {
      final username = await XcyApiClient.verifyToken(token);
      if (!mounted) return;
      setState(() {
        _tokenStatus = '有效: $username';
      });
      _addLog('[+] Token 验证通过，当前账号: $username');
      _showSnackBar('Token 有效 ($username)');
    } catch (e) {
      if (!mounted) return;
      setState(() => _tokenStatus = '验证失败');
      _addLog('[-] Token 验证失败: $e');
      _showSnackBar('Token 验证失败: $e', isError: true);
    } finally {
      if (mounted) setState(() => _isApiBusy = false);
    }
  }

  Future<void> _discoverBooks() async {
    final token = _tokenController.text.trim();
    if (token.isEmpty) {
      _showSnackBar('请先填写 Token 才能拉取云端书库', isError: true);
      return;
    }

    setState(() => _isApiBusy = true);
    _addLog('[*] 正在从云端拉取推荐书单...');
    try {
      final books = await XcyApiClient.fetchBookList(token);
      if (!mounted) return;
      var added = 0;
      setState(() {
        for (final entry in books.entries) {
          if (!_seriesMap.containsKey(entry.key)) {
            _seriesMap[entry.key] = entry.value;
            added++;
          }
        }
        _syncSelectedSeries();
      });
      _addLog('[+] 云端书单发现 ${books.length} 部（新增 $added 部）');
      _showSnackBar('发现 ${books.length} 部书籍');
    } catch (e) {
      _addLog('[-] 拉取书单失败: $e');
      _showSnackBar('拉取书单失败: $e', isError: true);
    } finally {
      if (mounted) setState(() => _isApiBusy = false);
    }
  }

  Future<void> _completeSeriesEpisodes() async {
    final token = _tokenController.text.trim();
    if (token.isEmpty) {
      _showSnackBar('请先填写 Token 再补全剧集', isError: true);
      return;
    }
    final series =
        _selectedSeriesKey != null ? _seriesMap[_selectedSeriesKey] : null;
    if (series == null || series.bookId.isEmpty) {
      _showSnackBar('请先选择一个含 bookId 的系列', isError: true);
      return;
    }

    setState(() => _isApiBusy = true);
    _addLog('[*] 正在云端补全《${series.bookTitle}》全集...');
    try {
      final episodes = await XcyApiClient.fetchEpisodesFromApi(
        token,
        series.bookId,
        series.bookTitle,
        onPage: (page, count) =>
            _addLog('  -> 第 $page 页已获取，累计 $count 集'),
      );
      if (!mounted) return;

      final existingUrls = series.chapters.map((e) => e.ossAudioUrl).toSet();
      final mediaUrls = _mediaItems.map((e) => e.url).toSet();
      var addedCount = 0;

      setState(() {
        for (final ep in episodes) {
          if (!existingUrls.contains(ep.ossAudioUrl)) {
            series.chapters.add(ep);
            existingUrls.add(ep.ossAudioUrl);
            addedCount++;
          }
          if (!mediaUrls.contains(ep.ossAudioUrl)) {
            _mediaItems.add(ep.toDownloadItem());
            mediaUrls.add(ep.ossAudioUrl);
          }
        }
        series.chapters.sort(LogParser.compareEpisodes);
        _syncSelectedSeries();
      });

      _addLog('[+] 《${series.bookTitle}》云端补全完成，新增 $addedCount 集');
      _showSnackBar('补全完成，新增 $addedCount 集');
    } catch (e) {
      _addLog('[-] 云端补全失败: $e');
      _showSnackBar('补全失败: $e', isError: true);
    } finally {
      if (mounted) setState(() => _isApiBusy = false);
    }
  }

  void _selectEpisodeRange() {
    final series =
        _selectedSeriesKey != null ? _seriesMap[_selectedSeriesKey] : null;
    if (series == null || series.chapters.isEmpty) {
      _showSnackBar('当前系列暂无已加载剧集，可点击「补全」获取', isError: true);
      return;
    }
    final start = int.tryParse(_rangeStartController.text.trim()) ?? 1;
    final end = int.tryParse(_rangeEndController.text.trim()) ?? 999999;
    final lo = start <= end ? start : end;
    final hi = start <= end ? end : start;

    final targetUrls = series.chapters
        .where((ep) => ep.episodeNum >= lo && ep.episodeNum <= hi)
        .map((ep) => ep.ossAudioUrl)
        .toSet();

    if (targetUrls.isEmpty) {
      _showSnackBar('第 $lo-$hi 集范围内没有匹配项', isError: true);
      return;
    }

    setState(() {
      var matched = 0;
      for (final item in _mediaItems) {
        if (targetUrls.contains(item.url)) {
          _selectedItems.add(item);
          matched++;
        }
      }
      _tabController.animateTo(0);
      _addLog('[*] 已勾选《${series.bookTitle}》第 $lo-$hi 集，共 $matched 项');
    });
  }

  Future<void> _startDownload() async {
    if (_isDownloading) return;

    final outDir = _outController.text.trim();
    if (outDir.isEmpty) {
      _showSnackBar('请指定有效的输出目录', isError: true);
      return;
    }
    if (_selectedItems.isEmpty) {
      _showSnackBar('请先勾选至少一个下载项', isError: true);
      return;
    }

    await _requestPermissions();

    setState(() {
      _isDownloading = true;
      _progressValue = 0.0;
      _progressLabel = '准备运行...';
      _consoleLogs.clear();
      _cancellationToken = CancellationToken();
    });

    final itemsToDownload = _selectedItems.toList();
    try {
      await Downloader.runDownloadTask(
        selectedItems: itemsToDownload,
        outDir: outDir,
        logFn: _addLog,
        onItemFinished: (item, result) {
          if (result.savedPath != null && mounted) {
            setState(() {
              _savedPaths[item.url] = result.savedPath!;
            });
          }
        },
        progressFn: (done, total) {
          if (!mounted) return;
          setState(() {
            _progressValue = total > 0 ? done / total : 0.0;
            _progressLabel =
                '进度: $done/$total (${(_progressValue * 100).toInt()}%)';
          });
        },
        doneFn: () {
          if (!mounted) return;
          setState(() {
            _isDownloading = false;
            _progressLabel =
                _cancellationToken.isCancelled ? '已中止' : '全部完成';
          });
          _showSnackBar(
            _cancellationToken.isCancelled ? '下载已中止' : '队列处理完成',
          );
        },
        cancellationToken: _cancellationToken,
      );
    } catch (e) {
      _addLog('[✗] 下载出现异常: $e');
      if (mounted) {
        setState(() {
          _isDownloading = false;
          _progressLabel = '发生错误';
        });
      }
    }
  }

  void _stopDownload() {
    _cancellationToken.cancel();
    _addLog('[!] 正在发送停止信号，等待当前分片结束...');
  }

  Future<void> _shareFile(DownloadItem item) async {
    String? existing = _savedPaths[item.url];
    if (existing == null || !File(existing).existsSync()) {
      final outDir = _outController.text.trim();
      existing = Downloader.resolveExistingDownloadPath(outDir, item);
      if (existing == null) {
        final safeDir = await Downloader.getSafeOutputDir(outDir);
        existing = Downloader.resolveExistingDownloadPath(safeDir, item);
      }
    }

    if (existing != null && File(existing).existsSync()) {
      try {
        await Share.shareXFiles(
          [XFile(existing)],
          text: '分享资源: ${item.filename}',
        );
      } catch (e) {
        _showSnackBar('分享失败: $e', isError: true);
      }
    } else {
      _showSnackBar('未找到已下载文件，请先完成下载', isError: true);
    }
  }

  List<DownloadItem> _filterItems(List<DownloadItem> list) {
    final q = _searchQuery.trim().toLowerCase();
    if (q.isEmpty) return list;
    return list
        .where(
          (item) =>
              item.filename.toLowerCase().contains(q) ||
              item.url.toLowerCase().contains(q),
        )
        .toList();
  }

  void _toggleCurrentTabSelection(List<DownloadItem> currentList, bool select) {
    setState(() {
      if (select) {
        _selectedItems.addAll(currentList);
      } else {
        _selectedItems.removeAll(currentList);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final filteredMedia = _filterItems(_mediaItems);
    final filteredImages = _filterItems(_imageItems);

    return Scaffold(
      appBar: AppBar(
        backgroundColor: const Color(0xFF15151C),
        toolbarHeight: 48,
        title: Row(
          children: [
            const Text(
              '星次元下载器',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold),
            ),
            const SizedBox(width: 8),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              decoration: BoxDecoration(
                color: const Color(0xFF4F46E5).withValues(alpha: 0.25),
                borderRadius: BorderRadius.circular(6),
                border: Border.all(
                  color: const Color(0xFF6366F1).withValues(alpha: 0.5),
                ),
              ),
              child: const Text(
                'v2.5.0',
                style: TextStyle(
                  fontSize: 11,
                  color: Color(0xFFA5B4FC),
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: _isSettingsExpanded ? '收起配置面板' : '展开配置面板',
            icon: Icon(
              _isSettingsExpanded
                  ? Icons.keyboard_arrow_up_rounded
                  : Icons.tune_rounded,
            ),
            onPressed: () {
              setState(() => _isSettingsExpanded = !_isSettingsExpanded);
            },
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
          child: Column(
            children: [
              if (_isSettingsExpanded)
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 220),
                  child: SingleChildScrollView(
                    child: _buildConfigPanel(),
                  ),
                ),
              if (_isSettingsExpanded) const SizedBox(height: 8),
              _buildSearchAndBatchBar(filteredMedia, filteredImages),
              const SizedBox(height: 6),
              TabBar(
                controller: _tabController,
                indicatorColor: const Color(0xFF6366F1),
                labelColor: Colors.white,
                unselectedLabelColor: Colors.white54,
                tabs: [
                  Tab(text: '音视频 (${filteredMedia.length})'),
                  Tab(text: '图片 (${filteredImages.length})'),
                ],
              ),
              Expanded(
                child: TabBarView(
                  controller: _tabController,
                  children: [
                    _buildItemList(filteredMedia, emptyHint: '暂无音视频，请先解析日志或补全剧集'),
                    _buildItemList(filteredImages, emptyHint: '暂无图片资源'),
                  ],
                ),
              ),
              const SizedBox(height: 6),
              _buildProgressAndConsole(),
              const SizedBox(height: 8),
              _buildBottomActions(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildConfigPanel() {
    final seriesKeys = _seriesMap.keys.toList();
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: const Color(0xFF181822),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFF282836)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          _buildPathRow(
            label: '日志路径',
            controller: _logController,
            hint: '选择 app.log',
            onPick: _pickLogFile,
            actionLabel: _isParsing ? '解析中' : '解析',
            onAction: _isParsing ? null : () => _parseLogFile(),
          ),
          const SizedBox(height: 6),
          _buildPathRow(
            label: '输出目录',
            controller: _outController,
            hint: '下载保存目录',
            onPick: _pickOutputDirectory,
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              const SizedBox(
                width: 60,
                child: Text(
                  'Token',
                  style: TextStyle(fontSize: 12, color: Colors.white70),
                ),
              ),
              Expanded(
                child: TextField(
                  controller: _tokenController,
                  style: const TextStyle(fontSize: 12),
                  decoration: InputDecoration(
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(
                      horizontal: 10,
                      vertical: 8,
                    ),
                    hintText: '自动从日志提取或手动粘贴 Bearer Token',
                    hintStyle: const TextStyle(fontSize: 11),
                    filled: true,
                    fillColor: const Color(0xFF101018),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(8),
                      borderSide: BorderSide.none,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 6),
              OutlinedButton(
                onPressed: _isApiBusy ? null : _verifyToken,
                style: OutlinedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                  minimumSize: const Size(0, 32),
                ),
                child: const Text('验证', style: TextStyle(fontSize: 11)),
              ),
              const SizedBox(width: 4),
              OutlinedButton(
                onPressed: _isApiBusy ? null : _discoverBooks,
                style: OutlinedButton.styleFrom(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                  minimumSize: const Size(0, 32),
                ),
                child: const Text('发现书单', style: TextStyle(fontSize: 11)),
              ),
            ],
          ),
          if (_tokenStatus.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(left: 60, top: 3),
              child: Text(
                _tokenStatus,
                style: const TextStyle(fontSize: 11, color: Color(0xFF34D399)),
              ),
            ),
          if (seriesKeys.isNotEmpty) ...[
            const SizedBox(height: 6),
            Row(
              children: [
                const SizedBox(
                  width: 60,
                  child: Text(
                    '系列剧集',
                    style: TextStyle(fontSize: 12, color: Colors.white70),
                  ),
                ),
                Expanded(
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    decoration: BoxDecoration(
                      color: const Color(0xFF101018),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: DropdownButtonHideUnderline(
                      child: DropdownButton<String>(
                        isExpanded: true,
                        isDense: true,
                        value: _selectedSeriesKey,
                        style: const TextStyle(
                          fontSize: 12,
                          color: Colors.white,
                        ),
                        dropdownColor: const Color(0xFF1C1C28),
                        items: seriesKeys.map((key) {
                          final s = _seriesMap[key]!;
                          return DropdownMenuItem<String>(
                            value: key,
                            child: Text(
                              '${s.bookTitle} (${s.count}集 ${s.episodeRange})',
                              overflow: TextOverflow.ellipsis,
                            ),
                          );
                        }).toList(),
                        onChanged: (val) {
                          setState(() {
                            _selectedSeriesKey = val;
                            _syncSelectedSeries();
                          });
                        },
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                ElevatedButton(
                  onPressed: _isApiBusy ? null : _completeSeriesEpisodes,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF4F46E5),
                    foregroundColor: Colors.white,
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                    minimumSize: const Size(0, 32),
                  ),
                  child: const Text('补全', style: TextStyle(fontSize: 11)),
                ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                const SizedBox(
                  width: 60,
                  child: Text(
                    '按集选取',
                    style: TextStyle(fontSize: 12, color: Colors.white70),
                  ),
                ),
                SizedBox(
                  width: 58,
                  child: TextField(
                    controller: _rangeStartController,
                    keyboardType: TextInputType.number,
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 12),
                    decoration: InputDecoration(
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(vertical: 6),
                      filled: true,
                      fillColor: const Color(0xFF101018),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(6),
                        borderSide: BorderSide.none,
                      ),
                    ),
                  ),
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 6),
                  child: Text('至', style: TextStyle(fontSize: 12)),
                ),
                SizedBox(
                  width: 58,
                  child: TextField(
                    controller: _rangeEndController,
                    keyboardType: TextInputType.number,
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 12),
                    decoration: InputDecoration(
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(vertical: 6),
                      filled: true,
                      fillColor: const Color(0xFF101018),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(6),
                        borderSide: BorderSide.none,
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: _selectEpisodeRange,
                  style: OutlinedButton.styleFrom(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                    minimumSize: const Size(0, 32),
                  ),
                  child: const Text('勾选区间', style: TextStyle(fontSize: 11)),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildPathRow({
    required String label,
    required TextEditingController controller,
    required String hint,
    required VoidCallback onPick,
    String? actionLabel,
    VoidCallback? onAction,
  }) {
    return Row(
      children: [
        SizedBox(
          width: 60,
          child: Text(
            label,
            style: const TextStyle(fontSize: 12, color: Colors.white70),
          ),
        ),
        Expanded(
          child: TextField(
            controller: controller,
            style: const TextStyle(fontSize: 12),
            decoration: InputDecoration(
              isDense: true,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 10,
                vertical: 8,
              ),
              hintText: hint,
              hintStyle: const TextStyle(fontSize: 11),
              filled: true,
              fillColor: const Color(0xFF101018),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
        const SizedBox(width: 4),
        IconButton(
          tooltip: '浏览',
          icon: const Icon(Icons.folder_open_rounded, size: 18),
          visualDensity: VisualDensity.compact,
          onPressed: onPick,
        ),
        if (actionLabel != null)
          ElevatedButton(
            onPressed: onAction,
            style: ElevatedButton.styleFrom(
              backgroundColor: const Color(0xFF4F46E5),
              foregroundColor: Colors.white,
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              minimumSize: const Size(0, 32),
            ),
            child: Text(actionLabel, style: const TextStyle(fontSize: 11)),
          ),
      ],
    );
  }

  Widget _buildSearchAndBatchBar(
    List<DownloadItem> filteredMedia,
    List<DownloadItem> filteredImages,
  ) {
    final activeList =
        _tabController.index == 0 ? filteredMedia : filteredImages;

    return Row(
      children: [
        Expanded(
          child: TextField(
            controller: _searchController,
            onChanged: (v) => setState(() => _searchQuery = v),
            style: const TextStyle(fontSize: 12),
            decoration: InputDecoration(
              isDense: true,
              prefixIcon: const Icon(Icons.search, size: 16),
              hintText: '搜索文件名或链接...',
              hintStyle: const TextStyle(fontSize: 11),
              filled: true,
              fillColor: const Color(0xFF181822),
              contentPadding: const EdgeInsets.symmetric(vertical: 8),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(8),
                borderSide: BorderSide.none,
              ),
            ),
          ),
        ),
        const SizedBox(width: 6),
        TextButton(
          onPressed: () => _toggleCurrentTabSelection(activeList, true),
          style: TextButton.styleFrom(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            minimumSize: const Size(0, 34),
          ),
          child: const Text('全选', style: TextStyle(fontSize: 12)),
        ),
        TextButton(
          onPressed: () => setState(() => _selectedItems.clear()),
          style: TextButton.styleFrom(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            minimumSize: const Size(0, 34),
          ),
          child: Text(
            '清空(${_selectedItems.length})',
            style: const TextStyle(fontSize: 12),
          ),
        ),
      ],
    );
  }

  Widget _buildItemList(
    List<DownloadItem> items, {
    required String emptyHint,
  }) {
    if (items.isEmpty) {
      return Center(
        child: Text(
          emptyHint,
          style: const TextStyle(color: Colors.white54, fontSize: 13),
        ),
      );
    }

    return ListView.builder(
      itemCount: items.length,
      itemBuilder: (context, index) {
        final item = items[index];
        final isChecked = _selectedItems.contains(item);
        final isSaved = _savedPaths.containsKey(item.url);

        return Container(
          margin: const EdgeInsets.symmetric(vertical: 2),
          decoration: BoxDecoration(
            color: const Color(0xFF16161F),
            borderRadius: BorderRadius.circular(8),
          ),
          child: ListTile(
            dense: true,
            visualDensity: VisualDensity.compact,
            leading: Checkbox(
              value: isChecked,
              onChanged: (val) {
                setState(() {
                  if (val == true) {
                    _selectedItems.add(item);
                  } else {
                    _selectedItems.remove(item);
                  }
                });
              },
            ),
            title: Text(
              item.filename,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 12.5, color: Colors.white),
            ),
            subtitle: Text(
              '${item.type} · ${item.url}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 10.5, color: Colors.white54),
            ),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (isSaved)
                  const Icon(
                    Icons.check_circle_rounded,
                    size: 16,
                    color: Color(0xFF34D399),
                  ),
                IconButton(
                  tooltip: '分享已下载文件',
                  icon: const Icon(Icons.share_rounded, size: 17),
                  onPressed: () => _shareFile(item),
                ),
              ],
            ),
            onTap: () {
              setState(() {
                if (isChecked) {
                  _selectedItems.remove(item);
                } else {
                  _selectedItems.add(item);
                }
              });
            },
          ),
        );
      },
    );
  }

  Widget _buildProgressAndConsole() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            Expanded(
              child: LinearProgressIndicator(
                value: _progressValue,
                minHeight: 6,
                backgroundColor: const Color(0xFF1F1F2B),
                valueColor: const AlwaysStoppedAnimation(Color(0xFF6366F1)),
                borderRadius: BorderRadius.circular(4),
              ),
            ),
            const SizedBox(width: 8),
            Text(
              _progressLabel,
              style: const TextStyle(fontSize: 11, color: Colors.white70),
            ),
            const SizedBox(width: 4),
            InkWell(
              onTap: () =>
                  setState(() => _isConsoleExpanded = !_isConsoleExpanded),
              child: Icon(
                _isConsoleExpanded
                    ? Icons.expand_more_rounded
                    : Icons.terminal_rounded,
                size: 18,
                color: Colors.white70,
              ),
            ),
          ],
        ),
        if (_isConsoleExpanded) ...[
          const SizedBox(height: 6),
          Container(
            height: 82,
            width: double.infinity,
            decoration: BoxDecoration(
              color: const Color(0xFF0C0D12),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: const Color(0xFF1E1E26)),
            ),
            padding: const EdgeInsets.all(8),
            child: ListView.builder(
              controller: _consoleScrollController,
              itemCount: _consoleLogs.length,
              itemBuilder: (context, idx) {
                return Text(
                  _consoleLogs[idx],
                  style: const TextStyle(
                    color: Color(0xFF39FF14),
                    fontFamily: 'monospace',
                    fontSize: 10.5,
                  ),
                );
              },
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildBottomActions() {
    return Row(
      children: [
        Expanded(
          child: Container(
            height: 42,
            decoration: BoxDecoration(
              gradient: _isDownloading
                  ? null
                  : const LinearGradient(
                      colors: [Color(0xFF4F46E5), Color(0xFF6366F1)],
                    ),
              color: _isDownloading ? Colors.grey : null,
              borderRadius: BorderRadius.circular(10),
            ),
            child: ElevatedButton(
              onPressed: _isDownloading ? null : _startDownload,
              style: ElevatedButton.styleFrom(
                backgroundColor: Colors.transparent,
                shadowColor: Colors.transparent,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10),
                ),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(Icons.play_arrow_rounded, color: Colors.white),
                  const SizedBox(width: 6),
                  Text(
                    _isDownloading ? '正在下载...' : '开始下载 (${_selectedItems.length})',
                    style: const TextStyle(
                      color: Colors.white,
                      fontWeight: FontWeight.bold,
                      fontSize: 13.5,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(width: 10),
        ElevatedButton(
          onPressed: _isDownloading ? _stopDownload : null,
          style: ElevatedButton.styleFrom(
            backgroundColor: const Color(0xFFEF4444),
            disabledBackgroundColor: const Color(0xFF2E2E38),
            foregroundColor: Colors.white,
            padding: const EdgeInsets.symmetric(horizontal: 14),
            minimumSize: const Size(0, 42),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(10),
            ),
          ),
          child: const Row(
            children: [
              Icon(Icons.stop_rounded, size: 18),
              SizedBox(width: 4),
              Text('停止', style: TextStyle(fontWeight: FontWeight.bold)),
            ],
          ),
        ),
      ],
    );
  }
}
