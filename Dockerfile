FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download de_core_news_md \
    && python -m spacy download en_core_web_md

COPY app ./app

ENV DB_PATH=/data/anonymizer.db
# GLiNER-Modell wird beim ersten Start von HuggingFace geladen und hier gecacht
ENV HF_HOME=/data/hf
VOLUME /data
EXPOSE 8800

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800"]
