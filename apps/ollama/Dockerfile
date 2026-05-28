FROM ollama/ollama:latest

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_MODEL=llama3.2:1b

COPY apps/ollama/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 11434

ENTRYPOINT []
CMD ["/start.sh"]
