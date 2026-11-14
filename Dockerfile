FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
# DB_PATH / SERVER_ID 由 compose 注入；两个进程共享同一个 SQLite 库文件
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
