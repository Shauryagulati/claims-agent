FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY data/ data/
COPY pytest.ini .

EXPOSE 8000

# The key is supplied at run time: docker run -e ANTHROPIC_API_KEY=... -p 8000:8000 <image>
# Startup fails fast with a readable message if it is missing.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
