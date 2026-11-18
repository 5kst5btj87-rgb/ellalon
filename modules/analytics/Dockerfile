FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk opencv-python-headless==4.10.* numpy onnxruntime==1.20.* tokenizers==0.20.*
# Object detector: YOLOX-Tiny (Megvii, Apache-2.0), 416x416, CPU friendly.
# If the download fails the module still builds; object rules then report "detector not available".
RUN mkdir -p /models && python -c "import urllib.request as u; u.urlretrieve('https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx', '/models/yolox_tiny.onnx')" \
    || echo "WARNING: could not download the YOLOX model"
# People tagging: CLIP ViT-B/32 (OpenAI, MIT licence), quantized ONNX export by Xenova (~155 MB).
# If the download fails the module still builds; tagging then reports "not available".
RUN python -c "import urllib.request as u; b='https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/'; \
u.urlretrieve(b+'onnx/vision_model_quantized.onnx', '/models/clip_vision.onnx'); \
u.urlretrieve(b+'onnx/text_model_quantized.onnx', '/models/clip_text.onnx'); \
u.urlretrieve(b+'tokenizer.json', '/models/tokenizer.json')" \
    || echo "WARNING: could not download the people-tagging model"
WORKDIR /app
COPY modules/analytics/ .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
