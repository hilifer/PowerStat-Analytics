FROM python:3.11-slim

# 系统依赖（OCR + 中文字体）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 \
    fonts-wqy-microhei \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# 安装标准精度中文语言包（替换 fast 版本，提升中文识别率）
ADD https://github.com/tesseract-ocr/tessdata/raw/main/chi_sim.traineddata \
    /usr/share/tesseract-ocr/5/tessdata/chi_sim.traineddata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "main.py"]
CMD ["--help"]
