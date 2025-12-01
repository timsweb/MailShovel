# Version 3.12 doesn't seem to work with the imap lib
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY shovel.py .

# The command to run when the container starts.
# By default, it will run for all accounts.
# You can override this when you run the container.
CMD [ "python", "shovel.py" ]
