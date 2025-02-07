# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'ui-yolov5.ui'
#
# Created by: PyQt5 UI code generator 5.15.9
#
# WARNING: Any manual changes made to this file will be lost when pyuic5 is
# run again.  Do not edit this file unless you know what you are doing.

from PyQt5.QtCore import Qt, QDateTime, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage, QColor, QPalette
from PyQt5.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox
from uiqt import Ui_MainWindow

from models.common import DetectMultiBackend
from utils.dataloaders import letterbox
from utils.general import (LOGGER, Profile, check_file, check_img_size, check_imshow, check_requirements, colorstr, cv2,
                           increment_path, non_max_suppression, print_args, scale_boxes, strip_optimizer, xyxy2xywh)
from utils.plots import Annotator, colors
from utils.torch_utils import select_device
from utils.dataloaders import IMG_FORMATS, VID_FORMATS, LoadImages
from lane import LANE_DETECTION

import sys
import os
import cv2
import numpy as np
import torch
import time
import json
import requests


class detThread(QThread):
    send_img = pyqtSignal(np.ndarray)
    send_cls = pyqtSignal(dict)
    send_dis = pyqtSignal(list)
    send_msg = pyqtSignal(str)
    send_dir = pyqtSignal(int)
    send_rio = pyqtSignal(float)
    send_dev = pyqtSignal(float)
    send_lane = pyqtSignal(bool, bool)

    def __init__(self, device='', weight='yolov5_k.pt', iou=0.45, conf=0.7):
        super(detThread, self).__init__()
        # 权重
        self.weight = weight
        self.cur_weight = weight
        # iou阈值
        self.iou = iou
        # 置信度阈值
        self.conf = conf
        self.device = select_device(device)
        self.half = False  # use FP16 half-precision inference
        self.dnn = False  # use OpenCV DNN for ONNX inference
        self.imgsz = (640, 640)  # inference size (height, width)
        self.line_thickness = 3  # 框线条宽度
        self.max_det = 1000  # 最多检测目标
        self.classes = None  # 是否只保留特定类别: --class 0, or --class 0 2 3
        self.agnostic_nms = False  # 进行nms是否也去除不同类别间的框class-agnostic NMS

        # 是否能够左右变道，默认能
        self.llane = True
        self.rlane = True
        # 车辆偏离度
        self.dev = 0.0
        # 曲率半径
        self.radio = 0
        # 车道方向，0直行，1左转，2右转
        self.dirt = 0
        self.obj = {}
        self.dis = []
        # 检测数据路径
        self.source = ''
        self.autosave = True
        self.save = './runs/merge'
        self.end = False
        self.vid_cap = None
        self.out = None
        self.percent_length = 1
        self.showDist = True

    def run(self):
        try:
            self.send_msg.emit('The detection model is initializing')
            # 车道线检测初始化
            lane_detect = LANE_DETECTION()
            # YOLOv5模型初始化
            model = DetectMultiBackend(self.weight, device=self.device, dnn=self.dnn, fp16=self.half)
            stride, names, pt = model.stride, model.names, model.pt
            imgsz = check_img_size(self.imgsz, s=stride)  # check image size

            # 加载数据集
            dataset = LoadImages(self.source, img_size=imgsz, stride=stride)
            dataset = iter(dataset)

            # Run inference
            bs = 1  # batch_size
            model.warmup(imgsz=(1 if pt or model.triton else bs, 3, *imgsz))  # warmup

            count = 0  # 记录当前处理对象次数
            while True:
                if self.end:
                    # 终止
                    self.vid_cap.release()  # yolov5内视频捕获终止
                    self.send_msg.emit('Finished')
                    if self.out is not None:
                        self.out.release()  # 保存视频终止
                if self.cur_weight != self.weight:
                    # 更换模型
                    self.send_msg.emit('The detection model is initializing')
                    model = DetectMultiBackend(self.cur_weight, device=self.device, dnn=self.dnn, fp16=self.half)
                    stride, names, pt = model.stride, model.names, model.pt
                    imgsz = check_img_size(self.imgsz, s=stride)  # check image size
                    bs = 1  # batch_size
                    model.warmup(imgsz=(1 if pt or model.triton else bs, 3, *imgsz))  # warmup
                    self.weight = self.cur_weight
                # 计数当前处理帧的个数
                count += 1
                # 获取下一个对象
                path, im, im0s, self.vid_cap, s = next(dataset)
                if self.vid_cap:
                    percent = int(count / self.vid_cap.get(cv2.CAP_PROP_FRAME_COUNT) * self.percent_length)
                    # print(percent,count,self.vid_cap.get(cv2.CAP_PROP_FRAME_COUNT))
                else:
                    percent = self.percent_length
                # 图像预处理2
                im = torch.from_numpy(im).to(model.device)  # 图像格式转换
                im = im.half() if model.fp16 else im.float()  # uint8 to fp16/32
                im /= 255  # 0 - 255 to 0.0 - 1.0归一化
                if len(im.shape) == 3:
                    im = im[None]  # expand for batch dim

                # 模型推理
                # 图片进行前向推理
                pred = model(im, augment=False, visualize=False)  # augment和visualize都是参数，预测是否采用数据增强、虚拟化特征？
                # nms除去多余的框
                pred = non_max_suppression(pred, self.conf, self.iou, self.classes, self.agnostic_nms,
                                           max_det=self.max_det)
                # 每张图片进行处理
                for i, det in enumerate(pred):
                    im0 = im0s.copy()
                    annotator = Annotator(im0, line_width=self.line_thickness, example=str(names))
                    l_ob, r_ob = False, False  # 默认左右没有车
                    self.obj = {name: 0 for name in names}
                    self.dis = []
                    # 检测车道线
                    img_bin, lx, rx, pty = lane_detect.detection(im0s.copy())
                    if len(det):
                        # 将坐标信息恢复到原始图像的尺寸
                        det[:, :4] = scale_boxes(im.shape[2:], det[:, :4], im0.shape).round()
                        for *xyxy, conf, cls in reversed(det):  # 框位置，置信度和类别id
                            c = int(cls)  # integer class
                            # 保存当前信息
                            '''xyxy_list.append(xyxy)
                            conf_list.append(conf)
                            class_id_list.append(c)'''
                            label_d, dist, xdirt = lane_detect.distance(xyxy)
                            self.dis.append(dist)  # 距离统计
                            # print(self.obj,names,c)
                            self.obj[c] += 1  # 类别计数
                            label = f'{names[c]} {conf:.2f}'
                            # 判断是否距离过近, 红色（后面BGR转RGB）
                            if dist < 5:
                                annotator.box_label(xyxy, label, color=(0, 0, 135))
                                colort = (0, 0, 135)
                                # 左右是否有障碍
                                if xdirt == 1:
                                    l_ob = True
                                elif xdirt == 2:
                                    r_ob = True
                            else:
                                annotator.box_label(xyxy, label, color=colors(c, True))
                                colort = colors(c, True)
                            # 是否显示距离标签
                            if self.showDist:
                                annotator.box_label2(xyxy, label_d, color=colort)
                    # 目标检测结果
                    im0 = annotator.result()
                    # 车道线检测
                    im0, self.dirt, self.dev, self.radio, lsp, rsp = lane_detect.get_info(im0, img_bin, lx, rx, pty)
                    # 信息处理
                    self.send_img.emit(im0)  # 显示检测结果图片
                    self.send_dir.emit(self.dirt)  # 方向信息
                    self.send_dev.emit(self.dev)  # 偏离度
                    self.send_rio.emit(self.radio)  # 曲率半径
                    if l_ob or (not lsp[0]):
                        self.llane = False
                    else:
                        self.llane = True
                    if r_ob or (not rsp[0]):
                        self.rlane = False
                    else:
                        self.rlane = True
                    self.send_lane.emit(self.llane, self.rlane)  # 是否能够变道
                    self.send_cls.emit(self.obj)
                    self.send_dis.emit(self.dis)

                    # 保存
                    if self.autosave:
                        os.makedirs(self.save, exist_ok=True)
                        if self.vid_cap is None:
                            # 图片
                            save_path = os.path.join(self.save,
                                                     time.strftime('%Y_%m_%d_%H_%M_%S', time.localtime()) + '.png')
                            cv2.imwrite(save_path, im0)
                        else:
                            # 视频
                            if count == 1:  # 第一帧初始化
                                fps = int(self.vid_cap.get(cv2.CAP_PROP_FPS))
                                if fps == 0:
                                    fps = 25
                                width, height = im0.shape[1], im0.shape[0]
                                save_path = os.path.join(self.save,
                                                         time.strftime('%Y_%m_%d_%H_%M_%S', time.localtime()) + '.mp4')
                                self.out = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*"mp4v"), fps,
                                                           (width, height))
                            self.out.write(im0)
                if percent == self.percent_length:
                    self.send_msg.emit('Finished')
                    if self.out is not None:
                        self.out.release()  # 保存视频终止
                    break
        # 异常处理
        except Exception as e:
            print(e)
            if self.out is not None:
                self.out.release()  # 保存视频终止
            # self.send_msg.emit('%s' % e)


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)
        self.setupUi(self)
        # 图标
        self.pix_lf = QPixmap('./icon/corner-up-left.png')
        self.pix_rg = QPixmap('./icon/corner-up-right.png')
        self.pix_up = QPixmap('./icon/arrow-up.png')
        self.pix_red = QPixmap('./icon/red.png')
        self.pix_blue = QPixmap('./icon/blue.png')

        # 计时器
        self.timer = QTimer()
        self.timer.timeout.connect(self.showTime)  # 这个通过调用槽函数来刷新时间
        self.timer.start(1000)  # 每隔一秒刷新一次，这里设置为1000ms  即1s
        self.showTime()

        # 天气
        self.timer_w = QTimer()
        self.timer_w.timeout.connect(self.get_weather)  # 这个通过调用槽函数来刷新天气情况
        self.timer_w.start(60000)  # 每隔1min刷新一次，这里设置为60000ms,即1min
        self.get_weather()

        # 提示信息
        self.timer_m = QTimer(self)
        self.timer_m.setSingleShot(True)  # 只执行1次
        self.timer_m.timeout.connect(self.timeout_msg)

        # yolov5 thread
        self.det_thread = detThread()
        self.det_thread.send_img.connect(lambda x: self.show_image(x, self.label_result))
        self.det_thread.send_msg.connect(lambda x: self.show_msg(x))
        self.det_thread.send_dev.connect(lambda x: self.show_dev(x))
        self.det_thread.send_rio.connect(lambda x: self.show_rio(x))
        self.det_thread.send_dir.connect(lambda x: self.show_dir(x))
        self.det_thread.send_lane.connect(lambda x, y: self.show_lane(x, y))
        self.det_thread.send_cls.connect(lambda x: self.show_obj(x))
        self.det_thread.send_dis.connect(lambda x: self.show_dis(x))

        self.det_thread.source = './data/video'
        self.pushButton_start.clicked.connect(self.btn_run)
        self.pushButton_end.clicked.connect(self.end)
        self.pushButton_save.clicked.connect(self.is_save)
        self.pushButton_video.clicked.connect(self.open_video)
        self.pushButton_image.clicked.connect(self.open_img)
        self.pushButton_model.clicked.connect(self.change_model)
        self.checkBox.clicked.connect(self.is_show)

        self.spinBox_conf.valueChanged.connect(lambda x: self.change_val(x, 'spinBox_conf'))
        self.Slider_conf.valueChanged.connect(lambda x: self.change_val(x, 'Slider_conf'))
        self.spinBox_iou.valueChanged.connect(lambda x: self.change_val(x, 'spinBox_iou'))
        self.Slider_iou.valueChanged.connect(lambda x: self.change_val(x, 'Slider_iou'))
        self.spinBox_conf.setValue(self.det_thread.conf)
        self.spinBox_iou.setValue(self.det_thread.iou)

    def showTime(self):
        # 获取系统当前时间
        time = QDateTime.currentDateTime()
        # 设置系统时间的显示格式
        timeDisplay = time.toString('hh:mm')
        self.label_time.setText(timeDisplay)

    @staticmethod
    def show_image(img_src, label):
        try:
            ih, iw, _ = img_src.shape
            w = label.geometry().width()
            h = label.geometry().height()
            # keep original aspect ratio
            if iw / w > ih / h:
                scal = w / iw
                nw = w
                nh = int(scal * ih)
                img_src_ = cv2.resize(img_src, (nw, nh))

            else:
                scal = h / ih
                nw = int(scal * iw)
                nh = h
                img_src_ = cv2.resize(img_src, (nw, nh))

            frame = cv2.cvtColor(img_src_, cv2.COLOR_BGR2RGB)
            img = QImage(frame.data, frame.shape[1], frame.shape[0], frame.shape[2] * frame.shape[1],
                         QImage.Format_RGB888)
            label.setPixmap(QPixmap.fromImage(img))

        except Exception as e:
            print(repr(e))

    # 是否自动保存
    def is_save(self):
        if self.pushButton_save.isChecked():
            # 点击是没选中，则选中
            self.det_thread.autosave = True
            self.pushButton_save.setChecked(Qt.Checked)
        else:
            self.det_thread.autosave = False
            self.pushButton_save.setChecked(Qt.Unchecked)

    # 是否显示距离
    def is_show(self):
        if self.checkBox.isChecked():
            # 点击是没选中，则选中
            self.det_thread.showDist = True
            self.checkBox.setChecked(Qt.Checked)
        else:
            self.det_thread.showDist = False
            self.checkBox.setChecked(Qt.Unchecked)

    def mbox(self, title, context):
        # 使用infomation信息框
        QMessageBox.information(self, title, context)

    def btn_run(self):
        if self.det_thread.source == '':
            self.mbox('提示', '请选择文件进行检测！')
            self.pushButton_start.setChecked(Qt.Unchecked)
        else:
            self.det_thread.end = False
            if self.pushButton_start.isChecked():
                # 是否已经按下
                self.pushButton_save.setEnabled(False)
                # self.det_thread.is_continue = True
                if not self.det_thread.isRunning():
                    self.det_thread.start()
                source = os.path.basename(self.det_thread.source)
                self.display_msg_s("Detecting >> Model：{}，File：{}".format(os.path.basename(self.det_thread.weight),
                                                                        source))
            '''else:
                self.det_thread.is_continue = False
                self.statistic_msg('Pause')'''

    # 终止
    def end(self):
        self.det_thread.end = True
        self.pushButton_save.setEnabled(True)

    def open_video(self):
        config_f = 'data/openfile.json'
        config = json.load(open(config_f, 'r', encoding='utf-8'))
        openfile = config['openfile']
        if not os.path.exists(openfile):
            openfile = os.getcwd()
        name, _ = QFileDialog.getOpenFileName(self, 'Video', openfile, "Video File(*.mp4 *.mkv *.avi *.flv)")
        if name:
            self.det_thread.source = name
            self.display_msg("Loaded file >> {}".format(os.path.basename(name)))
            config['openfile'] = os.path.dirname(name)
            config_json = json.dumps(config, ensure_ascii=False, indent=2)
            with open(config_f, 'w', encoding='utf-8') as f:
                f.write(config_json)
            self.end()

    def open_img(self):
        config_f = 'data/openfile.json'
        config = json.load(open(config_f, 'r', encoding='utf-8'))
        openfile = config['openfile']
        if not os.path.exists(openfile):
            openfile = os.getcwd()
        name, _ = QFileDialog.getOpenFileName(self, 'image', openfile, "Pic File(*.jpg *.png)")
        if name:
            self.det_thread.source = name
            self.display_msg_s("Loaded file >> {}".format(os.path.basename(name)))
            config['openfile'] = os.path.dirname(name)
            config_json = json.dumps(config, ensure_ascii=False, indent=2)
            with open(config_f, 'w', encoding='utf-8') as f:
                f.write(config_json)
            self.end()

    def change_model(self):
        config_f = 'data/openfile.json'
        config = json.load(open(config_f, 'r', encoding='utf-8'))
        openfile = config['openfile']
        if not os.path.exists(openfile):
            openfile = os.getcwd()
        name, _ = QFileDialog.getOpenFileName(self, 'Video', openfile, "PT File(*.pt)")
        if name:
            self.det_thread.cur_weight = name
        else:
            self.det_thread.cur_weight = './yolov5_k.pt'
        str1 = 'Model: ' + self.det_thread.cur_weight.split('/')[-1]
        self.pushButton_model.setText(str1)
        self.display_msg("Change Model >> " + str1)

    def change_val(self, x, control):
        if control == 'spinBox_conf':
            self.Slider_conf.setValue(int(x * 100))
        elif control == 'Slider_conf':
            self.spinBox_conf.setValue(x / 100)
            self.det_thread.conf = x / 100
        elif control == 'spinBox_iou':
            self.Slider_iou.setValue(int(x * 100))
        elif control == 'Slider_iou':
            self.spinBox_iou.setValue(x / 100)
            self.det_thread.iou = x / 100
        else:
            pass

    def display_msg(self, msg):     # 动态
        self.label_sentence.setText("(>ω<)：" + msg + " ~")
        self.timer_m.start(3000)

    def timeout_msg(self):
        if not self.det_thread.end:
            model= os.path.basename(self.det_thread.weight)
            file=os.path.basename(self.det_thread.source)
            self.display_msg_s("Detecting >> Model：{}，File：{}".format(model, file))
        else:
            self.label_sentence.setText("ヾ(•ω•`)o：Hi, wish you a good mood today ~")

    def display_msg_s(self, msg):   # 静态不用计时器
        self.label_sentence.setText("(>ω<)：" + msg + " ~")

    def show_msg(self, msg):
        # self.runButton.setChecked(Qt.Unchecked)
        self.display_msg(msg)
        if msg == "Finished":
            self.pushButton_save.setEnabled(True)
            self.pushButton_end.setChecked(Qt.Checked)  # end选中

    def show_dev(self, num):
        if num > 0:
            str1 = 'R {0:.2f}'.format(num)
        else:
            str1 = 'L {0:.2f}'.format(abs(num))
        self.label_dev_num.setText(str1)

    def show_rio(self, num):
        str1 = 'Radius of curvature: {0:5.0f} m'.format(num)
        self.label_direction_2.setText(str1)

    def show_dir(self, num):
        if num == 0:
            self.label_direction.setText('Go Straight')
            self.label.setPixmap(self.pix_up)
        elif num == 1:
            self.label_direction.setText('Turn Left')
            self.label.setPixmap(self.pix_lf)
        else:
            self.label_direction.setText('Turn Right')
            self.label.setPixmap(self.pix_rg)

    def show_lane(self, lbool, rbool):
        if lbool:
            self.label_car_l.setPixmap(self.pix_blue)
        else:
            self.label_car_l.setPixmap(self.pix_red)

        if rbool:
            self.label_car_r.setPixmap(self.pix_blue)
        else:
            self.label_car_r.setPixmap(self.pix_red)

    def show_dis(self, lst):
        self.label_fcw1.setText('Dis: None')
        self.label_fcw2.setText('2nd: None')
        self.label_fcw3.setText('3nd: None')
        if lst:
            sort_lst = sorted(lst)
            if len(lst) == 1:
                str1 = 'Dis: {0:.1f} m'.format(sort_lst[0])
                self.label_fcw1.setText(str1)
            elif len(lst) == 2:
                str1 = 'Dis: {0:.1f} m'.format(sort_lst[0])
                str2 = '2nd: {0:.1f} m'.format(sort_lst[1])
                self.label_fcw1.setText(str1)
                self.label_fcw2.setText(str2)
            else:
                str1 = 'Dis: {0:.1f} m'.format(sort_lst[0])
                str2 = '2nd: {0:.1f} m'.format(sort_lst[1])
                str3 = '2nd: {0:.1f} m'.format(sort_lst[2])
                self.label_fcw1.setText(str1)
                self.label_fcw2.setText(str2)
                self.label_fcw3.setText(str3)

    def show_obj(self, dict1):
        num_car, num_ped, num_cyc = dict1[0], dict1[2], dict1[1]
        num_sum = num_car + num_ped + num_cyc
        self.label_ob_num.setText('Object: ' + str(num_sum))
        self.label_ob_car.setText('Cars: ' + str(num_car))
        self.label_ob_cyc.setText('Cyclists: ' + str(num_cyc))
        self.label_ob_ped.setText('Pedestrians: ' + str(num_ped))

    def get_weather(self):
        ip = requests.get('https://ipecho.net/plain').text.strip()
        # latlong = requests.get(url='https://ipapi.co/{}/latlong/'.format(ip)).text.split(',')
        resp = requests.get(url='http://ip-api.com/line/%s?lang=en-US' % (ip))  # 可以用field传来参数
        if resp.status_code == 200:
            content = resp.content
            text = content.decode(encoding="utf-8").split()
            city = text[6]
            latlong = [text[8], text[9]]
            weather = requests.get(
                url='http://api.openweathermap.org/data/2.5/weather?lat={}&lon={}&appid=yourself_token&units=metric'.format(
                    latlong[0], latlong[1], )).json()
            T = weather['main']['temp']
            state = weather['weather'][0]['main']
            self.label_weather.setText(city + ': ' + state + ', ' + str(int(T)) + '℃')


if __name__ == '__main__':
    app = QApplication(sys.argv)
    ui = MainWindow()
    ui.show()
    sys.exit(app.exec_())
