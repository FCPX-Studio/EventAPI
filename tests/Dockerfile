# Test image: the production image + pytest, so tests run against the exact deployed environment.
# Build the production image first (tests/run.sh does both).
ARG APP_IMAGE=event-api:local
FROM ${APP_IMAGE}

USER root
RUN pip install --no-cache-dir pytest==8.4.2 httpx==0.28.1
COPY tests ./tests
RUN chown -R appuser /app
USER appuser

CMD ["python", "-m", "pytest", "tests", "-v", "-p", "no:cacheprovider"]
