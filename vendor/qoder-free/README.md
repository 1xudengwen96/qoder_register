# Qoder-Free

一个用于重置 Qoder 应用程序用户身份信息的现代化图形界面工具。

![Qoder-Free Interface](https://img.shields.io/badge/Platform-macOS-blue)
![Windows Support](https://img.shields.io/badge/Platform-Windows-blue)
![Python](https://img.shields.io/badge/Python-3.6+-green)
![PyQt5](https://img.shields.io/badge/GUI-PyQt5-orange)

**✨重要提示：重置了 Qoder 之后再注册请使用指纹浏览器，这样不会因为浏览器的缓存或指纹导致识别出重复注册。**

**另外，跟 Cursor 和 Augment 一样，后续检测会越来越严格，能用多久无法保证，且用且珍惜**

## 📋 目录

- [功能特性](#功能特性)
- [系统要求](#系统要求)
- [安装指南](#安装指南)
- [使用方法](#使用方法)
- [功能详解](#功能详解)
- [界面说明](#界面说明)
- [技术细节](#技术细节)
- [故障排除](#故障排除)
- [开发指南](#开发指南)
- [更新日志](#更新日志)

## ✨ 功能特性

- 💻 **重置机器ID** - 生成全新的机器标识符
- 📊 **重置遥测数据** - 重置应用程序的遥测和设备ID
- 🧹 **智能缓存清理** - 清理应用缓存但保护重要数据
- 🔥 **深度身份清理** - 清除所有网络状态、信任令牌和本地存储
- 🔐 **登录身份清理** - 专门清理认证令牌、登录状态和会话数据
- 🛡️ **硬件指纹重置** - 最强反检测功能，生成虚假硬件信息干扰检测
- 🚀 **超级深度清理** - 安全的系统级缓存清理、网络痕迹重置、浏览器指纹混淆
- 💬 **智能对话管理** - 可选择保留或清除聊天历史记录
- ⚡ **一键完整重置** - 包含所有9项重置功能的完整解决方案
- 🖥️ **现代化界面** - 基于PyQt5的美观图形界面
- 📝 **实时日志** - 详细的操作日志和状态监控
- 🔍 **状态检测** - 自动检测Qoder运行状态和数据完整性
- 🛡️ **安全保护** - 操作前确认，防止误操作
- 🌍 **跨平台支持** - 支持macOS、Windows、Linux三大操作系统
- 🌐 **多语言界面** - 支持中文/英文/俄文/葡语四种语言切换

### 🔍 深度身份识别清理

**为什么需要深度清理？**
基础重置工具只处理了基本的身份文件，但遗漏了以下关键身份识别信息：

#### 1. 核心身份标识符
- **machineid** - 机器唯一标识符
- **telemetry.machineId** - 遥测机器ID（SHA256哈希）
- **telemetry.devDeviceId** - 设备标识符  
- **telemetry.sqmId** - 软件质量度量ID

#### 2. 网络层身份信息（最关键）
- **Network Persistent State** - 网络服务器连接历史和网络指纹
- **TransportSecurity** - HSTS等传输安全状态记录
- **Trust Tokens & Trust Tokens-journal** - 信任令牌数据库
- **SharedStorage & SharedStorage-wal** - 共享存储数据库

#### 3. 本地存储身份信息
- **Local Storage/leveldb/** - 本地存储LevelDB数据库
- **Session Storage** - 会话存储数据
- **WebStorage** - Web存储数据
- **Shared Dictionary** - 共享字典数据

#### 4. 系统级身份信息
- **SharedClientCache/.info** - 语言服务器连接信息（端口/PID）
- **SharedClientCache/.lock** - 进程锁文件
- **SharedClientCache/mcp.json** - MCP配置文件
- **SharedClientCache/index/** - 索引数据目录
- **SharedClientCache/cache/** - 共享缓存数据

#### 5. 设备指纹和配置
- **Preferences** - 用户偏好设置（可能包含设备指纹）
- **Local State** - Chromium本地状态（包含加密密钥）
- **code.lock** - 代码锁文件
- **languagepacks.json** - 语言包配置

#### 6. 硬件指纹重置
- **硬件标识符重置** - cpu_id, gpu_id, memory_id, board_serial, bios_uuid
- **虚假硬件信息生成** - 根据系统类型生成真实硬件配置
  - **macOS**: Apple M2-M5 Pro 芯片、LPDDR5 内存、macOS 12.x-15.x 版本
  - **Windows**: Intel/AMD 处理器、NVIDIA/AMD 显卡、Windows 10/11 版本
  - **Linux**: 通用硬件配置、Linux 5.x/6.x 内核版本
- **混淆检测文件** - 创建多个虚假硬件信息文件干扰检测
- **系统信息重置** - 重置系统版本、架构信息、时区等系统指纹

#### 7. 登录身份清理
- **认证令牌清理** - 清除所有登录状态和会话数据
- **nonce 和 challenge 数据** - 清除认证挑战相关数据
- **设备认证数据** - DeviceMetadata, HardwareInfo, AutofillStrikeDatabase
- **用户配置清理** - 清除登录相关的用户偏好设置
- **SharedClientCache 登录状态** - 清理语言服务器连接信息和认证缓存



## �️ 系统要求

- **操作系统**: macOS 10.14+ 或 Windows 10+
- **Python**: 3.6 或更高版本
- **依赖库**: PyQt5
- **磁盘空间**: 至少 50MB 可用空间
- **权限**: 
  - macOS: 对 `~/Library/Application Support/Qoder` 的读写权限
  - Windows: 对 `%APPDATA%\Qoder` 的读写权限

## 📦 安装指南 | Installation Guide

### 方法一：直接运行（推荐）| Direct Execution (Recommended)

1. **克隆或下载项目** | **Clone or Download Project**
   ```bash
   git clone <repository-url>
   cd qoder-free
   ```

2. **安装依赖** | **Install Dependencies**
   ```bash
   pip3 install PyQt5
   ```

3. **运行程序** | **Run the Program**
   ```bash
   python3 qoder_reset_gui.py
   ```

### 方法二：使用启动脚本 | Using Launch Scripts

**macOS/Linux:**
```bash
chmod +x start_gui.sh
./start_gui.sh
```

**Windows:**
```cmd
start_gui.bat
```



## 🚀 使用方法 | Usage Guide

### 基本使用流程

1. **启动应用**
   - 双击运行 `qoder_reset_gui.py`
   - 或使用终端命令启动

2. **查看状态**
   - 程序启动后会自动检测当前状态
   - 查看日志区域了解系统状态

3. **选择操作**
   - 勾选"保留对话记录"（默认勾选）
   - 选择具体的重置操作

4. **执行重置**
   - 点击相应按钮执行操作
   - 查看日志确认操作结果

### 快速开始 | Quick Start

```bash
# 1. 安装依赖 | Install Dependencies
pip3 install PyQt5

# 2. 运行程序 | Run the Program
python3 qoder_reset_gui.py

# 3. 在界面中点击"一键修改所有配置" | Click "One-Click Reset All Settings" in the interface
```

## 🔧 功能详解

### 主要功能按钮

#### 🔴 **关闭Qoder**
   - 检查Qoder进程运行状态
   - 提示用户手动关闭应用程序
   - 确保重置操作的安全性

#### 💻 **重置机器ID**
   - 生成新的UUID作为机器标识符
   - 创建多个备份ID文件（deviceid, hardware_uuid, system_uuid等）
   - 修改 `~/Library/Application Support/Qoder/machineid` 文件
   - 同步更新storage.json中的相关标识符
   - 让Qoder识别为全新设备

#### 📊 **重置遥测数据**
   - 重置 `telemetry.machineId` 和 `telemetry.devDeviceId`
   - 新增 sessionId、installationId、clientId、userId、anonymousId等标识符
   - 随机生成系统版本、架构信息、时区等系统指纹
   - 修改 `storage.json` 文件中的遥测配置
   - 清除设备追踪信息

#### 🔥 **深度身份清理**
   - 清除所有网络状态和 Cookie
   - 清除所有本地存储数据
   - 清除 SharedClientCache 内部文件
   - 清除系统级别身份文件
   - 清除崩溃报告和缓存数据
   - **强制不保留对话**，最彻底的身份重置

#### 🔐 **登录身份清理**
   - 专门清理登录相关身份信息
   - 清理 SharedClientCache 中的登录状态文件
   - 清理认证令牌和会话数据
   - 清理 nonce 和 challenge 相关数据
   - 清理设备指纹和认证数据
   - 重置用户偏好中的登录相关配置

#### 🛡️ **硬件指纹重置** （最强反检测）
   - 重置所有可能的硬件标识符（cpu_id, gpu_id, memory_id等）
   - **根据系统类型生成虚假硬件信息**：
     - macOS: Apple M2-M5 Pro 芯片配置
     - Windows: Intel/AMD 处理器 + NVIDIA/AMD 显卡
     - Linux: 通用硬件配置
   - 清理硬件指纹相关文件和缓存
   - 创建多个虚假硬件信息文件干扰检测
   - 重置系统版本、架构、时区等系统指纹
   - **建议重启系统后再使用 Qoder**

#### ⚡ **一键修改所有配置** （推荐使用）
   - **包含所有8项功能的完整重置方案**：
     1. 重置机器ID
     2. 重置遥测数据
     3. 清理缓存数据
     4. 清理身份识别文件
     5. 高级身份清理
     6. 登录身份清理
     7. 硬件指纹重置
     8. 智能对话管理
   - 根据"保留对话记录"选项处理聊天数据
   - **最全面的反检测重置方案**

### 对话记录管理

- **保留对话记录**（默认勾选）
  - 完全保留：
    - `User/workspaceStorage/.../chatSessions/*.json` - 对话内容文件
    - `User/workspaceStorage/.../chatEditingSessions/` - 编辑会话状态
    - `User/settings.json` - 用户设置
  - 智能保留：
    - `Local Storage/leveldb/` - 本地存储数据库（可能包含对话索引）
    - `SharedClientCache/index/` - 选择性保留对话相关索引
  - 有条件清理：
    - `Session Storage` - 清理（可能包含身份信息）
    - `WebStorage` - 清理（可能包含身份信息）
    - `Shared Dictionary` - 清理

- **清除对话记录**（取消勾选）
  - 删除所有对话会话数据
  - 清除聊天编辑会话
  - 清理所有存储目录（包括 Local Storage）
  - 清理所有索引数据
  - 移除历史记录
  - 清理会话存储

## 🖼️ 界面说明

### 界面截图

![Qoder-Free GUI Screenshot](screenshot.webp)

## 🔧 技术细节

### 文件结构

```
qoder-free/
├── qoder_reset_gui.py          # 主程序文件（PyQt5界面，跨平台支持）
├── start_gui.sh               # macOS/Linux启动脚本
├── start_gui.bat              # Windows启动脚本
└── README.md                  # 完整说明文档
```

### 核心技术

- **GUI框架**: PyQt5 - 现代化跨平台GUI框架
- **进程检测**: 使用 `pgrep` 命令检测Qoder运行状态
- **文件操作**: Python pathlib 进行安全的文件路径操作
- **JSON处理**: 标准库json模块处理配置文件
- **UUID生成**: 使用uuid4()生成随机标识符
- **哈希计算**: SHA256算法生成遥测机器ID

### 操作的文件和目录

```
~/Library/Application Support/Qoder/  (或 Windows: %APPDATA%\Qoder\)
├── machineid                               # 机器ID文件
├── deviceid                                # 设备ID文件（新增）
├── hardware_uuid                           # 硬件UUID文件（新增）
├── system_uuid                             # 系统UUID文件（新增）
├── platform_id                             # 平台ID文件（新增）
├── installation_id                         # 安装ID文件（新增）
├── cpu_id                                  # CPU ID文件（硬件指纹）
├── gpu_id                                  # GPU ID文件（硬件指纹）
├── memory_id                               # 内存ID文件（硬件指纹）
├── board_serial                            # 主板序列号（硬件指纹）
├── bios_uuid                               # BIOS UUID（硬件指纹）
├── Network Persistent State                # 网络连接历史和指纹（关键）
├── TransportSecurity                       # HSTS等安全策略记录
├── Trust Tokens                           # 信任令牌数据库
├── Trust Tokens-journal                   # 信任令牌日志
├── SharedStorage                           # 共享存储数据库
├── SharedStorage-wal                       # 共享存储WAL文件
├── Preferences                             # 用户偏好设置
├── Secure Preferences                      # 安全偏好设置（新增）
├── Local State                             # Chromium本地状态
├── code.lock                               # 代码锁文件
├── languagepacks.json                      # 语言包配置
├── *.sock                                  # Socket文件
├── DeviceMetadata                          # 设备元数据（登录身份清理）
├── HardwareInfo                            # 硬件信息（登录身份清理）
├── SystemInfo                              # 系统信息（登录身份清理）
├── AutofillStrikeDatabase                  # 自动填充数据库（登录身份清理）
├── AutofillStrikeDatabase-journal          # 自动填充数据库日志
├── Feature Engagement Tracker             # 功能参与追踪器（登录身份清理）
├── Platform Notifications                  # 平台通知（新增）
├── VideoDecodeStats                        # 视频解码统计（新增）
├── OriginTrials                            # 源试验（新增）
├── BrowserMetrics                          # 浏览器指标（新增）
├── SafeBrowsing                            # 安全浏览（新增）
├── QuotaManager                            # 配额管理器（新增）
├── QuotaManager-journal                    # 配额管理器日志
├── Network Action Predictor                # 网络动作预测器（新增）
├── hardware_detection.json                # 虚假硬件检测文件（硬件指纹重置）
├── device_capabilities.json               # 虚假设备能力文件（硬件指纹重置）
├── system_features.json                   # 虚假系统特性文件（硬件指纹重置）
├── User/
│   ├── globalStorage/storage.json         # 遥测数据配置（增强版）
│   │                                      # 包含 sessionId, clientId, hardwareId 等
│   ├── settings.json                      # 用户设置（保留）
│   └── workspaceStorage/                  # 工作区存储（包含对话记录）
│       ├── */chatSessions/            # 对话会话文件（保留）
│       └── */chatEditingSessions/     # 编辑会话状态（保留）
├── SharedClientCache/                      # 共享客户端缓存
│   ├── .info                              # 语言服务器信息（端口/PID）
│   ├── .lock                              # 进程锁文件
│   ├── mcp.json                           # MCP配置文件
│   ├── index/                             # 索引数据目录（选择性保留）
│   └── cache/                             # 共享缓存数据
├── Local Storage/leveldb/                  # 本地存储LevelDB（保留对话时保留）
├── Session Storage/                        # 会话存储（可能包含身份信息）
├── WebStorage/                             # Web存储（可能包含身份信息）
├── Shared Dictionary/                      # 共享字典
├── Cache/                                  # 应用缓存
├── Code Cache/                             # 代码缓存
├── GPUCache/                               # GPU缓存
├── DawnGraphiteCache/                      # Dawn图形缓存
├── DawnWebGPUCache/                        # Dawn WebGPU缓存
├── ShaderCache/                            # 着色器缓存（新增）
├── DawnCache/                              # Dawn缓存（新增）
├── MediaCache/                             # 媒体缓存（硬件指纹）
├── Dictionaries/                           # 字典缓存（新增）
├── CachedData/                             # 缓存数据
├── CachedProfilesData/                     # 缓存配置数据
├── CachedExtensions/                       # 缓存扩展（新增）
├── IndexedDB/                              # IndexedDB数据库（新增）
├── CacheStorage/                           # 缓存存储（新增）
├── WebSQL/                                 # WebSQL数据库（新增）
├── Crashpad/                               # 崩溃报告目录
├── Service Worker/                         # 服务工作线程数据
├── Certificate Revocation Lists/           # 证书撤销列表（新增）
├── SSLCertificates/                        # SSL证书缓存（新增）
├── databases/                              # 数据库目录
├── logs/                                   # 日志文件
├── Backups/                                # 备份文件
└── clp/                                    # 剪贴板数据
```

### 📊 重置操作说明

#### ✅ **新增文件（v2.3.0）**:
- **硬件标识符**: cpu_id, gpu_id, memory_id, board_serial, bios_uuid
- **虚假硬件信息**: hardware_detection.json, device_capabilities.json, system_features.json
- **身份文件**: Secure Preferences, DeviceMetadata, HardwareInfo, SystemInfo
- **缓存扩展**: ShaderCache, DawnCache, MediaCache, CachedExtensions
- **网络文件**: Certificate Revocation Lists, SSLCertificates

#### 🔄 **storage.json 增强配置**:
```json
{
  "telemetry.machineId": "SHA256哈希值",
  "telemetry.devDeviceId": "UUID",
  "telemetry.sqmId": "UUID",
  "telemetry.sessionId": "UUID",
  "telemetry.installationId": "UUID", 
  "telemetry.clientId": "UUID",
  "telemetry.userId": "UUID",
  "telemetry.anonymousId": "UUID",
  "hardwareId": "UUID",
  "platformId": "UUID",
  "cpuId": "UUID",
  "gpuId": "UUID",
  "memoryId": "UUID",
  "system.platform": "系统类型",
  "system.arch": "系统架构",
  "system.version": "系统版本",
  "system.build": "系统版本号",
  "system.locale": "en-US",
  "system.timezone": "随机时区"
}
```

## 🚨 故障排除

### 常见问题

#### 1. PyQt5安装失败
```bash
# 如果pip安装失败，尝试使用conda
conda install pyqt

# 或者使用homebrew安装Python和PyQt5
brew install python-tk
pip3 install PyQt5
```

#### 2. 权限不足错误
```bash
# 确保对Qoder目录有读写权限
ls -la ~/Library/Application\ Support/Qoder/

# 如果权限不足，可以尝试修复权限
chmod -R u+rw ~/Library/Application\ Support/Qoder/
```

#### 3. Qoder目录不存在
- 确保已安装Qoder应用程序
- 至少运行过一次Qoder以创建配置目录
- 检查Qoder是否安装在正确位置

#### 4. 界面文字不可见
- 程序已自动处理macOS深色模式兼容性
- 如果仍有问题，尝试切换系统主题
- 重启应用程序

#### 5. 对话框按钮不可见
- 程序已设置全局样式确保按钮可见
- 如果问题持续，请检查系统PyQt5版本

### 日志分析

程序启动时会显示详细的状态检查信息：

```
[时间] Qoder-Free 重置工具已启动
[时间] ================================================
[时间] 1. 检查Qoder进程状态...
[时间]    ✅ Qoder未运行
[时间] 2. 检查Qoder目录...
[时间]    ✅ Qoder目录存在
[时间] 3. 检查机器ID文件...
[时间]    ✅ 机器ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
[时间] 4. 检查遥测数据文件...
[时间]    ✅ 遥测机器ID: xxxxxxxxxxxxxxxx...
[时间]    ✅ 设备ID: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
[时间] 5. 检查缓存目录...
[时间]    ✅ 发现 7/7 个缓存目录
[时间] 6. 检查对话记录...
[时间]    ✅ 发现 4/4 个对话相关目录
[时间] ================================================
[时间] 状态检查完成，可以开始操作
```

## 👨‍💻 开发指南

### 代码结构

```python
class QoderResetGUI(QMainWindow):
    def __init__(self):
        # 初始化界面和多语言支持
        # 设置当前语言为中文
        # 初始化多语言翻译字典和用户界面

    def init_translations(self):
        # 初始化多语言字典（支持中文/英文/俄文/葡语）
        # 包含所有UI文本、日志消息和对话框文本的翻译
        
    def init_ui(self):
        # 创建现代化PyQt5界面元素
        # 设置窗口尺寸、样式和布局
        # 创建所有8个功能按钮和对应的事件处理

    def change_language(self, language_text):
        # 动态语言切换功能
        # 支持中文、英文、俄文、葡语四种语言
        # 实时更新所有UI文本

    def initialize_status_check(self):
        # 增强的启动时状态检查（新增11项检查）
        # 检查Qoder进程、目录、朼器ID、遥测数据、缓存、对话记录等

    def check_qoder_running(self):
        # 跨平台进程检测（macOS/Windows/Linux）
        # 使用pgrep命令检测Qoder进程状态

    def generate_system_version(self, system_type):
        # 根据系统类型生成合适的系统版本号（新增）
        # macOS: 12.x.x-15.x.x, Windows: 10.0.x, Linux: 5.x.x/6.x.x

    # ========== 基础功能按钮 ==========
    def close_qoder(self):
        # 关闭Qoder功能
        # 检查进程状态并提示用户手动关闭

    def reset_machine_id(self):
        # 重置机器ID（增强版）
        # 生成新UUID并创建多个备份ID文件
        # 同步更新storage.json中的相关标识符

    def reset_telemetry(self):
        # 增强的遥测数据重置（新增sqmId和多个标识符）
        # 重置所有遥测相关标识符，包括sessionId、clientId等

    # ========== 高级功能按钮 ==========
    def deep_identity_cleanup(self):
        # 深度身份清理功能（新增）
        # 强制不保留对话，最彻底的身份重置
        
    def login_identity_cleanup(self):
        # 登录身份清理功能（新增）
        # 专门清理登录相关身份信息和认证数据

    def hardware_fingerprint_reset(self):
        # 硬件指纹重置功能（新增 - 最强反检测）
        # 重置所有硬件标识符并生成虚假硬件信息干扰检测

    def one_click_reset(self):
        # 一键重置功能（增强版）
        # 集成所有8项功能的完整重置方案

    # ========== 核心实现函数 ==========
    def perform_full_reset(self, preserve_chat=True):
        # 执行完整重置（增强版）
        # 1. 重置机器ID（增强版）
        # 2. 重置遥测数据（增强版）
        # 3. 清理缓存数据（增强版）
        # 4. 清理身份识别文件（增强版）
        # 5. 高级身份清理
        # 6. 登录身份清理（新增）
        # 7. 硬件指纹重置（新增）
        # 8. 智能对话管理

    def perform_advanced_identity_cleanup(self, qoder_support_dir, preserve_chat=False):
        # 高级身份清理（新增）
        # 清理SharedClientCache内部文件、系统级身份文件
        # 清理崩溃报告、socket文件、设备指纹文件等
        
    def perform_login_identity_cleanup(self, qoder_support_dir):
        # 登录相关身份清理（新增）
        # 清理SharedClientCache中的登录状态文件
        # 清理认证令牌、nonce、challenge数据
        # 清理设备指纹和认证数据

    def perform_hardware_fingerprint_reset(self, qoder_support_dir):
        # 执行硬件指纹重置的具体实现（新增）
        # 1. 重置所有硬件标识符（cpu_id, gpu_id, memory_id等）
        # 2. 重置storage.json中的硬件标识符
        # 3. 清理硬件指纹相关文件
        # 4. 清理硬件相关缓存
        # 5. 创建虚假硬件信息文件（根据系统类型适配）

    def clear_chat_history(self, qoder_support_dir):
        # 清除对话记录（增强版）
        # 清除工作区中的对话会话和编辑会话
        # 清除用户配置中的对话相关配置
        
    # ========== 工具函数 ==========
    def get_qoder_data_dir(self):
        # 跨平台数据目录获取（新增）
        # 支持macOS、Windows、Linux三大操作系统
    
    def log(self, message):
        # 日志输出函数（增强版）
        # 包含时间戳、自动滚动和多语言支持
        
    def tr(self, key):
        # 翻译函数，根据当前语言返回对应文本
        
    def update_ui_text(self):
        # 更新UI文本，用于语言切换时实时更新界面

# ========== 全局工具函数 ==========
def main():
    # 主函数，创建QApplication并启动GUI应用
    # 包含错误处理和程序退出逻辑
```

### 测试

建议的测试步骤：

1. **启动测试**: 运行程序确保界面正常显示
2. **功能测试**: 逐一测试各个按钮功能
3. **状态检查**: 验证初始状态检查是否正确
4. **重置测试**: 在测试环境中验证重置功能
5. **日志验证**: 确认操作日志显示正确

## 📝 更新日志

### v2.3.0 - 完整反检测版本 (最新版本)
- ⚡ **一键完整重置** - 集成所有8项功能的终极解决方案
- 🛡️ **硬件指纹重置** - 最强反检测功能，生成虚假硬件信息干扰检测
- 🔐 **登录身份清理** - 专门清理认证令牌、登录状态和会话数据
- 🌍 **系统智能适配** - 根据操作系统类型生成相应硬件配置
- 🔍 **增强日志输出** - 显示检测到的系统类型，便于调试和验证
- 📊 **遥测数据增强** - 新增 sessionId、installationId、clientId等多个标识符
- 🛡️ **身份文件扩展** - 新增 15+ 个身份文件的清理，包括硬件指纹相关文件
- 🧹 **缓存清理增强** - 新增 ShaderCache、DawnCache、CachedExtensions等缓存目录

### v2.2.0 - 智能跨平台版本
- 🌍 **跨平台支持** - 新增 Windows 和 Linux 支持
- 🔄 **智能对话保留** - 重新设计对话记录保留机制
- ✨ **多语言界面** - 支持中文/英文/俄文/葡语切换
- 🔧 **Windows 启动脚本** - 新增 start_gui.bat 自动环境检查
- 📊 **增强遥测重置** - 新增 telemetry.sqmId 重置
- 🔍 **精确身份清理** - 基于实际目录分析的清理策略
- 🧠 **智能索引保留** - 保留对话时选择性保留索引数据
- 🛡️ **安全增强** - 更精细的身份信息识别和清理

### v2.1.0 - 身份识别修复版本
- ✨ 新增深度身份清理功能
- 🔧 修复遗漏的关键身份识别文件
- 📈 增强状态检查功能
- 🎨 改进界面布局（2x2 按钮布局）
- 📋 完善日志输出和用户反馈
- 🔍 清除 Network Persistent State、Cookies、SharedStorage 等身份文件
- 🛠️ 清理 SharedClientCache 内部关键文件（.info, mcp.json 等）
- 🧹 清除崩溃报告、Socket 文件等其他身份信息

### v2.0.0
- ✨ 全新PyQt5界面，替代tkinter
- 🔧 修复对话框文字不可见问题
- 📝 添加自动滚动日志显示
- 💬 精确的对话记录保护机制
- 🔍 启动时自动状态检查
- 🎨 现代化界面设计，符合原型图要求

### v1.x.x (历史版本)
- 基于tkinter的图形界面
- 基本的重置功能
- 简单的日志显示

## ⚠️ 注意事项

1. **使用前备份**: 建议在使用前备份重要的Qoder配置和数据
2. **关闭应用**: 执行重置操作前必须完全关闭Qoder应用程序
3. **重新登录**: 重置后需要重新登录Qoder账户
4. **数据丢失**: 取消勾选"保留对话记录"将永久删除所有聊天历史
- ✅ **系统兼容**: 目前支持 macOS 和 Windows 系统

## 📄 许可证

本项目仅供学习和研究使用。请遵守相关法律法规和软件使用协议。

## 🤝 贡献

欢迎提交Issue和Pull Request来改进这个项目。

## 📞 支持

如果遇到问题，请：
1. 查看故障排除部分
2. 检查日志输出
3. 提交Issue描述问题

## 📱 现代化界面功能

### 🎨 设计特色
- **紫色横幅标题** - 现代化的顶部设计
- **圆角按钮** - 美观的现代化按钮设计
- **渐变色彩** - 专业的配色方案
- **响应式布局** - 适配不同窗口大小

### 🔧 主要功能
- 🔴 **检查 Qoder 状态** - 一键检查应用程序运行状态
- 🔍 **预览重置操作** - 安全的试运行模式
- ⚡ **执行重置操作** - 一键完成所有重置步骤

### ⚙️ 智能选项
- 💾 **自动备份** - 重置前创建完整备份
- 💬 **会话保留** - 可选择保留对话记录
- 📝 **详细日志** - 实时显示操作进度和结果



## 📋 重置内容

### 会被重置的内容
- ✅ **核心身份标识**
  - 机器ID (`machineid`)
  - 遥测机器ID (`telemetry.machineId`)
  - 设备ID (`telemetry.devDeviceId`) 
  - 软件质量度量ID (`telemetry.sqmId`)
- ✅ **网络身份指纹**
  - 网络持久状态 (`Network Persistent State`)
  - 传输安全记录 (`TransportSecurity`)
  - 信任令牌 (`Trust Tokens`)
  - 共享存储 (`SharedStorage`)
- ✅ **系统级文件**
  - 语言服务器信息 (`SharedClientCache/.info`, `mcp.json`)
  - 用户偏好设置 (`Preferences`)
  - Chromium本地状态 (`Local State`)
- ✅ **缓存目录**
  - Cache, GPUCache, DawnGraphiteCache, DawnWebGPUCache
  - Code Cache, SharedClientCache/cache
  - CachedData, CachedProfilesData

### 智能保留的内容（取决于“保留对话记录”设置）
- 🔄 **对话数据** - 对话内容和编辑会话（默认保留）
- 🔄 **本地存储** - Local Storage/leveldb（可能包含对话索引）
- 🔄 **索引数据** - SharedClientCache/index（选择性保留）

### 始终保留的内容
- ✅ 用户设置 (`User/settings.json`)
- ✅ 工作区配置 (`User/workspaceStorage/*/workspace.json`)
- ✅ 代码片段和扩展数据

## 🔍 故障排除

### 常见问题

1. **GUI窗口不显示**
   - 确保已安装PyQt5：`pip3 install PyQt5`
   - 尝试使用启动脚本：`./start_gui.sh`
   - 检查终端是否有错误信息

2. **"Qoder 正在运行"**
   - 完全关闭 Qoder 应用程序
   - 使用 Cmd+Q 或从菜单选择退出

3. **"未找到 python3"**
   - 安装 Python 3: https://www.python.org
   - 或使用 Homebrew: `brew install python3`

4. **"tkinter 不可用"**
   - 安装 python-tk: `brew install python-tk`

5. **重置后仍被识别为老用户**
   - **推荐方案**：使用“一键修改所有配置”（现已包含所有9项功能）
   - **逐步排查**：
     - 先使用“硬件指纹重置”获得最强反检测效果
     - 再使用“深度身份清理”清除所有遗漏的身份识别信息
     - 最后使用“登录身份清理”清除认证状态
     - 新增：使用“超级深度清理”功能进行系统级别清理
   - **验证清理效果**：检查日志确认所有身份文件都被清理：
     - `Network Persistent State` - 网络连接历史
     - `SharedStorage` - 共享存储数据库  
     - `Trust Tokens` - 信任令牌
     - `telemetry.*` - 所有遥测标识符
     - `hardwareId, cpu_id, gpu_id` - 硬件指纹
     - 新增：系统级缓存和网络痕迹
   - **取消勾选**“保留对话记录”以获得最彻底的清理
   - **环境变更**：考虑更换网络环境或使用 VPN
   - **系统重启**：重启系统后再次测试（特别是使用硬件指纹重置后）
   - **新增建议**：使用指纹浏览器或虚拟机重新注册

6. **安全系统级清理问题**
   - **已完全解决**：所有系统级清理功能都已采用安全模式
   - **白名单保护**：只清理与 Qoder/VSCode/Electron 相关的文件
   - **路径验证**：严格限制清理范围，不会误删其他应用程序文件
   - **保护机制**：自动跳过重要系统文件和其他应用程序的缓存
   - **用户级操作**：所有清理都在用户权限级别，不需要管理员权限
   - **系统稳定性**：跳过 DNS 缓存等可能影响网络功能的操作
   - **系统不匹配**：检查日志中的“检测到系统类型”信息
   - **虚假信息异常**：确认生成的硬件信息与系统类型匹配
   - **清理系统级缓存**：
   ```bash
   # 清理系统级 DNS 缓存
   sudo dscacheutil -flushcache
   
   # 重置网络设置可能有助于清除网络指纹
   ```

7. **日志调试信息**
   - 程序现在会显示检测到的系统类型
   - 确认虚假硬件信息生成是否与实际系统匹配
   - 检查所有重置操作的日志输出是否正常

7. **macOS GUI显示问题**
   - 检查Dock中的Python图标并点击激活
   - 确保终端有足够权限运行GUI应用
   - 尝试在不同的终端应用中运行



## ⚠️ 重要提醒

- **使用前备份**: 推荐使用备份功能保护重要数据
- **关闭应用**: 确保 Qoder 完全关闭后再运行重置
- **合法使用**: 仅用于合法目的，遵守软件使用条款
- **会话保留**: 默认保留对话记录，如需清理可取消勾选

## 🎉 开始使用

### 推荐启动方式：

**macOS/Linux:**
```bash
# 使用启动脚本（推荐）
./start_gui.sh

# 或直接运行
python3 qoder_reset_gui.py
```

**Windows:**
```cmd
# 使用启动脚本（推荐）
start_gui.bat

# 或直接运行
python qoder_reset_gui.py
```

享受全新的 Qoder 使用体验！

---

**版本**: 2.2.0  
**开发**: Mac：[Mr.T](https://www.74110.net/recommendation/qoder-free/)  Windows：[MRLTR-CMD](https://github.com/MRLTR-CMD) 
**支持**: macOS、Windows
