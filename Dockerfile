FROM python:3.10-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
# 容器默认 UTC，会导致报告检测时间差 8 小时——强制北京时间
ENV TZ=Asia/Shanghai
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8777
CMD ["python", "main.py", "--mode", "server"]
