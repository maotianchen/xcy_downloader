import 'dart:io';
import 'package:flutter/material.dart';
import 'package:file_picker/file_picker.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:share_plus/share_plus.dart';
import 'log_parser.dart';
import 'downloader.dart';

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

class _MainScreenState extends State<MainScreen> with SingleTickerProviderStateMixin {
  final TextEditingController _logController = TextEditingController(
    text: '/storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log',
  );
  final TextEditingController _outController = TextEditingController();
  final ScrollController _consoleScrollController = ScrollController();
  
  late TabController _tabController;

  // 状态变量
  List<DownloadItem> _mediaItems = [];
  List<DownloadItem> _imageItems = [];
  final Set<DownloadItem> _selectedItems = {};
  
  bool _isDownloading = false;
  double _progressValue = 0.0;
  String _progressLabel = "等待开始";
  final List<String> _consoleLogs = [];
  String _searchQuery = "";
  
  CancellationToken _cancellationToken = CancellationToken();
  bool _isSettingsExpanded = true;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _initPaths();
    // 首次尝试解析默认路径
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _parseLogFile(silent: true);
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    _logController.dispose();
    _outController.dispose();
    _consoleScrollController.dispose();
    super.dispose();
  }

  /// 初始化默认输出路径
  Future<void> _initPaths() async {
    // 默认使用安卓公共的 Download 目录，使用户下载的音频、视频和图片能在系统文件管理器中即刻直接可见！
    setState(() {
      _outController.text = "/storage/emulated/0/Download/XcyDownloads";
    });
  }

  /// 申请安卓存储相关权限
  Future<bool> _requestPermissions() async {
    if (Platform.isAndroid) {
      final statuses = await [
        Permission.storage,
        Permission.manageExternalStorage,
      ].request();
      
      final isStorageGranted = statuses[Permission.storage]?.isGranted ?? false;
      final isManageGranted = statuses[Permission.manageExternalStorage]?.isGranted ?? false;
      
      return isStorageGranted || isManageGranted;
    }
    return true;
  }

  /// 打印日志至终端控制台
  void _addLog(String msg) {
    setState(() {
      _consoleLogs.add(msg);
    });
    // 自动滚动到底部
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_consoleScrollController.hasClients) {
        _consoleScrollController.animateTo(
          _consoleScrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 150),
          curve: Curves.easeOut,
        );
      }
    });
  }

  /// 一键重置日志文件路径为默认配置
  void _resetLogPath() {
    setState(() {
      _logController.text = '/storage/emulated/0/Android/data/com.meother.qmyz/files/logs/app.log';
    });
    _parseLogFile();
  }

  /// 选择 app.log 文件路径
  Future<void> _pickLogFile() async {
    await _requestPermissions();
    try {
      final result = await FilePicker.platform.pickFiles(
        type: FileType.any,
        dialogTitle: '选择 app.log 日志文件',
      );
      if (result != null && result.files.single.path != null) {
        setState(() {
          _logController.text = result.files.single.path!;
        });
        _parseLogFile();
      }
    } catch (e) {
      _showSnackBar("打开文件选择器失败: $e", isError: true);
    }
  }

  /// 选择输出路径
  Future<void> _pickOutputDir() async {
    await _requestPermissions();
    try {
      final String? selectedDirectory = await FilePicker.platform.getDirectoryPath(
        dialogTitle: '选择保存输出目录',
      );
      if (selectedDirectory != null) {
        setState(() {
          _outController.text = selectedDirectory;
        });
      }
    } catch (e) {
      _showSnackBar("选择目录失败: $e", isError: true);
    }
  }

  /// 解析日志文件并更新列表
  Future<void> _parseLogFile({bool silent = false}) async {
    final path = _logController.text.trim();
    if (path.isEmpty) return;

    try {
      final file = File(path);
      if (!await file.exists()) {
        if (!silent) {
          _showSnackBar("解析失败：日志文件不存在，请检查路径或手动复制该日志文件", isError: true);
        }
        return;
      }

      final parsed = await LogParser.parseLog(path);
      setState(() {
        _mediaItems = parsed['media'] ?? [];
        _imageItems = parsed['image'] ?? [];
        
        // 默认全部选中
        _selectedItems.clear();
        _selectedItems.addAll(_mediaItems);
        _selectedItems.addAll(_imageItems);
      });
      if (!silent) {
        _showSnackBar("日志解析成功！共加载 ${_mediaItems.length + _imageItems.length} 个资源");
      }
    } catch (e) {
      if (!silent) {
        _showSnackBar("解析日志发生异常: $e", isError: true);
      }
    }
  }

  /// 列表快捷选择操作
  void _selectAction(String action) {
    setState(() {
      switch (action) {
        case 'all':
          _selectedItems.addAll(_mediaItems);
          _selectedItems.addAll(_imageItems);
          break;
        case 'none':
          _selectedItems.clear();
          break;
        case 'media':
          _selectedItems.clear();
          _selectedItems.addAll(_mediaItems);
          break;
        case 'image':
          _selectedItems.clear();
          _selectedItems.addAll(_imageItems);
          break;
      }
    });
  }

  /// 弹出底部通知
  void _showSnackBar(String msg, {bool isError = false}) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg, style: const TextStyle(color: Colors.white)),
        backgroundColor: isError ? Colors.redAccent : const Color(0xFF6366F1),
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        duration: const Duration(seconds: 3),
      ),
    );
  }

  /// 开始下载任务
  Future<void> _startDownload() async {
    if (_isDownloading) return;
    
    final logPath = _logController.text.trim();
    final outDir = _outController.text.trim();

    if (logPath.isEmpty || !await File(logPath).exists()) {
      _showSnackBar("请输入并确保有效的 app.log 路径！", isError: true);
      return;
    }
    if (outDir.isEmpty) {
      _showSnackBar("请指定有效的输出目录！", isError: true);
      return;
    }
    if (_selectedItems.isEmpty) {
      _showSnackBar("您没有勾选任何需要下载的资源！", isError: true);
      return;
    }

    await _requestPermissions();

    setState(() {
      _isDownloading = true;
      _progressValue = 0.0;
      _progressLabel = "准备运行...";
      _consoleLogs.clear();
      _cancellationToken = CancellationToken();
    });

    final itemsToDownload = _selectedItems.toList();

    // 在后台异步触发
    try {
      await Downloader.runDownloadTask(
        selectedItems: itemsToDownload,
        outDir: outDir,
        logFn: (msg) => _addLog(msg),
        progressFn: (done, total) {
          setState(() {
            _progressValue = done / total;
            _progressLabel = "进度: $done/$total  (${(_progressValue * 100).toInt()}%)";
          });
        },
        doneFn: () {
          setState(() {
            _isDownloading = false;
            _progressLabel = _cancellationToken.isCancelled ? "已中止！" : "已完成！";
          });
          _showSnackBar(_cancellationToken.isCancelled ? "下载已被中止" : "恭喜，全部任务下载完成！");
        },
        cancellationToken: _cancellationToken,
      );
    } catch (e) {
      _addLog("\n[✗] 运行发生致命异常: $e");
      setState(() {
        _isDownloading = false;
        _progressLabel = "发生错误";
      });
    }
  }

  /// 停止下载任务
  void _stopDownload() {
    _cancellationToken.cancel();
    _addLog("\n[!] 正在发出停止信号，请等待当前资源分片处理完毕...");
  }

  /// 分享已下载的文件
  Future<void> _shareFile(DownloadItem item) async {
    final outDir = _outController.text.trim();
    final isMedia = item.type == "音视频";
    final subDir = isMedia ? (item.filename.endsWith(".m4a") ? "music" : "video") : "picture";
    
    // 原名
    var finalName = item.filename;
    // 自动补齐下载规则名
    if (isMedia && !finalName.endsWith(".mp4") && !finalName.endsWith(".m4a") && !finalName.endsWith(".ts")) {
      finalName = item.filename.contains("chapter") || item.filename.contains("audio") || item.filename.contains("story")
          ? "${item.filename}.m4a"
          : "${item.filename}.mp4";
    }

    final filePath = "$outDir/$subDir/$finalName";
    final file = File(filePath);
    
    if (await file.exists()) {
      try {
        await Share.shareXFiles([XFile(filePath)], text: '分享资源: $finalName');
      } catch (e) {
        _showSnackBar("分享失败: $e", isError: true);
      }
    } else {
      _showSnackBar("找不到已下载文件，可能尚未下载完成：$filePath", isError: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    // 过滤检索项目
    final filteredMedia = _mediaItems.where((item) =>
        item.filename.toLowerCase().contains(_searchQuery.toLowerCase())).toList();
    final filteredImages = _imageItems.where((item) =>
        item.filename.toLowerCase().contains(_searchQuery.toLowerCase())).toList();

    return Scaffold(
      appBar: AppBar(
        title: const Text(
          '星次元下载器',
          style: TextStyle(fontWeight: FontWeight.bold, fontSize: 18),
        ),
        backgroundColor: const Color(0xFF13131A),
        elevation: 0,
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh, color: Colors.white70),
            tooltip: '重新解析日志',
            onPressed: _isDownloading ? null : () => _parseLogFile(),
          ),
        ],
      ),
      body: SingleChildScrollView(
        child: Padding(
          padding: const EdgeInsets.all(12.0),
          child: Column(
            children: [
              // 路径设置折叠条
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    "路径参数配置",
                    style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13, color: Colors.white70),
                  ),
                  TextButton.icon(
                    onPressed: () {
                      setState(() {
                        _isSettingsExpanded = !_isSettingsExpanded;
                      });
                    },
                    icon: Icon(
                      _isSettingsExpanded ? Icons.keyboard_arrow_up : Icons.keyboard_arrow_down,
                      size: 16,
                    ),
                    label: Text(
                      _isSettingsExpanded ? "收起配置" : "展开配置",
                      style: const TextStyle(fontSize: 12),
                    ),
                  ),
                ],
              ),
              if (_isSettingsExpanded) ...[
                _buildPathConfigCard(),
                const SizedBox(height: 10),
                _buildStorageTip(),
                const SizedBox(height: 12),
              ],
  
              // 搜索与快捷多选筛选
              _buildSearchAndFilters(),
              const SizedBox(height: 10),
  
              // 资源展示列表
              SizedBox(
                height: 380,
                child: _buildResourceList(filteredMedia, filteredImages),
              ),
              const SizedBox(height: 12),
  
              // 底部下载状态与控制台日志
              _buildConsoleAndProgressPanel(),
            ],
          ),
        ),
      ),
    );
  }

  /// 1. 路径设置面板组件
  Widget _buildPathConfigCard() {
    return Card(
      color: const Color(0xFF1E1E26),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(12.0),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // app.log 输入与操作
            const Text(
              '星次元日志路径:',
              style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13, color: Colors.white70),
            ),
            const SizedBox(height: 6),
            Container(
              decoration: BoxDecoration(
                color: const Color(0xFF13131A),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: const Color(0xFF2E2E38)),
              ),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              child: TextField(
                controller: _logController,
                maxLines: null, // 允许自动换行以显示完整路径
                keyboardType: TextInputType.multiline,
                style: const TextStyle(fontSize: 12, fontFamily: 'monospace', height: 1.4),
                decoration: const InputDecoration(
                  border: InputBorder.none,
                  isDense: true,
                  contentPadding: EdgeInsets.zero,
                ),
                onChanged: (val) {
                  _parseLogFile(silent: true);
                },
              ),
            ),
            const SizedBox(height: 6),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: _isDownloading ? null : _resetLogPath,
                  style: TextButton.styleFrom(
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    minimumSize: const Size(0, 36),
                  ),
                  child: const Text('重置', style: TextStyle(fontSize: 13)),
                ),
                const SizedBox(width: 8),
                ElevatedButton(
                  onPressed: _isDownloading ? null : _pickLogFile,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF2E2E38),
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    minimumSize: const Size(0, 36),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  ),
                  child: const Text('浏览日志', style: TextStyle(fontSize: 13)),
                ),
              ],
            ),
            const SizedBox(height: 10),
            
            // 输出目录选择
            const Text(
              '输出保存目录:',
              style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13, color: Colors.white70),
            ),
            const SizedBox(height: 6),
            Container(
              decoration: BoxDecoration(
                color: const Color(0xFF13131A),
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: const Color(0xFF2E2E38)),
              ),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              child: TextField(
                controller: _outController,
                maxLines: null, // 允许自动换行以显示完整路径
                keyboardType: TextInputType.multiline,
                style: const TextStyle(fontSize: 12, fontFamily: 'monospace', height: 1.4),
                decoration: const InputDecoration(
                  border: InputBorder.none,
                  isDense: true,
                  contentPadding: EdgeInsets.zero,
                ),
              ),
            ),
            const SizedBox(height: 6),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                ElevatedButton(
                  onPressed: _isDownloading ? null : _pickOutputDir,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFF2E2E38),
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    minimumSize: const Size(0, 36),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                  ),
                  child: const Text('选择保存目录', style: TextStyle(fontSize: 13)),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  /// 2. 分区存储提示组件
  Widget _buildStorageTip() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: const Color(0xFF231C12),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFF4C3615)),
      ),
      child: const Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.info_outline, color: Colors.orangeAccent, size: 18),
          SizedBox(width: 10),
          Expanded(
            child: Text(
              "提示：受 Android 11+ 系统限制直接访问 Android/data 目录影响，若上方默认路径解析报错，请使用手机系统文件管理器（或MT管理器）将该 app.log 复制到共享目录（如 Download）后再点击“浏览”进行选择解析即可。",
              style: TextStyle(color: Color(0xFFFFD180), fontSize: 11, height: 1.4),
            ),
          ),
        ],
      ),
    );
  }

  /// 3. 搜索与多选过滤芯片组件
  Widget _buildSearchAndFilters() {
    final int totalCount = _mediaItems.length + _imageItems.length;
    final int selectedCount = _selectedItems.length;

    return Column(
      children: [
        // 搜索框
        Row(
          children: [
            Expanded(
              child: Container(
                height: 40,
                decoration: BoxDecoration(
                  color: const Color(0xFF1E1E26),
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: const Color(0xFF2E2E38)),
                ),
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Row(
                  children: [
                    const Icon(Icons.search, color: Colors.white38, size: 18),
                    const SizedBox(width: 8),
                    Expanded(
                      child: TextField(
                        style: const TextStyle(fontSize: 13),
                        decoration: const InputDecoration(
                          hintText: '搜索文件名...',
                          hintStyle: TextStyle(color: Colors.white38),
                          border: InputBorder.none,
                          isDense: true,
                          contentPadding: EdgeInsets.zero,
                        ),
                        onChanged: (val) {
                          setState(() {
                            _searchQuery = val;
                          });
                        },
                      ),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(width: 10),
            // 已选中状态计数
            Text(
              '已选 $selectedCount / $totalCount 项',
              style: const TextStyle(fontSize: 12, fontWeight: FontWeight.bold, color: Color(0xFF6366F1)),
            ),
          ],
        ),
        const SizedBox(height: 8),

        // 选择操作按钮
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              _buildFilterActionChip('全选', () => _selectAction('all')),
              const SizedBox(width: 6),
              _buildFilterActionChip('全不选', () => _selectAction('none')),
              const SizedBox(width: 6),
              _buildFilterActionChip('仅选音视频', () => _selectAction('media')),
              const SizedBox(width: 6),
              _buildFilterActionChip('仅选图片', () => _selectAction('image')),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildFilterActionChip(String label, VoidCallback onTap) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: const Color(0xFF2E2E38),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Text(
          label,
          style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w600, color: Colors.white70),
        ),
      ),
    );
  }

  /// 4. 资源展示树状/卡片列表组件
  Widget _buildResourceList(List<DownloadItem> mediaList, List<DownloadItem> imageList) {
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFF1E1E26),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          TabBar(
            controller: _tabController,
            indicatorColor: const Color(0xFF6366F1),
            labelColor: const Color(0xFF6366F1),
            unselectedLabelColor: Colors.white60,
            indicatorSize: TabBarIndicatorSize.tab,
            dividerColor: const Color(0xFF2E2E38),
            tabs: [
              Tab(text: '音视频 (${mediaList.length})'),
              Tab(text: '图片 (${imageList.length})'),
            ],
          ),
          Expanded(
            child: TabBarView(
              controller: _tabController,
              children: [
                _buildListTabContent(mediaList, isMedia: true),
                _buildListTabContent(imageList, isMedia: false),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildListTabContent(List<DownloadItem> items, {required bool isMedia}) {
    if (items.isEmpty) {
      return const Center(
        child: Text(
          '无匹配的资源',
          style: TextStyle(color: Colors.white30),
        ),
      );
    }

    return ListView.builder(
      itemCount: items.length,
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemBuilder: (context, idx) {
        final item = items[idx];
        final isSelected = _selectedItems.contains(item);

        IconData fileIcon = Icons.image_rounded;
        Color iconColor = Colors.cyan;
        if (isMedia) {
          final isAudio = item.filename.endsWith(".m4a") ||
              item.filename.contains("chapter") ||
              item.filename.contains("audio") ||
              item.filename.contains("story");
          fileIcon = isAudio ? Icons.music_note_rounded : Icons.play_circle_filled_rounded;
          iconColor = isAudio ? Colors.orangeAccent : Colors.redAccent;
        }

        return Container(
          margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
          decoration: BoxDecoration(
            color: const Color(0xFF13131A),
            borderRadius: BorderRadius.circular(10),
            border: Border.all(
              color: isSelected ? const Color(0x7F6366F1) : const Color(0xFF2E2E38),
              width: isSelected ? 1.5 : 1,
            ),
          ),
          child: ListTile(
            dense: true,
            leading: Icon(fileIcon, color: iconColor, size: 24),
            title: Text(
              item.filename,
              style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            subtitle: Text(
              item.url,
              style: const TextStyle(color: Colors.white38, fontSize: 10, fontFamily: 'monospace'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                // 分享发送按钮
                IconButton(
                  icon: const Icon(Icons.share, size: 18, color: Colors.white54),
                  tooltip: '分享/发送已下载文件',
                  onPressed: () => _shareFile(item),
                ),
                Checkbox(
                  value: isSelected,
                  activeColor: const Color(0xFF6366F1),
                  onChanged: (bool? val) {
                    setState(() {
                      if (val == true) {
                        _selectedItems.add(item);
                      } else {
                        _selectedItems.remove(item);
                      }
                    });
                  },
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  /// 5. 底部控制台与下载进度看板
  Widget _buildConsoleAndProgressPanel() {
    return Card(
      color: const Color(0xFF1E1E26),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      child: Padding(
        padding: const EdgeInsets.all(12.0),
        child: Column(
          children: [
            // 进度信息
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  _progressLabel,
                  style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold, color: Colors.white70),
                ),
                if (_isDownloading)
                  const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2, color: Color(0xFF6366F1)),
                  ),
              ],
            ),
            const SizedBox(height: 6),
            
            // 渐变进度条
            ClipRRect(
              borderRadius: BorderRadius.circular(4),
              child: LinearProgressIndicator(
                value: _progressValue,
                minHeight: 8,
                backgroundColor: const Color(0xFF13131A),
                valueColor: const AlwaysStoppedAnimation<Color>(Color(0xFF6366F1)),
              ),
            ),
            const SizedBox(height: 10),

            // 控制台命令行式终端打印
            Container(
              height: 120,
              width: double.infinity,
              decoration: BoxDecoration(
                color: const Color(0xFF0C0D12),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: const Color(0xFF1E1E26)),
              ),
              padding: const EdgeInsets.all(10),
              child: ListView.builder(
                controller: _consoleScrollController,
                itemCount: _consoleLogs.length,
                itemBuilder: (context, idx) {
                  return Padding(
                    padding: const EdgeInsets.symmetric(vertical: 1.5),
                    child: Text(
                      _consoleLogs[idx],
                      style: const TextStyle(
                        color: Color(0xFF39FF14), // 精英绿控制台字体
                        fontFamily: 'monospace',
                        fontSize: 11,
                      ),
                    ),
                  );
                },
              ),
            ),
            const SizedBox(height: 12),

            // 操作控制行
            Row(
              children: [
                // “开始下载”大按钮，采用炫酷的 Indigo 渐变色
                Expanded(
                  child: Container(
                    height: 44,
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
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                      ),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Icon(Icons.play_arrow_rounded, color: Colors.white),
                          const SizedBox(width: 8),
                          Text(
                            _isDownloading ? '正在下载...' : '开始下载已选项',
                            style: const TextStyle(
                              color: Colors.white,
                              fontWeight: FontWeight.bold,
                              fontSize: 14,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                
                // “停止”红色按钮
                ElevatedButton(
                  onPressed: _isDownloading ? _stopDownload : null,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: const Color(0xFFEF4444),
                    disabledBackgroundColor: const Color(0xFF2E2E38),
                    foregroundColor: Colors.white,
                    padding: const EdgeInsets.symmetric(horizontal: 16),
                    minimumSize: const Size(0, 44),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
                  ),
                  child: const Row(
                    children: [
                      Icon(Icons.stop_rounded),
                      SizedBox(width: 4),
                      Text('停止', style: TextStyle(fontWeight: FontWeight.bold)),
                    ],
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
