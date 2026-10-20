FROM python:3.13-alpine3.22
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/app.py"]
