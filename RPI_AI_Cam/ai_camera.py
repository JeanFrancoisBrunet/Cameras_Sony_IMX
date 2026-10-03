# imx500_detector.py

import sys
from functools import lru_cache
import cv2
import numpy as np
from picamera2 import MappedArray, Picamera2
from picamera2.devices import IMX500
from picamera2.devices.imx500 import NetworkIntrinsics, postprocess_nanodet_detection

class IMX500Detector:
    def __init__(self, model_path="/usr/share/imx500-models/imx500_network_yolov8n_pp.rpk"):
        self.last_detections = []
        self.last_results = None
        
        # Initialize IMX500
        self.imx500 = IMX500(model_path)
        self.intrinsics = self.imx500.network_intrinsics
        
        if not self.intrinsics:
            self.intrinsics = NetworkIntrinsics()
            self.intrinsics.task = "object detection"
        elif self.intrinsics.task != "object detection":
            raise ValueError("Network is not an object detection task")

        # Set default labels if none provided
        if self.intrinsics.labels is None:
            with open("assets/coco_labels.txt", "r") as f:
                self.intrinsics.labels = f.read().splitlines()

        # Set additional options
        self.intrinsics.ignore_dash_labels = True
        self.intrinsics.preserve_aspect_ratio = True
        self.intrinsics.update_with_defaults()
        
        # Initialize camera
        self.picam2 = Picamera2(self.imx500.camera_num)
        
    def start(self, show_preview=True):
        """Start the detector"""
        config = self.picam2.create_preview_configuration(
            controls={"FrameRate": self.intrinsics.inference_rate}, 
            buffer_count=12
        )
        
        self.imx500.show_network_fw_progress_bar()
        self.picam2.start(config, show_preview=show_preview)
        
        if self.intrinsics.preserve_aspect_ratio:
            self.imx500.set_auto_aspect_ratio()
            
        self.picam2.pre_callback = self._draw_detections
        
    def stop(self):
        """Stop the detector"""
        self.picam2.stop()
        
    def get_detections(self):
        """Get the latest detections"""
        self.last_results = self._parse_detections(self.picam2.capture_metadata())
        return self.last_results
    
    def get_labels(self):
        """Get the list of detection labels"""
        labels = self.intrinsics.labels
        if self.intrinsics.ignore_dash_labels:
            labels = [label for label in labels if label and label != "-"]
        return labels

    def _parse_detections(self, metadata):
        """Internal method to parse detections"""
        bbox_normalization = self.intrinsics.bbox_normalization
        threshold = 0.55
        iou = 0.65
        max_detections = 10

        np_outputs = self.imx500.get_outputs(metadata, add_batch=True)
        input_w, input_h = self.imx500.get_input_size()
        
        if np_outputs is None:
            return self.last_detections

        if self.intrinsics.postprocess == "nanodet":
            boxes, scores, classes = postprocess_nanodet_detection(
                outputs=np_outputs[0], 
                conf=threshold, 
                iou_thres=iou,
                max_out_dets=max_detections
            )[0]
            from picamera2.devices.imx500.postprocess import scale_boxes
            boxes = scale_boxes(boxes, 1, 1, input_h, input_w, False, False)
        else:
            boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
            if bbox_normalization:
                boxes = boxes / input_h
            boxes = np.array_split(boxes, 4, axis=1)
            boxes = zip(*boxes)

        self.last_detections = [
            Detection(box, category, score, metadata, self.imx500, self.picam2)
            for box, score, category in zip(boxes, scores, classes)
            if score > threshold
        ]
        return self.last_detections

    def _draw_detections(self, request, stream="main"):
        """Internal method to draw detections"""
        if self.last_results is None:
            return
            
        labels = self.get_labels()
        with MappedArray(request, stream) as m:
            for detection in self.last_results:
                x, y, w, h = detection.box
                label = f"{labels[int(detection.category)]} ({detection.conf:.2f})"

                (text_width, text_height), baseline = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                )
                text_x = x + 5
                text_y = y + 15

                overlay = m.array.copy()
                cv2.rectangle(
                    overlay,
                    (text_x, text_y - text_height),
                    (text_x + text_width, text_y + baseline),
                    (255, 255, 255),
                    cv2.FILLED
                )

                alpha = 0.30
                cv2.addWeighted(overlay, alpha, m.array, 1 - alpha, 0, m.array)
                cv2.putText(
                    m.array, label, (text_x, text_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1
                )
                cv2.rectangle(m.array, (x, y), (x + w, y + h), (0, 255, 0, 0), thickness=2)

class Detection:
    def __init__(self, coords, category, conf, metadata, imx500, picam2):
        """Create a Detection object, recording the bounding box, category and confidence."""
        self.category = category
        self.conf = conf
        self.box = imx500.convert_inference_coords(coords, metadata, picam2)