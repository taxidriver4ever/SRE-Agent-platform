# Maven 阶段负责编译，运行镜像不包含构建工具。
FROM maven:3.9.9-eclipse-temurin-21 AS build
WORKDIR /workspace
COPY pom.xml ./
RUN mvn -q -DskipTests dependency:go-offline
COPY src ./src
RUN mvn -q test package

# 官方 OpenTelemetry Java 自动探针提供 HTTP、JDBC 和下游请求 Trace。
FROM otel/autoinstrumentation-java:2.17.1 AS otel
FROM eclipse-temurin:21-jre
ARG VCS_REF=unknown
ARG VCS_SOURCE=local/order-service
LABEL org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.source=$VCS_SOURCE
WORKDIR /app
COPY --from=build /workspace/target/order-service-0.1.0.jar app.jar
COPY --from=otel /javaagent.jar /otel/javaagent.jar
ENV JAVA_TOOL_OPTIONS="-javaagent:/otel/javaagent.jar"
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
