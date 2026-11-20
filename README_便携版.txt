Qoder Register 便携版（Windows x64）
====================================

一、目标机器需要先装好（不在这包里）：
1. 比特浏览器客户端，登录好账号，打开客户端（默认本地 API 54345 端口）
2. Qoder 桌面客户端（https://qoder.com/download 装好，不用登录）
   - 若安装位置不是默认路径，运行后到前端面板「客户端路径」里指定

二、首次使用：
1. 双击 setup.bat —— 检测并静默安装 Python 3.12.10（约 1 分钟，只装一次）
2. 双击 start.bat —— 启动控制台，浏览器自动打开 http://127.0.0.1:8877
3. 控制台页面上可改邮箱清单、并发、代理等配置

三、自带内容说明：
- data/mail_list.txt  邮箱清单（格式：email----密码----clientId----refreshToken，一行一个）
- config/secrets.yaml 打码平台密钥（2Captcha/超级鹰）——包里含真实密钥，勿外传
- data/  运行时自动生成（数据库/日志/产物），换机不带旧数据即全新开始

四、常见问题：
- 提示「请先运行 setup.bat」→ Python 没装或不在 PATH，重跑 setup.bat
- 打不开控制台 → 检查比特浏览器是否在跑（http://127.0.0.1:54345）
- 中文乱码 → start.bat 已设 PYTHONIOENCODING=utf-8，用 cmd 而非老式终端
