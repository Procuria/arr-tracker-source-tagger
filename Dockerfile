FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py

# For webhook mode (optional)
EXPOSE 8787

ENTRYPOINT ["python", "/app/main.py"]
