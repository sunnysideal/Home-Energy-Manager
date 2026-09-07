FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py /app/app.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
