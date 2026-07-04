FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY api.py .

# Переменные окружения
ENV PORT=8080
ENV ADMIN_USER=admin
ENV ADMIN_PASS=gpt4m2024

# Запуск
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
