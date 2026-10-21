# minimal metrics sidecar container (~80 MB compressed)
# tag only -- never pin digests (they expire when images are re-pushed)
FROM python:3.12-slim
WORKDIR /app

RUN pip install --no-cache-dir nvidia-ml-py

COPY gather.py .
RUN chmod 755 /app/gather.py
EXPOSE 19532
CMD ["python", "/app/gather.py"]