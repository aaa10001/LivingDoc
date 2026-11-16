# LivingDoc 运行镜像
#
# 这个镜像存在的意义不只是"能跑"：它是本项目三份 ADR 的验证环境。
#   ADR-001  Celery prefork 在 Windows 不可用 -> Linux 下用 prefork + max-tasks-per-child
#   ADR-002  Redis 无 Windows 版            -> 容器里起 redis:7，验证真实 visibility timeout
#   ADR-007  本机无 JRE，plantuml 校验从未执行 -> 镜像内装 JRE + plantuml，闸门第一次真正闭合
#
# 构建：
#   docker build -t livingdoc:dev .
# 只装运行依赖（不带 psycopg）时：
#   docker build --build-arg REQUIREMENTS=requirements.txt -t livingdoc:sqlite .
# 需要跑容器内测试时（多带 tests/，其余与运行镜像一致）：
#   docker build --target test -t livingdoc:test .
#
# 2026-11 瘦身：改成多阶段。运行层只保留 venv、应用代码、JRE 与 plantuml.jar；
# 下载 jar 用的 curl、pip 缓存、tests/ 都不进最终镜像。JRE 从 21 降到 17
# （PlantUML 1.2024.7 只要求 Java 8+），再省几十 MB。

ARG PYTHON_VERSION=3.11
# 固定 bookworm：trixie 已经移除 openjdk-17-jre-headless，只剩 21（体积更大）。
# PlantUML 只要求 Java 8+，17 完全够用；用 bookworm 也顺手锁住基础镜像的大版本。
ARG DEBIAN_CODENAME=bookworm

# ---------------------------------------------------------------- 公共底座
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_CODENAME} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 换国内 apt 源：deb.debian.org 在国内通常只有几十 KB/s，装 JRE 会等到超时。
# 用 sed 而不是整份替换 sources.list：Debian 12+ 用 deb822 格式的 debian.sources，
# 两种文件布局都覆盖，改不到也不至于让构建失败（|| true）。
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g; s|http://security.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g; s|http://security.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi

# ---------------------------------------------------------------- Python 依赖
FROM base AS builder

ARG REQUIREMENTS=requirements-postgres.txt
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /build
COPY requirements.txt requirements-postgres.txt ./
# venv 单独放 /opt/venv：运行层整目录 COPY，不碰系统 Python，也不会把 pip 缓存带过去。
# 装完删掉 pip/setuptools：运行期不需要包管理器，少 28MB；测试目标用同一套依赖，
# 需要装包时请重新构建，而不是在容器里 pip install（那会写进临时层，不可复现）。
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r ${REQUIREMENTS} \
 && rm -rf /opt/venv/lib/python*/site-packages/pip \
           /opt/venv/lib/python*/site-packages/pip-*.dist-info \
           /opt/venv/lib/python*/site-packages/setuptools \
           /opt/venv/lib/python*/site-packages/setuptools-*.dist-info \
           /opt/venv/lib/python*/site-packages/_distutils_hack \
           /opt/venv/lib/python*/site-packages/distutils-precedence.pth \
           /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.11 \
 && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} +

# ---------------------------------------------------------------- 精简 JRE + plantuml.jar
FROM base AS plantuml

# plantuml.jar 不走 apt：Debian 的 plantuml 包会连 default-jre（带 GUI）一起装，
# 多出两百多 MB。这里直接从 Maven Central 拉固定版本，Maven 国内镜像兜底。
# curl 只存在于这一层，最终镜像里没有它。
#
# JRE 也不整包复制：PlantUML 只需要 java.base / java.desktop 等几个模块，
# 用 jlink 裁一个专用 runtime（约 50MB，整包 openjdk-17-jre-headless 是 184MB）。
# 裁完立刻用一张真实类图跑 -pipe，确认缺模块会在构建期而不是演示时炸。
ARG PLANTUML_VERSION=1.2024.7
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl openjdk-17-jdk-headless \
      binutils libfreetype6 libfontconfig1 libharfbuzz0b \
 && mkdir -p /opt/plantuml \
 && ( curl -fsSL --retry 3 -o /opt/plantuml/plantuml.jar \
        "https://repo1.maven.org/maven2/net/sourceforge/plantuml/plantuml/${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" \
      || curl -fsSL --retry 3 -o /opt/plantuml/plantuml.jar \
        "https://maven.aliyun.com/repository/public/net/sourceforge/plantuml/plantuml/${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar" ) \
 && test -s /opt/plantuml/plantuml.jar \
 && jlink --add-modules java.base,java.desktop,java.logging,java.naming,java.xml,jdk.unsupported \
      --strip-debug --no-man-pages --no-header-files --compress=2 --output /opt/java \
 && printf '@startuml\nclass Smoke\n@enduml\n' > /tmp/smoke.puml \
 && /opt/java/bin/java -Djava.awt.headless=true -jar /opt/plantuml/plantuml.jar \
      -tsvg -pipe < /tmp/smoke.puml | grep -q '<svg' \
 && rm -f /tmp/smoke.puml \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- 运行层
FROM base AS runtime

# git       : 阶段二 clone / diff 依赖，容器里必须有
# 不再装完整 JRE：/opt/java 是上一阶段 jlink 出来的专用 runtime。
# 字体库仍要装：headless JVM 在初始化字体子系统时 dlopen freetype/fontconfig/harfbuzz，
# 缺了会报 UnsatisfiedLinkError: libharfbuzz.so.0: cannot open shared object file。
# 这是本阶段第一次实跑 -checkonly 才暴露出来的依赖，文档里写过"无 JRE 未验证"，
# 现在补上这三个库才算真验证。
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
      libfreetype6 libfontconfig1 libharfbuzz0b \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=plantuml /opt/java /opt/java
COPY --from=plantuml /opt/plantuml/plantuml.jar /opt/plantuml/plantuml.jar

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PLANTUML_JAR=/opt/plantuml/plantuml.jar \
    JAVA_HOME=/opt/java \
    JAVA_BIN=/opt/java/bin/java \
    DATABASE_URL=sqlite:////app/var/livingdoc.sqlite \
    WORK_DIR=/app/var/work \
    REPO_CACHE_DIR=/app/var/repos

WORKDIR /app

# 只复制运行时需要的代码：tests/ 与 pytest.ini 属于 test 目标。
COPY app ./app
COPY scripts ./scripts

# 非 root 运行：容器里的进程不需要写 /app 之外的东西
RUN useradd --create-home --uid 1001 livingdoc \
 && ln -s /opt/java/bin/java /usr/local/bin/java \
 && mkdir -p /app/var \
 && chown -R livingdoc:livingdoc /app
USER livingdoc

EXPOSE 8000

# 不依赖 curl：直接用 Python 标准库，镜像里少一个包。
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------- 测试目标
# 与运行镜像共享同一套依赖与 JRE，只多 tests/。用它验证"镜像里的代码"而不是本机 venv：
#   docker run --rm livingdoc:test python -m pytest -q
FROM runtime AS test

COPY tests ./tests
COPY pytest.ini ./

USER root
RUN chown -R livingdoc:livingdoc /app/tests /app/pytest.ini
USER livingdoc

# ---------------------------------------------------------------- 默认目标
# Docker 默认构建最后一个 stage；把它显式指回 runtime，避免 `docker build .`
# 悄悄产出带 tests/ 的测试镜像。要测试时显式 --target test。
FROM runtime AS release