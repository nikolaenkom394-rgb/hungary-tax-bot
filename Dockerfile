FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tax_bot.py .

CMD ["python", "tax_bot.py"]
