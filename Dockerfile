FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8085

ENV FLASK_APP=app.py \
    FLASK_DEBUG=1 \
    DEBUG=1
CMD ["flask", "run", "--host", "0.0.0.0", "--port", "8085", "--debug"]
