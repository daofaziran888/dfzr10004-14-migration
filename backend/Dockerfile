FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# 审计追加式事件日志（audit-events.jsonl）、迁移检查点与旧数据快照目录。
# 运行时通过 AUDIT_DATA_DIR 覆盖；compose/编排环境应把该目录挂到持久卷。
RUN mkdir -p /srv/data/audit
ENV AUDIT_DATA_DIR=/srv/data/audit
VOLUME ["/srv/data/audit"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
