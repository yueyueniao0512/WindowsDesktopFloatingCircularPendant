# -*- coding: utf-8 -*-
"""
桌面悬浮圆形挂件（Windows / PySide6）

特性
- 无边框、透明、置顶；仅“圆 ∪ 文本框”区域可交互，其余鼠标穿透
- 圆形图片持续旋转，全程不中断
- 仅圆形区域支持左键按住拖拽；文本框区域不可拖拽
- 外部文本流 -> 文本框从圆边缘弹出、宽度自适应、取圆内平均色做底色 + 对比色文字
- 实时边界检测：放不下则中断动画、收回、换向再弹出；原侧恢复充足后换回（带迟滞）
- 文本流消失 -> 收回动画，回到只有旋转圆的状态

运行：pip install PySide6 然后 python desktop_widget.py
退出：双击圆形，或发送 __QUIT__（见 send_text.py）
"""

import sys

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QSize, QTimer
from PySide6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontMetrics, QIcon, QImage, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QFileDialog, QHBoxLayout, QLabel, QMenu,
    QPushButton, QRubberBand, QSystemTrayIcon, QVBoxLayout,
)

# ================== 用户可配置区 ==================
# 直接写死目标图片的绝对路径（改成你自己的图片路径；找不到时自动用内置渐变圆兜底）
IMAGE_PATH = r"C:\Users\yueyueniao\Downloads\anno.jpg"

TCP_HOST = "127.0.0.1"
TCP_PORT = 17321          # 文本流入口端口
DEMO = True               # 启动时自动演示弹出/收回；正式接入外部流时改为 False
PREFERRED_DIR = "right"   # 原本/默认弹出方向

R = 46                    # 圆半径
BOX_H = 46                # 预留（文本框高度现取圆直径 2*R，见几何计算）
GAP = 10                  # 圆与文本框间距
PAD = 8                   # 窗口留白（抗锯齿）
TEXT_PAD = 18             # 左右文字内边距
MAX_TEXT_W = 1200         # 文本框最大宽度保护
STEP = 0.16               # 弹出/收回每帧进度（约 6 帧 ≈ 100ms，快到刚好可察觉）
WIDTH_STEP = 0.35         # 文本框宽度平滑跟随系数（越大越快贴合目标宽度）
ANGLE_STEP = 2.4          # 旋转每帧角度
HYST = 24                 # 迟滞阈值：原本侧需多出该空间才切回
TICK_MS = 16              # 帧间隔 ≈ 60fps
BORDER_W = 2.0            # 描边宽度（圆 + 文本框）
BORDER_ALPHA = 130        # 描边透明度 0-255，越大越明显
OPACITY = 0.8             # 窗口整体不透明度（圆 + 文本框）
ROT_PAD = 6               # 圆图画布旋转留白(px)：防止旋转时方形边界扫进圆盘产生随旋转移动的毛边
FONT_FAMILY, FONT_SIZE = "Microsoft YaHei", 12
# =================================================

class _CropLabel(QLabel):
    """裁剪预览图：把鼠标事件转发给所属对话框。"""
    def __init__(self, dialog):
        super().__init__(dialog)
        self._dialog = dialog

    def mousePressEvent(self, e):
        self._dialog._on_press(e)

    def mouseMoveEvent(self, e):
        self._dialog._on_move(e)

    def mouseReleaseEvent(self, e):
        self._dialog._on_release(e)


class CropDialog(QDialog):
    """简易裁剪编辑器：在预览图上按住左键拖拽框选，确定后返回源图坐标 QRect。"""
    def __init__(self, image, init_rect=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择裁剪区域")
        self.src = image
        self.scale = min(1.0, 480.0 / max(1, image.width()),
                         480.0 / max(1, image.height()))
        disp = image.scaled(max(1, int(image.width() * self.scale)),
                            max(1, int(image.height() * self.scale)),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.label = _CropLabel(self)
        self.label.setPixmap(QPixmap.fromImage(disp))
        self.label.setFixedSize(disp.size())
        self.band = QRubberBand(QRubberBand.Rectangle, self.label)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        btns = QHBoxLayout()
        ok = QPushButton("确定"); ok.clicked.connect(self.accept)
        cancel = QPushButton("取消"); cancel.clicked.connect(self.reject)
        btns.addStretch(); btns.addWidget(ok); btns.addWidget(cancel)
        lay.addLayout(btns)
        self._origin = QPoint()
        self._disp_rect = QRect()
        if init_rect is not None:
            self._disp_rect = QRect(int(init_rect.left() * self.scale),
                                    int(init_rect.top() * self.scale),
                                    int(init_rect.width() * self.scale),
                                    int(init_rect.height() * self.scale))
            self.band.setGeometry(self._disp_rect)
            self.band.show()

    def _on_press(self, e):
        self._origin = e.position().toPoint()
        self._disp_rect = QRect(self._origin, QSize())
        self.band.setGeometry(self._disp_rect)
        self.band.show()

    def _on_move(self, e):
        if not self.band.isVisible():
            return
        self._disp_rect = QRect(self._origin, e.position().toPoint()).normalized()
        self.band.setGeometry(self._disp_rect)

    def _on_release(self, e):
        self._disp_rect = QRect(self._origin, e.position().toPoint()).normalized()
        self.band.setGeometry(self._disp_rect)

    def crop_rect(self):
        """返回裁剪区域 QRect；框选过小则返回 None（表示整图）。"""
        if self._disp_rect.width() < 8 or self._disp_rect.height() < 8:
            return None
        s = self.scale
        return QRect(int(self._disp_rect.left() / s), int(self._disp_rect.top() / s),
                     int(self._disp_rect.width() / s), int(self._disp_rect.height() / s))


class DesktopWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 不用 setWindowOpacity：Windows 分层窗口上它会走 SetLayeredWindowAttributes，
        # 破坏 per-pixel alpha，把抗锯齿边缘二值化成硬锯齿。改为绘制级半透明。

        self.font = QFont(FONT_FAMILY, FONT_SIZE)
        self.setFont(self.font)
        self.fm = QFontMetrics(self.font)

        # 状态
        self.angle = 0.0
        self.progress = 0.0
        self.pending_dir = None
        self.current_dir = PREFERRED_DIR
        self.text = ""
        self.disp_w = 0.0          # 实际显示的文本宽度（平滑跟随目标宽度）
        self.win_rect = QRect()

        self.dragging = False
        self.drag_offset = QPoint()

        self.screen = QApplication.primaryScreen().availableGeometry()
        # 圆心逻辑坐标（屏幕坐标）
        self.cx = self.screen.center().x()
        self.cy = self.screen.center().y()

        self.crop_rect = None      # 源图裁剪区域 QRect；None = 整图（默认居中取正方形）

        self.circle_pix = self._build_circle_pixmap()
        self.avg_color, self.text_color = self._build_colors()

        self._init_tcp()
        self._init_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)

        self.update_geometry()
        self.show()

        if DEMO:
            QTimer.singleShot(900, lambda: self.set_text("你好，我是桌面小助手，这是弹出的文字气泡"))
            QTimer.singleShot(5200, lambda: self.set_text("文本宽度会随内容自动拉长，颜色取自圆形图片"))
            QTimer.singleShot(9500, self.clear_text)

    # ---------- 图片与配色 ----------
    def _fallback_image(self):
        """兜底：内置紫蓝渐变（图片不可用时使用）。

        只画整幅渐变矩形，不做椭圆硬裁剪——圆形状完全交给 _build_circle_pixmap
        的逐像素羽化控制，避免源图自带锯齿被带进圆图。
        """
        side = 256
        img = QImage(side, side, QImage.Format_ARGB32)
        p = QPainter(img)
        for y in range(side):
            t = y / side
            p.fillRect(0, y, side, 1,
                       QColor(int(90 + 70 * t), int(80 + 40 * t), int(220 - 60 * t)))
        p.end()
        return img

    def _load_source_image_raw(self):
        """原始图片（未裁剪），供裁剪编辑器使用；含兜底。"""
        img = QImage(IMAGE_PATH)
        return img if not img.isNull() else self._fallback_image()

    def _load_source_image(self):
        """按当前 crop_rect 裁剪后的源图；无裁剪时居中取正方形。"""
        img = self._load_source_image_raw()
        if self.crop_rect is not None:
            r = self.crop_rect.intersected(img.rect())
            if not r.isEmpty():
                img = img.copy(r)          # 用户自定义裁剪区域（可非正方，后续居中取方）
        return img

    def _build_circle_pixmap(self):
        """生成带平滑 alpha 边缘的圆形图，画布四周留旋转白边。

        关键（"毛边随图片旋转"的根因修复）：圆盘外缘必须离画布方形边界足够远。
        旋转是 rotate(pixmap) 完成的——画布的方角会一起转。若圆盘紧贴画布边界，
        转到斜角时方角/边界就会扫进可见圆盘，形成随旋转移动的角落毛边。
        留 ROT_PAD 白边后，方形边界永远转不到圆盘，任何角度都是干净圆。
        """
        src = self._load_source_image()
        side = min(src.width(), src.height())
        src = src.copy((src.width() - side) // 2, (src.height() - side) // 2,
                       side, side)

        # 圆盘直径 2R，四周各留 ROT_PAD 透明白边
        disc = 2 * R
        n = disc + 2 * ROT_PAD
        scaled = src.scaled(disc, disc, Qt.IgnoreAspectRatio,
                            Qt.SmoothTransformation)
        scaled = scaled.convertToFormat(QImage.Format_ARGB32)

        out = QImage(n, n, QImage.Format_ARGB32)
        out.fill(Qt.transparent)
        c = n / 2.0                      # 画布中心 = 圆盘中心
        radius = R - 0.5                 # 圆盘半径，留在画布内
        feather = 1.4                    # 羽化宽度(px)：alpha 在此带内线性 1->0
        for y in range(n):
            for x in range(n):
                dist = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
                a = max(0.0, min(1.0, (radius + feather / 2 - dist) / feather))
                if a <= 0:
                    continue
                sx, sy = x - ROT_PAD, y - ROT_PAD   # 圆盘像素映射回缩放源图
                if 0 <= sx < disc and 0 <= sy < disc:
                    sc = scaled.pixelColor(sx, sy)
                    out.setPixelColor(x, y, QColor(sc.red(), sc.green(), sc.blue(),
                                                   int(255 * a)))
        self.circle_pix_offset = ROT_PAD  # 绘制时把画布左上角对齐到 (-R-pad)
        return QPixmap.fromImage(out)

    def _build_colors(self):
        src = self._load_source_image().scaled(48, 48, Qt.IgnoreAspectRatio,
                                               Qt.SmoothTransformation)
        r = g = b = n = 0
        for y in range(48):
            for x in range(48):
                dx, dy = x - 24, y - 24
                if dx * dx + dy * dy <= 24 * 24:
                    c = QColor(src.pixelColor(x, y))
                    r += c.red(); g += c.green(); b += c.blue(); n += 1
        if n == 0:
            return QColor(80, 80, 180), QColor(255, 255, 255)
        avg = QColor(r // n, g // n, b // n)
        lum = 0.299 * r / n + 0.587 * g / n + 0.114 * b / n
        fg = QColor(20, 20, 20) if lum >= 128 else QColor(250, 250, 250)
        return avg, fg

    # ---------- 系统托盘 + 对外图片/裁剪 API ----------
    def _init_tray(self):
        """系统托盘：预留给“选图 / 裁剪”交互的入口。无托盘环境自动跳过。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(QIcon(self.circle_pix), self)
        self.tray.setToolTip("桌面悬浮挂件（左键拖动圆形移动 / 双击退出）")
        menu = QMenu()
        act_pick = menu.addAction("选择图片…")
        act_pick.triggered.connect(self.api_choose_image)
        act_crop = menu.addAction("裁剪图片区域…")
        act_crop.triggered.connect(self.api_choose_crop)
        act_reset = menu.addAction("恢复默认圆形")
        act_reset.triggered.connect(self.api_reset_image)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

    # 对外 API（供后续 UI / 外部调用）：
    def api_set_image(self, path):
        """更换圆形图片。path: 图片绝对路径；返回是否成功。"""
        img = QImage(path)
        if img.isNull():
            if self.tray:
                self.tray.showMessage("图片无效", path, QSystemTrayIcon.Warning, 2500)
            return False
        global IMAGE_PATH
        IMAGE_PATH = path
        self.crop_rect = None
        self._refresh_image()
        return True

    def api_set_crop(self, rect):
        """设置裁剪区域。rect: QRect 或 (x, y, w, h)（源图像素坐标）；None 表示不裁剪。"""
        if rect is None:
            self.crop_rect = None
        elif isinstance(rect, (tuple, list)):
            self.crop_rect = QRect(*rect)
        else:
            self.crop_rect = rect
        self._refresh_image()

    def api_choose_image(self):
        """弹出文件对话框选图（托盘“选择图片…”调用）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择圆形图片", IMAGE_PATH,
            "图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if path:
            self.api_set_image(path)

    def api_choose_crop(self):
        """弹出裁剪编辑器框选区域（托盘“裁剪图片区域…”调用）。"""
        dlg = CropDialog(self._load_source_image_raw(), self.crop_rect, self)
        if dlg.exec() == QDialog.Accepted:
            self.api_set_crop(dlg.crop_rect())

    def api_reset_image(self):
        """恢复默认（无裁剪、使用内置渐变圆）。"""
        global IMAGE_PATH
        IMAGE_PATH = ""
        self.crop_rect = None
        self._refresh_image()

    def _refresh_image(self):
        """图片/裁剪变化后重建圆图、配色与托盘图标。"""
        self.circle_pix = self._build_circle_pixmap()
        self.avg_color, self.text_color = self._build_colors()
        if self.tray:
            self.tray.setIcon(QIcon(self.circle_pix))
        self.update()

    # ---------- TCP 文本流 ----------
    def _init_tcp(self):
        self.server = QTcpServer(self)
        self.server.listen(QHostAddress(TCP_HOST), TCP_PORT)
        self.server.newConnection.connect(self._on_new_connection)
        self.sockets = []

    def _on_new_connection(self):
        sock = self.server.nextPendingConnection()
        self.sockets.append(sock)
        sock.readyRead.connect(lambda s=sock: self._on_ready(s))
        sock.disconnected.connect(lambda s=sock: self.sockets.remove(s)
                                  if s in self.sockets else None)

    def _on_ready(self, sock):
        data = bytes(sock.readAll()).decode("utf-8", "ignore").strip()
        if not data:
            return
        if data == "__CLEAR__":
            self.clear_text()
        elif data == "__QUIT__":
            QApplication.quit()
        elif data.startswith("__REPLACE__"):
            # 覆盖模式：清空后显示全新文本
            self.set_text(data[len("__REPLACE__"):].replace("\r", "").replace("\n", " "))
        else:
            # 文本流：新片段追加到已有文本，框随之拉长
            self.set_text(data.replace("\r", "").replace("\n", " "), append=True)

    # ---------- 对外接口 ----------
    def set_text(self, text, append=False):
        """接收文本流。append=True 时把新片段接到已有文本后（流式累积）。"""
        t = text.strip()
        if append and self.text:
            self.text = (self.text + t)[:MAX_TEXT_W]
        else:
            self.text = t[:MAX_TEXT_W]
        if not self.text:
            self.clear_text()

    def clear_text(self):
        self.text = ""
        self.pending_dir = None  # 收回后不再弹出

    # ---------- 计算 ----------
    def target_width(self):
        """文本对应的目标宽度。
        绘制时文字区宽度 = bw - GAP - 2*TEXT_PAD（左内边距含 GAP），
        因此目标宽度必须把 GAP 算进去，否则最后一个字会被裁掉。"""
        if not self.text:
            return 0
        return min(MAX_TEXT_W,
                   self.fm.horizontalAdvance(self.text) + 2 * TEXT_PAD + GAP)

    def box_width(self):
        """当前显示宽度，用于几何/绘制，随目标宽度平滑跟随。"""
        return self.disp_w

    def desired_direction(self, need):
        """稳定幂等的方向决策：原本侧优先(带迟滞)；不够则选空间更大的一侧并锁定。
        关键：步骤3不参考 current_dir，避免“两边都放不下”时每帧翻转导致窗口横跳(抽搐)。"""
        right_space = self.screen.right() - (self.cx + R) - GAP
        left_space = (self.cx - R) - GAP - self.screen.left()

        # 1) 原本侧重新充足(含迟滞) -> 切回原本侧（“恢复富足换回”的触发）
        pref_space = right_space if PREFERRED_DIR == "right" else left_space
        if pref_space >= need + HYST:
            return PREFERRED_DIR

        # 2) 原本侧不够，但当前方向仍够用 -> 保持，避免无谓换向
        cur_space = right_space if self.current_dir == "right" else left_space
        if cur_space >= need:
            return self.current_dir

        # 3) 当前方向也不够：选空间更大的一侧并锁定（不参考 current_dir，故不会震荡）
        return "right" if right_space >= left_space else "left"

    # ---------- 主循环 ----------
    def tick(self):
        self.angle = (self.angle + ANGLE_STEP) % 360  # 旋转永不停

        # 显示宽度平滑跟随目标宽度：文本流逐段追加时，框可见地拉长
        target_w = self.target_width()
        if self.disp_w < target_w:
            step = max(1.0, (target_w - self.disp_w) * WIDTH_STEP)
            self.disp_w = min(target_w, self.disp_w + step)
        elif self.disp_w > target_w:
            step = max(1.0, (self.disp_w - target_w) * WIDTH_STEP)
            self.disp_w = max(target_w, self.disp_w - step)

        # 边界检测按目标宽度预判，避免文字撑出屏幕
        need = target_w
        desired = self.desired_direction(need)

        if self.pending_dir is not None:
            # 收回中：锁定方向，不因边界抖动每帧改向（这是“圆球抽搐”的根因）。
            # 收归零后用当下最新空间重算弹出方向，仍满足实时检测。
            if self.progress <= 0:
                self.pending_dir = None
                self.current_dir = self.desired_direction(need)
        elif desired != self.current_dir:
            if self.progress <= 0:
                self.current_dir = desired          # 隐藏状态下直接换向，无需走收回
            else:
                self.pending_dir = desired          # 中断当前动画，先收回

        goal = 0.0 if (self.pending_dir is not None or not self.text) else 1.0
        if self.progress < goal:
            self.progress = min(goal, self.progress + STEP)
        elif self.progress > goal:
            self.progress = max(goal, self.progress - STEP)

        self.update_geometry()
        self.update()

    def update_geometry(self):
        # bw = 露出长度（圆外可见部分）；文本框内端固定在圆心，高度 = 圆直径
        bw = int(self.box_width() * self.progress)
        circle = QRect(self.cx - R, self.cy - R, 2 * R, 2 * R)
        box = QRect()
        if bw > 0:
            top = self.cy - R
            # 胶囊形圆角半径 R：横边直线段从 box.left+R 开始
            # 要让该端点=cx（圆最高点），box.left 必须=cx-R
            if self.current_dir == "right":
                box = QRect(self.cx - R, top, 2 * R + bw, 2 * R)
            else:
                box = QRect(self.cx - R - bw, top, 2 * R + bw, 2 * R)
        union = circle.united(box).adjusted(-PAD, -PAD, PAD, PAD)
        if union != self.win_rect:
            self.win_rect = union
            self.setGeometry(union)

        # 关键修复：绝不使用 setMask。
        # QRegion 椭圆是「二值(1-bit)硬边」区域、无抗锯齿，用水平扫描条带逼近，
        # 在 45° 对角方向（左上/右上等）内缩误差最大；其边界几乎贴着圆盘羽化
        # 外缘，会把平滑的 per-pixel alpha 硬裁成阶梯 —— 这正是"固定在左上角
        # 的阶梯毛刺"。且该 region 相对窗口固定，不随圆内图片旋转，与"随旋转
        # 毛边"是两个独立问题（后者已用 ROT_PAD 留白修复）。
        # WA_TranslucentBackground 的分层窗口中，alpha=0 的像素天然鼠标穿透
        # （Windows 分层窗口 hit-testing 规则），因此去掉 mask 后：
        #   · 圆外/文本框外的透明区仍穿透下层窗口；
        #   · 圆与文本框的可见边缘完全由各自 alpha 决定，无二值硬裁 => 无阶梯。
        # 拖拽仍由 mousePressEvent 的圆内几何判断保证（文本框区域不拖拽）。

    def _local(self, rect):
        return QRect(rect.left() - self.win_rect.left(),
                     rect.top() - self.win_rect.top(),
                     rect.width(), rect.height())

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        # 整体半透明：绘制级 opacity（不用窗口级 setWindowOpacity，后者在 Windows
        # 分层窗口上会破坏 per-pixel alpha，让抗锯齿边缘退化为硬边）
        p.setOpacity(OPACITY)

        lc = self._local(QRect(self.cx - R, self.cy - R, 2 * R, 2 * R))
        ccx, ccy = lc.center().x(), lc.center().y()

        # 统一描边：用对比色的半透明版本，圆与文本框同色系
        border_col = QColor(self.text_color)
        border_col.setAlpha(BORDER_ALPHA)
        border_pen = QPen(border_col, BORDER_W)
        d = BORDER_W / 2  # 内缩半个线宽，描边不被窗口边界裁掉

        bw = int(self.box_width() * self.progress)
        if bw > 0:
            top = self.cy - R
            # 胶囊圆角半径 R：横边直线段从 left+R 起。令 left=cx-R 使端点落在 cx=圆上下端点
            box_screen = (QRect(self.cx - R, top, 2 * R + bw, 2 * R)
                          if self.current_dir == "right"
                          else QRect(self.cx - R - bw, top, 2 * R + bw, 2 * R))
            box = self._local(box_screen)

            # 文字绘制区 = 圆外的“露出段”，左右两侧均贴紧圆、留出 GAP + TEXT_PAD
            if self.current_dir == "right":
                text_rect = QRect(self.cx + R + GAP, top, bw - GAP, 2 * R)
                align = Qt.AlignVCenter | Qt.AlignLeft
            else:
                text_rect = QRect(self.cx - R - bw, top, bw - GAP, 2 * R)
                align = Qt.AlignVCenter | Qt.AlignRight
            text_rect = self._local(text_rect).adjusted(TEXT_PAD, 0, -TEXT_PAD, 0)

            p.save()
            p.setClipRect(box)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(self.avg_color))
            p.drawRoundedRect(box, R, R)  # 高度=2R，圆角半径 R => 胶囊形，右端正好半圆
            p.setPen(QPen(self.text_color))
            p.setFont(self.font)
            p.drawText(text_rect, align, self.text)
            p.restore()

            # 文本框外描边（内缩 d，胶囊圆角半径同步减 d）
            p.save()
            p.setClipRect(box)
            p.setPen(border_pen)
            p.setBrush(Qt.NoBrush)
            inner = box.adjusted(d, d, -d, -d)
            p.drawRoundedRect(inner, R - d, R - d)
            p.restore()

        # 圆最后画，盖住文本框伸入圆内的部分 -> 视觉上文本框从圆内部长出
        # 圆图带 ROT_PAD 旋转白边：把画布左上角放在 -(R+pad)，圆盘中心正好落在 ccx,ccy。
        p.save()
        p.translate(ccx, ccy)
        p.rotate(self.angle)
        p.drawPixmap(-R - ROT_PAD, -R - ROT_PAD, self.circle_pix)
        p.restore()

        # 圆形外描边：贴合圆盘边缘（半径 ≈ R），覆盖 alpha 羽化带、藏住边界
        p.save()
        p.setPen(border_pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(lc.left() + d, lc.top() + d,
                             2 * R - BORDER_W, 2 * R - BORDER_W))
        p.restore()
        p.end()

    # ---------- 拖拽：仅圆形区域 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            g = event.globalPosition()
            if (g.x() - self.cx) ** 2 + (g.y() - self.cy) ** 2 <= R * R:
                self.dragging = True
                self.drag_offset = QPoint(g.x() - self.cx, g.y() - self.cy)
                self.grabMouse()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging:
            g = event.globalPosition()
            self.cx = int(g.x() - self.drag_offset.x())
            self.cy = int(g.y() - self.drag_offset.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.dragging:
            self.dragging = False
            self.releaseMouse()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        g = event.globalPosition()
        if (g.x() - self.cx) ** 2 + (g.y() - self.cy) ** 2 <= R * R:
            QApplication.quit()


def _setup_dpi_awareness():
    """声明 DPI 感知——锯齿根因修复。

    未声明时，Windows 会对无边框透明窗口做 DPI 虚拟化：先按 96DPI 渲染窗口位图，
    再由系统拉伸到 125%/150%（实测截图里 92px 的圆被拉成 116px ≈ 1.25×）。
    这种位图二次拉伸会把原本平滑的 1px 抗锯齿边缘重新采样成阶梯锯齿，
    且窗口落点不在整数像素时左右不对称——这正是"左上毛边、右下顺滑"的真相。
    声明感知后按物理像素 1:1 渲染（或由 Qt 高质量高 DPI 渲染），边缘恢复平滑。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()        # Vista+ 兜底
    except Exception:
        pass


if __name__ == "__main__":
    _setup_dpi_awareness()                              # 必须在 QApplication 之前
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)  # 保留精确缩放因子，不取整
    app = QApplication(sys.argv)
    w = DesktopWidget()
    sys.exit(app.exec())
