FROM python:3.12-slim
RUN pip install --no-cache-dir discord.py==2.5.2 && useradd --create-home bot
COPY bot.py /app/bot.py
USER bot
CMD ["python", "-u", "/app/bot.py"]
