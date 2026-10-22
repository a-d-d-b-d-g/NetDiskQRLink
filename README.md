# NetDiskQRLink · 网盘二维码 → 分享链接

把网盘分享二维码（图片 / 截图 / 摄像头）直接转成**可复制的分享链接**，提取码自动分离并拼进链接。
**全程本地离线：不联网、不上传图片、不登录任何网盘账号。**

> 适用场景：别人发来一张二维码截图，你要的是能直接点开的链接；或者一堆二维码图片需要批量转成链接。

---

## 下载

前往 [Releases](../../releases) 下载 `NetDiskQRLink.exe`，双击即用，免安装（Windows x64）。

> exe 未做代码签名，首次运行 SmartScreen 可能提示"未知发布者"：点「更多信息」→「仍要运行」。
> 若杀软误报，请将文件加入白名单。源码完全公开，可自行审阅或自行打包。

---

## 功能

| 能力 | 说明 |
| --- | --- |
| 截图粘贴 | `Win + Shift + S` 截二维码 → 窗口内 `Ctrl + V` |
| 自动监听 | 软件打开期间，截图完成瞬间自动识别，无需点按钮；关闭软件即停止监听（**无后台常驻进程**） |
| 拖拽 | 二维码图片直接拖进窗口 |
| 批量选文件 | 一次多选，批量解析 |
| 摄像头扫描 | 手机屏幕 / 纸质二维码对准摄像头即可 |
| 提取码分离 | `?pwd=ab12`、`提取码：ab12`、`访问码: ab12`、整段分享文案均可解析 |
| 链接拼接 | 百度 / 夸克 / 123 等自动补 `pwd=` 参数，点开即进 |
| 自动打开浏览器 | 识别成功即用默认浏览器打开，同一条链接只自动开一次（可关闭） |
| 历史记录 | 识别结果进表格，可导出 txt / csv |

快捷键：`Ctrl+V` 粘贴 · `Ctrl+O` 选图 · `Ctrl+C` 复制链接 · `Esc` 清空

## 支持的网盘

百度网盘、夸克网盘、阿里云盘、UC 网盘、123 云盘、迅雷云盘、天翼云盘、移动云盘/和彩云、
115 网盘、腾讯微云、蓝奏云、坚果云、Google Drive、OneDrive、Dropbox、MEGA。

其它网盘的二维码同样能解出链接，只是「网盘类型」显示为"未识别"，链接照样可用。

## 识别率

内置 `pyzbar` + `OpenCV QRCodeDetector` 双引擎，每个引擎跑 6 组预处理变体（放大、灰度、反色、OTSU、自适应二值化、旋转），任一命中即返回，单图耗时约 120~470 ms。

已实测：反色二维码、旋转 8°、缩放到 50%、一张图多个二维码、纯文本二维码（正确标记"未识别"，不伪造链接）。

---

## 从源码运行

```bash
pip install pillow numpy opencv-python pyzbar tkinterdnd2
python qrlink.py                # 启动界面
python qrlink.py a.png b.png    # 启动并直接解析指定图片
```

## 自行打包

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name NetDiskQRLink ^
  --icon NetDiskQRLink.ico ^
  --collect-all pyzbar --collect-all tkinterdnd2 ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas ^
  qrlink.py
```

## 目录结构

```
qrlink.py            # 全部源码，约 700 行，单文件（tkinter GUI + 解码 + 解析）
NetDiskQRLink.ico    # 图标源文件，16/24/32/48/64/128/256 多尺寸
使用说明.md           # 完整使用说明与排障
requirements.txt     # 运行依赖
```

## 技术要点

- **解码**：`pyzbar` + `OpenCV QRCodeDetector.detectAndDecodeMulti` 双引擎互补
- **解析**：正则抽取链接 → 网盘规则库匹配 → 提取码正则族匹配 → 链接规范化（补 `pwd` 参数）
- **界面**：标准库 tkinter，无重型 GUI 依赖
- **隐私**：无任何网络请求代码，解码完全在本机 CPU 完成

## License

[MIT](LICENSE)
