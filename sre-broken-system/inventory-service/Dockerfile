# Go 构建在容器中完成，因此宿主机不需要安装 Go SDK。
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
# 单元测试属于镜像发布门禁，随后构建无 CGO 的最小 Linux 二进制。
RUN go test ./... && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/inventory-service ./cmd/server

FROM alpine:3.21
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/inventory-service
LABEL org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.source=$VCS_SOURCE
RUN addgroup -S app && adduser -S app -G app
USER app
COPY --from=build /out/inventory-service /inventory-service
EXPOSE 8081
ENTRYPOINT ["/inventory-service"]
