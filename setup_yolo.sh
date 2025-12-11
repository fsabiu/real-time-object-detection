#!/bin/bash
# Download YOLOv8n model for testing
mkdir -p models
wget -O models/yolov8n.pt https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt
wget -O models/yolov8l.pt https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8l.pt
