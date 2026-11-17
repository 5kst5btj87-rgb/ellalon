"""Detector plugin interface.

A detector receives frames from a camera and returns zero or more detections.
Add a new one (object detection, LPR, line crossing, loitering...) by subclassing
Detector and adding it to DETECTORS.
"""
import os
import time

import cv2
import numpy as np


class Detector:
    name = "base"

    def __init__(self, camera: dict):
        self.camera = camera

    def process(self, frame: np.ndarray) -> list[dict]:
        """Return a list of detections: {"type": str, "severity": str, "data": {...}}"""
        raise NotImplementedError


class MotionDetector(Detector):
    name = "motion"

    def __init__(self, camera):
        super().__init__(camera)
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)
        self.min_area = float(os.environ.get("MOTION_MIN_AREA", "0.01"))
        self.cooldown = float(os.environ.get("MOTION_COOLDOWN", "10"))
        self.last = 0.0

    def process(self, frame):
        small = cv2.resize(frame, (320, int(320 * frame.shape[0] / frame.shape[1])))
        mask = self.bg.apply(cv2.GaussianBlur(small, (5, 5), 0))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        ratio = float(np.count_nonzero(mask)) / mask.size
        if ratio >= self.min_area and time.time() - self.last > self.cooldown:
            self.last = time.time()
            return [{"type": "motion", "severity": "low", "data": {"area": round(ratio, 4)}}]
        return []


DETECTORS = [MotionDetector]
