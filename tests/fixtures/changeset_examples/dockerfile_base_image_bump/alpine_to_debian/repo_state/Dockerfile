FROM alpine:3.18

RUN apk add --no-cache curl ca-certificates

WORKDIR /app
COPY app.sh ./

CMD ["./app.sh"]
