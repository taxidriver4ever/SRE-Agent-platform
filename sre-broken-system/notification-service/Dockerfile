# Compile and test the Go service in a reproducible builder image.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN go test ./... && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/notification-service ./cmd/server

FROM alpine:3.21
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/notification-service
LABEL org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.source=$VCS_SOURCE
RUN addgroup -S app && adduser -S app -G app
USER app
COPY --from=build /out/notification-service /notification-service
EXPOSE 8084
ENTRYPOINT ["/notification-service"]
