FROM python:3.11-bookworm

COPY . /app
WORKDIR /app
RUN pip3 install -r requirements.txt

EXPOSE 8080

CMD ["flask", "run", "--with-threads", "--port", "8080", "--host", "0.0.0.0"]
